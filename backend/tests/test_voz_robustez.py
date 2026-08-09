"""Lo que le pasa al bucle de voz cuando el mundo no colabora.

`test_voz_barge_in.py` prueba la máquina de turnos con un cliente que se porta
bien: manda PCM alineado, a 16 kHz, y se queda hasta el final. Este fichero es lo
contrario — el navegador que se va a media síntesis, el trozo de audio partido
por la mitad, Whisper que revienta, dos llamadas a la vez. Ninguna de esas cosas
es rara: la primera ocurre cada vez que alguien cierra la pestaña.

La regla que se está protegiendo es una sola: **un turno que falla no puede
llevarse la llamada**. En una demo en vivo la diferencia entre «esa pregunta no
la ha cogido» y «se ha cortado la llamada» es toda la diferencia.
"""

from __future__ import annotations

import asyncio
import logging
import re

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_voz_barge_in import (
    STTFalso,
    TTSFalso,
    a_pcm16,
    cargar,
    crear_sesion,
    esperar,
    inyectar,
    silencio,
)

from app.voice import vad as modulo_vad
from app.voice.pipeline_ws import ClienteLLMFalso, crear_router
from app.voice.tts import dividir_en_frases
from app.voice.vad import SAMPLE_RATE


class STTQueRevienta:
    """Whisper caído, el disco lleno, el modelo sin descargar: da igual cuál."""

    def __init__(self) -> None:
        self.llamadas = 0

    async def transcribir(self, audio):
        self.llamadas += 1
        raise RuntimeError("Whisper se cayó")


def _turno(ms_silencio: int = 800) -> np.ndarray:
    return np.concatenate((cargar("turno_corto"), silencio(ms_silencio)))


# ---------------------------------------------------------------------------
# Un turno que falla no puede llevarse la llamada
# ---------------------------------------------------------------------------
async def test_un_turno_que_revienta_no_mata_la_sesion():
    """El fallo se registraba dentro de la tarea y RESUCITABA en el turno siguiente.

    `_responder` corre en una `asyncio.Task`. Si lanza, la excepción se queda
    dormida ahí hasta que alguien hace `await` sobre la tarea — y el único que lo
    hace es `_cancelar_turno()`, o sea el turno SIGUIENTE. El resultado era que
    el paciente hacía una segunda pregunta y el WebSocket moría con un
    `RuntimeError` de la pregunta anterior, sin ninguna relación con lo que
    estaba pasando.
    """
    sesion, _, _ = crear_sesion()
    sesion._stt = STTQueRevienta()

    await inyectar(sesion, _turno())
    assert await esperar(lambda: bool(sesion.turnos)), "el primer turno no terminó"

    # Segundo turno: aquí es donde estallaba.
    await inyectar(sesion, _turno())
    assert await esperar(lambda: len(sesion.turnos) >= 2), "la sesión no aceptó un segundo turno"

    await sesion.cerrar()


async def test_un_turno_que_revienta_queda_registrado():
    """Silencioso es peor que roto: el administrador tiene que poder verlo.

    No hay `except: pass`, pero el efecto era el mismo — la excepción viva dentro
    de una tarea que nadie mira no aparece en ningún log hasta que es demasiado
    tarde. La métrica del turno guarda el motivo.
    """
    sesion, _, _ = crear_sesion()
    sesion._stt = STTQueRevienta()

    await inyectar(sesion, _turno())
    assert await esperar(lambda: bool(sesion.turnos))

    assert sesion.turnos[-1].error, "el turno fallido no dejó rastro"
    assert "Whisper" in sesion.turnos[-1].error
    await sesion.cerrar()


async def test_cerrar_funciona_aunque_el_ultimo_turno_fallara():
    """`cerrar()` es el `finally` del endpoint: si lanza, el motor de TTS no se
    cierra y —con ElevenLabs o Cartesia— queda un cliente HTTP colgando por cada
    llamada que terminó mal."""
    sesion, _, _ = crear_sesion()
    sesion._stt = STTQueRevienta()

    await inyectar(sesion, _turno())
    assert await esperar(lambda: bool(sesion.turnos))

    await sesion.cerrar()   # antes: RuntimeError("Whisper se cayó")


