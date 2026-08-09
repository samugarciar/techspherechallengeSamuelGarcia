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
import logging
import tempfile
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from app.agent.llm_client import LLMClient, Mensaje, RespuestaLLM
from app.core.config import get_settings
from app.voice.tts import SAMPLE_RATE as TTS_SAMPLE_RATE
from app.voice.tts import TTSEngine, crear_motor, dividir_en_frases
from app.voice.vad import SAMPLE_RATE as STT_SAMPLE_RATE
from app.voice.vad import DetectorTurnos, Evento, ParametrosVAD

log = logging.getLogger("voz.pipeline")

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

MS_TURNO_MAXIMO = 60_000
"""Techo del buffer de un turno, en ms de audio.

`_recortar_a_preroll()` solo recorta mientras el paciente NO habla —hay que
conservar el turno entero para Whisper—, así que sin este tope el buffer crece a
32 kB/s durante todo el tiempo que el VAD siga viendo voz. Un micrófono abierto
en una sala con gente no genera nunca los 640 ms de silencio que cierran el
turno, y el buffer no para.

Lo caro no es la RAM. Es que cuando por fin llegue el silencio, ese buffer entero
se escribe a un WAV y se le pasa a Whisper: un turno de diez minutos son varios
minutos de transcripción de algo que ya no le importa a nadie. Con el tope se
descarta lo más viejo y se conserva el último minuto, que es lo que el paciente
acaba de decir y por tanto lo único que hay que contestar.

No se fuerza un fin de turno artificial al llegar al tope: eso haría al agente
interrumpir a un paciente que está contando algo largo, que es justo el error que
`ms_silencio_fin_turno` está calibrado para no cometer."""

