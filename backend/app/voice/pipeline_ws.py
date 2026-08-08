"""Opción B — bucle de voz sobre WebSocket propio, sin framework de orquestación.

Existe para responder con números a la decisión nº 3 del README («Pipecat, no un
WebSocket propio»), que hasta ahora era una opinión. Aquí está la alternativa
completa: VAD, fin de turno, barge-in y reproducción, escritos a mano. Lo que
cueste está medido en `docs/VOZ_COMPARATIVA.md`.

Tres decisiones estructurales
-----------------------------

**1. `SesionVoz` no sabe qué es un WebSocket.** Recibe audio por un método y
emite por dos callbacks. Eso permite medir el pipeline entero inyectando un WAV,
sin navegador, sin micrófono y sin red — que es la única forma de que «el
barge-in tarda 180 ms» sea una medición y no una impresión. El endpoint
WebSocket de abajo son 30 líneas encima de eso.

**2. El corte del barge-in es doble: servidor y cliente.** Cortar la síntesis en
el servidor no calla al agente si el navegador ya tiene 2 s de audio en el
buffer. Por eso se manda un mensaje de control `parar` que vacía el buffer del
cliente, y la métrica que cuenta es cuándo deja de *sonar*, no cuándo deja de
sintetizarse. `ReproductorSimulado` modela exactamente ese buffer para poder
medirlo sin navegador.

**3. Audio del navegador en PCM int16 crudo a 16 kHz.** Sin Opus, sin
contenedores, sin resampleo en el servidor: el `AudioWorklet` de la página ya
entrega Float32 a la frecuencia del contexto y convertirlo allí es trivial.
Se descartó MediaRecorder/WebM porque obliga a decodificar en el servidor
(ffmpeg por trozo) y añade el retardo de contenedor del propio códec, que es
justo lo que hay que ahorrar en el camino de voz.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from app.agent.llm_client import LLMClient, Mensaje, RespuestaLLM
from app.core.config import get_settings
from app.voice.tts import SAMPLE_RATE as TTS_SAMPLE_RATE
from app.voice.tts import TTSEngine, crear_motor, dividir_en_frases
from app.voice.vad import SAMPLE_RATE as STT_SAMPLE_RATE
from app.voice.vad import DetectorTurnos, Evento, ParametrosVAD

MS_TROZO_SALIDA = 20
"""Tamaño del trozo de audio que se manda al navegador. 20 ms es el estándar de
facto en voz en tiempo real (Opus, WebRTC): más pequeño multiplica los mensajes
del WebSocket sin ganar nada; más grande engorda el buffer que hay que vaciar en
un barge-in y empeora justo la métrica que importa."""

MS_PREROLL = 300
"""Audio anterior a la detección de voz que se conserva para el STT.

