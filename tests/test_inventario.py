"""vocero-crm 067 — Nea consulta la tienda Shopify (`buscar_inventario`).

La herramienta existe solo si esta Nea tiene `SHOPIFY_*` y el perfil del CRM
trae `inventario` encendida (bandera `TIENDA` del CRM). Lo que el modelo no
debe soltar (el número exacto sin cantidad, el precio «a cotizar») no le
llega: lo filtra `app/tienda.py`.
"""
from __future__ import annotations

import json

import httpx

from app.llm import LlmReply, ToolCall
from app.main import create_app
from app.profile import BusinessProfile
from app.tienda import resumen
from app.tools import CLAVE_DE_HERRAMIENTA
from tests.conftest import CRM_CONV_ID, CRM_URL, crm_context, despachar, make_ctx, make_settings, mock_crm_basics

DOMINIO = "tienda.test"
SHOPIFY = f"https://{DOMINIO}/api/2026-10/graphql.json"


def _producto(titulo="Contactor ABB AF26", sku="AF26-30-00-13", monto="1840.00", qty=10, disponible=True):
    return {
        "title": titulo,
        "vendor": "ABB",
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/123",
                    "sku": sku,
                    "title": "Default Title",
                    "availableForSale": disponible,
                    "quantityAvailable": qty,
                    "price": {"amount": monto, "currencyCode": "USD"},
                }
            ]
        },
    }


class _Perfil:
    def __init__(self, perfil: BusinessProfile) -> None:
        self._p = perfil

    async def get(self) -> BusinessProfile:
        return self._p


def _ctx(tienda=True, tools=None):
    extra = {"shopify_store_domain": DOMINIO, "shopify_storefront_token": "shpat_test"} if tienda else {}
    ctx = make_ctx(make_settings(**extra))
    ctx.profile = _Perfil(BusinessProfile(agent_name="Sofi", tools=tools))
    return ctx


async def _turno(ctx, texto="¿tienen contactores ABB? ocupo 12"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    ) as client:
        await despachar(client, ctx, esperar=True, text=texto)


def _nombres(schemas):
    return {t["function"]["name"] for t in schemas or []}


# --- lo que el modelo ve de un producto ---------------------------------------


def test_sin_cantidad_no_viaja_el_numero():
    r = resumen(_producto(qty=37), "contactor abb", None)
    assert r["disponible"] is True
    assert "existencias" not in r and "alcanza" not in r
    assert r["precio"] == "1840.00" and r["moneda"] == "USD"


def test_con_cantidad_dice_si_alcanza_y_cuantas_hay():
    r = resumen(_producto(qty=5), "x", 12)
    assert r["alcanza"] is False and r["existencias"] == 5
    assert resumen(_producto(qty=20), "x", 12)["alcanza"] is True


def test_precio_a_cotizar_no_lleva_numero():
    for monto in ("0.00", "9999.00", "99999.00"):
        r = resumen(_producto(monto=monto), "x", None)
        assert r["precio_a_cotizar"] is True
        assert "precio" not in r and "moneda" not in r


def test_agotado_es_lo_que_la_tienda_no_vende():
    r = resumen(_producto(qty=0, disponible=False), "x", None)
    assert r["disponible"] is False and "sobre_pedido" not in r


def test_cero_piezas_con_venta_abierta_es_sobre_pedido():
    # FAK-R-KC11-I en la tienda real: availableForSale y 0 piezas.
    p = _producto(qty=0)
    p["variants"]["nodes"][0]["currentlyNotInStock"] = True
    r = resumen(p, "x", None)
    assert r["disponible"] is True and r["sobre_pedido"] is True
    assert "existencias" not in r
    con = resumen(p, "x", 2)
    assert con["alcanza"] is False and con["existencias"] == 0 and con["sobre_pedido"] is True


def test_elige_la_variante_del_sku_que_nombro():
    p = _producto()
    p["variants"]["nodes"].append(
        {"id": "gid://shopify/ProductVariant/456", "sku": "AF38-30-00-13", "title": "38 A",
         "availableForSale": True, "quantityAvailable": 3, "price": {"amount": "2500.00", "currencyCode": "USD"}}
    )
    r = resumen(p, "af38-30-00-13", None)
    assert r["sku"] == "AF38-30-00-13" and r["producto"].endswith("38 A")


def test_buscar_inventario_es_de_la_clave_inventario():
    assert CLAVE_DE_HERRAMIENTA["buscar_inventario"] == "inventario"


def test_un_env_sin_llenar_no_es_tienda():
    s = make_settings(shopify_store_domain="REEMPLAZA_mi-tienda.myshopify.com", shopify_storefront_token="REEMPLAZA_x")
    assert s.tienda_configurada is False
    assert make_settings().tienda_configurada is False
    assert make_settings(shopify_store_domain=DOMINIO, shopify_storefront_token="t").tienda_configurada is True


