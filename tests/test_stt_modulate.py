"""vocero-crm 067 (D15) — Notas de voz con Modulate (`STT_PROVIDER=modulate`).

Con Modulate, la nota de voz va a su API batch tal cual (OGG/Opus), en
español y con el vocabulario de la tienda. Si falla, el agente le pide al
lead que lo escriba: no cae al otro proveedor ni supone lo que decía.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.media import describe_item
from app.state import InboundMessage
from app.stt import URL, ModulateStt, SttError, terminos_de
from tests.conftest import CRM_URL, make_ctx, make_settings


def test_terminos_respeta_los_limites_y_no_repite():
    assert terminos_de(" ABB, Eaton ,,ABB, clema ") == ["ABB", "Eaton", "clema"]
    assert terminos_de("") == []
    muchos = ",".join(f"t{i}" for i in range(2000))
    assert len(terminos_de(muchos)) == 1000


def test_solo_con_modulate_y_llave():
    assert make_settings(stt_provider="modulate", modulate_api_key="k").usa_modulate is True
    assert make_settings(stt_provider="modulate").usa_modulate is False
    assert make_settings(stt_provider="", modulate_api_key="k").usa_modulate is False


async def test_manda_el_audio_en_espanol_con_el_vocabulario(respx_mock):
    ruta = respx_mock.post(URL).mock(return_value=httpx.Response(200, json={"text": "necesito tres selectores", "duration_ms": 1200}))
    stt = ModulateStt("llave-x", ["ABB", "clema"])
    texto = await stt.transcribe(b"OggS-bytes", "audio/ogg; codecs=opus", filename="nota-de-voz.ogg")
    assert texto == "necesito tres selectores"
    pedido = ruta.calls[0].request
    assert pedido.headers["X-API-Key"] == "llave-x"
    cuerpo = pedido.content.decode("latin-1")
    assert 'name="upload_file"; filename="nota-de-voz.ogg"' in cuerpo
    assert 'name="language"' in cuerpo and "\r\nes\r\n" in cuerpo
    assert json.dumps({"custom_terms": ["ABB", "clema"]}) in cuerpo


@pytest.mark.parametrize("respuesta", [httpx.Response(429, json={"error": "Concurrent request limit reached."}),
                                       httpx.Response(200, json={"text": "  "})])
async def test_si_modulate_falla_lanza(respx_mock, respuesta):
    respx_mock.post(URL).mock(return_value=respuesta)
    with pytest.raises(SttError):
        await ModulateStt("k").transcribe(b"x", "audio/ogg")


def _nota_de_voz():
    return InboundMessage(
        wa_message_id="wamid.1", identity="5216860000000", type="audio",
        media_id="media_1", media_mime="audio/ogg; codecs=opus", media_voice=True,
    )


async def test_con_modulate_la_nota_se_transcribe_ahi(respx_mock):
    ctx = make_ctx(make_settings(stt_provider="modulate", modulate_api_key="k"))
    ctx.stt = ModulateStt("k")
    respx_mock.get(f"{CRM_URL}/api/bot/media/media_1").mock(
        return_value=httpx.Response(200, content=b"OggS", headers={"content-type": "audio/ogg"})
    )
    respx_mock.post(URL).mock(return_value=httpx.Response(200, json={"text": "tienen contactores ABB"}))
    parte = await describe_item(ctx, _nota_de_voz())
    assert parte.text == "[Nota de voz del lead, transcrita]: tienen contactores ABB"
    await ctx.crm.aclose()


async def test_si_modulate_falla_se_le_pide_que_lo_escriba(respx_mock):
    ctx = make_ctx()
    ctx.stt = ModulateStt("k")
    respx_mock.get(f"{CRM_URL}/api/bot/media/media_1").mock(
        return_value=httpx.Response(200, content=b"OggS", headers={"content-type": "audio/ogg"})
    )
    respx_mock.post(URL).mock(return_value=httpx.Response(503))
    parte = await describe_item(ctx, _nota_de_voz())
    assert "NO pudiste escuchar" in parte.text and "¿me lo escribes?" in parte.text
    assert "nota de voz." not in parte.text.split("¿me lo escribes?")[1]  # no pide otra nota de voz
    await ctx.crm.aclose()