El VAD confirma la voz 96 ms tarde por diseño, y Whisper sin el ataque de la
primera sílaba se come palabras cortas («sí», «no»), que en un seguimiento
clínico son las respuestas más frecuentes. 300 ms cubre la confirmación con
margen y cuesta 9,6 kB de buffer."""


# ---------------------------------------------------------------------------
# LLM de sustitución
# ---------------------------------------------------------------------------
class ClienteLLMFalso(LLMClient):
    """Implementa `LLMClient` con una respuesta fija tras un retardo configurable.

    GEMINI_API_KEY está vacía y el pipeline no puede quedarse sin medir por eso.
    Además aísla lo que interesa: la latencia del *pipeline*, no la de la red de
    Google, que varía entre ejecuciones y haría incomparables las dos opciones.

    El intercambio por el real es una línea — `crear_cliente()` en vez de
    `ClienteLLMFalso()` — porque las dos implementan la misma interfaz.

    Vive en este módulo, y no en `servicios_pipecat.py`, porque este módulo no
    importa Pipecat: así el cliente falso es compartible por las dos opciones sin
    que la Opción B arrastre Pipecat solo para tener un LLM de mentira.

    `ttft_ms=400` es el centro del rango que el README estima para Gemini 2.5
    Flash (300-600 ms).
    """

    RESPUESTA = (
        "Entiendo, gracias por contármelo. Voy a anotar que la herida está "
        "algo enrojecida. ¿Ha tenido fiebre por encima de treinta y ocho grados?"
    )

    def __init__(
        self,
        respuesta: str | None = None,
        ttft_ms: float = 400.0,
        ms_por_trozo: float = 12.0,
        trozo_chars: int = 18,
    ) -> None:
        self.respuesta = self.RESPUESTA if respuesta is None else respuesta
        self.ttft_ms = ttft_ms
        self.ms_por_trozo = ms_por_trozo
        self.trozo_chars = trozo_chars

    async def responder(
        self,
        sistema: str,
        mensajes: list[Mensaje],
        herramientas: list[dict[str, Any]] | None = None,
    ) -> RespuestaLLM:
        await asyncio.sleep(self.ttft_ms / 1000)
        return RespuestaLLM(self.respuesta, [], self.ttft_ms)

    async def stream(self, sistema: str, mensajes: list[Mensaje]) -> AsyncIterator[str]:
        await asyncio.sleep(self.ttft_ms / 1000)
        for i in range(0, len(self.respuesta), self.trozo_chars):
            if i:
                await asyncio.sleep(self.ms_por_trozo / 1000)
            yield self.respuesta[i : i + self.trozo_chars]


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------
@dataclass
class MetricasTurno:
    """Un turno completo, etapa por etapa. Todo en ms de reloj de pared."""

    fin_de_turno_ms: float = 0.0
    """Desde que el paciente calla hasta que el pipeline lo declara. Es el coste
    del umbral de silencio, y el que hace que el agente parezca dormido."""

    stt_ms: float = 0.0
    escritura_wav_ms: float = 0.0
    llm_ttft_ms: float = 0.0
    tts_primera_frase_ms: float = 0.0
    primer_audio_ms: float = 0.0
    """Desde el fin de la voz del paciente hasta el primer trozo de audio emitido.
    Es LA cifra: lo que el paciente percibe como «tarda en contestar»."""

    texto_paciente: str = ""
    texto_agente: str = ""

    @property
    def primer_audio_desde_deteccion_ms(self) -> float:
        """Lo mismo, descontando la espera de fin de turno.

        Es la cifra comparable con el «presupuesto de latencia» del README, que
        se midió etapa a etapa y por tanto **no** incluye la detección de turno.
        Compararlas sin descontarla haría parecer que el pipeline se ha
        degradado 640 ms cuando lo único que pasa es que ahora se mide algo que
        antes no se medía."""
        return self.primer_audio_ms - self.fin_de_turno_ms


@dataclass
class MetricasBargeIn:
    ms_hasta_corte_servidor: float = 0.0
    """Desde el inicio real de la voz del paciente hasta que el servidor deja de
    sintetizar."""

    ms_hasta_silencio: float = 0.0
    """Desde el inicio real de la voz del paciente hasta que el reproductor deja
    de emitir. Es la métrica honesta: lo otro no lo oye nadie."""

    audio_descartado_ms: float = 0.0
    detectado: bool = False


# ---------------------------------------------------------------------------
# Modelo del reproductor del navegador
# ---------------------------------------------------------------------------
class ReproductorSimulado:
    """El buffer de audio del cliente, en el servidor, para poder medirlo.

    Un navegador que recibe audio por WebSocket lo encola en un `AudioWorklet` y
    lo reproduce a ritmo real. Si el servidor deja de mandar, el cliente sigue
    sonando hasta agotar la cola: ahí está el error clásico de medir el barge-in
    en el servidor y creer que es instantáneo.

    Esta clase reproduce ese comportamiento con el mismo reloj que el resto de la
    medición, así que sirve para las DOS opciones: es el sumidero común que hace
    comparables los dos números.
    """

    def __init__(self, sample_rate: int = TTS_SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._muestras_en_cola = 0
        self._sonando_hasta = 0.0     # perf_counter en el que se agotaría la cola
        self.t_ultimo_sonido = 0.0
        self.ms_descartados = 0.0
        self.total_ms_reproducidos = 0.0

    def encolar(self, pcm16: bytes) -> None:
        ahora = time.perf_counter()
        muestras = len(pcm16) // 2
        ms = muestras * 1000 / self.sample_rate
        inicio = max(ahora, self._sonando_hasta)
        self._sonando_hasta = inicio + ms / 1000
        self.t_ultimo_sonido = self._sonando_hasta
        self.total_ms_reproducidos += ms

    def vaciar(self) -> float:
        """Barge-in: tira lo que quede en la cola. Devuelve los ms descartados."""
        ahora = time.perf_counter()
        restante_ms = max(0.0, (self._sonando_hasta - ahora) * 1000)
        self._sonando_hasta = ahora
        self.t_ultimo_sonido = ahora
        self.ms_descartados += restante_ms
        self.total_ms_reproducidos -= restante_ms
        return restante_ms

    @property
    def sonando(self) -> bool:
        return time.perf_counter() < self._sonando_hasta


# ---------------------------------------------------------------------------
# La sesión
# ---------------------------------------------------------------------------
EnviarAudio = Callable[[bytes], Awaitable[None]]
EnviarEvento = Callable[[dict], Awaitable[None]]


class SesionVoz:
    """Una llamada. Turnos, barge-in y reproducción escritos a mano.

    El bucle vive en `recibir_audio`, que es reentrante y no bloquea: todo lo
    caro (STT, LLM, TTS) va a una tarea aparte que se puede cancelar. Esa
    cancelabilidad ES el barge-in; si el trabajo del turno se hiciera en línea
    con la recepción de audio, no habría forma de interrumpirlo.
    """

    def __init__(
        self,
        *,
        enviar_audio: EnviarAudio,
        enviar_evento: EnviarEvento | None = None,
        stt: Any | None = None,
        motor_tts: TTSEngine | None = None,
        llm: LLMClient | None = None,
        params_vad: ParametrosVAD | None = None,
        sistema: str = "",
    ) -> None:
        from app.voice.stt import WhisperSTT

        self._enviar_audio = enviar_audio
        self._enviar_evento = enviar_evento or _sin_eventos
        self._stt = stt or WhisperSTT()
        # Si el motor viene de fuera lo comparten varias llamadas, así que esta
        # sesión no puede cerrarlo: cerrar el cliente HTTP de un motor de nube
        # dejaría muda la llamada siguiente.
        self._motor_propio = motor_tts is None
        self._motor = motor_tts or crear_motor(get_settings().tts_engine_local)
        self._llm = llm or ClienteLLMFalso()
        self._sistema = sistema
        self.vad = DetectorTurnos(params_vad)

        # Buffer del turno. Se guarda el índice de muestra del primer byte para
        # poder recortar el segmento con precisión de muestra en vez de "más o
        # menos por donde iba".
        self._buffer = bytearray()
        self._buffer_desde = 0
        self._muestras_recibidas = 0

        self._tarea: asyncio.Task | None = None
        self._suena_hasta = 0.0
        """perf_counter en que el agente TERMINARÁ de sonar en el cliente.

        No basta con un booleano puesto a True mientras se emite: el audio se
        manda tan rápido como se sintetiza (el cliente lo encola), así que el
        bucle de emisión acaba en milisegundos mientras el paciente sigue oyendo
        al agente durante segundos. Con un booleano, `_agente_hablando` volvía a
        False casi al instante y **el barge-in no se detectaba nunca**: se medían
        0 de 3 interrupciones. El servidor tiene que llevar el reloj de
        reproducción del cliente, que es justo lo que el transporte de salida de
        Pipecat hace por su cuenta."""

        self._t_voz_paciente = 0.0    # perf_counter del inicio real del turno actual

        self.turnos: list[MetricasTurno] = []
        self.barge_ins: list[MetricasBargeIn] = []
        self.reproductor: ReproductorSimulado | None = None

    # -- entrada de audio ---------------------------------------------------
    async def recibir_audio(self, pcm16: bytes) -> None:
        """Punto de entrada único. `pcm16` es int16 LE mono a 16 kHz."""
        t_llegada = time.perf_counter()
        self._buffer += pcm16
        muestras = len(pcm16) // 2
        self._muestras_recibidas += muestras

        for ev in self.vad.procesar_pcm16(pcm16):
            if ev.tipo is Evento.EMPEZO_A_HABLAR:
                await self._empezo_a_hablar(ev.ms, ev.ms_decision, t_llegada)
            else:
                await self._dejo_de_hablar(ev.ms, ev.ms_decision, t_llegada)

        # El agente deja de hablar cuando termina de SONAR, no cuando termina de
        # sintetizarse. Se comprueba aquí, en el flujo de audio entrante, porque
        # es lo único que corre continuamente; un temporizador aparte habría que
        # cancelarlo en cada barge-in.
        if not self.agente_hablando:
            self.vad.agente_deja_de_hablar()

        if not self.vad.hablando:
            self._recortar_a_preroll()

    @property
    def agente_hablando(self) -> bool:
        return time.perf_counter() < self._suena_hasta

    def _recortar_a_preroll(self) -> None:
        maximo = int(MS_PREROLL * STT_SAMPLE_RATE / 1000) * 2
        if len(self._buffer) > maximo:
            sobra = len(self._buffer) - maximo
            del self._buffer[:sobra]
            self._buffer_desde += sobra // 2

    # -- transiciones -------------------------------------------------------
    async def _empezo_a_hablar(self, ms_inicio: float, ms_decision: float, t: float) -> None:
        # El instante REAL en que empezó la voz, reconstruido hacia atrás desde
        # la llegada del trozo: el VAD sabe cuántos ms de audio han pasado, así
        # que la corrección es exacta y no una estimación.
        retraso_ms = self.vad.ms_stream - ms_inicio
        self._t_voz_paciente = t - retraso_ms / 1000
        await self._enviar_evento({"tipo": "paciente_habla"})

        if self.agente_hablando:
            await self._barge_in(self._t_voz_paciente)

    async def _barge_in(self, t_voz: float) -> None:
        """Callar al agente. Es la operación con más prisa de todo el sistema."""
        m = MetricasBargeIn(detectado=True)
        await self._cancelar_turno()
        m.ms_hasta_corte_servidor = (time.perf_counter() - t_voz) * 1000

        # Vaciar el buffer del cliente. Sin esto el agente sigue sonando lo que
        # le quede encolado y el corte del servidor es una ficción.
        await self._enviar_evento({"tipo": "parar"})
        if self.reproductor is not None:
            m.audio_descartado_ms = self.reproductor.vaciar()
        m.ms_hasta_silencio = (time.perf_counter() - t_voz) * 1000

        self._suena_hasta = time.perf_counter()
        self.vad.agente_deja_de_hablar()
        self.barge_ins.append(m)

    async def _dejo_de_hablar(self, ms_fin: float, ms_decision: float, t: float) -> None:
        retraso_ms = self.vad.ms_stream - ms_fin
        t_fin_voz = t - retraso_ms / 1000

        # Recorte con precisión de muestra. El buffer empieza en `_buffer_desde`
        # (el pre-roll, porque mientras el paciente habla no se recorta) y se
        # corta en el fin de voz: la cola de silencio no va al STT, que la
        # transcribiría como una alucinación («Gracias por ver el vídeo»).
        fin = max(0, int(ms_fin * STT_SAMPLE_RATE / 1000) - self._buffer_desde)
        segmento = bytes(self._buffer[: fin * 2])
        self._buffer.clear()
        self._buffer_desde = self._muestras_recibidas

        await self._enviar_evento({"tipo": "fin_de_turno"})
        await self._cancelar_turno()
        self._tarea = asyncio.create_task(self._responder(segmento, t_fin_voz, t))

    async def _cancelar_turno(self) -> None:
        if self._tarea is None:
            return
        self._tarea.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._tarea
        self._tarea = None

    # -- el turno -----------------------------------------------------------
    async def _responder(self, segmento: bytes, t_fin_voz: float, t_decision: float) -> None:
        m = MetricasTurno()
        m.fin_de_turno_ms = (t_decision - t_fin_voz) * 1000
        try:
            t0 = time.perf_counter()
            ruta = await asyncio.to_thread(_wav_temporal, segmento, STT_SAMPLE_RATE)
            m.escritura_wav_ms = (time.perf_counter() - t0) * 1000
            try:
                trans = await self._stt.transcribir(ruta)
            finally:
                ruta.unlink(missing_ok=True)
            m.stt_ms = trans.duracion_ms
            m.texto_paciente = trans.texto
            await self._enviar_evento({"tipo": "transcripcion", "texto": trans.texto})

            await self._hablar(m, trans.texto, t_fin_voz)
        except asyncio.CancelledError:
            raise
        finally:
            self.turnos.append(m)

    async def _hablar(self, m: MetricasTurno, texto_usuario: str, t_fin_voz: float) -> None:
        """Streaming LLM → troceado por frases → síntesis → emisión.

        El troceado por frases es la razón de que el primer audio salga antes de
        que el LLM termine: en cuanto hay una frase cerrada se sintetiza y se
        manda, y el resto se va sirviendo detrás.
        """
        t_llm = time.perf_counter()
        pendiente = ""
        primera = True
        emitidas: list[str] = []

        async for trozo in self._llm.stream(self._sistema, [Mensaje("user", texto_usuario)]):
            if primera:
                m.llm_ttft_ms = (time.perf_counter() - t_llm) * 1000
                primera = False
            pendiente += trozo
            # La última pieza puede estar a medio escribir: se deja para la
            # siguiente vuelta. Sintetizar media frase produce entonaciones
            # cortadas que suenan peor que esperar 12 ms más.
            frases = dividir_en_frases(pendiente)
            if len(frases) > 1:
                for frase in frases[:-1]:
                    await self._emitir(m, frase, t_fin_voz)
                    emitidas.append(frase)
                # El resto se recorta del texto CRUDO, no del troceado: los
                # trozos vienen con `strip()` aplicado, y reasignar `pendiente`
                # desde ellos se come el espacio del final. El síntoma es
                # «graciaspor contármelo» en cuanto el trozo del LLM parte
                # justo en un espacio, que ocurre continuamente.
                cola = frases[-1]
                corte = pendiente.rfind(cola)
                pendiente = pendiente[corte:] if corte >= 0 else cola

        if resto := pendiente.strip():
            await self._emitir(m, resto, t_fin_voz)
            emitidas.append(resto)

        m.texto_agente = " ".join(emitidas)
        # OJO: aquí NO se apaga `agente_hablando`. Se ha terminado de *emitir*,
        # pero el cliente sigue reproduciendo lo encolado, y durante ese rato el
        # paciente todavía puede —y suele— interrumpir. Lo apaga `recibir_audio`
        # cuando el reloj de reproducción se agota.
        await self._enviar_evento({"tipo": "fin_audio", "texto": m.texto_agente})

    async def _emitir(self, m: MetricasTurno, frase: str, t_fin_voz: float) -> None:
        t0 = time.perf_counter()
        audio = await self._motor.sintetizar(frase)
        ms = (time.perf_counter() - t0) * 1000
        if not m.tts_primera_frase_ms:
            m.tts_primera_frase_ms = ms

        if not self.agente_hablando:
            self.vad.agente_empieza_a_hablar()
            await self._enviar_evento({"tipo": "agente_habla", "texto": frase})

        pcm16 = (np.clip(audio.pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        por_trozo = int(audio.sample_rate * MS_TROZO_SALIDA / 1000) * 2
        for i in range(0, len(pcm16), por_trozo):
            trozo = pcm16[i : i + por_trozo]
            await self._enviar_audio(trozo)
            # Avanza el reloj de reproducción del cliente: encolar N ms de audio
            # alarga N ms el rato durante el cual el agente «está hablando».
            ms_trozo = (len(trozo) // 2) * 1000 / audio.sample_rate
            self._suena_hasta = max(time.perf_counter(), self._suena_hasta) + ms_trozo / 1000
            if not m.primer_audio_ms:
                m.primer_audio_ms = (time.perf_counter() - t_fin_voz) * 1000
            # Cede el control: sin esto la emisión monopoliza el event loop y el
            # audio entrante del paciente no se procesa hasta el final — o sea,
            # no habría barge-in posible.
            await asyncio.sleep(0)

    # -- cierre -------------------------------------------------------------
    async def cerrar(self) -> None:
        await self._cancelar_turno()
        if self._motor_propio:
            await self._motor.cerrar()


async def _sin_eventos(_: dict) -> None:
    return None


def _wav_temporal(pcm16: bytes, sample_rate: int) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        destino = Path(tmp.name)
    with wave.open(str(destino), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return destino


# ---------------------------------------------------------------------------
# El endpoint — 30 líneas encima de `SesionVoz`
# ---------------------------------------------------------------------------
@dataclass
class _Conexion:
    """Adaptador entre `SesionVoz` y un WebSocket de FastAPI."""

    ws: Any
    enviados: int = field(default=0)

    async def audio(self, pcm16: bytes) -> None:
        await self.ws.send_bytes(pcm16)
        self.enviados += 1

    async def evento(self, datos: dict) -> None:
        await self.ws.send_json(datos)


def crear_router(
    *,
    stt: Any | None = None,
    motor_tts: TTSEngine | None = None,
    llm: LLMClient | None = None,
    params_vad: ParametrosVAD | None = None,
    sistema: str = "",
):
    """Devuelve un `APIRouter` con `/ws/voz`.

    El router se construye en una función y no a nivel de módulo para poder
    inyectarle dobles en las pruebas. `app/main.py` es de otro agente: la
    anotación para que lo monte está en `docs/CONTRATO_API.md` §Cambios sobre el
    contrato.

    El STT y el motor de TTS se crean **una vez aquí**, no por conexión, por dos
    razones: cargar Kokoro o Whisper en el `accept()` del WebSocket añadiría
    segundos al inicio de cada llamada, y un motor mal configurado tiene que
    fallar al arrancar el servidor —donde se ve— y no en mitad de una demo.

    OJO con `WebSocket`: se importa a nivel de módulo, no aquí. Este módulo usa
    `from __future__ import annotations`, así que las anotaciones son cadenas y
    FastAPI las resuelve con `get_type_hints()` contra los *globales del módulo*.
    Con el import dentro de la función, `WebSocket` no está en esos globales,
    FastAPI no reconoce el parámetro y lo trata como un parámetro de query: el
    handshake se cierra con un 1008 y `{'loc': ['query','ws'], 'msg': 'Field
    required'}`, que no dice absolutamente nada sobre la causa real.
    """
    from fastapi import APIRouter

    from app.voice.stt import WhisperSTT

    stt = stt or WhisperSTT()
    motor_tts = motor_tts or crear_motor(get_settings().tts_engine_local)
    router = APIRouter()

    @router.websocket("/ws/voz")
    async def voz(ws: WebSocket) -> None:
        await ws.accept()
        conexion = _Conexion(ws)
        sesion = SesionVoz(
            enviar_audio=conexion.audio,
            enviar_evento=conexion.evento,
            stt=stt,
            motor_tts=motor_tts,
            llm=llm,
            params_vad=params_vad,
            sistema=sistema,
        )
        await ws.send_json({"tipo": "listo", "sample_rate_entrada": STT_SAMPLE_RATE,
                            "sample_rate_salida": TTS_SAMPLE_RATE})
        try:
            while True:
                mensaje = await ws.receive()
                if (datos := mensaje.get("bytes")) is not None:
                    await sesion.recibir_audio(datos)
                elif mensaje.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            await sesion.cerrar()

    return router
