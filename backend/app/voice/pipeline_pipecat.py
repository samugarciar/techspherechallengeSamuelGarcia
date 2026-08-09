"""Opción A — el bucle de voz sobre Pipecat 1.7.

El plan del README daba Pipecat por decidido. Este módulo lo construye de verdad
para poder decidirlo con números; el veredicto está en `docs/VOZ_COMPARATIVA.md`.

Lo primero que hay que saber: **la API que describe el README ya no existe.**
En Pipecat 1.7 el VAD dejó de ser un parámetro del transporte
(`TransportParams(vad_analyzer=…)`, que es lo que enseñan todos los tutoriales) y
pasó a ser un procesador más del pipeline, y la decisión de fin de turno se
extrajo a un `UserTurnProcessor` con estrategias enchufables. El montaje correcto
hoy es::

    transport.input()
      → VADProcessor(SileroVADAnalyzer)          # emite VADUserStarted/Stopped
      → WhisperClinicoSTT                        # segmenta con esos frames
      → UserTurnProcessor(estrategias)           # decide el fin de turno y ordena el barge-in
      → LLM
      → TTS
      → transport.output()

El STT va **antes** del `UserTurnProcessor` y no después: la estrategia de parada
por defecto espera a tener una transcripción, y las transcripciones solo viajan
hacia abajo. Puesto al revés, el turno nunca termina. Eso no está en la
documentación; se descubre leyendo `turns/user_stop/`.

Segunda sorpresa: la estrategia de parada por defecto de 1.7 no es un umbral de
silencio, es `LocalSmartTurnAnalyzerV3`, un modelo de detección semántica de
turno que se descarga aparte. Aquí se usa `SpeechTimeoutUserTurnStopStrategy`,
que es la de umbral, para comparar peras con peras contra la Opción B.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.agent.llm_client import LLMClient
from app.voice.pipeline_ws import ClienteLLMFalso, ReproductorSimulado
from app.voice.servicios_pipecat import LLMFalsoProcessor, MotorLocalTTS, WhisperClinicoSTT
from app.voice.tts import SAMPLE_RATE as TTS_SAMPLE_RATE
from app.voice.tts import TTSEngine
from app.voice.vad import SAMPLE_RATE as STT_SAMPLE_RATE

MS_SILENCIO_VAD = 0.2
"""`stop_secs` de Silero. Es solo la primera mitad de la decisión de fin de turno
en Pipecat: encima va el `user_speech_timeout` del `UserTurnProcessor`. Los dos
suman, y esa suma es lo que se compara contra el umbral único de la Opción B."""


# ---------------------------------------------------------------------------
# Transporte de laboratorio: audio inyectado, sin navegador
# ---------------------------------------------------------------------------
class EntradaInyectada(BaseInputTransport):
    """Un `BaseInputTransport` en el que se empuja audio desde un fichero.

    Se usa el transporte REAL de Pipecat, no un procesador propio que simule
    frames de VAD: si se falsificaran los frames se estaría midiendo nuestro
    código y no el suyo, y la comparación no valdría nada. Lo único que cambia
    respecto a `SmallWebRTCTransport` es de dónde salen las muestras.
    """

    def __init__(self, params: TransportParams | None = None) -> None:
        super().__init__(
            params
            or TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=STT_SAMPLE_RATE,
                audio_out_enabled=False,
            )
        )

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # Sin esto no existe `_audio_in_queue` y el primer `push_audio_frame`
        # revienta con un AttributeError que además se pierde: ocurre dentro de
        # una tarea del runner, así que el proceso se queda colgado sin traza.
        # Los transportes reales lo llaman al conectar el cliente; uno inyectado
        # no tiene cliente, así que se declara listo al arrancar.
        await self.set_transport_ready(frame)

    async def inyectar(self, pcm16: bytes) -> None:
        await self.push_audio_frame(
            InputAudioRawFrame(
                audio=pcm16, sample_rate=self._params.audio_in_sample_rate, num_channels=1
            )
        )


class SalidaMedida(BaseOutputTransport):
    """Un `BaseOutputTransport` que en vez de escribir en un socket anota cuándo.

    Importa que sea el transporte de salida de verdad porque es él quien
    reproduce a ritmo real y quien vacía el buffer en una interrupción — el
    barge-in de Pipecat se juega justo ahí. Lo que se sustituye es solo el
    destino final de los bytes.
    """

    def __init__(self, reproductor: ReproductorSimulado | None = None) -> None:
        super().__init__(
            TransportParams(
                audio_out_enabled=True,
                audio_out_sample_rate=TTS_SAMPLE_RATE,
                audio_out_end_silence_secs=0,
                audio_out_auto_silence=False,
            )
        )
        self.reproductor = reproductor or ReproductorSimulado(TTS_SAMPLE_RATE)
        self.t_primer_audio: float | None = None
        self.t_silencio: float | None = None
        """Instante en que el audio dejó de sonar por una interrupción. Se
        congela aquí y no se deduce después: tras el corte llega el audio del
        turno SIGUIENTE, y mirar «el último audio de la ejecución» mediría eso."""
        self.ms_descartados_en_corte: float = 0.0
        self.bytes_escritos = 0
        self._hubo_audio = False

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # Los transportes reales llaman a esto cuando el cliente conecta. Aquí no
        # hay cliente que esperar, así que se declara listo al arrancar.
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        if self.t_primer_audio is None:
            self.t_primer_audio = time.perf_counter()
        self.bytes_escritos += len(frame.audio)
        self._hubo_audio = True
        self.reproductor.encolar(frame.audio)
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Solo cuenta la interrupción que corta audio EN CURSO. Pipecat emite un
        # `InterruptionFrame` también al empezar el primer turno del paciente,
        # cuando todavía no ha sonado nada; contar aquella daría un tiempo de
        # corte negativo (se midió: −5.372 ms) en vez de un barge-in.
        if isinstance(frame, InterruptionFrame) and self._hubo_audio:
            self._hubo_audio = False
            # Equivalente al mensaje `parar` de la Opción B: el cliente tira lo
            # que tuviera encolado. Sin esto el corte sería una ficción del
            # servidor: lo que se haya adelantado sigue sonando en el navegador.
            self.ms_descartados_en_corte = self.reproductor.vaciar()
            self.t_silencio = time.perf_counter()


# ---------------------------------------------------------------------------
# Sonda de medición
# ---------------------------------------------------------------------------
class Sonda(FrameProcessor):
    """Observa el paso de frames y los fecha. No modifica nada.

    Va justo antes de la salida para que los tiempos que anota sean los del
    audio que se emite, no los de una etapa intermedia.
    """

    def __init__(self) -> None:
        super().__init__()
        self.t_transcripcion: float | None = None
        self.t_fin_turno: float | None = None
        self.t_primer_tts: float | None = None
        self.t_ultimo_tts: float | None = None
        self.t_interrupcion: float | None = None
        self.transcripciones: list[str] = []
        self.interrupciones = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        ahora = time.perf_counter()
        if isinstance(frame, TranscriptionFrame):
            self.t_transcripcion = self.t_transcripcion or ahora
            self.transcripciones.append(frame.text)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.t_fin_turno = self.t_fin_turno or ahora
        elif isinstance(frame, TTSAudioRawFrame):
            self.t_primer_tts = self.t_primer_tts or ahora
            self.t_ultimo_tts = ahora
        elif isinstance(frame, InterruptionFrame):
            self.t_interrupcion = ahora
            self.interrupciones += 1
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------------
# Montaje
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Piezas:
    """Todo lo que hay que poder inspeccionar después de una ejecución."""

    pipeline: Pipeline
    entrada: Any
    salida: Any
    stt: WhisperClinicoSTT
    tts: MotorLocalTTS
    llm: LLMFalsoProcessor
    sonda: Sonda
    vad: VADProcessor
    turnos: UserTurnProcessor


def parametros_vad(stop_secs: float = MS_SILENCIO_VAD) -> VADParams:
    """VAD de Silero afinado igual que el de la Opción B, para que la comparación
    mida el orquestador y no dos ajustes distintos del mismo modelo.

    `min_volume=0.0` porque el umbral de volumen de Pipecat (0.6 por defecto) se
    come los finales de frase susurrados, que en un seguimiento clínico son
    respuestas legítimas («…sí, un poco»).
    """
    return VADParams(confidence=0.5, start_secs=0.096, stop_secs=stop_secs, min_volume=0.0)


def construir(
    *,
    entrada: Any = None,
    salida: Any = None,
    motor_tts: TTSEngine | None = None,
    llm: LLMClient | None = None,
    stop_secs: float = MS_SILENCIO_VAD,
    espera_habla_s: float = 0.6,
    sistema: str = "",
) -> Piezas:
    """Monta el pipeline completo. Con `entrada`/`salida` a None usa el de laboratorio.

    Para producción se le pasa `entrada=transport.input()` y
    `salida=transport.output()` de un `SmallWebRTCTransport` — ver
    `crear_transporte_webrtc()`.
    """
    entrada = entrada if entrada is not None else EntradaInyectada()
    salida = salida if salida is not None else SalidaMedida()

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=parametros_vad(stop_secs)))
    stt = WhisperClinicoSTT()
    turnos = UserTurnProcessor(
        user_turn_strategies=UserTurnStrategies(
            start=[VADUserTurnStartStrategy()],
            # `wait_for_transcript=False`: la Opción B tampoco espera a la
            # transcripción para declarar el turno cerrado, y si aquí se
            # esperara, el fin de turno de Pipecat llevaría dentro los ~400 ms
            # de Whisper y la comparación mediría cosas distintas.
            stop=[
                SpeechTimeoutUserTurnStopStrategy(
                    user_speech_timeout=espera_habla_s, wait_for_transcript=False
                )
            ],
        )
    )
    tts = MotorLocalTTS(motor=motor_tts)
    llm_proc = LLMFalsoProcessor(llm or ClienteLLMFalso(), sistema)
    sonda = Sonda()

    pipeline = Pipeline([entrada, vad, stt, turnos, llm_proc, tts, sonda, salida])
    return Piezas(pipeline, entrada, salida, stt, tts, llm_proc, sonda, vad, turnos)


def crear_transporte_webrtc(conexion: Any, stop_secs: float = MS_SILENCIO_VAD) -> Any:
    """`SmallWebRTCTransport` con los parámetros de audio del proyecto.

    No se instancia en `construir()` porque necesita una conexión WebRTC ya
    negociada (oferta/respuesta SDP), que la trae el endpoint HTTP. Se deja
    aquí para que el montaje de producción sea una línea.
    """
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    return SmallWebRTCTransport(
        webrtc_connection=conexion,
        params=TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=STT_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=TTS_SAMPLE_RATE,
        ),
    )
