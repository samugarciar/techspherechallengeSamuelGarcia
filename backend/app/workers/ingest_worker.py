"""Worker de ingesta: consume la cola de `jobs` y aprende documentos.

    cd backend && uv run python -m app.workers.ingest_worker

Corre en un proceso APARTE de la API, y no por escalabilidad — con este volumen
un hilo bastaría. Es por memoria y por latencia: embeber con bge-m3 ocupa la GPU
y bloquea un hilo durante segundos, y si eso viviera dentro del proceso de
FastAPI, subir un protocolo de cuarenta páginas metería un pico de latencia justo
en el pipeline de voz, con un paciente al teléfono. Separados, lo peor que puede
pasar es que la ingesta tarde un poco más.

El bucle es deliberadamente aburrido: reclamar, procesar, marcar, repetir. Toda
la corrección vive en `app/db/queue.py` (SKIP LOCKED, backoff, huérfanos) y en la
transacción de promoción de `app/rag/ingest.py`. Lo que el worker sí aporta es
apagarse bien: al recibir SIGTERM deja de reclamar y termina el documento que
tiene entre manos, para no dejar un documento a medio embeber que otro worker
tendría que rehacer desde cero.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import time

from app.db import queue
from app.db.pool import close_pool, open_pool, transaction
from app.rag import embeddings, ingest

log = logging.getLogger("ingesta")

# Sondeo en vez de LISTEN/NOTIFY. Un segundo es imperceptible al lado de los
# segundos que cuesta embeber, y NOTIFY obligaría a mantener una conexión
# dedicada y a resolver igualmente el sondeo de respaldo para los reintentos con
# backoff, que no disparan ninguna notificación.
ESPERA_VACIO_S = 1.0

# Cada cuánto se buscan documentos cuyo worker murió y ya nadie va a reintentar.
# Solo se hace con la cola vacía, así que nunca compite con trabajo real; y con
# este periodo un documento huérfano tarda como mucho ABANDONO + 30 s en dejar de
# fingir que sigue procesándose.
BARRIDO_HUERFANOS_S = 30.0


async def _procesar(job: queue.Job) -> None:
    t0 = time.perf_counter()
    log.info("job %s · documento %s · intento %s", job.id, job.document_id, job.intentos)

    try:
        resultado = await ingest.procesar_documento(job.document_id, job_id=job.id)
    except Exception as exc:  # noqa: BLE001 — un job no puede tumbar el worker
        log.exception("job %s reventó", job.id)
        await queue.fallar(job.id, f"{type(exc).__name__}: {exc}")
        return

    ms = (time.perf_counter() - t0) * 1000

    if resultado.ok:
        log.info("job %s · listo · %s fragmentos · %.0f ms", job.id, resultado.chunks, ms)
        await queue.completar(job.id)
    elif resultado.desaparecido:
        # El admin borró el documento mientras se procesaba. Reintentar sería
        # perseguir algo que ya no existe y acabaría pintando un error rojo en la
        # consola por un borrado que salió bien.
        log.info("job %s · documento borrado durante la ingesta; se descarta", job.id)
        await queue.completar(job.id)
    else:
        log.warning("job %s · falló: %s", job.id, resultado.error)
        await queue.fallar(job.id, resultado.error or "error desconocido")


async def _barrer_huerfanos() -> None:
    """Cierra lo que dejó atrás un worker muerto. Nunca puede tumbar el bucle."""
    try:
        for document_id in await ingest.sepultar_abandonados():
            log.warning("documento %s sepultado: su worker murió y no quedan intentos",
                        document_id)
    except Exception:  # noqa: BLE001 — el barrido es mantenimiento, no la faena
        log.exception("el barrido de huérfanos falló")


async def bucle(parar: asyncio.Event, worker: str) -> None:
    proximo_barrido = 0.0
    while not parar.is_set():
        async with transaction() as conn:
            job = await queue.tomar_trabajo(conn, worker=worker)

        if job is None:
            ahora = time.monotonic()
            if ahora >= proximo_barrido:
                proximo_barrido = ahora + BARRIDO_HUERFANOS_S
                await _barrer_huerfanos()

            # `wait_for` en vez de `sleep`: así una señal corta la espera al
            # instante en lugar de dejar el apagado colgado hasta un segundo.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(parar.wait(), timeout=ESPERA_VACIO_S)
            continue

        await _procesar(job)


async def main(concurrencia: int, precalentar: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Esta consola es la prueba visible de que el agente está aprendiendo, y a
    # nivel INFO httpx narra cada petición que sentence-transformers hace al Hub:
    # sesenta líneas de ruido por cada línea que importa.
    for ruidoso in ("httpx", "httpcore", "urllib3", "filelock", "sentence_transformers"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    await open_pool()
    parar = asyncio.Event()

    bucle_actual = asyncio.get_running_loop()
    for señal in (signal.SIGINT, signal.SIGTERM):
        bucle_actual.add_signal_handler(señal, parar.set)

    if precalentar:
        # Cargar bge-m3 aquí y no en el primer documento: si no, el primer
        # protocolo que suba el jurado tarda los segundos del modelo además de
        # los suyos, y en pantalla parece que la ingesta se ha colgado.
        log.info("cargando el modelo de embeddings…")
        t0 = time.perf_counter()
        await asyncio.to_thread(embeddings.precalentar)
        log.info("modelo listo en %.1f s", time.perf_counter() - t0)

    base = f"{os.uname().nodename}:{os.getpid()}"
    log.info("worker %s escuchando la cola (%s en paralelo)", base, concurrencia)

    try:
        await asyncio.gather(
            *(bucle(parar, f"{base}#{n}") for n in range(concurrencia))
        )
    finally:
        log.info("parando; cerrando el pool")
        await close_pool()


def cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--concurrencia",
        type=int,
        default=1,
        help="documentos en paralelo. Por defecto 1: bge-m3 satura la GPU y "
             "solaparlos no acelera, solo compite con el pipeline de voz",
    )
    ap.add_argument(
        "--sin-precalentar",
        action="store_true",
        help="no cargar el modelo al arrancar (útil en pruebas)",
    )
    args = ap.parse_args()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(max(1, args.concurrencia), not args.sin_precalentar))


if __name__ == "__main__":
    cli()
