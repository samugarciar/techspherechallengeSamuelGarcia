"""El barge-in y el troceado de la respuesta, sobre `SesionVoz` (Opción B).

Es la parte del bucle de voz que no se puede probar «a oído» y la que más fácil
se rompe en silencio: si el barge-in deja de funcionar, el agente sigue
respondiendo y suena a contestador automático, pero ninguna prueba de STT o de
TTS se entera.

Los modelos reales no participan: el STT y el TTS son dobles deterministas. Lo
que se prueba aquí es la máquina de turnos, y meter Whisper dentro solo añadiría
400 ms y variabilidad a cada aserción. Whisper ya tiene sus propias pruebas y su
propio spike.

Las dos primeras pruebas son regresiones de errores que existieron de verdad;
está anotado en cada una.
"""

import asyncio
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.voice.pipeline_ws import ClienteLLMFalso, ReproductorSimulado, SesionVoz
from app.voice.stt import Transcripcion
from app.voice.tts import Audio, TTSEngine
from app.voice.vad import SAMPLE_RATE, ParametrosVAD

AUDIO = Path(__file__).resolve().parents[2] / "scripts" / "spikes" / "audio"
MS_TROZO = 20


def cargar(nombre: str) -> np.ndarray:
    pcm, sr = sf.read(AUDIO / f"{nombre}.wav", dtype="float32")
    assert sr == SAMPLE_RATE
    return pcm if pcm.ndim == 1 else pcm.mean(axis=1)


def a_pcm16(pcm: np.ndarray) -> bytes:
    return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class STTFalso:
    """Devuelve siempre lo mismo, al instante. La transcripción no es lo que se
    está probando y Whisper añadiría 400 ms y ruido a cada aserción."""

    def __init__(self, texto: str = "Tengo la herida un poco roja.") -> None:
        self.texto = texto
        self.llamadas = 0

    async def transcribir(self, audio) -> Transcripcion:
        self.llamadas += 1
        return Transcripcion(self.texto, "es", 1.0)


class TTSFalso(TTSEngine):
    """Sintetiza `segundos_por_frase` de tono, instantáneamente.

    Tono y no silencio para que, si alguna vez se escucha la salida de una
    prueba, se oiga que hay audio. Instantáneo para que el reloj de reproducción
    del cliente sea la única variable temporal del test.
    """

    nombre = "falso"

    def __init__(self, segundos_por_frase: float = 2.0) -> None:
        self.segundos = segundos_por_frase
        self.frases: list[str] = []

    async def sintetizar(self, texto: str) -> Audio:
        self.frases.append(texto)
        n = int(24_000 * self.segundos)
        t = np.arange(n, dtype=np.float32) / 24_000
        return Audio((0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 24_000)


def crear_sesion(**kwargs) -> tuple[SesionVoz, ReproductorSimulado, list[dict]]:
    repro = ReproductorSimulado()
    eventos: list[dict] = []

    async def enviar_audio(pcm16: bytes) -> None:
        repro.encolar(pcm16)

    async def enviar_evento(datos: dict) -> None:
        eventos.append(datos)

    sesion = SesionVoz(
        enviar_audio=enviar_audio,
        enviar_evento=enviar_evento,
        stt=STTFalso(),
        motor_tts=kwargs.pop("motor_tts", TTSFalso()),
        llm=kwargs.pop("llm", ClienteLLMFalso(ttft_ms=10.0, ms_por_trozo=0.0)),
        params_vad=kwargs.pop("params_vad", ParametrosVAD()),
        **kwargs,
    )
    sesion.reproductor = repro
    return sesion, repro, eventos


async def inyectar(sesion: SesionVoz, pcm: np.ndarray, *, en_tiempo_real: bool = False) -> None:
    """Mete audio por donde lo metería el WebSocket.

    `en_tiempo_real=False` adelanta el audio del paciente sin esperar: el reloj
    del VAD cuenta muestras, así que los turnos salen igual y la prueba tarda
    milisegundos en vez de segundos. Se pone a True solo cuando la aserción mide
    tiempo de pared, como en el barge-in.
    """
    por_trozo = int(SAMPLE_RATE * MS_TROZO / 1000)
    for i in range(0, len(pcm), por_trozo):
        await sesion.recibir_audio(a_pcm16(pcm[i : i + por_trozo]))
        if en_tiempo_real:
            await asyncio.sleep(MS_TROZO / 1000)


async def esperar(condicion, limite_s: float = 3.0) -> bool:
    fin = asyncio.get_running_loop().time() + limite_s
    while asyncio.get_running_loop().time() < fin:
        if condicion():
            return True
        await asyncio.sleep(0.01)
    return False


def silencio(ms: int) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * ms / 1000), dtype=np.float32)


# ---------------------------------------------------------------------------
async def test_un_turno_completo_produce_audio():
    sesion, repro, eventos = crear_sesion()
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    assert await esperar(lambda: bool(sesion.turnos)), "el turno no terminó"

    m = sesion.turnos[-1]
    assert m.texto_paciente == "Tengo la herida un poco roja."
    assert m.primer_audio_ms > 0
    assert repro.total_ms_reproducidos > 0
    assert {"fin_de_turno", "transcripcion"} <= {e["tipo"] for e in eventos}
    await sesion.cerrar()


