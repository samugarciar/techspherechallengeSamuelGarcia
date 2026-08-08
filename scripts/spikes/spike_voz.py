"""Spike 6 — Pipecat contra WebSocket propio, decidido por medición.

El plan daba Pipecat por decidido. Samuel pidió construir las dos y elegir con
números. Este script produce esos números; la lectura está en
`docs/VOZ_COMPARATIVA.md`.

Qué mide, y por qué así
-----------------------
Sin micrófono ni humano, la única forma de que «tarda 1,5 s» sea un dato es
inyectar un WAV conocido **a ritmo real** (trozos de 20 ms con espera) y fechar
lo que sale. Acelerar la inyección falsearía todo lo que depende del reloj: el
fin de turno, el barge-in y el ritmo del reproductor.

Cada escenario se ejecuta contra las DOS opciones con el mismo audio, el mismo
LLM falso (TTFT fijo) y el mismo motor de TTS. Lo único distinto es el
orquestador, que es lo que se está juzgando.

    1. turno              — latencia por etapa hasta el primer audio
    2. barge-in           — el paciente pisa al agente; ¿en cuántos ms se calla?
    3. fin de turno       — barrido de umbrales de silencio
    4. stt                — el WhisperSTTServiceMLX de Pipecat contra el nuestro

Uso::

    cd backend && uv run python ../scripts/spikes/spike_voz.py todo
    cd backend && uv run --with kokoro python ../scripts/spikes/spike_voz.py todo --tts kokoro

El motor por defecto es `say` (macOS) porque `kokoro` NO está declarado en
`backend/pyproject.toml` y no se puede instalar sin tocarlo — ver el informe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "backend"))

AUDIO = Path(__file__).parent / "audio"
SALIDA = Path(__file__).parent / "out"

from app.voice.pipeline_ws import (  # noqa: E402
    ClienteLLMFalso,
    ReproductorSimulado,
    SesionVoz,
)
from app.voice.tts import crear_motor  # noqa: E402
from app.voice.vad import (  # noqa: E402
    SAMPLE_RATE,
    DetectorTurnos,
    ParametrosVAD,
    limites_de_voz,
)

MS_TROZO_ENTRADA = 20
"""Lo que manda un `AudioWorklet` del navegador por mensaje. Inyectar en trozos
más grandes escondería la latencia de troceado que sí existe en producción."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def cargar(nombre: str) -> np.ndarray:
    pcm, sr = sf.read(AUDIO / f"{nombre}.wav", dtype="float32")
    if sr != SAMPLE_RATE:
        raise SystemExit(f"{nombre}.wav está a {sr} Hz; se esperaba {SAMPLE_RATE}")
    return pcm if pcm.ndim == 1 else pcm.mean(axis=1)


def con_silencio(pcm: np.ndarray, ms: int) -> np.ndarray:
    """Añade silencio al final. Es lo que dispara el fin de turno.

    Los clips de `say` terminan justo con la última sílaba, así que sin esto el
    detector nunca vería el silencio que cierra el turno — y el escenario no
    probaría nada.
    """
    return np.concatenate((pcm, np.zeros(int(SAMPLE_RATE * ms / 1000), dtype=np.float32)))


def a_pcm16(pcm: np.ndarray) -> bytes:
    return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def mezclar_en(base: np.ndarray, encima: np.ndarray, ms_offset: int) -> np.ndarray:
    """Superpone `encima` sobre `base` a partir de `ms_offset`.

    Sirve para el escenario de barge-in: durante la respuesta del agente, el
    micrófono del paciente sigue abierto y hay que meter voz por él. Se mezcla
    (no se sustituye) porque en una llamada real el micro capta las dos cosas.
    """
    inicio = int(SAMPLE_RATE * ms_offset / 1000)
    salida = base.copy()
    if inicio >= len(salida):
        salida = np.concatenate((salida, np.zeros(inicio - len(salida) + 1, dtype=np.float32)))
    fin = inicio + len(encima)
    if fin > len(salida):
        salida = np.concatenate((salida, np.zeros(fin - len(salida), dtype=np.float32)))
    salida[inicio:fin] += encima
    return np.clip(salida, -1.0, 1.0)


