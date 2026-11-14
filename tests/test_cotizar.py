"""vocero-crm 067 — Nea deja el borrador de una cotización (`cotizar`).

Una persona aprueba toda cotización: el modelo jamás ve ni manda el enlace.
La herramienta re-consulta la tienda, deja la nota interna en el CRM (que es
el candado: con `cotizar` apagada o la IA en pausa, 409 y no se escribe nada
más), agrega la cotización a la ficha, mueve a «Cotización pendiente» si
existe y hace handoff después de la despedida.
"""
from __future__ import annotations

import json

import httpx

from app.llm import LlmReply, ToolCall
from app.main import create_app
from app.profile import BusinessProfile
from app.tienda import armar_cotizacion, elegir, resumen_de_cotizacion, texto_de_nota
from app.tools import CLAVE_DE_HERRAMIENTA
from tests.conftest import CRM_CONV_ID, CRM_URL, crm_context, despachar, make_ctx, make_settings, mock_crm_basics

DOMINIO = "tienda.test"
SHOPIFY = f"https://{DOMINIO}/api/2026-10/graphql.json"
ORIGEN = "https://e-centinela.test"
V1 = "gid://shopify/ProductVariant/111"
V2 = "gid://shopify/ProductVariant/222"


def _variante(vid=V1, titulo="Selector C3SS7-10B-20", sku="1sfa619216r1026", monto="11.17", qty=10, afs=True, sobre=False):
    return {
        "id": vid, "sku": sku, "title": "Default Title",
        "availableForSale": afs, "currentlyNotInStock": sobre, "quantityAvailable": qty,
        "price": {"amount": monto, "currencyCode": "USD"},
        "product": {"title": titulo, "vendor": "ABB"},
    }


def _producto(variante):
    """La forma de un nodo de `search`: el producto con sus variantes."""
    v = {k: x for k, x in variante.items() if k != "product"}
    return {"title": variante["product"]["title"], "vendor": "ABB", "variants": {"nodes": [v]}}


def test_elegir_por_numero_de_parte_en_el_titulo_o_sku():
    a = _producto(_variante(titulo="C3SS7-10B-20 selector negro"))
    b = _producto(_variante(V2, titulo="C3SS7-10B-21 selector rojo", sku="otro"))
    estado, v = elegir([a, b], "c3ss7 10b 20")
    assert estado == "ok" and v["id"] == V1 and v["product"]["title"].startswith("C3SS7-10B-20")
    assert elegir([a, b], "OTRO")[0] == "ok"  # por SKU
    assert elegir([a, b], "selector") == ("ambiguo", ["C3SS7-10B-20 selector negro", "C3SS7-10B-21 selector rojo"])
    assert elegir([a], "selector")[0] == "ok"  # uno solo
    assert elegir([], "x") == ("no_encontrado", [])


# --- la cotización -------------------------------------------------------------


def test_al_enlace_solo_entra_lo_que_se_compra_ya():
    por_id = {
        V1: _variante(),
        V2: _variante(V2, "Sonda tipo K", "TJ36", monto="9999.00"),
    }
    cot = armar_cotizacion([(V1, 3), (V2, 1)], por_id, ORIGEN)
    assert cot["url"] == f"{ORIGEN}/cart/111:3"
    assert cot["totalEnlace"] == "33.51" and cot["moneda"] == "USD"
    sonda = cot["lineas"][1]
    assert sonda["enEnlace"] is False and sonda["motivo"] == "precio_a_cotizar" and sonda["precioUnitario"] is None


def test_motivos_de_fuera_del_enlace():
    por_id = {
        V1: _variante(qty=2),  # pide 5, hay 2
        V2: _variante(V2, qty=0, sobre=True),
    }
    cot = armar_cotizacion([(V1, 5), (V2, 1), ("gid://shopify/ProductVariant/999", 1)], por_id, ORIGEN)
    assert [l.get("motivo") for l in cot["lineas"]] == ["sin_existencia", "sobre_pedido", "no_encontrado"]
    assert cot["url"] is None and cot["totalEnlace"] is None
    agotado = armar_cotizacion([(V1, 1)], {V1: _variante(afs=False, qty=0)}, ORIGEN)
    assert agotado["lineas"][0]["motivo"] == "sin_existencia"