async def test_el_agente_sigue_hablando_despues_de_emitir():
    """REGRESIÓN. El audio se manda tan rápido como se sintetiza, así que el
    bucle de emisión acaba en milisegundos mientras el paciente sigue oyendo al
    agente durante segundos.

    La primera versión marcaba `agente_hablando = False` al acabar de emitir, y
    el barge-in dejaba de detectarse: 0 interrupciones de 3 en el spike. El
    estado tiene que venir del reloj de reproducción del cliente.
    """
    sesion, _, _ = crear_sesion(motor_tts=TTSFalso(segundos_por_frase=2.0))
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    assert await esperar(lambda: sesion.agente_hablando), "nunca empezó a hablar"

    # Se ha terminado de EMITIR (hay métricas del turno) y sin embargo sigue
    # sonando: eso es exactamente lo que la regresión rompía.
    assert await esperar(lambda: bool(sesion.turnos))
    assert sesion.agente_hablando
    await sesion.cerrar()


async def test_el_troceado_no_se_come_los_espacios():
    """REGRESIÓN. `dividir_en_frases()` devuelve los trozos con `strip()`, así
    que reasignar el buffer pendiente desde ellos pegaba palabras: el agente
    decía «graciaspor contármelo».

    Se reproduce partiendo la respuesta del LLM en trozos de 7 caracteres, que
    garantiza cortes en mitad de los espacios.
    """
    frase = "Entiendo, gracias por contármelo. Voy a anotar la herida enrojecida."
    tts = TTSFalso(segundos_por_frase=0.2)
    sesion, _, _ = crear_sesion(
        motor_tts=tts,
        llm=ClienteLLMFalso(respuesta=frase, ttft_ms=5.0, ms_por_trozo=0.0, trozo_chars=7),
    )
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    assert await esperar(lambda: bool(sesion.turnos))

    dicho = " ".join(tts.frases)
    assert "graciaspor" not in dicho
    assert "gracias por contármelo" in dicho
    await sesion.cerrar()


async def test_barge_in_corta_el_audio():
    """Lo que decide si esto es una conversación o un contestador.

    El paciente responde, el agente empieza a hablar (4 s de audio encolado) y
    el paciente le pisa. Tiene que quedarse callado antes de 300 ms y tirar el
    audio que quedaba por reproducir.
    """
    sesion, repro, eventos = crear_sesion(motor_tts=TTSFalso(segundos_por_frase=4.0))
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    assert await esperar(lambda: sesion.agente_hablando), "nunca empezó a hablar"

    await inyectar(sesion, cargar("interrupcion"), en_tiempo_real=True)

    assert len(sesion.barge_ins) == 1, "no se detectó la interrupción"
    b = sesion.barge_ins[0]
    assert b.detectado
    # 96 ms son el umbral de confirmación de voz del VAD; el resto es el corte.
    assert 50 < b.ms_hasta_silencio < 300, b.ms_hasta_silencio
    assert b.audio_descartado_ms > 500, "no se tiró el audio que quedaba en cola"
    assert not sesion.agente_hablando
    assert {"parar"} <= {e["tipo"] for e in eventos}
    assert not repro.sonando
    await sesion.cerrar()


async def test_tras_el_barge_in_se_atiende_el_turno_nuevo():
    """Cortar no basta: lo que el paciente dijo al interrumpir es un turno y hay
    que contestarlo. Si no, el agente se calla para siempre."""
    sesion, _, _ = crear_sesion(motor_tts=TTSFalso(segundos_por_frase=4.0))
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    assert await esperar(lambda: sesion.agente_hablando)

    await inyectar(sesion, cargar("interrupcion"), en_tiempo_real=True)
    await inyectar(sesion, silencio(800))

    assert await esperar(lambda: len(sesion.turnos) >= 2), "no hubo segundo turno"
    await sesion.cerrar()


async def test_sin_voz_no_hay_turno():
    sesion, repro, _ = crear_sesion()
    await inyectar(sesion, silencio(2000))
    assert sesion.turnos == []
    assert repro.total_ms_reproducidos == 0
    await sesion.cerrar()


@pytest.mark.parametrize("umbral", [400, 640])
async def test_el_umbral_de_fin_de_turno_manda(umbral):
    """El fin de turno medido por la sesión tiene que ser el umbral configurado.

    Es la cifra que la comparativa suma al presupuesto de latencia; si la sesión
    la calculara mal, el número publicado sería mentira.
    """
    sesion, _, _ = crear_sesion(params_vad=ParametrosVAD(ms_silencio_fin_turno=umbral))
    await inyectar(
        sesion, np.concatenate((cargar("turno_corto"), silencio(umbral + 300))), en_tiempo_real=True
    )
    assert await esperar(lambda: bool(sesion.turnos))
    assert sesion.turnos[0].fin_de_turno_ms == pytest.approx(umbral, abs=80)
    await sesion.cerrar()