@dataclass
class Resultado:
    escenario: str
    opcion: str
    datos: dict = field(default_factory=dict)


def mediana(xs: list[float]) -> float:
    return round(statistics.median(xs), 1) if xs else 0.0


# ---------------------------------------------------------------------------
# Inyector a ritmo real
# ---------------------------------------------------------------------------
class Inyector:
    """Reproduce un array de audio hacia un consumidor asíncrono en tiempo real.

    Mantiene el ritmo contra un reloj absoluto y no con `sleep(0.02)` acumulado:
    si el pipeline se queda pensando 60 ms, el siguiente trozo sale antes para
    recuperar, igual que hace un navegador. Con `sleep` acumulado la inyección
    se iría retrasando y las latencias medidas saldrían infladas.
    """

    def __init__(self, pcm: np.ndarray, consumidor) -> None:
        self.pcm = pcm
        self.consumidor = consumidor
        self.t0 = 0.0

    async def correr(self) -> None:
        por_trozo = int(SAMPLE_RATE * MS_TROZO_ENTRADA / 1000)
        self.t0 = time.perf_counter()
        for i in range(0, len(self.pcm), por_trozo):
            objetivo = self.t0 + (i / SAMPLE_RATE)
            if (espera := objetivo - time.perf_counter()) > 0:
                await asyncio.sleep(espera)
            await self.consumidor(a_pcm16(self.pcm[i : i + por_trozo]))


# ---------------------------------------------------------------------------
# Opción B — WebSocket propio
# ---------------------------------------------------------------------------
async def correr_b(
    pcm: np.ndarray,
    *,
    motor: str,
    params: ParametrosVAD | None = None,
    ttft_ms: float = 400.0,
    cola_extra_s: float = 6.0,
) -> tuple[SesionVoz, float]:
    repro = ReproductorSimulado()
    sesion = SesionVoz(
        enviar_audio=_enviar_a(repro),
        enviar_evento=_evento_a(repro),
        motor_tts=crear_motor(motor),
        llm=ClienteLLMFalso(ttft_ms=ttft_ms),
        params_vad=params,
    )
    sesion.reproductor = repro
    iny = Inyector(pcm, sesion.recibir_audio)
    await iny.correr()
    # El audio de entrada se acaba antes que la respuesta: hay que esperar a que
    # el turno termine, o se mediría un pipeline a medias.
    await asyncio.sleep(cola_extra_s)
    await sesion.cerrar()
    return sesion, iny.t0


def _enviar_a(repro: ReproductorSimulado):
    async def enviar(pcm16: bytes) -> None:
        repro.encolar(pcm16)

    return enviar


def _evento_a(repro: ReproductorSimulado):
    async def evento(datos: dict) -> None:
        return None

    return evento


# ---------------------------------------------------------------------------
# Opción A — Pipecat
# ---------------------------------------------------------------------------
async def correr_a(
    pcm: np.ndarray,
    *,
    motor: str,
    stop_secs: float = 0.2,
    espera_habla_s: float = 0.6,
    ttft_ms: float = 400.0,
    cola_extra_s: float = 6.0,
):
    from pipecat.frames.frames import EndFrame, StartFrame
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask

    from app.voice.pipeline_pipecat import construir

    piezas = construir(
        motor_tts=crear_motor(motor),
        llm=ClienteLLMFalso(ttft_ms=ttft_ms),
        stop_secs=stop_secs,
        espera_habla_s=espera_habla_s,
    )
    tarea = PipelineTask(
        piezas.pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=24_000,
            enable_metrics=True,
        ),
        cancel_on_idle_timeout=False,
    )
    runner = PipelineRunner(handle_sigint=False)
    corriendo = asyncio.create_task(runner.run(tarea))
    await asyncio.sleep(0.5)   # que el StartFrame recorra el pipeline

    iny = Inyector(pcm, piezas.entrada.inyectar)
    await iny.correr()
    await asyncio.sleep(cola_extra_s)

    await tarea.queue_frame(EndFrame())
    try:
        await asyncio.wait_for(corriendo, timeout=10)
    except (TimeoutError, asyncio.TimeoutError):
        await tarea.cancel()
    _ = StartFrame  # documenta qué frames intervienen; no se instancia aquí
    return piezas, iny.t0