# ---------------------------------------------------------------------------
# Audio que no es el que se acordó
# ---------------------------------------------------------------------------
async def test_un_trozo_de_longitud_impar_no_tumba_la_llamada():
    """PCM int16: un trozo impar es medio sample.

    `np.frombuffer(datos, dtype="<i2")` lanza `ValueError` con cualquier longitud
    impar, y para cuando lo hace el byte suelto YA está en `self._buffer`: aunque
    se capturara arriba, el buffer quedaría desalineado y todo lo que viniera
    detrás sonaría a ruido blanco. Hay que descartarlo antes de tocar nada.
    """
    sesion, _, _ = crear_sesion()

    await sesion.recibir_audio(b"\x01\x02\x03")     # 3 bytes: 1 sample y medio

    # Y después de la basura la sesión sigue entendiendo audio bueno.
    await inyectar(sesion, _turno())
    assert await esperar(lambda: bool(sesion.turnos)), "la sesión no se recuperó"
    assert sesion.turnos[-1].texto_paciente == "Tengo la herida un poco roja."
    await sesion.cerrar()


async def test_basura_binaria_por_el_websocket_no_cierra_el_socket():
    """El mismo caso, extremo a extremo. Un `AudioWorklet` que manda un trozo
    partido —o cualquiera con `websocat`— no debe cerrar la llamada."""
    app = FastAPI()
    app.include_router(crear_router(stt=STTFalso(), motor_tts=TTSFalso(segundos_por_frase=0.2)))

    with TestClient(app) as c, c.websocket_connect("/ws/voz") as ws:
        assert ws.receive_json()["tipo"] == "listo"
        ws.send_bytes(b"\x01\x02\x03")
        # El socket sigue vivo: manda audio bueno y responde.
        por_trozo = int(SAMPLE_RATE * 20 / 1000)
        pcm = _turno(900)
        for i in range(0, len(pcm), por_trozo):
            ws.send_bytes(a_pcm16(pcm[i : i + por_trozo]))
        for _ in range(400):
            m = ws.receive()
            if m.get("text") and '"fin_audio"' in m["text"]:
                break
        else:
            pytest.fail("el turno no llegó a fin_audio tras la basura binaria")


async def test_un_trozo_vacio_no_hace_nada():
    """Un `send_bytes(b"")` es legal en WebSocket y algunos navegadores lo mandan
    al parar el micrófono."""
    sesion, _, _ = crear_sesion()
    await sesion.recibir_audio(b"")
    assert sesion.turnos == []
    await sesion.cerrar()


async def test_el_sample_rate_equivocado_se_denuncia(caplog):
    """El fallo más desconcertante posible: nadie revienta, simplemente no pasa nada.

    El contrato dice 16 kHz de subida. Si el navegador manda 48 kHz sin
    remuestrear —un `AudioContext` sin `sampleRate` fijado hace exactamente
    eso—, el VAD ve el triple de audio del que hay, los umbrales de 96 ms y de
    640 ms se cumplen en un tercio del tiempo real, y Whisper transcribe una
    grabación acelerada 3x. No hay excepción, no hay log, y el síntoma es «el
    agente no me entiende». Al menos tiene que quedar dicho en el log.
    """
    caplog.set_level(logging.WARNING, logger="voz.pipeline")
    sesion, _, _ = crear_sesion()
    # Un cliente honesto a 48 kHz: manda cada 50 ms lo que a él le parecen 50 ms
    # de audio, que son 2.400 muestras en vez de las 800 que el servidor espera.
    trozo = a_pcm16(np.zeros(int(48_000 * 0.05), dtype=np.float32))
    for _ in range(40):
        await sesion.recibir_audio(trozo)
        await asyncio.sleep(0.05)

    assert any("frecuencia" in r.message.lower() for r in caplog.records), (
        "un cliente a otro sample rate no dejó ni una advertencia"
    )
    await sesion.cerrar()


