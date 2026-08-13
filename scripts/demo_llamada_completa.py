"""Una llamada de seguimiento entera, de punta a punta y sin micrófono.

    cd backend && uv run python ../scripts/demo_llamada_completa.py

Es el hermano de `demo_aprender_olvidar.py`: guion de demo y prueba de regresión
a la vez. Aquel demuestra el requisito del enunciado —subir = aprender, borrar =
olvidar—; éste demuestra que las tres capas construidas por separado se tocan.

Qué recorre, y por qué cada paso está aquí:

    1. POST /api/calls              la llamada existe en la base y tiene saludo
    2. WS /ws/voz?call_id=…         el cable que no existía: la voz sabe a qué
                                    llamada pertenece
    3. audio real por el cable      generado con `say`, en trozos de 20 ms y a
                                    ritmo real, como lo mandaría el navegador
    4. los siete mensajes           `listo` `estado` `transcripcion` `citas`
                                    `bandera_roja` `metricas` `fin`
    5. fiebre de 39,5               el agente corta el guion, da la indicación,
                                    confirma que se entendió y escala
    6. GET /api/calls/{id}          la conversación quedó escrita, con las citas
                                    de cada turno

Sin micrófono a propósito, y por el mismo motivo que la Fase 1 se probaba sin
voz: si la única forma de comprobar la integración fuera hablarle a un portátil,
nunca sabrías si falló la costura o falló el sonido.

Termina en verde o en rojo. Cada comprobación se imprime con su resultado, así
que cuando se ponga en rojo se ve *cuál* de las seis se rompió.

Qué hace falta antes de lanzarlo
--------------------------------
El backend con voz, apuntando a una base con la semilla y con algún protocolo ya
aprendido (si no, el agente no tiene nada que citar y el paso de `citas` falla,
que es exactamente lo que debe pasar):

    cd backend
    DATABASE_URL=postgresql://postop:postop@localhost:5433/postop_t2 \\
      VOZ=1 TTS_ENGINE_LOCAL=say uv run uvicorn app.main:app --port 8030

    # y en otra terminal, para que aprenda el protocolo:
    cd backend && DATABASE_URL=… uv run python -m app.workers.ingest_worker
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

RAIZ = Path(__file__).resolve().parents[1]

SAMPLE_RATE_ENTRADA = 16_000
"""Lo que esperan Silero y Whisper. `say` lo genera nativamente con
`--data-format=LEI16@16000`, así que no hay que remuestrear nada: el fallo más
desconcertante de este endpoint es mandarle otra frecuencia, porque no rompe
nada y simplemente hace que el agente «no te entienda»."""

MS_TROZO = 20
"""Lo que manda un `AudioWorklet` del navegador por mensaje."""

MS_COLA_DE_SILENCIO = 900
"""Silencio que se añade al final de cada intervención.