# ---------------------------------------------------------------------------
# Escenario 1 — un turno completo
# ---------------------------------------------------------------------------
async def escenario_turno(motor: str, repeticiones: int) -> list[Resultado]:
    pcm = con_silencio(cargar("apendicectomia"), 1500)
    _, fin_voz_ms = limites_de_voz(pcm)
    salida: list[Resultado] = []

    for opcion in ("B", "A"):
        acum: dict[str, list[float]] = {}
        textos: list[str] = []
        for _ in range(repeticiones):
            if opcion == "B":
                sesion, t0 = await correr_b(pcm, motor=motor)
                if not sesion.turnos:
                    continue
                m = sesion.turnos[-1]
                repro = sesion.reproductor
                acum.setdefault("fin_de_turno_ms", []).append(m.fin_de_turno_ms)
                acum.setdefault("escritura_wav_ms", []).append(m.escritura_wav_ms)
                acum.setdefault("stt_ms", []).append(m.stt_ms)
                acum.setdefault("llm_ttft_ms", []).append(m.llm_ttft_ms)
                acum.setdefault("tts_1a_frase_ms", []).append(m.tts_primera_frase_ms)
                acum.setdefault("primer_audio_ms", []).append(m.primer_audio_ms)
                acum.setdefault("audio_emitido_ms", []).append(repro.total_ms_reproducidos)
                textos.append(m.texto_paciente)
            else:
                piezas, t0 = await correr_a(pcm, motor=motor)
                s = piezas.sonda
                t_fin_voz = t0 + fin_voz_ms / 1000
                if s.t_primer_tts is None:
                    continue
                acum.setdefault("fin_de_turno_ms", []).append(
                    (s.t_fin_turno - t_fin_voz) * 1000 if s.t_fin_turno else 0.0
                )
                acum.setdefault("escritura_wav_ms", []).append(piezas.stt.ms_escritura_wav)
                acum.setdefault("stt_ms", []).append(
                    piezas.stt.ultima.duracion_ms if piezas.stt.ultima else 0.0
                )
                acum.setdefault("llm_ttft_ms", []).append(piezas.llm.ttft_ms or 0.0)
                acum.setdefault("tts_1a_frase_ms", []).append(piezas.tts.ms_primera_sintesis or 0.0)
                acum.setdefault("primer_audio_ms", []).append((s.t_primer_tts - t_fin_voz) * 1000)
                acum.setdefault(
                    "audio_emitido_ms", []
                ).append(piezas.salida.reproductor.total_ms_reproducidos)
                textos.extend(s.transcripciones)

        datos = {k: mediana(v) for k, v in acum.items()}
        datos["transcripcion"] = textos[-1] if textos else ""
        datos["n"] = repeticiones
        salida.append(Resultado("turno", opcion, datos))
    return salida


