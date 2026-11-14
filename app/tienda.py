"""Inventario de la tienda Shopify (vocero-crm 067, herramienta `inventario`).

Nea consulta la Storefront API directo, con el token privado del negocio: el
CRM no guarda credenciales de la tienda. Es solo lectura.

Las reglas del negocio viven aquí y no en el prompt, porque lo que el modelo
no recibe no lo puede soltar:

- El número exacto de existencias solo viaja si el lead pidió una cantidad
  (spec 067, D12). Sin cantidad, el modelo sabe si hay o no, nada más.
- «Precio a cotizar» es un precio de 0, 9999 o 99999, la misma regla del
  storefront (`ecentinela/lib/money.ts`). Ese precio NO viaja: el modelo no
  tiene número que inventar.
- Como mucho 3 resultados: con varios, el modelo pide elegir (D13).
- `disponible` es `availableForSale`, igual que el storefront. Con 0 piezas y
  la venta abierta viene `sobre_pedido`: se vende, pero no hay en almacén.

Un fallo de Shopify regresa `{"ok": False, ...}`: nunca tumba el turno.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger("nea.tienda")

API_VERSION = "2026-10"
MAX_RESULTADOS = 3
PRECIO_A_COTIZAR = {0.0, 9999.0, 99999.0}
TIMEOUT = 10.0

_QUERY = """
query Inventario($q: String!, $n: Int!) {
  search(query: $q, first: $n, types: [PRODUCT], unavailableProducts: LAST) {
    nodes {
      ... on Product {
        title
        vendor
        variants(first: 10) {
          nodes {
            id
            sku
            title
            availableForSale
            currentlyNotInStock
            quantityAvailable
            price { amount currencyCode }
          }
        }
      }
    }
  }
}
"""


async def _graphql(settings: Settings, query: str, variables: dict[str, Any], client: httpx.AsyncClient | None) -> dict[str, Any] | None:
    """El `data` de una consulta, o None si Shopify no contestó bien."""
    url = f"https://{settings.shopify_store_domain.strip()}/api/{API_VERSION}/graphql.json"
    headers = {
        "content-type": "application/json",
        "Shopify-Storefront-Private-Token": settings.shopify_storefront_token.strip(),
    }
    propio = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await http.post(url, json={"query": query, "variables": variables}, headers=headers)
        datos = r.json() if r.status_code == 200 else None
        if not datos or datos.get("errors") or not datos.get("data"):
            # Sin el token en el log: solo el estado y el primer error.
            logger.warning("tienda: Shopify respondió %s %s", r.status_code, str((datos or {}).get("errors"))[:200])
            return None
        data: dict[str, Any] = datos["data"]
        return data
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("tienda: no alcancé Shopify: %s", type(exc).__name__)
        return None
    finally:
        if propio:
            await http.aclose()


def _norma(texto: str) -> str:
    """Para comparar números de parte: «C3SS7-10B-20» == «c3ss7 10b 20»."""
    return re.sub(r"[^a-z0-9]", "", (texto or "").lower())


def elegir(nodos: list[dict[str, Any]], producto: str) -> tuple[str, Any]:
    """Pura: qué producto de la búsqueda es el que el lead confirmó.

    `("ok", variante)` con el producto pegado; `("ambiguo", [títulos])` si
    hay varios y ninguno coincide exacto; `("no_encontrado", [])` si nada.
    """
    meta = _norma(producto)
    exactos = []
    for n in nodos:
        vs = ((n.get("variants") or {}).get("nodes")) or []
        por_sku = next((v for v in vs if meta and _norma(v.get("sku") or "") == meta), None)
        if por_sku or (len(meta) >= 4 and meta in _norma(n.get("title") or "")):
            exactos.append((n, por_sku or (vs[0] if vs else None)))
    elegido = exactos[0] if len(exactos) == 1 else None
    if elegido is None and not exactos and len(nodos) == 1:
        vs = ((nodos[0].get("variants") or {}).get("nodes")) or []
        elegido = (nodos[0], vs[0] if vs else None)
    if elegido and elegido[1]:
        n, v = elegido
        return "ok", {**v, "product": {"title": n.get("title"), "vendor": n.get("vendor")}}
    if not nodos:
        return "no_encontrado", []
    return "ambiguo", [e[0].get("title") for e in exactos] or [n.get("title") for n in nodos]


async def resolver(
    settings: Settings, producto: str, client: httpx.AsyncClient | None = None
) -> tuple[str, Any] | None:
    """El producto que el lead confirmó, consultado AHORA (la lista se armó
    turnos atrás y el modelo no guarda ids entre turnos). None si Shopify no
    contestó."""
    data = await _graphql(settings, _QUERY, {"q": producto, "n": MAX_RESULTADOS}, client)
    if data is None:
        return None
    return elegir([n for n in data["search"]["nodes"] if n], producto)


def es_precio_a_cotizar(monto: Any) -> bool:
    try:
        return float(monto) in PRECIO_A_COTIZAR
    except (TypeError, ValueError):
        return True


def _tokens(texto: str) -> set[str]:
    return {t for t in re.split(r"[\s,;/]+", texto.lower()) if t}


def _variante(variantes: list[dict[str, Any]], consulta: str) -> dict[str, Any] | None:
    """La variante cuyo SKU nombró el lead; si ninguna, la primera."""
    if not variantes:
        return None
    pedidos = _tokens(consulta)
    for v in variantes:
        if (v.get("sku") or "").lower() in pedidos:
            return v
    return variantes[0]


def resumen(producto: dict[str, Any], consulta: str, cantidad: int | None) -> dict[str, Any] | None:
    """Lo que el modelo ve de UN producto. Pura: la prueban los tests."""
    v = _variante(((producto.get("variants") or {}).get("nodes")) or [], consulta)
    if v is None:
        return None
    precio = v.get("price") or {}
    existencias = v.get("quantityAvailable")
    # Se puede comprar = lo que dice la tienda (`availableForSale`), como el
    # storefront. Con 0 piezas y la venta abierta (Shopify: «seguir vendiendo
    # sin existencias»), es sobre pedido: se vende, pero no hay en almacén.
    disponible = bool(v.get("availableForSale"))
    sobre_pedido = disponible and bool(v.get("currentlyNotInStock"))
    titulo = producto.get("title") or ""
    if v.get("title") and v["title"] != "Default Title":
        titulo = f"{titulo} — {v['title']}"
    out: dict[str, Any] = {
        "producto": titulo,
        "sku": v.get("sku") or None,
        "marca": producto.get("vendor") or None,
        "variante_id": v.get("id"),
        "disponible": disponible,
    }
    if sobre_pedido:
        out["sobre_pedido"] = True
    if es_precio_a_cotizar(precio.get("amount")):
        out["precio_a_cotizar"] = True
    else:
        out["precio"] = f"{float(precio['amount']):.2f}"
        out["moneda"] = precio.get("currencyCode")
    if cantidad is not None:
        # Solo con cantidad: si alcanza y, si no, cuántas hay (D12).
        if existencias is None:
            out["alcanza"] = disponible and not sobre_pedido
        else:
            out["alcanza"] = existencias >= cantidad
            out["existencias"] = max(int(existencias), 0)
    return out


async def buscar_inventario(
    settings: Settings,
    consulta: str,
    cantidad: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    consulta = (consulta or "").strip()
    if not consulta:
        return {"ok": False, "error": "consulta_vacia", "detalle": "dime qué producto buscar"}
    data = await _graphql(settings, _QUERY, {"q": consulta, "n": MAX_RESULTADOS}, client)
    if data is None:
        return {"ok": False, "error": "tienda_no_disponible", "detalle": "no pude consultar la tienda; no inventes existencias ni precios"}

    productos = [
        p
        for p in (resumen(n, consulta, cantidad) for n in data["search"]["nodes"] if n)
        if p is not None
    ][:MAX_RESULTADOS]
    if not productos:
        return {"ok": True, "productos": [], "nota": "no encontré ese producto en la tienda"}
    return {"ok": True, "productos": productos}


# --- Cotización (vocero-crm 067, herramienta `cotizar`) -------------------------

MAX_LINEAS = 20


def _numerico(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]


def armar_cotizacion(
    pedido: list[tuple[str, int]],
    por_id: dict[str, dict[str, Any]],
    origen: str,
) -> dict[str, Any]:
    """La cotización con lo que la tienda dice AHORA. Pura: la prueban los tests.

    Al enlace solo entra lo que se puede comprar ya, con piezas suficientes y
    precio (spec 067, D11). Lo demás va marcado con su motivo, sin precio
    inventado.
    """
    lineas: list[dict[str, Any]] = []
    moneda: str | None = None
    total = 0.0
    en_enlace: list[str] = []
    for variante_id, cantidad in pedido:
        v = por_id.get(variante_id)
        if v is None:
            lineas.append({"varianteId": variante_id, "sku": None, "titulo": None, "cantidad": cantidad,
                           "precioUnitario": None, "enEnlace": False, "motivo": "no_encontrado"})
            continue
        precio = v.get("price") or {}
        titulo = (v.get("product") or {}).get("title") or ""
        if v.get("title") and v["title"] != "Default Title":
            titulo = f"{titulo} — {v['title']}"
        existencias = v.get("quantityAvailable")
        a_cotizar = es_precio_a_cotizar(precio.get("amount"))
        if a_cotizar:
            motivo = "precio_a_cotizar"
        elif not v.get("availableForSale"):
            motivo = "sin_existencia"
        elif v.get("currentlyNotInStock"):
            motivo = "sobre_pedido"
        elif existencias is not None and existencias < cantidad:
            motivo = "sin_existencia"
        else:
            motivo = None
        linea: dict[str, Any] = {
            "varianteId": variante_id,
            "sku": v.get("sku") or None,
            "titulo": titulo,
            "cantidad": cantidad,
            "precioUnitario": None if a_cotizar else f"{float(precio['amount']):.2f}",
            "enEnlace": motivo is None,
        }
        if motivo:
            linea["motivo"] = motivo
        if motivo is None:
            moneda = moneda or precio.get("currencyCode")
            total += float(precio["amount"]) * cantidad
            en_enlace.append(f"{_numerico(variante_id)}:{cantidad}")
        lineas.append(linea)
    return {
        "moneda": moneda,
        "lineas": lineas,
        "totalEnlace": f"{total:.2f}" if en_enlace else None,
        "url": f"{origen}/cart/{','.join(en_enlace)}" if en_enlace else None,
    }


_MOTIVO = {
    "precio_a_cotizar": "precio a cotizar",
    "sin_existencia": "sin existencia suficiente",
    "sobre_pedido": "sobre pedido (sin piezas en almacén)",
    "no_encontrado": "no lo encontré en la tienda",
}


def texto_de_nota(cot: dict[str, Any], transcripcion: str | None = None) -> str:
    """Lo que la persona lee en el hilo para aprobar (formato de la spec 067)."""
    moneda = cot.get("moneda") or ""
    filas = [
        f"Cotización enviada al cliente por el agente, compra directa ({cot['id']})"
        if cot.get("directa")
        else f"Cotización pendiente de aprobar ({cot['id']})"
    ]
    for l in cot["lineas"]:
        nombre = l.get("titulo") or l["varianteId"]
        if l.get("sku"):
            nombre = f"{nombre} [{l['sku']}]"
        if l["enEnlace"]:
            filas.append(f"• {l['cantidad']} × {nombre} — ${float(l['precioUnitario']):,.2f} {moneda} c/u — en el enlace")
        else:
            filas.append(f"• {l['cantidad']} × {nombre} — {_MOTIVO[l['motivo']]} — NO va en el enlace")
    if cot.get("url"):
        filas.append(f"Total del enlace: ${float(cot['totalEnlace']):,.2f} {moneda}, IVA incluido (precio vigente al momento del pago)")
        filas.append(f"Enlace: {cot['url']}")
    else:
        filas.append("Sin enlace: ninguna línea se puede comprar ya.")
    filas.append(f"Requiere CFDI: {'sí' if cot.get('cfdi') else 'no'}")
    if transcripcion:
        filas.append(f"Pedido por audio: «{transcripcion.strip()}»")
    return "\n".join(filas)


def resumen_de_cotizacion(cot: dict[str, Any]) -> str:
    """Una línea para la ficha del CRM (escalares de hasta 500 caracteres).

    Sin enlace: la ficha vuelve al prompt, y el modelo no manda enlaces.
    """
    moneda = cot.get("moneda") or ""
    partes = []
    for l in cot["lineas"]:
        nombre = (l.get("titulo") or l.get("sku") or "?")[:40]
        estado = "en enlace" if l["enEnlace"] else _MOTIVO[l["motivo"]]
        partes.append(f"{l['cantidad']}× {nombre} ({estado})")
    total = f" Total del enlace ${float(cot['totalEnlace']):,.2f} {moneda}." if cot.get("totalEnlace") else ""
    estado = "enviada (compra directa)" if cot.get("directa") else "pendiente de aprobar"
    texto = f"{cot['id']} {cot.get('creada', '')[:10]}, {estado}: {'; '.join(partes)}.{total} CFDI: {'sí' if cot.get('cfdi') else 'no'}."
    return texto if len(texto) <= 500 else texto[:497] + "…"


def es_compra_directa(cot: dict[str, Any], maximo: float) -> bool:
    """Pura: ¿el agente puede mandar el enlace él mismo?

    Solo si TODO se compra ya (una orden mixta la ve una persona) y el total
    no pasa del máximo, en la moneda de la tienda. Máximo 0 = nunca.
    """
    lineas = cot.get("lineas") or []
    return (
        maximo > 0
        and bool(lineas)
        and all(l["enEnlace"] for l in lineas)
        and cot.get("totalEnlace") is not None
        and float(cot["totalEnlace"]) <= maximo
    )