El umbral de fin de turno son 640 ms: sin esta cola, el VAD nunca daría por
cerrado lo que el paciente acaba de decir y la llamada se quedaría esperando."""

VOZ_SAY = "Monica"

# Las siete del contrato. `parar` no está: es del bucle de voz y solo aparece si
# alguien interrumpe, cosa que este guion no hace.
MENSAJES_DEL_CONTRATO = (
    "listo", "estado", "transcripcion", "citas", "bandera_roja", "metricas", "fin",
)


# ---------------------------------------------------------------------------
# El guion del paciente
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Intervencion:
    texto: str
    espera: str
    """Qué se está demostrando con esta frase. Se imprime, no es decorado."""


GUION = (
    Intervencion(
        "Sí, sí, soy yo. Mi cédula es uno cero tres cuatro cinco seis siete ocho nueve cero.",
        "verificación de identidad contra los datos vivos",
    ),
    Intervencion(
        "Pues mire, la herida la tengo un poco roja, y quería saber si ya me puedo duchar.",
        "consulta clínica: debe citar el protocolo",
    ),
    Intervencion(
        "Ah, y otra cosa. Me acabo de tomar la temperatura y tengo treinta y nueve y medio.",
        "BANDERA ROJA: el agente tiene que cortar el guion",
    ),
    Intervencion(
        "Sí, lo he entendido. Voy ahora mismo para urgencias.",
        "confirmación: solo aquí puede colgar (decisión 2)",
    ),
)


def cita_en_una_linea(cita: dict) -> str:
    """«protocolo.pdf › Cuidado de la herida · p. 3».

    Mismo formato en el paso 3 (lo que llega por el WebSocket) y en el paso 4 (lo
    que quedó escrito en `call_turns`), porque una de las seis comprobaciones es
    justamente que sean lo mismo: si se imprimen distinto, la vista compara mal.
    """
    pagina = f" · p. {cita['page']}" if cita.get("page") else ""
    return f"{cita['filename']} › {cita['heading']}{pagina}"


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def sintetizar(texto: str, destino: Path) -> bytes:
    """`say` → PCM int16 LE mono a 16 kHz, ya con su cola de silencio."""
    subprocess.run(
        ["say", "-v", VOZ_SAY, "-o", str(destino),
         f"--data-format=LEI16@{SAMPLE_RATE_ENTRADA}", texto],
        check=True, capture_output=True,
    )
    with wave.open(str(destino), "rb") as w:
        if w.getframerate() != SAMPLE_RATE_ENTRADA or w.getnchannels() != 1:
            raise SystemExit(
                f"`say` devolvió {w.getframerate()} Hz y {w.getnchannels()} canales; "
                f"se esperaban {SAMPLE_RATE_ENTRADA} Hz mono"
            )
        pcm = w.readframes(w.getnframes())
    silencio = b"\x00\x00" * int(SAMPLE_RATE_ENTRADA * MS_COLA_DE_SILENCIO / 1000)
    return pcm + silencio


class RelojDeReproduccion:
    """Cuánto le queda al agente por sonar en este cliente.

    Es el mismo modelo que `ReproductorSimulado` del pipeline, y hace falta por
    lo mismo: el servidor manda el audio tan rápido como lo sintetiza, así que
    cuando termina de mandarlo el agente todavía está hablando durante segundos.
    Sin esperar a que se agote, la primera frase del guion caería encima del
    saludo, el pipeline lo trataría —con razón— como una interrupción, y la demo
    parecería rota cuando lo que estaría es funcionando demasiado bien.
    """

    def __init__(self) -> None:
        self.sample_rate = 24_000
        self._suena_hasta = 0.0
        self.segundos_recibidos = 0.0

    def encolar(self, pcm16: bytes) -> None:
        segundos = (len(pcm16) // 2) / self.sample_rate
        self.segundos_recibidos += segundos
        self._suena_hasta = max(time.monotonic(), self._suena_hasta) + segundos

    async def esperar_silencio(self, margen_s: float = 0.35) -> None:
        restante = self._suena_hasta - time.monotonic()
        if restante > 0:
            await asyncio.sleep(restante + margen_s)


# ---------------------------------------------------------------------------
# Estado de la llamada, visto desde el cliente
# ---------------------------------------------------------------------------
@dataclass
class Escucha:
    reloj: RelojDeReproduccion
    tipos: set[str] = field(default_factory=set)
    citas: list[dict] = field(default_factory=list)
    bandera: dict | None = None
    fin: dict | None = None
    metricas: list[dict] = field(default_factory=list)
    dicho_por_el_agente: list[str] = field(default_factory=list)
    transcrito: list[str] = field(default_factory=list)
    hablando: asyncio.Event = field(default_factory=asyncio.Event)
    terminada: asyncio.Event = field(default_factory=asyncio.Event)

    def anotar(self, mensaje: dict) -> None:
        tipo = mensaje.get("tipo", "")
        self.tipos.add(tipo)

        match tipo:
            case "transcripcion":
                self.transcrito.append(mensaje["texto"])
                print(f"      paciente │ {mensaje['texto']}")
            case "citas":
                nuevas = [c for c in mensaje["citas"] if c not in self.citas]
                self.citas.extend(nuevas)
                for c in nuevas:
                    print(f"         cita │ {cita_en_una_linea(c)}")
            case "bandera_roja":
                self.bandera = mensaje
                print(f"\n      ⚑ BANDERA ROJA │ {mensaje['motivo']} "
                      f"({mensaje.get('urgencia', '?')})\n")
            case "metricas":
                self.metricas.append(mensaje["ms"])
            case "fin_audio":
                if texto := mensaje.get("texto"):
                    self.dicho_por_el_agente.append(texto)
                    print(f"        agente │ {texto}")
                self.hablando.set()
            case "fin":
                self.fin = mensaje
                self.terminada.set()
                self.hablando.set()


async def escuchar(ws, escucha: Escucha) -> None:
    """Consume el socket hasta que se cierre. Corre en su propia tarea porque el
    servidor habla mientras nosotros mandamos audio: leer solo entre frases
    dejaría el buffer creciendo y las latencias medidas serían mentira."""
    try:
        async for mensaje in ws:
            if isinstance(mensaje, bytes):
                escucha.reloj.encolar(mensaje)
                continue
            datos = json.loads(mensaje)
            if datos.get("tipo") == "listo":
                escucha.reloj.sample_rate = datos.get("sample_rate_salida", 24_000)
            escucha.anotar(datos)
    except ConnectionClosed:
        # Esperado: tras `fin` el servidor cierra él. No es un fallo, y por eso
        # se captura ESTA excepción y no `Exception` — un JSON mal formado o un
        # campo que falte tienen que salir a la luz, no confundirse con un cierre
        # normal.
        pass
    finally:
        escucha.terminada.set()
        escucha.hablando.set()


async def decir(ws, pcm: bytes) -> None:
    """Manda el audio en trozos de 20 ms y a ritmo real.

    A ritmo real y no de golpe porque el VAD cuenta muestras pero el barge-in y
    el fin de turno se miden en tiempo de pared: inyectar 4 segundos de audio en
    un milisegundo mediría un pipeline que no existe.
    """
    por_trozo = int(SAMPLE_RATE_ENTRADA * MS_TROZO / 1000) * 2
    siguiente = time.monotonic()
    for i in range(0, len(pcm), por_trozo):
        await ws.send(pcm[i : i + por_trozo])
        siguiente += MS_TROZO / 1000
        if (espera := siguiente - time.monotonic()) > 0:
            await asyncio.sleep(espera)


# ---------------------------------------------------------------------------
# Preparación
# ---------------------------------------------------------------------------
def elegir_paciente(pacientes: list[dict]) -> dict:
    """El operado más recientemente, que es a quien llamaría un servicio real."""
    con_cirugia = [p for p in pacientes if p.get("cirugia")]
    if not con_cirugia:
        raise SystemExit(
            "No hay pacientes con cirugía en esta base. ¿Le has pasado el seed?\n"
            "  docker exec -i postop_db psql -U postop -d postop_t2 "
            "< backend/app/db/seed.sql"
        )
    return min(con_cirugia, key=lambda p: p["cirugia"].get("dias_desde") or 999)


def asegurar_protocolos(c: httpx.Client, api: str, corpus: Path) -> bool:
    """Que el hospital tenga sus protocolos aprendidos. False si no se pudo.

    Se suben **los tres**, no solo el del paciente al que se va a llamar. Con uno
    solo, `buscar_protocolo` acertaría por no tener alternativa y la demo no
    demostraría nada sobre el retrieval: con los tres, recuperar la sección de
    signos de alarma del procedimiento correcto es un acierto de verdad. Es
    también lo que se parece a un hospital.

    Sin protocolos el agente no puede citar nada —y hace bien: el grounding
    obligatorio prefiere no responder a inventarse la respuesta— pero entonces
    esta demo no podría demostrar el mensaje `citas`. Se comprueba antes y se
    dice, en vez de terminar en rojo por una razón que no es la que se prueba.
    """
    pdfs = sorted(corpus.glob("protocolo_*.pdf"))
    if not pdfs:
        print(f"    no hay protocolos en {corpus}")
        return False

    documentos = c.get(f"{api}/documents").json().get("documentos", [])
    listos = {d["filename"] for d in documentos if d["status"] == "ready"}
    pendientes = [p for p in pdfs if p.name not in listos]

    if listos:
        print(f"    ya aprendidos: {', '.join(sorted(listos))}")
    for pdf in pendientes:
        print(f"    subiendo {pdf.name}")
        with open(pdf, "rb") as fh:
            c.post(f"{api}/documents", files={"file": (pdf.name, fh)}).raise_for_status()

    if not pendientes:
        return True

    t0 = time.time()
    while time.time() - t0 < 300:
        estados = {
            d["filename"]: d
            for d in c.get(f"{api}/documents").json().get("documentos", [])
        }
        if fallidos := [n for n, d in estados.items() if d["status"] == "failed"]:
            print(f"    FALLÓ la ingesta de {', '.join(fallidos)}")
            return False
        if all(estados.get(p.name, {}).get("status") == "ready" for p in pdfs):
            fragmentos = sum(estados[p.name]["embedded_count"] for p in pdfs)
            print(f"    {len(pdfs)} protocolos aprendidos en {time.time() - t0:.1f}s "
                  f"({fragmentos} fragmentos)")
            return True
        time.sleep(1.0)

    print("    los documentos no llegaron a 'ready'. ¿Corre el worker de ingesta?\n"
          "      cd backend && DATABASE_URL=… uv run python -m app.workers.ingest_worker")
    return False


# ---------------------------------------------------------------------------
# La llamada
# ---------------------------------------------------------------------------
async def llamar(base_ws: str, call_id: str, clips: list[bytes]) -> Escucha:
    url = f"{base_ws}/ws/voz?call_id={call_id}"
    print(f"\n2. CONECTAR   {url}")

    async with connect(url, max_queue=None) as ws:
        escucha = Escucha(reloj=RelojDeReproduccion())
        lector = asyncio.create_task(escuchar(ws, escucha))

        # El saludo del AI Act lo dice el servidor al conectar. Hay que dejarle
        # terminar: hablarle encima sería un barge-in, no un turno.
        print("\n3. HABLAR     (esperando al saludo del agente)")
        await asyncio.wait_for(escucha.hablando.wait(), timeout=60)
        await escucha.reloj.esperar_silencio()

        for intervencion, pcm in zip(GUION, clips, strict=True):
            if escucha.terminada.is_set():
                print("      (la llamada ya ha terminado; no se dice el resto del guion)")
                break
            print(f"\n    → {intervencion.espera}")
            escucha.hablando.clear()
            await decir(ws, pcm)
            try:
                await asyncio.wait_for(escucha.hablando.wait(), timeout=90)
            except TimeoutError:
                print("      (el agente no contestó en 90 s)")
                break
            await escucha.reloj.esperar_silencio()

        # Tras la escalada el servidor manda `fin`; se le da un momento para que
        # llegue si aún no está.
        if not escucha.terminada.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(escucha.terminada.wait(), timeout=10)

        lector.cancel()
    return escucha


# ---------------------------------------------------------------------------
# Veredicto
# ---------------------------------------------------------------------------
def comprobar(escucha: Escucha, detalle: dict) -> list[tuple[bool, str, str]]:
    """Las seis comprobaciones del encargo, cada una con lo que se observó."""
    faltan = [t for t in MENSAJES_DEL_CONTRATO if t not in escucha.tipos]
    turnos = detalle.get("turnos", [])
    del_agente = [t for t in turnos if t["quien"] == "agente"]
    del_paciente = [t for t in turnos if t["quien"] == "paciente"]
    con_citas = [t for t in del_agente if t.get("citas")]
    con_ms = [t for t in turnos if t.get("ms")]

    return [
        (
            not faltan,
            "llegan los siete mensajes del contrato",
            f"vistos {len(MENSAJES_DEL_CONTRATO) - len(faltan)}/7"
            + (f"; faltan {', '.join(faltan)}" if faltan else ""),
        ),
        (
            escucha.bandera is not None,
            "el detector de banderas rojas disparó",
            (escucha.bandera or {}).get("motivo", "no saltó"),
        ),
        (
            (escucha.fin or {}).get("motivo") == "escalada",
            "la llamada termina con fin/escalada",
            f"motivo={(escucha.fin or {}).get('motivo', 'no llegó `fin`')}",
        ),
        (
            detalle.get("escalada") is True and detalle.get("estado") == "completada",
            "la llamada se publica escalada y completada",
            (
                f"estado={detalle.get('estado')} escalada={detalle.get('escalada')} "
                f"urgencia={detalle.get('urgencia_escalada')}"
            ),
        ),
        (
            len(del_paciente) >= 2 and len(del_agente) >= 3,
            "la conversación quedó escrita en call_turns",
            (
                f"{len(turnos)} turnos: {len(del_agente)} del agente, "
                f"{len(del_paciente)} del paciente"
            ),
        ),
        (
            bool(con_citas) and bool(con_ms),
            "los turnos llevan sus citas y sus latencias",
            f"{len(con_citas)} turnos con cita, {len(con_ms)} con latencias",
        ),
    ]


async def principal() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    # 8030 y no 8000: el 8000 es el del README y suele estar ocupado por la
    # consola de administración mientras se prueba esto.
    p.add_argument("--api", default="http://localhost:8030")
    p.add_argument("--token", default="cambiar-esto-en-local")
    p.add_argument(
        "--corpus",
        default=str(RAIZ / "eval/corpus_prueba"),
        help="carpeta con los protocolos que se suben si la base no los tiene",
    )
    a = p.parse_args()
    api = f"{a.api.rstrip('/')}/api"
    base_ws = a.api.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")

    with httpx.Client(headers={"X-Admin-Token": a.token}, timeout=60.0) as c:
        print("0. PREPARAR")
        try:
            salud = c.get(f"{api}/health", timeout=5.0).json()
        except httpx.HTTPError:
            print(
                f"    No hay ninguna API escuchando en {api}.\n"
                f"    Levántala con voz:\n"
                f"      cd backend && DATABASE_URL=postgresql://postop:postop@"
                f"localhost:5433/postop_t2 \\\n"
                f"        VOZ=1 TTS_ENGINE_LOCAL=say uv run uvicorn app.main:app --port 8030"
            )
            return 1
        if not salud.get("modelos_listos"):
            print("    (aviso: bge-m3 y el reranker aún se cargan; el primer turno irá lento)")
        if not asegurar_protocolos(c, api, Path(a.corpus)):
            return 1

        pacientes = c.get(f"{api}/patients").json()["pacientes"]
        paciente = elegir_paciente(pacientes)

        print("\n1. ABRIR LA LLAMADA")
        r = c.post(f"{api}/calls", json={"patient_id": paciente["id"]})
        r.raise_for_status()
        llamada = r.json()
        call_id = llamada["call_id"]
        print(f"    {llamada['paciente']} — {llamada['cirugia']}, "
              f"día {llamada['dias_postop']} de postoperatorio")
        print(f"    call_id {call_id}")

        print("\n   SINTETIZANDO las frases del paciente con `say`…")
        with tempfile.TemporaryDirectory() as tmp:
            clips = [
                sintetizar(i.texto, Path(tmp) / f"{n}.wav")
                for n, i in enumerate(GUION)
            ]
        segundos = sum(len(pcm) // 2 for pcm in clips) / SAMPLE_RATE_ENTRADA
        print(f"    {len(clips)} intervenciones, {segundos:.1f}s de audio")

        try:
            escucha = await llamar(base_ws, call_id, clips)
        except OSError as causa:
            print(f"\n    No se pudo abrir /ws/voz: {causa}\n"
                  f"    ¿Está el backend arrancado con VOZ=1?")
            return 1

        print("\n4. LEER EL HISTORIAL")
        detalle = c.get(f"{api}/calls/{call_id}").json()
        for turno in detalle.get("turnos", []):
            citas = "".join(
                f"\n              ↳ {cita_en_una_linea(cita)}"
                for cita in turno.get("citas") or []
            )
            ms = turno.get("ms") or {}
            sello = f"  [{', '.join(f'{k} {v}ms' for k, v in ms.items())}]" if ms else ""
            print(f"    {turno['ordinal']:>2}. {turno['quien']:<8} "
                  f"{turno['texto'][:74]}{sello}{citas}")

    print("\n5. VEREDICTO")
    resultados = comprobar(escucha, detalle)
    for ok, titulo, observado in resultados:
        print(f"    {'✓' if ok else '✗'} {titulo}")
        print(f"        {observado}")

    if escucha.metricas:
        totales = [m.get("total", 0) for m in escucha.metricas]
        print(f"\n    latencia hasta el primer audio: "
              f"{min(totales)}–{max(totales)} ms en {len(totales)} turnos")

    todo_bien = all(ok for ok, _, _ in resultados)
    print("\n" + ("  ✓ LA COSTURA AGUANTA: voz, agente clínico e historial son la "
                  "misma llamada."
                  if todo_bien else
                  "  ✗ LA INTEGRACIÓN ESTÁ ROTA — mirar las cruces de arriba."))
    print(f"\n    La pantalla: {a.api.replace('8030', '5173')}/calls/{call_id}")
    return 0 if todo_bien else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