# ---------------------------------------------------------------------------
# Escenario 2 — barge-in
# ---------------------------------------------------------------------------
async def escenario_barge_in(motor: str, repeticiones: int) -> list[Resultado]:
    """El paciente responde, el agente empieza a hablar, y el paciente le pisa.

    El instante de la interrupción se elige a mano (`MS_INTERRUPCION`) para caer
    con seguridad dentro de la respuesta del agente: si cayera antes, no habría
    nada que cortar y la medición no significaría nada.
    """
    MS_INTERRUPCION = 3800
    base = con_silencio(cargar("turno_corto"), 6000)
    corte = cargar("interrupcion")
    pcm = mezclar_en(base, corte, MS_INTERRUPCION)
    ms_voz_corte, _ = limites_de_voz(corte)
    inicio_real_ms = MS_INTERRUPCION + ms_voz_corte

    salida: list[Resultado] = []
    for opcion in ("B", "A"):
        cortes_servidor: list[float] = []
        cortes_audibles: list[float] = []
        descartados: list[float] = []
        detectados = 0
        for _ in range(repeticiones):
            if opcion == "B":
                sesion, t0 = await correr_b(pcm, motor=motor, cola_extra_s=3.0)
                for b in sesion.barge_ins:
                    detectados += 1
                    cortes_servidor.append(b.ms_hasta_corte_servidor)
                    cortes_audibles.append(b.ms_hasta_silencio)
                    descartados.append(b.audio_descartado_ms)
            else:
                piezas, t0 = await correr_a(pcm, motor=motor, cola_extra_s=3.0)
                s = piezas.sonda
                t_voz = t0 + inicio_real_ms / 1000
                if s.t_interrupcion is not None:
                    detectados += 1
                    cortes_servidor.append((s.t_interrupcion - t_voz) * 1000)
                if s.t_ultimo_tts is not None and s.t_interrupcion is not None:
                    # El último audio que llegó a sonar. Si el TTS siguió
                    # emitiendo después de la interrupción, aquí se ve.
                    fin = max(
                        piezas.salida.reproductor.t_ultimo_sonido,
                        s.t_ultimo_tts,
                    )
                    cortes_audibles.append((fin - t_voz) * 1000)
                    descartados.append(piezas.salida.reproductor.ms_descartados)

        salida.append(
            Resultado(
                "barge_in",
                opcion,
                {
                    "detectados": detectados,
                    "de": repeticiones,
                    "corte_servidor_ms": mediana(cortes_servidor),
                    "silencio_audible_ms": mediana(cortes_audibles),
                    "audio_descartado_ms": mediana(descartados),
                },
            )
        )
    return salida


# ---------------------------------------------------------------------------
# Escenario 3 — calibración del fin de turno
# ---------------------------------------------------------------------------
def escenario_fin_de_turno() -> list[Resultado]:
    """Barrido del umbral de silencio contra dos riesgos opuestos.

    - Falso corte: el detector cierra el turno en una pausa *dentro* de la frase
      del paciente. Se prueba con `turno_dudoso` (pausas internas de 350, 500 y
      250 ms, insertadas con `[[slnc]]` de `say` para que la duración sea exacta
      y no una estimación) y `turno_pausa_larga` (una pausa de 700 ms, el caso
      que ningún umbral razonable puede salvar).
    - Retraso: cuánto tarda en cerrar cuando el paciente sí ha terminado.

    El umbral bueno es el más bajo que no produce ningún falso corte, porque
    cada 100 ms de umbral son 100 ms que el agente parece dormido.
    """
    clips = {
        n: cargar(n)
        for n in (
            "turno_largo",
            "turno_corto",
            "apendicectomia",
            "si_corto",
            "turno_dudoso",
            "turno_pausa_larga",
        )
    }
    filas = []
    for umbral in (240, 320, 400, 480, 560, 640, 800, 1000):
        params = ParametrosVAD(ms_silencio_fin_turno=umbral)
        falsos = 0
        culpables: list[str] = []
        retrasos = []
        for nombre, pcm in clips.items():
            largo = con_silencio(pcm, 2000)
            det = DetectorTurnos(params)
            eventos = det.procesar(largo)
            fines = [e for e in eventos if e.tipo.name == "DEJO_DE_HABLAR"]
            # Más de un fin de turno = el detector partió la intervención.
            if len(fines) > 1:
                falsos += len(fines) - 1
                culpables.append(nombre)
            if fines:
                retrasos.append(fines[-1].ms_decision - fines[-1].ms)
            else:
                falsos += 1
                culpables.append(f"{nombre}(sin cierre)")
        filas.append(
            {
                "umbral_ms": umbral,
                "falsos_cortes": falsos,
                "clips_rotos": ",".join(culpables) or "-",
                "retraso_medio_ms": mediana(retrasos),
            }
        )
    return [Resultado("fin_de_turno", "VAD", {"barrido": filas})]