def test_la_nota_dice_lo_que_la_persona_necesita():
    cot = armar_cotizacion([(V1, 3), (V2, 1)], {V1: _variante(), V2: _variante(V2, "Sonda", "TJ36", monto="0")}, ORIGEN)
    cot.update(id="cot_x", cfdi=True)
    texto = texto_de_nota(cot, "necesito tres selectores")
    assert "Cotización pendiente de aprobar (cot_x)" in texto
    assert "3 × Selector C3SS7-10B-20 [1sfa619216r1026] — $11.17 USD c/u — en el enlace" in texto
    assert "Sonda [TJ36] — precio a cotizar — NO va en el enlace" in texto
    assert f"Enlace: {ORIGEN}/cart/111:3" in texto
    assert "Requiere CFDI: sí" in texto
    assert "Pedido por audio: «necesito tres selectores»" in texto


def test_el_resumen_cabe_en_la_ficha_y_no_lleva_enlace():
    lineas = [(f"gid://shopify/ProductVariant/{i}", 1) for i in range(20)]
    por_id = {v: _variante(v, titulo="Producto con un nombre larguísimo " * 3) for v, _ in lineas}
    cot = armar_cotizacion(lineas, por_id, ORIGEN) | {"id": "cot_x", "creada": "2026-11-13T00:00:00Z", "cfdi": False}
    r = resumen_de_cotizacion(cot)
    assert len(r) <= 500 and "http" not in r and r.startswith("cot_x 2026-11-13")


def test_cotizar_es_de_la_clave_cotizar():
    assert CLAVE_DE_HERRAMIENTA["cotizar"] == "cotizar"


# --- el turno ------------------------------------------------------------------


class _Perfil:
    def __init__(self, perfil: BusinessProfile) -> None:
        self._p = perfil

    async def get(self) -> BusinessProfile:
        return self._p


def _ctx():
    ctx = make_ctx(make_settings(shopify_store_domain=DOMINIO, shopify_storefront_token="t", cart_link_origin=ORIGEN))
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi"))
    return ctx


def _rutas(respx_mock, nota_status=201):
    rutas = mock_crm_basics(respx_mock)
    contexto = crm_context()
    contexto["lead"]["nextStages"] = [{"id": "st_c", "name": "Cotización pendiente"}, {"id": "st_e", "name": "Cotización enviada"}]
    rutas["context"].mock(return_value=httpx.Response(200, json=contexto))
    rutas["notes"] = respx_mock.post(f"{CRM_URL}/api/bot/notes").mock(
        return_value=httpx.Response(nota_status, json={"nota": {"id": "nota_1"}} if nota_status == 201
                                    else {"error": {"code": "herramienta_apagada", "message": "apagada"}})
    )
    rutas["stage"] = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        return_value=httpx.Response(200, json={"ok": True, "changed": True, "lead": {"id": "ld_1", "stageName": "Cotización pendiente"}})
    )
    def _busqueda(request):
        q = json.loads(request.content)["variables"]["q"]
        nodos = {"C3SS7-10B-20": [_producto(_variante())], "TJ36": [_producto(_variante(V2, "Sonda", "TJ36", monto="0"))]}
        return httpx.Response(200, json={"data": {"search": {"nodes": nodos.get(q, [])}}})

    rutas["shopify"] = respx_mock.post(SHOPIFY).mock(side_effect=_busqueda)
    return rutas


async def _turno(ctx, texto="sí, así está bien, y sí ocupo factura"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    ) as client:
        await despachar(client, ctx, esperar=True, text=texto)


def _llamada(confirmado=True):
    return LlmReply(content=None, tool_calls=[ToolCall(id="c", name="cotizar", arguments={
        "lineas": [{"producto": "C3SS7-10B-20", "cantidad": 3}, {"producto": "TJ36", "cantidad": 1}],
        "confirmado": confirmado, "requiere_cfdi": True,
    })])


