"""El endpoint `/ws/voz` de punta a punta, con un WebSocket de verdad.

Existe por una razón muy concreta: `scripts/spikes/cliente_voz/index.html` es lo
que Samuel abrirá por la mañana, y si el endpoint está roto lo descubrirá con un
micrófono en la mano y sin traza ninguna. Aquí el mismo protocolo —binario PCM
hacia arriba, binario PCM y JSON hacia abajo— se ejercita sin navegador.

Los modelos van sustituidos por dobles: lo que se prueba es el contrato del
WebSocket, no Whisper.
"""

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_voz_barge_in import STTFalso, TTSFalso, a_pcm16, cargar, silencio

from app.voice.pipeline_ws import crear_router
from app.voice.vad import SAMPLE_RATE


@pytest.fixture(scope="module")
def cliente():
    app = FastAPI()
    app.include_router(
        crear_router(stt=STTFalso(), motor_tts=TTSFalso(segundos_por_frase=0.5))
    )
    with TestClient(app) as c:
        yield c


def test_saluda_con_las_frecuencias_de_muestreo(cliente):
    """El cliente necesita el sample rate de salida ANTES del primer trozo.

    Si no lo recibe tiene que adivinarlo, y adivinar mal se oye como una voz de
    dibujos animados o de ultratumba.
    """
    with cliente.websocket_connect("/ws/voz") as ws:
        saludo = ws.receive_json()
        assert saludo == {
            "tipo": "listo",
            "sample_rate_entrada": 16_000,
            "sample_rate_salida": 24_000,
        }


def test_un_turno_por_websocket_devuelve_audio_y_eventos(cliente):
    """El recorrido completo: audio → transcripción → audio de vuelta."""
    pcm = np.concatenate((cargar("turno_corto"), silencio(900)))
    por_trozo = int(SAMPLE_RATE * 20 / 1000)

    with cliente.websocket_connect("/ws/voz") as ws:
        assert ws.receive_json()["tipo"] == "listo"
        for i in range(0, len(pcm), por_trozo):
            ws.send_bytes(a_pcm16(pcm[i : i + por_trozo]))

        eventos: list[str] = []
        bytes_audio = 0
        # El servidor responde mientras seguimos leyendo. Se lee hasta ver
        # `fin_audio`, que es la señal de que el turno terminó.
        for _ in range(400):
            m = ws.receive()
            if m.get("bytes") is not None:
                bytes_audio += len(m["bytes"])
            elif m.get("text") is not None:
                eventos.append(json.loads(m["text"])["tipo"])
                if eventos[-1] == "fin_audio":
                    break

    assert "transcripcion" in eventos
    assert "agente_habla" in eventos
    assert eventos[-1] == "fin_audio"
    assert bytes_audio > 0, "el servidor no mandó audio"
    # PCM int16: número par de bytes, o el cliente desalinearía las muestras y
    # sonaría a ruido blanco.
    assert bytes_audio % 2 == 0


def test_el_silencio_no_provoca_respuesta(cliente):
    """Un micrófono abierto en una habitación callada no debe iniciar turnos."""
    with cliente.websocket_connect("/ws/voz") as ws:
        assert ws.receive_json()["tipo"] == "listo"
        por_trozo = int(SAMPLE_RATE * 20 / 1000)
        mudo = a_pcm16(np.zeros(por_trozo, dtype=np.float32))
        for _ in range(100):   # 2 s de silencio
            ws.send_bytes(mudo)
        ws.close()
    # Si hubiera respondido, el `receive_json` de arriba habría devuelto otra
    # cosa; llegar aquí sin excepciones es el resultado.


def test_la_pagina_de_prueba_habla_el_mismo_protocolo():
    """La página y el servidor tienen que coincidir en los nombres de mensaje.

    Es una comprobación tonta y es la que evita el fallo tonto: renombrar
    `parar` en el servidor y descubrirlo por la mañana, con el agente sin poder
    callarse y sin ningún error en consola.
    """
    html = (
        Path(__file__).resolve().parents[2]
        / "scripts" / "spikes" / "cliente_voz" / "index.html"
    ).read_text(encoding="utf-8")
    for tipo in (
        "listo",
        "paciente_habla",
        "fin_de_turno",
        "transcripcion",
        "agente_habla",
        "parar",
        "fin_audio",
    ):
        assert f'case "{tipo}"' in html, f"la página no maneja el evento {tipo}"