# --- el turno ------------------------------------------------------------------


async def test_con_tienda_se_ensena_consulta_y_regresa_al_modelo(respx_mock):
    ctx = _ctx()
    mock_crm_basics(respx_mock)
    shopify = respx_mock.post(SHOPIFY).mock(
        return_value=httpx.Response(200, json={"data": {"search": {"nodes": [_producto(qty=37)]}}})
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="i", name="buscar_inventario", arguments={"consulta": "contactor ABB", "cantidad": 12})]),
        LlmReply(content="Sí, tenemos el AF26 y alcanza para tus 12."),
    ]
    await _turno(ctx)

    primera = ctx.llm.calls[0]
    assert "buscar_inventario" in _nombres(primera["tools"])
    assert "llama buscar_inventario" in primera["messages"][0]["content"]
    assert shopify.called
    pedido = shopify.calls[0].request
    assert pedido.headers["Shopify-Storefront-Private-Token"] == "shpat_test"
    assert json.loads(pedido.content)["variables"] == {"q": "contactor ABB", "n": 3}
    resultado = json.loads(ctx.llm.calls[1]["messages"][-1]["content"])
    assert resultado["ok"] is True
    assert resultado["productos"][0]["alcanza"] is True and resultado["productos"][0]["existencias"] == 37
    await ctx.crm.aclose()


async def test_sin_tienda_no_existe(respx_mock):
    ctx = _ctx(tienda=False)
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="Hola, ¿en qué te ayudo?")]
    await _turno(ctx)
    assert "buscar_inventario" not in _nombres(ctx.llm.calls[0]["tools"])
    assert "buscar_inventario" not in ctx.llm.calls[0]["messages"][0]["content"]
    await ctx.crm.aclose()


async def test_apagada_en_el_crm_no_existe(respx_mock):
    # El CRM sin TIENDA no manda `inventario` en tools.
    ctx = _ctx(tools=["ficha", "etapa", "escalar"])
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="Hola, ¿en qué te ayudo?")]
    await _turno(ctx)
    assert "buscar_inventario" not in _nombres(ctx.llm.calls[0]["tools"])
    assert "buscar_inventario" not in ctx.llm.calls[0]["messages"][0]["content"]
    await ctx.crm.aclose()


async def test_si_shopify_falla_el_modelo_lo_sabe_y_el_turno_sigue(respx_mock):
    ctx = _ctx()
    rutas = mock_crm_basics(respx_mock)
    respx_mock.post(SHOPIFY).mock(return_value=httpx.Response(503))
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="i", name="buscar_inventario", arguments={"consulta": "contactor"})]),
        LlmReply(content="No pude revisar la tienda ahorita, ¿te paso con una persona?"),
    ]
    await _turno(ctx)
    resultado = json.loads(ctx.llm.calls[1]["messages"][-1]["content"])
    assert resultado == {"ok": False, "error": "tienda_no_disponible", "detalle": "no pude consultar la tienda; no inventes existencias ni precios"}
    assert rutas["messages"].called  # el lead sí recibió respuesta
    await ctx.crm.aclose()


async def test_preguntar_por_un_producto_mueve_a_consulta_de_producto(respx_mock):
    # En vivo el modelo nunca llamaba move_stage por su cuenta.
    ctx = _ctx()
    rutas = mock_crm_basics(respx_mock)
    contexto = crm_context()
    contexto["lead"]["nextStages"] = [{"id": "st_c", "name": "Consulta de producto"}, {"id": "st_q", "name": "Cotización pendiente"}]
    rutas["context"].mock(return_value=httpx.Response(200, json=contexto))
    stage = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        return_value=httpx.Response(200, json={"ok": True, "changed": True, "lead": {"id": "ld_1", "stageName": "Consulta de producto"}})
    )
    respx_mock.post(SHOPIFY).mock(return_value=httpx.Response(200, json={"data": {"search": {"nodes": [_producto()]}}}))
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="i", name="buscar_inventario", arguments={"consulta": "contactor ABB"})]),
        LlmReply(content="Sí, tenemos el AF26."),
    ]
    await _turno(ctx)
    assert json.loads(stage.calls[0].request.content) == {"conversationId": CRM_CONV_ID, "stage": "Consulta de producto"}
    await ctx.crm.aclose()


async def test_sin_esa_etapa_adelante_no_se_mueve(respx_mock):
    ctx = _ctx()
    mock_crm_basics(respx_mock)  # sin nextStages
    stage = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(return_value=httpx.Response(200, json={}))
    respx_mock.post(SHOPIFY).mock(return_value=httpx.Response(200, json={"data": {"search": {"nodes": [_producto()]}}}))
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="i", name="buscar_inventario", arguments={"consulta": "contactor ABB"})]),
        LlmReply(content="Sí, tenemos el AF26."),
    ]
    await _turno(ctx)
    assert not stage.called
    await ctx.crm.aclose()