async def test_el_ritmo_normal_no_genera_ruido_en_el_log(caplog):
    """El detector anterior no puede gritar por un cliente correcto: una alarma
    que salta siempre es peor que no tenerla."""
    caplog.set_level(logging.WARNING, logger="voz.pipeline")
    sesion, _, _ = crear_sesion()
    trozo = a_pcm16(np.zeros(int(SAMPLE_RATE * 0.05), dtype=np.float32))
    for _ in range(120):                      # 6 s a 16 kHz, en tiempo real
        await sesion.recibir_audio(trozo)
        await asyncio.sleep(0.05)

    assert not [r for r in caplog.records if "frecuencia" in r.message.lower()]
    await sesion.cerrar()


# ---------------------------------------------------------------------------
# Fugas
# ---------------------------------------------------------------------------
def test_el_modelo_de_vad_no_se_recarga_por_conexion(monkeypatch):
    """Silero se cargaba entero en cada `accept()` del WebSocket.

    `crear_router()` documenta que el STT y el TTS se crean una sola vez «porque
    cargar Kokoro o Whisper en el accept() añadiría segundos al inicio de cada
    llamada». El VAD es el tercer modelo y se le escapó: `SesionVoz` construye un
    `DetectorTurnos`, que construye una `InferenceSession` nueva por conexión.

    La sesión de ONNX se puede compartir sin más porque el estado del LSTM viaja
    explícitamente como entrada y salida de `run()` — no vive dentro del modelo.
    """
    import onnxruntime

    creadas = {"n": 0}
    original = onnxruntime.InferenceSession

    class Contada(original):
        def __init__(self, *args, **kwargs):
            creadas["n"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(onnxruntime, "InferenceSession", Contada)
    monkeypatch.setattr(modulo_vad, "_sesiones", {})   # caché limpia para la prueba

    app = FastAPI()
    app.include_router(crear_router(stt=STTFalso(), motor_tts=TTSFalso(segundos_por_frase=0.2)))
    with TestClient(app) as c:
        for _ in range(4):
            with c.websocket_connect("/ws/voz") as ws:
                assert ws.receive_json()["tipo"] == "listo"

    assert creadas["n"] <= 1, f"{creadas['n']} cargas del modelo de VAD para 4 llamadas"


async def test_el_buffer_del_turno_tiene_tope():
    """Un paciente que no para de hablar, o un micrófono abierto en una sala con
    gente: el VAD dice «habla» y no deja de decirlo.

    `_recortar_a_preroll()` solo recorta mientras el paciente NO habla, que es lo
    correcto —hay que conservar el turno entero para Whisper— pero deja el buffer
    creciendo a 32 kB/s sin techo mientras el VAD siga viendo voz. Medido: 40
    repeticiones del clip de prueba dejan 92 s de audio (2,9 MB) y sigue subiendo.

    Lo caro no es la RAM: es que cuando por fin llegue el silencio, ese buffer
    entero se escribe a un WAV y se le pasa a Whisper, que se queda minutos
    transcribiendo un turno que nadie va a contestar ya.
    """
    sesion, _, _ = crear_sesion()
    voz = cargar("turno_corto")               # 2,3 s de habla continua
    for _ in range(40):                       # ~92 s sin una sola pausa
        await sesion.recibir_audio(a_pcm16(voz))

    segundos = len(sesion._buffer) / 2 / SAMPLE_RATE
    assert segundos <= 70, f"el buffer del turno llegó a {segundos:.0f} s sin tope"
    await sesion.cerrar()


def test_las_metricas_del_vad_no_crecen_sin_fin():
    """`latencias_ventana_ms` guardaba un float por ventana de 32 ms — 31 por
    segundo, para siempre. No tumba nada, pero es una lista que solo crece
    dentro de un objeto que vive lo que dure la llamada."""
    detector = modulo_vad.DetectorTurnos()
    ventanas = modulo_vad.MUESTRAS_LATENCIA + 500
    detector.procesar(np.zeros(modulo_vad.VENTANA * ventanas, dtype=np.float32))

    assert detector.metricas.ventanas == ventanas       # el contador SÍ es total
    assert len(detector.metricas.latencias_ventana_ms) <= modulo_vad.MUESTRAS_LATENCIA, (
        "la lista de latencias no tiene tope"
    )
    assert detector.metricas.p95_ms > 0, "el p95 tiene que seguir siendo calculable"


# ---------------------------------------------------------------------------
# Troceado por frases
# ---------------------------------------------------------------------------
def test_el_troceado_normaliza_los_saltos_de_linea():
    """`dividir_en_frases` fusiona las muletillas con un espacio simple, así que
    el trozo devuelto NO es un substring literal del texto de entrada cuando el
    original traía un salto de línea. Es correcto para el TTS y es la causa del
    test siguiente."""
    assert dividir_en_frases("Ahora dígame cómo se encuentra. Sí.\nBien.") == [
        "Ahora dígame cómo se encuentra.",
        "Sí. Bien.",
    ]


async def test_el_troceado_no_pega_palabras_con_saltos_de_linea():
    """REGRESIÓN de la regresión: «graciaspor contármelo» volvía por otra puerta.

    `_hablar()` recorta el buffer pendiente con `pendiente.rfind(cola)` para
    conservar el espacio final del texto crudo. Pero `cola` viene del troceado,
    que fusiona con un espacio simple: si el LLM emitió un `\\n` ahí —y un LLM
    escribe saltos de línea constantemente— el `rfind` devuelve -1 y el camino de
    respaldo (`pendiente = cola`) reintroduce exactamente el bug que el `rfind`
    existía para evitar, porque `cola` viene con `strip()`.

    El resultado es que el TTS recibe «Bien.Muchas gracias» y lo pronuncia como
    una sola palabra. Se reproduce con trozos de 14 caracteres, que es donde el
    corte del LLM cae justo detrás del espacio.
    """
    respuesta = "Ahora dígame cómo se encuentra. Sí.\nBien. Muchas gracias por su tiempo."
    tts = TTSFalso(segundos_por_frase=0.2)
    sesion, _, _ = crear_sesion(
        motor_tts=tts,
        llm=ClienteLLMFalso(respuesta=respuesta, ttft_ms=5.0, ms_por_trozo=0.0, trozo_chars=14),
    )
    await inyectar(sesion, _turno())
    assert await esperar(lambda: bool(sesion.turnos))

    dicho = " ".join(tts.frases)
    pegado = re.search(r"[.!?][A-Za-zÁÉÍÓÚÑáéíóúñ]", dicho)
    assert pegado is None, f"palabras pegadas en «{dicho}»"
    assert "Muchas gracias por su tiempo" in dicho
    await sesion.cerrar()


# ---------------------------------------------------------------------------
# Dos llamadas a la vez
# ---------------------------------------------------------------------------
def test_dos_clientes_a_la_vez_no_comparten_turnos():
    """El STT y el motor de TTS SÍ se comparten (es lo que ahorra la carga por
    llamada); lo que no puede compartirse es el estado conversacional. Se
    comprueba que cada socket recibe su propio `listo` y su propio audio."""
    app = FastAPI()
    app.include_router(crear_router(stt=STTFalso(), motor_tts=TTSFalso(segundos_por_frase=0.2)))
    por_trozo = int(SAMPLE_RATE * 20 / 1000)
    pcm = _turno(900)

    with TestClient(app) as c, c.websocket_connect("/ws/voz") as a, c.websocket_connect(
        "/ws/voz"
    ) as b:
        assert a.receive_json()["tipo"] == "listo"
        assert b.receive_json()["tipo"] == "listo"

        # Solo A habla. B no debe recibir ni audio ni eventos de turno.
        for i in range(0, len(pcm), por_trozo):
            a.send_bytes(a_pcm16(pcm[i : i + por_trozo]))

        bytes_a = 0
        for _ in range(400):
            m = a.receive()
            if m.get("bytes") is not None:
                bytes_a += len(m["bytes"])
            elif m.get("text") and '"fin_audio"' in m["text"]:
                break
        assert bytes_a > 0, "el cliente que habló no recibió audio"