# ---------------------------------------------------------------------------
# Escenario 4 — STT: el de Pipecat contra el nuestro
# ---------------------------------------------------------------------------
async def escenario_stt(repeticiones: int) -> list[Resultado]:
    """El criterio es el del README: latencia Y «apendicectomía» bien escrita."""
    from app.core.config import get_settings
    from app.voice.stt import WhisperSTT

    ruta = AUDIO / "apendicectomia.wav"
    modelo = get_settings().stt_model
    salida: list[Resultado] = []

    nuestro = WhisperSTT()
    nuestro.precalentar()
    ms, textos = [], []
    for _ in range(repeticiones):
        r = await nuestro.transcribir(ruta)
        ms.append(r.duracion_ms)
        textos.append(r.texto)
    salida.append(
        Resultado(
            "stt",
            "propio (small + prompt clínico)",
            {"ms": mediana(ms), "texto": textos[-1], "acierta": _acierta(textos[-1])},
        )
    )

    # El servicio de Pipecat, con el MISMO modelo, para aislar la única
    # diferencia real: que no admite `initial_prompt`.
    import mlx_whisper

    pcm, _ = sf.read(ruta, dtype="float32")
    ms, textos = [], []
    for _ in range(repeticiones):
        t0 = time.perf_counter()
        out = await asyncio.to_thread(
            mlx_whisper.transcribe,
            pcm,
            path_or_hf_repo=modelo,
            temperature=0.0,
            language="es",
        )
        ms.append((time.perf_counter() - t0) * 1000)
        textos.append(out["text"].strip())
    salida.append(
        Resultado(
            "stt",
            "WhisperSTTServiceMLX de Pipecat (sin initial_prompt)",
            {"ms": mediana(ms), "texto": textos[-1], "acierta": _acierta(textos[-1])},
        )
    )
    return salida


def _acierta(texto: str) -> bool:
    return "apendicectom" in texto.lower()


# ---------------------------------------------------------------------------
def imprimir(resultados: list[Resultado]) -> None:
    for r in resultados:
        print(f"\n[{r.escenario}] {r.opcion}")
        for k, v in r.datos.items():
            if isinstance(v, list):
                for fila in v:
                    print("   ", fila)
            else:
                print(f"    {k:24s} {v}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "escenario",
        choices=["turno", "barge-in", "fin-de-turno", "stt", "todo"],
        nargs="?",
        default="todo",
    )
    ap.add_argument("--tts", default="say", help="motor TTS local: say | kokoro | piper")
    ap.add_argument("-n", "--repeticiones", type=int, default=3)
    args = ap.parse_args()

    SALIDA.mkdir(exist_ok=True)
    resultados: list[Resultado] = []
    quiere = args.escenario

    if quiere in ("stt", "todo"):
        resultados += await escenario_stt(args.repeticiones)
    if quiere in ("fin-de-turno", "todo"):
        resultados += escenario_fin_de_turno()
    if quiere in ("turno", "todo"):
        resultados += await escenario_turno(args.tts, args.repeticiones)
    if quiere in ("barge-in", "todo"):
        resultados += await escenario_barge_in(args.tts, args.repeticiones)

    imprimir(resultados)
    destino = SALIDA / "voz_comparativa.json"
    destino.write_text(
        json.dumps([asdict(r) for r in resultados], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n→ {destino}")


if __name__ == "__main__":
    asyncio.run(main())
