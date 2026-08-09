"""Los módulos autónomos del proyecto, envueltos como servicios de Pipecat.

`stt.py` y `tts.py` se escribieron como módulos autónomos —una clase, un método,
sin dependencias del orquestador— y esa decisión es la que permite construir las
dos opciones sin duplicar nada. El precio es este fichero: Pipecat no consume
módulos, consume *servicios* con ciclo de vida, métricas y manejo de
interrupción. Aquí está ese pegamento; lo que cuesta está contado en
`docs/VOZ_COMPARATIVA.md` §Coste de integración.

Por qué no se usa `WhisperSTTServiceMLX`, el servicio que Pipecat 1.7 ya trae
--------------------------------------------------------------------------
Lo trae, funciona, y usa el mismo `mlx_whisper` que nosotros. Pero su `run_stt()`
llama a::

    mlx_whisper.transcribe(audio, path_or_hf_repo=…, temperature=…, language=…)

y **no expone `initial_prompt`**: no hay forma de pasarle el sesgo de vocabulario
clínico. Ese sesgo es la decisión de la Fase 0 —los 45 ms que compran
«apendicectomía» en vez de «appendicitomía»—, así que adoptarlo sería revertirla.
Además su modelo por defecto es `whisper-tiny` en inglés. La medición comparada
está en la comparativa; la conclusión es que el servicio propio se queda.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import wave
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import numpy as np
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from app.agent.llm_client import LLMClient, Mensaje
from app.core.config import get_settings
from app.voice.pipeline_ws import ClienteLLMFalso
from app.voice.stt import Transcripcion, WhisperSTT
from app.voice.tts import SAMPLE_RATE as TTS_SAMPLE_RATE
from app.voice.tts import Audio, TTSEngine, crear_motor

STT_SAMPLE_RATE = 16_000


class WhisperClinicoSTT(SegmentedSTTService):
    """`app.voice.stt.WhisperSTT` como servicio de Pipecat.

    `wants_wav_segments = False` porque queremos el segmento en PCM int16 crudo:
    la envoltura WAV la ponemos nosotros, ya que `WhisperSTT.transcribir()` solo
    acepta rutas de fichero. Ese rodeo por disco se mide aparte
    (`ms_escritura_wav`) y es el coste real de no poder tocar `stt.py`; la
    corrección propuesta —que acepte también un `np.ndarray`— está en el informe.
    """

    def __init__(self, *, stt: WhisperSTT | None = None, **kwargs) -> None:
        super().__init__(sample_rate=STT_SAMPLE_RATE, **kwargs)
        self._stt = stt or WhisperSTT()
        self.ultima: Transcripcion | None = None
        self.ms_escritura_wav: float = 0.0

    @property
    def wants_wav_segments(self) -> bool:
        return False

    def can_generate_metrics(self) -> bool:
        return True

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            await self.start_processing_metrics()
            t0 = time.perf_counter()
            ruta = await asyncio.to_thread(escribir_wav_temporal, audio, self.sample_rate)
            self.ms_escritura_wav = (time.perf_counter() - t0) * 1000
            try:
                self.ultima = await self._stt.transcribir(ruta)
            finally:
                ruta.unlink(missing_ok=True)
            await self.stop_processing_metrics()

            if texto := self.ultima.texto.strip():
                yield TranscriptionFrame(texto, self._user_id, time_now_iso8601(), Language.ES)
        except Exception as e:  # noqa: BLE001 — un fallo de STT no debe tumbar la llamada
            yield ErrorFrame(error=f"STT falló: {e}")

    async def precalentar(self) -> None:
        await asyncio.to_thread(self._stt.precalentar)


def escribir_wav_temporal(pcm16: bytes, sample_rate: int) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        destino = Path(tmp.name)
    with wave.open(str(destino), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return destino


class MotorLocalTTS(TTSService):
    """Cualquier `TTSEngine` de `tts.py` como servicio de Pipecat.

    Deliberadamente NO se llama aquí a `dividir_en_frases()`: el `TTSService`
    base ya agrega el texto del LLM por frases antes de invocar `run_tts`.
    Trocear dos veces produciría trozos de tres palabras y una llamada al motor
    por cada uno. El troceado propio sigue siendo el de la Opción B, donde no hay
    agregador que lo haga — y esa es una de las cosas que Pipecat sí regala.

    Se acepta un motor ya construido para que el spike pueda medir con `say`
    (siempre disponible) o con Kokoro sin tocar la base de datos. Sin él se cae a
    `settings.tts_engine_local`, que es lo que se usa siempre hoy: el diseño
    previsto era que decidiera `VoiceRouter` según el modo activo —y que de paso
    contabilizara el gasto en `tts_usage`— pero nadie lo construye todavía. Ver
    `app/voice/voice_mode.py`.
    """

    def __init__(self, *, motor: TTSEngine | None = None, **kwargs) -> None:
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            sample_rate=TTS_SAMPLE_RATE,
            **kwargs,
        )
        self._motor = motor or crear_motor(get_settings().tts_engine_local)
        self.ms_primera_sintesis: float | None = None
        self.frases_sintetizadas = 0

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        try:
            await self.start_tts_usage_metrics(text)
            t0 = time.perf_counter()
            audio: Audio = await self._motor.sintetizar(text)
            ms = (time.perf_counter() - t0) * 1000
            self.frases_sintetizadas += 1
            if self.ms_primera_sintesis is None:
                self.ms_primera_sintesis = ms

            async for frame in self._stream_audio_frames_from_iterator(
                _un_solo_trozo(audio),
                in_sample_rate=audio.sample_rate,
                context_id=context_id,
            ):
                await self.stop_ttfb_metrics()
                yield frame
        except Exception as e:  # noqa: BLE001
            yield ErrorFrame(error=f"TTS falló: {e}")
        finally:
            await self.stop_ttfb_metrics()

    async def cerrar(self) -> None:
        await self._motor.cerrar()


async def _un_solo_trozo(audio: Audio) -> AsyncIterator[bytes]:
    """`sintetizar()` devuelve la frase entera; Pipecat espera un iterador.

    No hay streaming *dentro* de la frase porque ningún motor local del proyecto
    lo ofrece: la granularidad de streaming del sistema es la frase, y de ahí
    sale `dividir_en_frases()`.
    """
    yield (np.clip(audio.pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class LLMFalsoProcessor(FrameProcessor):
    """El cliente LLM (falso o real) como procesador de Pipecat.

    Se implementa como `FrameProcessor` y no como `LLMService` a propósito:
    `LLMService` arrastra adaptadores por proveedor, agregadores de contexto y
    registro de funciones, que son ortogonales a lo que decide esta comparativa
    (transporte, VAD, turnos, barge-in). Emite exactamente los frames que el
    `TTSService` consume, que es lo único que el pipeline necesita de él.

    La generación va en una tarea cancelable, y no en línea, porque eso ES el
    barge-in: si el paciente interrumpe, el LLM que estaba escribiendo tiene que
    callarse, o el TTS seguirá recibiendo texto de la respuesta vieja.

    Dispara con `UserStoppedSpeakingFrame` y no con `TranscriptionFrame`: quien
    decide que el turno terminó es el `UserTurnProcessor`, y arrancar con la
    transcripción se saltaría esa decisión — el agente contestaría a la primera
    frase de un paciente que todavía está hablando. Es exactamente lo que hace
    `LLMContextAggregatorPair` en un pipeline de Pipecat completo.
    """

    def __init__(self, cliente: LLMClient | None = None, sistema: str = "") -> None:
        super().__init__()
        self._cliente = cliente or ClienteLLMFalso()
        self._sistema = sistema
        self._tarea: asyncio.Task | None = None
        self._transcripcion = ""
        self.ttft_ms: float | None = None
        self.interrupciones = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            self.interrupciones += 1
            await self._cancelar()
            self._transcripcion = ""
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            self._transcripcion = f"{self._transcripcion} {frame.text}".strip()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStoppedSpeakingFrame) and self._transcripcion:
            await self.push_frame(frame, direction)
            await self._cancelar()
            texto, self._transcripcion = self._transcripcion, ""
            self._tarea = self.create_task(self._generar(texto))
            return

        await self.push_frame(frame, direction)

    async def _cancelar(self) -> None:
        if self._tarea is not None:
            await self.cancel_task(self._tarea)
            self._tarea = None

    async def _generar(self, texto_usuario: str) -> None:
        t0 = time.perf_counter()
        await self.push_frame(LLMFullResponseStartFrame())
        primero = True
        async for trozo in self._cliente.stream(self._sistema, [Mensaje("user", texto_usuario)]):
            if primero:
                self.ttft_ms = (time.perf_counter() - t0) * 1000
                primero = False
            await self.push_frame(LLMTextFrame(trozo))
        await self.push_frame(LLMFullResponseEndFrame())


__all__ = [
    "LLMFalsoProcessor",
    "MotorLocalTTS",
    "WhisperClinicoSTT",
    "escribir_wav_temporal",
]