MS_VENTANA_RITMO = 4_000
"""Cuánto audio hay que acumular antes de opinar sobre la frecuencia de muestreo.

Menos que esto y el arranque del micrófono —que suele entregar un lote de golpe—
daría falsos positivos."""


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

    error: str | None = None
    """Por qué se quedó a medias este turno, si se quedó.

    Existe porque un turno fallido es invisible de otro modo: corre dentro de una
    `asyncio.Task` y su excepción no llega a ningún sitio hasta que alguien hace
    `await` sobre la tarea. Con esto, quien inspeccione `sesion.turnos` —una
    prueba, o el panel de trazas de la Fase 6— ve el hueco y el motivo."""

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
# El enganche con una llamada de verdad
# ---------------------------------------------------------------------------
class LlamadaEnCurso(Protocol):
    """Lo que el bucle de voz necesita saber de una llamada persistida, y nada más.

    `SesionVoz` no sabe qué es un paciente, ni una fila de `calls`, ni el agente
    clínico de la Fase 4: sigue hablando con un `LLMClient` y, opcionalmente, con
    estos cuatro métodos. Quien los implementa es `app/api/llamadas.py` —que sí
    sabe de las dos cosas— y quien los enchufa es `app/main.py`.

    Se descartó importar el agente aquí (`from app.agent.agente import ...`
    dentro del pipeline). Ataría el bucle de voz a la Fase 4 y, sobre todo,
    obligaría al arnés de medición (`scripts/spikes/spike_voz.py`) y a los tests
    de voz a arrastrar Postgres y el LLM real para medir milisegundos de audio.
    Con la inyección, cambiar el agente clínico por `ClienteLLMFalso` sigue
    siendo un argumento.

    ── Por qué `turno_terminado` NO es `async` ──────────────────────────────
    Se llama dentro del `finally` del turno, con el paciente al teléfono, y
    `_cancelar_turno()` espera a esa tarea antes de empezar el turno siguiente:
    un `await` lento aquí —una escritura en la base que tarda— se paga en
    silencio delante del paciente. Siendo síncrono, lo único que se puede hacer
    es encolar y volver, que es exactamente lo que debe ocurrir. La firma impone
    la regla en vez de pedirla en un comentario.

    Y por eso también se llama desde el `finally`: un turno cortado por barge-in
    o reventado a mitad **también** ocurrió, y la transcripción tiene que
    contarlo. Un `await` no sería seguro ahí; una llamada síncrona sí.
    """

    def saludo_inicial(self) -> str | None:
        """La primera intervención del agente, si la llamada ya la tiene escrita.

        La dice el servidor al conectar: es una constante (declaración de sistema
        automatizado del AI Act) que `POST /api/calls` ya generó y registró, así
        que aquí solo hay que pronunciarla."""

    def turno_terminado(self, metricas: MetricasTurno) -> None:
        """Un turno que ya se ha emitido —entero o a medias—, para registrarlo."""

    def motivo_de_fin(self) -> str | None:
        """`completada` | `escalada` | `cortada` si la llamada debe terminar ya.

        Se consulta al cerrar cada turno. Es el camino por el que la decisión 2
        del contrato —ante bandera roja el agente corta— llega hasta el cliente
        en forma de mensaje `fin`."""

    async def cerrar(self) -> None:
        """La conexión se ha ido. Última oportunidad de vaciar lo pendiente."""


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
        llamada: LlamadaEnCurso | None = None,
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
        # Sin `llamada` esto es una sesión suelta: se habla, se mide y no se
        # persiste nada. Es el modo del arnés de medición y el de
        # `scripts/spikes/cliente_voz/`, y tiene que seguir funcionando sin base
        # de datos ni agente clínico detrás.
        self._llamada = llamada
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

        # Vigilancia de la frecuencia de muestreo del cliente. Ver `_vigilar_ritmo`.
        self._t_primer_audio = 0.0
        self._muestras_en_ventana = 0
        self._ritmo_denunciado = False

        self._agente_sonaba = False
        """Si el agente estaba sonando la última vez que se miró.

        Existe solo para emitir `estado: escuchando` en el instante en que deja
        de sonar. Sin él la pantalla se quedaría en «Hablando» hasta que el
        paciente abriera la boca, que es justo cuando alguien mira el indicador
        para saber si le toca hablar."""

        self.terminada = False
        """La llamada ha terminado por decisión del agente clínico (bandera roja
        confirmada, guion completado). Deja de aceptarse audio y el endpoint
        cierra el WebSocket."""

        self.turnos: list[MetricasTurno] = []
        self.barge_ins: list[MetricasBargeIn] = []
        self.reproductor: ReproductorSimulado | None = None

    # -- entrada de audio ---------------------------------------------------
    async def recibir_audio(self, pcm16: bytes) -> None:
        """Punto de entrada único. `pcm16` es int16 LE mono a 16 kHz.

        Lo primero que hace es desconfiar de lo que llega. Este método es la
        frontera con el navegador y con cualquiera que abra el WebSocket: todo lo
        que entre por aquí es de fuera y puede venir mal.
        """
        if self.terminada:
            # La llamada ya se cerró (bandera roja confirmada, o guion acabado).
            # El navegador tarda un momento en soltar el micrófono y lo que
            # llegue en ese hueco no puede abrir un turno nuevo: sería el agente
            # respondiendo después de despedirse.
            return

        t_llegada = time.perf_counter()

        if impares := len(pcm16) % 2:
            # int16 LE: una longitud impar es medio sample. `np.frombuffer` lanza
            # `ValueError` con ella y —peor— el byte suelto ya habría entrado en
            # `_buffer`, dejando TODO lo que venga detrás desplazado un byte, o
            # sea ruido blanco a partir de ahí. Se tira el byte y se sigue: un
            # trozo partido por el transporte no puede costar la llamada.
            log.warning("trozo de audio de longitud impar (%d bytes); se descarta el sobrante",
                        len(pcm16))
            pcm16 = pcm16[:-impares]
        if not pcm16:
            return

        self._vigilar_ritmo(len(pcm16) // 2, t_llegada)
        self._buffer += pcm16
        muestras = len(pcm16) // 2
        self._muestras_recibidas += muestras
        self._recortar_al_maximo()

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
            if self._agente_sonaba:
                self._agente_sonaba = False
                await self._enviar_evento({"tipo": "estado", "fase": "escuchando"})
            self.vad.agente_deja_de_hablar()

        if not self.vad.hablando:
            self._recortar_a_preroll()

    @property
    def agente_hablando(self) -> bool:
        return time.perf_counter() < self._suena_hasta

    def _recortar_a_preroll(self) -> None:
        self._recortar_a(MS_PREROLL)

    def _recortar_al_maximo(self) -> None:
        """Techo duro del buffer, se esté hablando o no. Ver `MS_TURNO_MAXIMO`."""
        self._recortar_a(MS_TURNO_MAXIMO)

    def _recortar_a(self, ms: float) -> None:
        maximo = int(ms * STT_SAMPLE_RATE / 1000) * 2
        if len(self._buffer) > maximo:
            sobra = len(self._buffer) - maximo
            del self._buffer[:sobra]
            self._buffer_desde += sobra // 2

    def _vigilar_ritmo(self, muestras: int, ahora: float) -> None:
        """Denuncia en el log a un cliente que no manda a 16 kHz.

        Es el fallo más desconcertante que puede tener este endpoint porque no
        rompe nada: si el navegador manda 48 kHz sin remuestrear —un
        `AudioContext` sin `sampleRate` fijado hace exactamente eso—, el VAD ve
        el triple de audio del que hay, los umbrales de 96 y 640 ms se cumplen en
        un tercio del tiempo real, y Whisper transcribe una grabación acelerada.
        No hay excepción, no hay error, y el síntoma que le llega a Samuel es «el
        agente no me entiende».

        No se remuestrea ni se rechaza a propósito: el servidor no sabe si el
        exceso es la frecuencia o una ráfaga de audio grabado (los tests inyectan
        turnos enteros de golpe, y eso es legítimo). Lo que sí puede hacer, y
        vale más que cualquier heurística, es dejarlo dicho una vez.
        """
        if self._ritmo_denunciado:
            return
        if self._t_primer_audio == 0.0:
            self._t_primer_audio = ahora
            return

        self._muestras_en_ventana += muestras
        transcurrido = ahora - self._t_primer_audio
        if self._muestras_en_ventana * 1000 / STT_SAMPLE_RATE < MS_VENTANA_RITMO:
            return

        ritmo = self._muestras_en_ventana / transcurrido if transcurrido > 0 else 0.0
        self._ritmo_denunciado = True
        # x1.5 y no x1.1: hay que distinguir «otra frecuencia» (x3 con 48 kHz,
        # x1.5 con 24 kHz) de «el cliente va un poco adelantado», que es normal.
        if ritmo > STT_SAMPLE_RATE * 1.5:
            log.warning(
                "el cliente parece mandar audio a otra frecuencia de muestreo: "
                "%.0f muestras/s recibidas frente a las %d acordadas. "
                "El VAD y Whisper darán resultados sin sentido.",
                ritmo, STT_SAMPLE_RATE,
            )

    # -- transiciones -------------------------------------------------------
    async def _empezo_a_hablar(self, ms_inicio: float, ms_decision: float, t: float) -> None:
        # El instante REAL en que empezó la voz, reconstruido hacia atrás desde
        # la llegada del trozo: el VAD sabe cuántos ms de audio han pasado, así
        # que la corrección es exacta y no una estimación.
        retraso_ms = self.vad.ms_stream - ms_inicio
        self._t_voz_paciente = t - retraso_ms / 1000
        # `estado` es el mensaje del contrato de llamadas; `paciente_habla` es el
        # del bucle de voz, que la página de pruebas y el frontend ya manejan. Se
        # mandan los dos: son el mismo hecho contado a dos clientes distintos, y
        # retirar el viejo rompería `scripts/spikes/cliente_voz/` sin avisar.
        await self._enviar_evento({"tipo": "estado", "fase": "escuchando"})
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

        await self._enviar_evento({"tipo": "estado", "fase": "pensando"})
        await self._enviar_evento({"tipo": "fin_de_turno"})
        await self._cancelar_turno()
        self._tarea = asyncio.create_task(self._responder(segmento, t_fin_voz, t))

    async def _cancelar_turno(self) -> None:
        """Cancela el turno en vuelo y **recoge** su resultado.

        Lo segundo es la parte que faltaba y costaba la llamada entera. Si la
        tarea ya había terminado con una excepción —Whisper caído, el motor de
        TTS sin modelo, el cliente que se fue a media síntesis y `send_bytes`
        falló—, `cancel()` no hace nada y el `await` la RELANZA aquí. Aquí es:
        dentro de `_dejo_de_hablar`, o sea en el turno siguiente, o dentro de
        `cerrar()`, o sea en el `finally` del endpoint. En los dos casos el error
        salía a kilómetros de donde ocurrió, mataba el WebSocket y —desde
        `cerrar()`— se saltaba el cierre del motor de TTS, dejando un cliente
        HTTP colgando por cada llamada que terminara mal.

        Recoger no es tragar: `_responder` ya la registró en el log y en
        `MetricasTurno.error` antes de que llegara hasta aquí.
        """
        tarea, self._tarea = self._tarea, None
        if tarea is None:
            return
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            # Solo se absorbe la cancelación de ESA tarea. Si la cancelada es
            # esta corrutina (el endpoint cerrándose), hay que dejarla pasar o el
            # cierre se queda a medias.
            if not tarea.cancelled():
                raise
        except Exception:
            log.debug("el turno cancelado ya venía fallado", exc_info=True)

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
            # `quien` y `parcial` los pide el contrato de llamadas. Aquí valen
            # siempre lo mismo —el STT solo transcribe al paciente, y este
            # pipeline no emite parciales— pero se mandan explícitos: que el
            # cliente tenga que suponer quién habló es como se acaba pintando la
            # frase del paciente en el lado del agente.
            await self._enviar_evento(
                {"tipo": "transcripcion", "quien": "paciente",
                 "texto": trans.texto, "parcial": False}
            )

            await self._hablar(m, trans.texto, t_fin_voz)

            # Fin de turno: aquí están medidas TODAS las etapas y ya ha salido el
            # audio, así que publicar las latencias no le cuesta nada al
            # paciente. Se emite en el camino de éxito y no en el `finally`
            # porque un turno cortado por barge-in no tiene latencias que
            # significar: el agente no llegó a decir lo que iba a decir.
            await self._enviar_evento({"tipo": "metricas", "ms": _ms_del_turno(m)})
            await self._comprobar_fin()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Un turno que revienta NO puede llevarse la llamada. Antes la
            # excepción se quedaba dormida dentro de la tarea, sin log ni rastro,
            # hasta que `_cancelar_turno` la despertaba en el turno siguiente.
            # Aquí muere: se registra con traza para el administrador y se anota
            # en la métrica para quien inspeccione la sesión.
            #
            # Se descartó avisar al cliente con un mensaje de control nuevo: los
            # siete tipos de `/ws/voz` están fijados en el contrato y en la página
            # de prueba, y añadir un octavo que nadie maneja no arregla nada. Lo
            # que el paciente percibe —el agente no contesta a esa pregunta— ya
            # está cubierto por el turno siguiente, que ahora sí funciona.
            m.error = f"{type(e).__name__}: {e}"
            log.exception("el turno de voz falló; la llamada continúa")
        finally:
            self.turnos.append(m)
            # También para un turno cortado o fallado: ocurrió, y la
            # transcripción que se le enseña al equipo clínico tiene que
            # contarlo. Es síncrono a propósito —ver `LlamadaEnCurso`— porque
            # este `finally` corre a veces bajo cancelación, donde un `await` no
            # es seguro.
            if self._llamada is not None:
                try:
                    self._llamada.turno_terminado(m)
                except Exception:
                    # Perder el registro es malo; cortar una llamada clínica por
                    # no poder registrarla es peor.
                    log.exception("no se pudo registrar el turno; la llamada continúa")

    async def _hablar(self, m: MetricasTurno, texto_usuario: str, t_fin_voz: float) -> None:
        """Streaming LLM → troceado por frases → síntesis → emisión.

        El troceado por frases es la razón de que el primer audio salga antes de
        que el LLM termine: en cuanto hay una frase cerrada se sintetiza y se
        manda, y el resto se va sirviendo detrás.
        """
        t_llm = time.perf_counter()
        pendiente = ""
        primera = True

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
                # El resto se recorta del texto CRUDO, no del troceado: los
                # trozos vienen con `strip()` aplicado, y reasignar `pendiente`
                # desde ellos se come el espacio del final. El síntoma es
                # «graciaspor contármelo» en cuanto el trozo del LLM parte
                # justo en un espacio, que ocurre continuamente.
                cola = frases[-1]
                corte = pendiente.rfind(cola)
                if corte >= 0:
                    pendiente = pendiente[corte:]
                else:
                    # `dividir_en_frases` fusiona las muletillas con un espacio
                    # SIMPLE, así que la cola deja de ser un substring literal en
                    # cuanto el LLM escribió un salto de línea ahí — y un LLM
                    # escribe saltos de línea a todas horas. Por ese camino de
                    # respaldo volvía el mismo bug que el `rfind` evita: `cola`
                    # está `strip()`eada y el trozo siguiente se pegaba a ella
                    # («Bien.Muchas gracias», que el TTS pronuncia como una sola
                    # palabra). Se le devuelve el blanco final del texto crudo,
                    # que es lo único que se había perdido.
                    pendiente = cola + pendiente[len(pendiente.rstrip()):]

        if resto := pendiente.strip():
            await self._emitir(m, resto, t_fin_voz)

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
            self._agente_sonaba = True
            await self._enviar_evento({"tipo": "estado", "fase": "hablando"})

        # El texto dicho se acumula AQUÍ, frase a frase, por dos motivos:
        #
        # 1. Un turno cortado por barge-in deja escrito lo que el agente sí llegó
        #    a decir. Con la asignación única al final de `_hablar`, una
        #    interrupción borraba del registro medio párrafo que el paciente
        #    había oído perfectamente.
        # 2. `agente_habla` se manda ACUMULADO y no frase suelta. El cliente
        #    trata una intervención parcial sustituyéndola, no concatenándola
        #    —es lo correcto para un STT que revisa lo que ya dijo—, así que
        #    mandar frases sueltas haría que la transcripción en vivo del agente
        #    parpadeara mostrando solo la última.
        m.texto_agente = f"{m.texto_agente} {frase}".strip()
        await self._enviar_evento({"tipo": "agente_habla", "texto": m.texto_agente})

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

    # -- apertura -----------------------------------------------------------
    def saludar(self, texto: str) -> None:
        """Dice la primera frase sin que nadie haya preguntado.

        Va como tarea cancelable, igual que un turno, y por el mismo motivo: el
        saludo del AI Act dura unos ocho segundos y quien descuelga un teléfono
        dice «¿sí?» encima casi siempre. Si esto no fuera interrumpible, la
        primera experiencia de la llamada sería el agente hablando por encima del
        paciente.

        No espera a que suene: devuelve en cuanto la tarea está creada, para que
        el endpoint pueda ponerse a recibir audio de inmediato.
        """
        self._tarea = asyncio.create_task(self._decir(texto))

    async def _decir(self, texto: str) -> None:
        m = MetricasTurno()
        t0 = time.perf_counter()
        try:
            for frase in dividir_en_frases(texto) or [texto]:
                await self._emitir(m, frase, t0)
            await self._enviar_evento({"tipo": "fin_audio", "texto": m.texto_agente})
        except asyncio.CancelledError:
            raise
        except Exception:
            # El saludo no puede llevarse la llamada: sin él la conversación
            # sigue siendo posible, el paciente simplemente empieza a hablar.
            log.exception("no se pudo decir el saludo de apertura")

    # -- fin ----------------------------------------------------------------
    async def _comprobar_fin(self) -> None:
        """¿Ha decidido el agente clínico que la llamada termina aquí?

        Se pregunta al cerrar cada turno, que es cuando el agente acaba de
        decidirlo y cuando el audio de su despedida ya ha salido. Preguntarlo
        antes cortaría la llamada a mitad de la frase que la cierra.
        """
        if self._llamada is None:
            return
        motivo = self._llamada.motivo_de_fin()
        if motivo is None:
            return
        self.terminada = True
        await self._enviar_evento({"tipo": "fin", "motivo": motivo})

    # -- cierre -------------------------------------------------------------
    async def cerrar(self) -> None:
        await self._cancelar_turno()
        if self._llamada is not None:
            # Antes que el motor de TTS: aquí es donde se vacía lo que quede
            # pendiente de escribir en la base, y hacerlo después de cerrar el
            # motor no cambia nada salvo el orden en que fallaría.
            await self._llamada.cerrar()
        if self._motor_propio:
            await self._motor.cerrar()


async def _sin_eventos(_: dict) -> None:
    return None


def _ms_del_turno(m: MetricasTurno) -> dict[str, int]:
    """`MetricasTurno` traducida a las etapas que publica el contrato.

    Los nombres internos son más precisos que los del contrato —`llm_ttft_ms` no
    es «el LLM», es el tiempo hasta su primer token— pero el panel de la pantalla
    está construido sobre `stt`, `retrieval`, `llm` y `tts`, y esta función es el
    único sitio donde se hace la traducción. `retrieval` no aparece a propósito:
    el pipeline de voz no mide el RAG, lo mide el agente por dentro, y publicar
    un cero ahí diría «la búsqueda tardó 0 ms» en vez de «esto no lo mido yo».

    `total` es `primer_audio_ms`: lo que el paciente percibe como «tarda en
    contestar», que es la única cifra de todas estas que él nota.
    """
    return {
        "stt": round(m.stt_ms),
        "llm": round(m.llm_ttft_ms),
        "tts": round(m.tts_primera_frase_ms),
        "total": round(m.primer_audio_ms),
    }


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


FabricaLlamada = Callable[[str, EnviarEvento], Awaitable["LlamadaEnCurso | None"]]


def crear_router(
    *,
    stt: Any | None = None,
    motor_tts: TTSEngine | None = None,
    llm: LLMClient | None = None,
    fabrica_llamada: FabricaLlamada | None = None,
    params_vad: ParametrosVAD | None = None,
    sistema: str = "",
):
    """Devuelve un `APIRouter` con `/ws/voz`.

    El router se construye en una función y no a nivel de módulo para poder
    inyectarle dobles en las pruebas. `app/main.py` es de otro agente: la
    anotación para que lo monte está en `docs/CONTRATO_API.md` §Cambios sobre el
    contrato.

    ── `llm` contra `fabrica_llamada` ──────────────────────────────────────
    Son los dos modos del endpoint y la diferencia es el estado por llamada:

    - `llm=` es **un** cliente compartido por todas las conexiones. Vale para lo
      que no tiene memoria: `ClienteLLMFalso`, el arnés de medición, la página
      de pruebas con micrófono.
    - `fabrica_llamada=` se invoca **una vez por conexión** con el `call_id` de
      la query, y devuelve el agente clínico de ESA llamada. Hace falta porque
      el agente guarda el historial y la fase de la llamada en memoria (§Cambios
      punto 6 del contrato): compartir una instancia entre dos llamadas
      simultáneas mezclaría las dos conversaciones en el mismo historial y le
      contaría a un paciente lo que dijo el otro.

    Sin `call_id` en la query no se llama a la fábrica y la sesión cae a `llm`:
    es la «sesión suelta sin persistir» que el contrato describe.

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
    async def voz(ws: WebSocket, call_id: str | None = None) -> None:
        await ws.accept()
        conexion = _Conexion(ws)
        await ws.send_json({"tipo": "listo", "sample_rate_entrada": STT_SAMPLE_RATE,
                            "sample_rate_salida": TTS_SAMPLE_RATE})

        llamada: LlamadaEnCurso | None = None
        if call_id:
            if fabrica_llamada is None:
                log.warning(
                    "llega ?call_id=%s pero el router se montó sin fabrica_llamada: "
                    "la sesión no se va a persistir", call_id,
                )
            else:
                try:
                    llamada = await fabrica_llamada(call_id, conexion.evento)
                except Exception:
                    log.exception("no se pudo abrir la sesión clínica de %s", call_id)

            if llamada is None:
                # Ruidoso a propósito. El caso real es un backend reiniciado a
                # mitad de llamada (§Cambios punto 6): el agente vivía en memoria
                # y ya no está. Seguir con `ClienteLLMFalso` dejaría una llamada
                # que suena bien, no guarda ni un turno y no se parece en nada a
                # lo que la pantalla dice que está pasando — el fallo silencioso
                # que este proyecto lleva evitando desde la Fase 1. Se cierra
                # diciendo por qué.
                await ws.send_json({"tipo": "fin", "motivo": "cortada"})
                await ws.close()
                return

        sesion = SesionVoz(
            enviar_audio=conexion.audio,
            enviar_evento=conexion.evento,
            stt=stt,
            motor_tts=motor_tts,
            # El mismo objeto entra por las dos puertas: es a la vez quien
            # responde (`LLMClient`) y quien lleva la cuenta de la llamada
            # (`LlamadaEnCurso`). Separarlo en dos parámetros es lo que permite
            # que `SesionVoz` siga sin saber que la Fase 4 existe.
            llm=llamada or llm,
            llamada=llamada,
            params_vad=params_vad,
            sistema=sistema,
        )
        if llamada is not None and (saludo := llamada.saludo_inicial()):
            sesion.saludar(saludo)

        try:
            while not sesion.terminada:
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