async def test_cotizar_deja_nota_ficha_etapa_y_handoff(respx_mock):
    ctx = _ctx()
    rutas = _rutas(respx_mock)
    ctx.llm.replies = [_llamada(), LlmReply(content="Listo, una persona revisa tu cotización y te la confirma en breve.")]
    await _turno(ctx)

    assert "cotizar" in {t["function"]["name"] for t in ctx.llm.calls[0]["tools"]}
    assert "llama cotizar" in ctx.llm.calls[0]["messages"][0]["content"]
    nota = json.loads(rutas["notes"].calls[0].request.content)
    assert nota["conversationId"] == CRM_CONV_ID
    assert f"Enlace: {ORIGEN}/cart/111:3" in nota["texto"] and "Requiere CFDI: sí" in nota["texto"]
    fichas = [json.loads(c.request.content)["ficha"] for c in rutas["ficha"].calls]
    ficha = next(f for f in fichas if "ultima_cotizacion" in f)
    # La ficha del CRM solo guarda escalares de hasta 500 caracteres.
    assert all(isinstance(v, (str, bool, int, float)) for v in ficha.values())
    assert ficha["requiere_cfdi"] is True and len(ficha["ultima_cotizacion"]) <= 500
    assert "/cart/" not in ficha["ultima_cotizacion"] and "Total del enlace $33.51 USD" in ficha["ultima_cotizacion"]
    assert json.loads(rutas["stage"].calls[0].request.content)["stage"] == "Cotización pendiente"
    assert rutas["messages"].called and rutas["handoff"].called
    assert json.loads(rutas["handoff"].calls[0].request.content)["reason"] == "modelo"
    # El enlace jamás vuelve al modelo.
    resultado = ctx.llm.calls[1]["messages"][-1]["content"]
    assert "http" not in resultado and "/cart/" not in resultado
    await ctx.crm.aclose()


async def test_sin_confirmacion_no_cotiza(respx_mock):
    ctx = _ctx()
    rutas = _rutas(respx_mock)
    ctx.llm.replies = [_llamada(confirmado=False), LlmReply(content="¿Así está bien la lista?")]
    await _turno(ctx, "cotízame eso")
    assert json.loads(ctx.llm.calls[1]["messages"][-1]["content"])["error"] == "falta_confirmacion"
    assert not rutas["notes"].called and not rutas["shopify"].called and not rutas["handoff"].called
    await ctx.crm.aclose()


async def test_producto_ambiguo_vuelve_al_modelo_sin_escribir_nada(respx_mock):
    ctx = _ctx()
    rutas = _rutas(respx_mock)
    rutas["shopify"].mock(return_value=httpx.Response(200, json={"data": {"search": {"nodes": [
        _producto(_variante(titulo="Selector negro")), _producto(_variante(V2, titulo="Selector rojo", sku="r"))]}}}))
    ctx.llm.replies = [LlmReply(content=None, tool_calls=[ToolCall(id="c", name="cotizar", arguments={
        "lineas": [{"producto": "selector", "cantidad": 1}], "confirmado": True, "requiere_cfdi": False})]),
        LlmReply(content="¿Cuál de los dos selectores es?")]
    await _turno(ctx)
    resultado = json.loads(ctx.llm.calls[1]["messages"][-1]["content"])
    assert resultado["error"] == "producto_ambiguo" and resultado["opciones"] == ["Selector negro", "Selector rojo"]
    assert not rutas["notes"].called and not rutas["handoff"].called
    await ctx.crm.aclose()


async def test_si_el_crm_rechaza_la_nota_no_se_escribe_nada_mas(respx_mock):
    ctx = _ctx()
    rutas = _rutas(respx_mock, nota_status=409)
    ctx.llm.replies = [_llamada(), LlmReply(content="Te paso con una persona.")]
    await _turno(ctx)
    assert json.loads(ctx.llm.calls[1]["messages"][-1]["content"])["error"] == "herramienta_apagada"
    assert not any("ultima_cotizacion" in json.loads(c.request.content)["ficha"] for c in rutas["ficha"].calls)
    assert not rutas["stage"].called
    await ctx.crm.aclose()


async def test_sin_hablar_de_factura_no_cotiza(respx_mock):
    # En vivo el modelo se saltaba la pregunta y llenaba requiere_cfdi a ojo.
    ctx = _ctx()
    rutas = _rutas(respx_mock)
    ctx.llm.replies = [_llamada(), LlmReply(content="¿Requieres factura (CFDI)?")]
    await _turno(ctx, "sí, así está bien")
    assert json.loads(ctx.llm.calls[1]["messages"][-1]["content"])["error"] == "falta_cfdi"
    assert not rutas["notes"].called and not rutas["handoff"].called
    await ctx.crm.aclose()
