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
    try:
        monto = float(precio.get("amount"))
    except (TypeError, ValueError):
        monto = 0.0
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
    if monto in PRECIO_A_COTIZAR:
        out["precio_a_cotizar"] = True
    else:
        out["precio"] = f"{monto:.2f}"
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
    url = f"https://{settings.shopify_store_domain.strip()}/api/{API_VERSION}/graphql.json"
    headers = {
        "content-type": "application/json",
        "Shopify-Storefront-Private-Token": settings.shopify_storefront_token.strip(),
    }
    body = {"query": _QUERY, "variables": {"q": consulta, "n": MAX_RESULTADOS}}
    propio = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await http.post(url, json=body, headers=headers)
        datos = r.json() if r.status_code == 200 else None
        if not datos or datos.get("errors") or not datos.get("data"):
            # Sin el token en el log: solo el estado y el primer error.
            logger.warning("tienda: Shopify respondió %s %s", r.status_code, str((datos or {}).get("errors"))[:200])
            return {"ok": False, "error": "tienda_no_disponible", "detalle": "no pude consultar la tienda; no inventes existencias ni precios"}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("tienda: no alcancé Shopify: %s", type(exc).__name__)
        return {"ok": False, "error": "tienda_no_disponible", "detalle": "no pude consultar la tienda; no inventes existencias ni precios"}
    finally:
        if propio:
            await http.aclose()

    productos = [
        p
        for p in (resumen(n, consulta, cantidad) for n in datos["data"]["search"]["nodes"] if n)
        if p is not None
    ][:MAX_RESULTADOS]
    if not productos:
        return {"ok": True, "productos": [], "nota": "no encontré ese producto en la tienda"}
    return {"ok": True, "productos": productos}
