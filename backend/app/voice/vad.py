"""Detección de actividad de voz y de turnos — Silero VAD por ONNX, sin Pipecat.

Es la pieza que la Opción B (WebSocket propio) tiene que escribir a mano y que
Pipecat promete regalar. Vive aparte del pipeline a propósito: así se puede
medir con audio inyectado, sin transporte, sin navegador y sin micrófono.

Dos decisiones que explican la forma del módulo:

**1. El reloj es el contador de muestras, no `time.monotonic()`.**
Cada ventana de 512 muestras a 16 kHz son exactamente 32 ms de audio. Si el
detector fecha los eventos contando muestras, la misma grabación produce
*siempre* los mismos milisegundos, corra la prueba en una máquina cargada o
vacía. Con reloj de pared la medida de «cuánto tarda en decidir que el paciente
terminó» se contaminaría con el jitter del event loop, que es justo lo que no
queremos medir. El coste de CPU se mide aparte (`ms_computo`).

**2. Dos umbrales de silencio, no uno.**
Terminar un turno y detectar una interrupción son decisiones distintas con
costes distintos:

  - Fin de turno: equivocarse por abajo corta al paciente a media frase. Caro.
    Se exige silencio sostenido (`ms_silencio_fin_turno`, ver la calibración en
    `docs/VOZ_COMPARATIVA.md`).
  - Barge-in: equivocarse por arriba deja al agente hablando encima del
    paciente, que es lo que hace que un agente suene a contestador. Se dispara
    con la primera voz sostenida (`ms_inicio_barge_in`), mucho antes.

Silero v5 exige ventanas de 512 muestras a 16 kHz. Se descartó un VAD de
energía (RMS): con la voz del agente saliendo por el altavoz y volviendo por el
micro, la energía sola confunde eco con paciente; Silero distingue voz de ruido
y es lo bastante barato (ver `ms_computo`) para correrlo en cada ventana.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
VENTANA = 512                      # 32 ms — tamaño obligatorio de Silero v5 a 16 kHz
MS_POR_VENTANA = VENTANA * 1000 / SAMPLE_RATE
_CONTEXTO = 64                     # Silero v5 concatena 64 muestras de contexto previo

# Copia local del modelo (2.3 MB). Se vendoriza a propósito: la Opción B no debe
# depender de que Pipecat esté instalado para funcionar, porque el sentido de la
# opción es precisamente no depender de Pipecat.
_MODELO_VENDORIZADO = Path(__file__).with_name("modelos") / "silero_vad.onnx"


class Evento(Enum):
    """Transiciones del detector. Solo hay dos, y las dos son bordes."""

    EMPEZO_A_HABLAR = "empezo"
    DEJO_DE_HABLAR = "dejo"


@dataclass(slots=True, frozen=True)
class EventoVAD:
    tipo: Evento
    ms: float
    """Milisegundos desde el inicio del stream, contados por muestras.

    Para `DEJO_DE_HABLAR` es el instante en que **terminó la voz**, no el
    instante en que el detector lo decidió: la espera de silencio se resta. Así
    `ms` responde «¿dónde acabó de hablar?» y `ms_decision` responde «¿cuándo lo
    supimos?», que son las dos preguntas del informe y no son la misma.
    """
    ms_decision: float


@dataclass(slots=True)
class ParametrosVAD:
    """Umbrales del detector. Los valores por defecto salen de la calibración
    documentada en `docs/VOZ_COMPARATIVA.md` §Fin de turno."""

    umbral: float = 0.5
    """Probabilidad de voz de Silero por encima de la cual la ventana cuenta como
    voz. Pipecat usa 0.7 más un umbral de volumen; aquí se baja a 0.5 porque el
    volumen ya lo normaliza el navegador y 0.7 se come los finales de frase
    susurrados («…sí, un poco»), que son exactamente los que no hay que cortar."""

    ms_inicio: int = 96
    """Voz sostenida antes de declarar que el paciente habla (3 ventanas). Filtra
    toses, golpes de mesa y el clic del micrófono."""

    ms_silencio_fin_turno: int = 640
    """Silencio sostenido antes de declarar el turno terminado. Es EL parámetro
    del módulo: ver la medición en la comparativa."""

    ms_inicio_barge_in: int = 96
    """Voz sostenida antes de cortar al agente. Igual de corto que `ms_inicio`:
    lo que se paga por equivocarse es un corte de audio de 100 ms, mucho más
    barato que pisar al paciente."""


MUESTRAS_LATENCIA = 4_096
"""Cuántas latencias de ventana se conservan para el p95.

