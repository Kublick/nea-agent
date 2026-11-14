"""Transcripción con Modulate (vocero-crm 067, D15; `STT_PROVIDER=modulate`).

Alternativa al modelo que oye del LLM (`ctx.llm.transcribe`): Velma de
Modulate, batch y síncrono. La nota de voz de WhatsApp (OGG/Opus) entra tal
cual. Con `MODULATE_TERMINOS`, las marcas y categorías de la tienda van como
vocabulario propio: son lo que un STT general escribe mal.

Si falla, lanza: `app/media.py` lo convierte en el marcador honesto y el
agente le pide al lead que lo escriba. No cae al otro proveedor (Q25).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("nea.stt")

URL = "https://platform.modulate.ai/api/velma-2-stt-batch"
TIMEOUT = 60.0
# Límites de `custom_terms` de Modulate: 1000 términos, < 8000 caracteres.
MAX_TERMINOS = 1000
MAX_CARACTERES = 7900


class SttError(Exception):
    pass


def terminos_de(crudo: str) -> list[str]:
    """`MODULATE_TERMINOS` (separados por coma) → lista dentro de los límites."""
    vistos: list[str] = []
    total = 0
    for t in (x.strip() for x in (crudo or "").split(",")):
        if not t or t in vistos or len(vistos) >= MAX_TERMINOS or total + len(t) > MAX_CARACTERES:
            continue
        vistos.append(t)
        total += len(t)
    return vistos


class ModulateStt:
    def __init__(self, api_key: str, terminos: list[str] | None = None, client: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._terminos = terminos or []
        self._client = client

    async def transcribe(self, data: bytes, mime: str, filename: str = "audio.ogg") -> str:
        form: dict[str, Any] = {"language": "es"}
        if self._terminos:
            form["config"] = json.dumps({"custom_terms": self._terminos}, ensure_ascii=False)
        content_type = (mime or "audio/ogg").split(";")[0].strip()
        propio = self._client is None
        http = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            r = await http.post(
                URL,
                headers={"X-API-Key": self._key},
                data=form,
                files={"upload_file": (filename, data, content_type)},
            )
        except httpx.HTTPError as exc:
            raise SttError(f"Modulate no respondió: {type(exc).__name__}") from exc
        finally:
            if propio:
                await http.aclose()
        if r.status_code != 200:
            # Sin la llave en el log: solo el estado y el principio del cuerpo.
            raise SttError(f"Modulate respondió {r.status_code}: {r.text[:200]}")
        texto = str((r.json() or {}).get("text") or "").strip()
        if not texto:
            raise SttError("Modulate no oyó nada")
        return texto