Eran todas, en una lista que solo crecía: 31 floats por segundo de llamada,
dentro de un objeto que vive lo que dure la llamada. No tumba nada —son 2,5 MB a
la hora— pero es una fuga de manual y el p95 no mejora por tener más muestras.
4.096 ventanas son ~2 min de audio, que es la ventana en la que la cifra
significa algo: si el VAD se degrada porque Whisper le está robando la CPU, lo
que interesa es el p95 de ahora, no el promedio con el arranque en frío dentro."""


@dataclass(slots=True)
class Metricas:
    ventanas: int = 0
    ms_computo: float = 0.0
    """Tiempo de CPU acumulado dentro de la ONNX. Dividido por `ventanas` da el
    coste real por ventana de 32 ms; si se acercara a 32 ms el VAD no correría
    en tiempo real."""

    latencias_ventana_ms: deque[float] = field(
        default_factory=lambda: deque(maxlen=MUESTRAS_LATENCIA)
    )

    @property
    def ms_por_ventana(self) -> float:
        return self.ms_computo / self.ventanas if self.ventanas else 0.0

    @property
    def p95_ms(self) -> float:
        if not self.latencias_ventana_ms:
            return 0.0
        return float(np.percentile(np.fromiter(self.latencias_ventana_ms, dtype=float), 95))


def ruta_modelo() -> Path:
    """Localiza el .onnx. Orden: variable de entorno → copia local → Pipecat.

    La variable de entorno existe para que Samuel pueda apuntar a otra versión
    de Silero sin tocar código.
    """
    if env := os.environ.get("SILERO_VAD_ONNX"):
        return Path(env)
    if _MODELO_VENDORIZADO.exists():
        return _MODELO_VENDORIZADO
    from importlib import resources

    return Path(str(resources.files("pipecat.audio.vad.data").joinpath("silero_vad.onnx")))


# Sesiones de ONNX compartidas por ruta de modelo. Ver `_sesion_compartida`.
_sesiones: dict[str, object] = {}
_cerrojo_sesiones = threading.Lock()


def _sesion_compartida(ruta: Path):
    """Una sola `InferenceSession` por fichero de modelo, para todo el proceso.

    Se construía una por `DetectorTurnos`, y hay un `DetectorTurnos` por
    `SesionVoz`, o sea uno por conexión de `/ws/voz`: cada llamada volvía a
    cargar Silero. `crear_router()` documenta que el STT y el TTS se crean una
    sola vez «porque cargar Kokoro o Whisper en el accept() añadiría segundos al
    inicio de cada llamada»; el VAD es el tercer modelo y se le escapó. Medido:
    ~27 ms y una arena de memoria propia por conexión, en una máquina de 16 GB
    que ya comparte Whisper, bge-m3 y el reranker.

    Compartirla es seguro, y no por suerte: el estado del LSTM viaja
    explícitamente como entrada y salida de `run()` (`state`), no vive dentro de
    la sesión. Dos llamadas simultáneas conservan cada una su memoria en su
    `_SileroOnnx`. `InferenceSession.run` es reentrante por contrato de
    onnxruntime, así que tampoco hace falta serializar las llamadas — solo la
    construcción, que es lo que protege el cerrojo.

    Se descartó cachear con `functools.lru_cache`: haría falta un `Path`
    hashable y, sobre todo, no habría forma limpia de vaciarla desde una prueba.
    """
    import onnxruntime

    clave = str(ruta)
    if (sesion := _sesiones.get(clave)) is not None:
        return sesion

    with _cerrojo_sesiones:
        if (sesion := _sesiones.get(clave)) is not None:
            return sesion
        opciones = onnxruntime.SessionOptions()
        # Un hilo por operador: el VAD compite con Whisper y el TTS por la CPU, y
        # con ventanas de 512 muestras el paralelismo no compra nada.
        opciones.inter_op_num_threads = 1
        opciones.intra_op_num_threads = 1
        sesion = onnxruntime.InferenceSession(
            clave,
            providers=["CPUExecutionProvider"],
            sess_options=opciones,
        )
        _sesiones[clave] = sesion
        return sesion


class _SileroOnnx:
    """Envoltorio mínimo del modelo. Estado LSTM entre ventanas: es un modelo con
    memoria, y reiniciarlo en cada ventana degrada mucho la detección.

    El estado es lo único propio de esta instancia; la sesión de ONNX se comparte
    (ver `_sesion_compartida`)."""

    def __init__(self, ruta: Path | None = None) -> None:
        self._sesion = _sesion_compartida(ruta or ruta_modelo())
        self.reiniciar()

    def reiniciar(self) -> None:
        self._estado = np.zeros((2, 1, 128), dtype=np.float32)
        self._contexto = np.zeros((1, _CONTEXTO), dtype=np.float32)

    def __call__(self, ventana: np.ndarray) -> float:
        x = np.concatenate((self._contexto, ventana.reshape(1, -1)), axis=1)
        salida, self._estado = self._sesion.run(
            None,
            {"input": x, "state": self._estado, "sr": np.array(SAMPLE_RATE, dtype=np.int64)},
        )
        self._contexto = x[:, -_CONTEXTO:]
        return float(salida[0][0])


class DetectorTurnos:
    """Máquina de estados de dos estados sobre la probabilidad de Silero.

    Se alimenta con audio en trozos de cualquier tamaño (el navegador manda lo
    que le da la gana) y emite eventos. Es un generador y no un callback a
    propósito: un generador se puede probar sin event loop.
    """

    def __init__(self, params: ParametrosVAD | None = None, ruta_modelo_: Path | None = None):
        self.params = params or ParametrosVAD()
        self._modelo = _SileroOnnx(ruta_modelo_)
        self.metricas = Metricas()
        self._resto = np.zeros(0, dtype=np.float32)
        self._muestras = 0            # muestras consumidas: es el reloj del módulo
        self._hablando = False
        self._ms_voz_seguida = 0.0
        self._ms_silencio_seguido = 0.0
        self._ms_fin_voz = 0.0        # última ventana con voz, para fechar el fin de turno
        self._hablando_el_agente = False
        self.ultima_probabilidad = 0.0

    # -- entrada ------------------------------------------------------------
    def procesar(self, pcm: np.ndarray) -> list[EventoVAD]:
        """Consume float32 mono en [-1, 1] a 16 kHz y devuelve los eventos nuevos."""
        return list(self.iterar(pcm))

    def procesar_pcm16(self, datos: bytes) -> list[EventoVAD]:
        """Igual, pero con los bytes crudos que llegan del navegador (int16 LE)."""
        pcm = np.frombuffer(datos, dtype="<i2").astype(np.float32) / 32768.0
        return list(self.iterar(pcm))

    def iterar(self, pcm: np.ndarray) -> Iterator[EventoVAD]:
        self._resto = np.concatenate((self._resto, np.asarray(pcm, dtype=np.float32)))
        while len(self._resto) >= VENTANA:
            ventana, self._resto = self._resto[:VENTANA], self._resto[VENTANA:]
            evento = self._ventana(ventana)
            if evento is not None:
                yield evento

    # -- núcleo -------------------------------------------------------------
    def _ventana(self, ventana: np.ndarray) -> EventoVAD | None:
        t0 = time.perf_counter()
        p = self._modelo(ventana)
        dt = (time.perf_counter() - t0) * 1000
        self.metricas.ventanas += 1
        self.metricas.ms_computo += dt
        self.metricas.latencias_ventana_ms.append(dt)

        self._muestras += VENTANA
        ahora = self._muestras * 1000 / SAMPLE_RATE
        self.ultima_probabilidad = p
        hay_voz = p >= self.params.umbral

        if hay_voz:
            self._ms_voz_seguida += MS_POR_VENTANA
            self._ms_silencio_seguido = 0.0
            self._ms_fin_voz = ahora
        else:
            self._ms_silencio_seguido += MS_POR_VENTANA
            self._ms_voz_seguida = 0.0

        umbral_inicio = (
            self.params.ms_inicio_barge_in if self._hablando_el_agente else self.params.ms_inicio
        )
        if not self._hablando and self._ms_voz_seguida >= umbral_inicio:
            self._hablando = True
            # Se fecha el inicio en el momento en que la voz EMPEZÓ, no cuando se
            # confirmó: si no, cada medición de barge-in llevaría dentro los
            # 96 ms de confirmación y no se sabría cuál es cuál.
            inicio = ahora - self._ms_voz_seguida
            return EventoVAD(Evento.EMPEZO_A_HABLAR, inicio, ahora)

        if self._hablando and self._ms_silencio_seguido >= self.params.ms_silencio_fin_turno:
            self._hablando = False
            return EventoVAD(Evento.DEJO_DE_HABLAR, self._ms_fin_voz, ahora)

        return None

    # -- estado del agente --------------------------------------------------
    def agente_empieza_a_hablar(self) -> None:
        """El pipeline avisa de que el TTS está emitiendo.

        Cambia el umbral de arranque al de barge-in. No se silencia el VAD
        mientras habla el agente —que sería lo fácil— porque entonces el
        paciente no podría interrumpir, que es justo lo que hay que soportar.
        """
        self._hablando_el_agente = True

    def agente_deja_de_hablar(self) -> None:
        self._hablando_el_agente = False

    @property
    def hablando(self) -> bool:
        return self._hablando

    @property
    def ms_stream(self) -> float:
        """Posición actual en el stream, en ms de audio consumido."""
        return self._muestras * 1000 / SAMPLE_RATE

    def reiniciar(self) -> None:
        self._modelo.reiniciar()
        self._resto = np.zeros(0, dtype=np.float32)
        self._muestras = 0
        self._hablando = False
        self._ms_voz_seguida = 0.0
        self._ms_silencio_seguido = 0.0
        self._ms_fin_voz = 0.0


# ---------------------------------------------------------------------------
# Utilidades de medición — se usan desde los spikes y los tests
# ---------------------------------------------------------------------------
def limites_de_voz(pcm: np.ndarray, params: ParametrosVAD | None = None) -> tuple[float, float]:
    """Devuelve (ms de la primera ventana con voz, ms de la última) de un clip.

    Es la «verdad sobre el terreno» contra la que se mide el fin de turno: un
    clip de `say` trae silencio delante y detrás, así que sin esto la latencia
    medida incluiría un silencio que no es culpa del detector.
    """
    d = DetectorTurnos(params or ParametrosVAD())
    pcm = np.asarray(pcm, dtype=np.float32)
    primera = ultima = None
    for i in range(0, len(pcm) - VENTANA + 1, VENTANA):
        p = d._modelo(pcm[i : i + VENTANA])
        if p >= d.params.umbral:
            ms = (i + VENTANA) * 1000 / SAMPLE_RATE
            primera = ms - MS_POR_VENTANA if primera is None else primera
            ultima = ms
    return (primera or 0.0, ultima or 0.0)
