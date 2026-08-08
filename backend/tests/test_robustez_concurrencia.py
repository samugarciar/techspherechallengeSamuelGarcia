"""Dos cosas a la vez, y una de ellas muriéndose.

Un worker, una API y un administrador tocando el mismo documento al mismo tiempo.
Lo que se defiende aquí:

  - Dos subidas del MISMO contenido no pueden dejar dos versiones consultables ni
    reventar el índice único parcial `documents_active_sha_idx`.
  - Un borrado que cae dentro de la transacción de promoción se aplica igual y no
    deja fragmentos detrás.
  - Un worker muerto de golpe (SIGKILL, sin oportunidad de limpiar) no deja el
    documento colgado para siempre ni fragmentos huérfanos consultables.
  - Un documento largo no pierde su job por tardar más que el umbral de abandono.

Los tests que matan procesos usan un worker de verdad en un proceso aparte: un
SIGKILL simulado con un `raise` no prueba nada, porque lo que hay que demostrar es
justo lo que pasa cuando NO se ejecuta ningún `finally`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from app.core.config import get_settings
from app.db import queue
from app.db.pool import connection
from app.rag import embeddings, ingest
from tests.ayuda_db import (  # noqa: F401 — `pool` es una fixture
    contar_recuperables,
    crear_documento,
    hay_postgres,
    limpiar_documentos,
    pool,
)
from tests.test_parsing import PDF

pytestmark = [
    hay_postgres,
    pytest.mark.skipif(not PDF.exists(), reason="genera antes eval/corpus_prueba"),
]

BACKEND = Path(__file__).resolve().parents[1]

PROTOCOLO = """# Protocolo de alta

## Cuidado de la herida

Mantenga el apósito seco durante las primeras 48 horas. Puede ducharse a partir
de ese momento, secando la zona sin frotar y sin aplicar cremas ni povidona.

## Signos de alarma

Acuda a urgencias si presenta fiebre superior a 38.5 grados, si la herida supura
o si el dolor aumenta en vez de ceder a partir del tercer día.

## Medicación

Tome el analgésico pautado cada ocho horas durante los tres primeros días y
después solo si tiene dolor. No suspenda el antibiótico aunque se encuentre bien.
"""


def _vectores_falsos(semilla: int = 0):
    """bge-m3 sustituido: aquí se prueba la concurrencia, no la calidad."""
    async def _embeber(textos, batch_size=16):
        rng = np.random.default_rng(semilla)
        return rng.standard_normal((len(textos), 1024)).astype(np.float32)

    return _embeber


async def _sembrar_archivo(contenido: str, nombre: str) -> tuple:
    """Deja el archivo en storage y devuelve (ruta, sha) sin crear la fila."""
    destino = get_settings().storage_dir / f"{uuid4().hex}_{nombre}"
    destino.write_text(contenido, encoding="utf-8")
    return destino, ingest.sha256_archivo(destino)


# ---------------------------------------------------------------------------
# Mismo contenido, dos workers
# ---------------------------------------------------------------------------
async def test_dos_workers_promoviendo_el_mismo_contenido_no_se_pisan(pool,  # noqa: F811
                                                                     monkeypatch):
    """El caso que revienta `documents_active_sha_idx` si nadie lo ordena.

    Dos subidas del mismo archivo, procesadas a la vez. Ninguna ve a la otra
    porque ninguna ha hecho COMMIT, así que ambas se creen la primera versión y
    la segunda choca contra el índice único parcial. El síntoma en la consola es
    un documento en 'failed' con «duplicate key value violates unique constraint
    "documents_active_sha_idx"» — en inglés, y sobre un contenido que en realidad
    SÍ está aprendido, por la otra fila.

    Lo correcto es lo que describe el contrato: una queda 'ready' y la otra
    'superseded'. Nunca dos consultables, nunca una 'failed'.
    """
    monkeypatch.setattr(embeddings, "embeber_lote", _vectores_falsos())

    ruta, sha = await _sembrar_archivo(PROTOCOLO, "protocolo.md")
    primera = await crear_documento("protocolo.md", ruta, sha, mime="text/markdown")
    segunda = await crear_documento("protocolo.md", ruta, sha, mime="text/markdown")

    try:
        resultados = await asyncio.gather(
            ingest.procesar_documento(primera),
            ingest.procesar_documento(segunda),
        )
        assert all(r.ok for r in resultados), [r.error for r in resultados]

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT id, status FROM documents WHERE sha256 = %s", (sha,)
            )
            estados = {f["id"]: f["status"] for f in await cur.fetchall()}

        assert sorted(estados.values()) == ["ready", "superseded"], estados
        listo = next(i for i, e in estados.items() if e == "ready")
        retirado = next(i for i, e in estados.items() if e == "superseded")
        assert await contar_recuperables(listo) > 0
        assert await contar_recuperables(retirado) == 0, "dos versiones consultables a la vez"
    finally:
        await limpiar_documentos(primera, segunda)
        ruta.unlink(missing_ok=True)


async def test_los_dos_llegan_a_la_promocion_a_la_vez(pool, monkeypatch):  # noqa: F811
    """El mismo ataque, pero con la coincidencia forzada en vez de esperada.

    El test de arriba depende de que los dos pipelines se solapen por casualidad.
    Aquí una barrera los suelta en el mismo instante justo antes de la
    transacción de promoción, que es la sección crítica de verdad.
    """
    barrera = asyncio.Barrier(2)
    rng = np.random.default_rng(1)

    async def _embeber_al_unisono(textos, batch_size=16):
        vectores = rng.standard_normal((len(textos), 1024)).astype(np.float32)
        await barrera.wait()
        return vectores

    monkeypatch.setattr(embeddings, "embeber_lote", _embeber_al_unisono)

    # Un solo lote por documento: así la barrera se cruza una vez y los dos
    # entran a la promoción pegados.
    corto = "# Alta\n\n" + "Puede ducharse a partir de las 48 horas. " * 8
    ruta, sha = await _sembrar_archivo(corto, "corto.md")
    a = await crear_documento("corto.md", ruta, sha, mime="text/markdown")
    b = await crear_documento("corto.md", ruta, sha, mime="text/markdown")

    try:
        resultados = await asyncio.gather(
            ingest.procesar_documento(a), ingest.procesar_documento(b)
        )
        assert all(r.ok for r in resultados), [r.error for r in resultados]

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT status, count(*) AS n FROM documents WHERE sha256 = %s GROUP BY 1",
                (sha,),
            )
            por_estado = {f["status"]: f["n"] for f in await cur.fetchall()}
        assert por_estado == {"ready": 1, "superseded": 1}, por_estado
    finally:
        await limpiar_documentos(a, b)
        ruta.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Borrar mientras se promueve
# ---------------------------------------------------------------------------
async def test_borrar_dentro_de_la_promocion_no_deja_fragmentos(pool, monkeypatch):  # noqa: F811
    """El borrado más difícil de colar: dentro de la transacción que promueve.

    Se dispara justo después de que la promoción haya insertado los fragmentos y
    puesto el documento en 'ready', pero antes del COMMIT. El borrado se queda
    esperando el cerrojo de fila, entra en cuanto la promoción confirma, y se
    lleva el documento y sus vectores por CASCADE. Lo que no puede quedar es un
    documento borrado con fragmentos vivos, ni un 'ready' fantasma.
    """
    monkeypatch.setattr(embeddings, "embeber_lote", _vectores_falsos(2))

    ruta, sha = await _sembrar_archivo(PROTOCOLO, "a_borrar.md")
    document_id = await crear_documento("a_borrar.md", ruta, sha, mime="text/markdown")

    original = ingest._registrar
    borrado: list[asyncio.Task] = []

    async def _registrar_y_borrar(conn, doc_id, filename, evento, detalle=None):
        await original(conn, doc_id, filename, evento, detalle)
        if evento == "ready" and not borrado:
            # Sin await: el DELETE tiene que quedarse bloqueado en el cerrojo de
            # fila que esta misma transacción mantiene. Esperarlo aquí sería un
            # abrazo mortal, y esperar a que "no pase nada" es justo el escenario.
            borrado.append(asyncio.create_task(ingest.olvidar_documento(doc_id)))
            await asyncio.sleep(0.1)

    monkeypatch.setattr(ingest, "_registrar", _registrar_y_borrar)

    try:
        resultado = await ingest.procesar_documento(document_id)
        assert resultado.ok
        assert await borrado[0] is True

        async with connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM documents WHERE id = %s",
                                     (document_id,))
            assert (await cur.fetchone())["n"] == 0, "el borrado se perdió"
            cur = await conn.execute("SELECT count(*) AS n FROM chunks WHERE document_id = %s",
                                     (document_id,))
            assert (await cur.fetchone())["n"] == 0, "quedaron fragmentos de un documento borrado"
        assert await contar_recuperables(document_id) == 0
    finally:
        await limpiar_documentos(document_id)
        ruta.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Un worker que muere a media faena
# ---------------------------------------------------------------------------
async def _estado(document_id) -> str | None:
    async with connection() as conn:
        cur = await conn.execute("SELECT status FROM documents WHERE id = %s", (document_id,))
        fila = await cur.fetchone()
    return fila["status"] if fila else None


async def _esperar_estado(document_id, estados: set[str], limite_s: float) -> str:
    fin = time.monotonic() + limite_s
    visto = None
    while time.monotonic() < fin:
        visto = await _estado(document_id)
        if visto in estados:
            return visto
        await asyncio.sleep(0.2)
    raise AssertionError(f"el documento se quedó en '{visto}' sin llegar a {estados}")


async def test_un_worker_muerto_a_media_faena_no_deja_el_documento_colgado(pool,  # noqa: F811
                                                                          monkeypatch):
    """SIGKILL de verdad, no una excepción: sin `finally`, sin apagado ordenado.

    Tres cosas que comprobar, y la tercera es la que importa para la garantía:
      (a) el documento no se queda atascado para siempre,
      (b) pasado el umbral de abandono otro worker lo recupera y lo termina,
      (c) mientras tanto NO hay fragmentos huérfanos consultables — la promoción
          es atómica, así que un worker muerto a mitad no deja medio protocolo.
    """
    async with connection() as conn:
        await conn.execute("DELETE FROM jobs")

    destino = get_settings().storage_dir / f"{uuid4().hex}_protocolo.pdf"
    destino.write_bytes(PDF.read_bytes())
    document_id = await crear_documento("protocolo.pdf", destino,
                                        ingest.sha256_archivo(destino))
    job_id = await queue.encolar(document_id)

    entorno = {**os.environ, "PYTHONPATH": ".",
               "DATABASE_URL": os.environ["DATABASE_URL"]}
    worker = subprocess.Popen(
        [sys.executable, "-m", "app.workers.ingest_worker", "--sin-precalentar"],
        cwd=str(BACKEND), env=entorno,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Se le deja llegar hasta el troceado o el embebido y se le mata ahí.
        t0 = time.monotonic()
        alcanzado = await _esperar_estado(document_id, {"chunking", "embedding"}, 120)
        worker.send_signal(signal.SIGKILL)
        worker.wait(timeout=30)
        print(f"\nworker muerto con el documento en '{alcanzado}' "
              f"tras {time.monotonic() - t0:.1f} s")

        # (c) nada consultable a medias, ni siquiera un fragmento.
        assert await contar_recuperables(document_id) == 0
        async with connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM chunks WHERE document_id = %s",
                                     (document_id,))
            assert (await cur.fetchone())["n"] == 0, "quedaron fragmentos huérfanos"

            cur = await conn.execute(
                "SELECT status, attempts, locked_by FROM jobs WHERE id = %s", (job_id,)
            )
            job = await cur.fetchone()
        # Que el job lo reclamara EL PROCESO QUE SE MATÓ y no este test: sin esta
        # comprobación, un worker que muriera al arrancar dejaría el test en verde
        # sin haber ejercitado nada.
        assert str(worker.pid) in (job["locked_by"] or ""), (
            f"el job lo reclamó '{job['locked_by']}', no el worker que se mató"
        )
        assert job["status"] == "running", (
            "el job debería seguir reclamado por un worker que ya no existe"
        )
        assert job["attempts"] == 1

        # (a)+(b) pasado el abandono, otro worker lo reclama. Se baja el umbral
        # para no esperar cinco minutos: lo que se prueba es la regla, no el reloj.
        monkeypatch.setattr(queue, "ABANDONO", timedelta(seconds=1))
        await asyncio.sleep(1.2)

        async with connection() as conn, conn.transaction():
            recuperado = await queue.tomar_trabajo(conn, worker="relevo")
        assert recuperado is not None, "nadie puede recuperar el job de un worker muerto"
        assert recuperado.id == job_id
        assert recuperado.intentos == 2, "el intento del worker muerto tiene que contar"

        monkeypatch.setattr(embeddings, "embeber_lote", _vectores_falsos(3))
        resultado = await ingest.procesar_documento(document_id, job_id=recuperado.id)
        assert resultado.ok, resultado.error
        assert await _estado(document_id) == "ready"
        assert await contar_recuperables(document_id) > 0
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=30)
        await limpiar_documentos(document_id)
        destino.unlink(missing_ok=True)


async def test_un_documento_que_agota_los_intentos_deja_de_fingir_que_procesa(pool,  # noqa: F811
                                                                              monkeypatch):
    """El estado que la consola no sabría interpretar: 'embedding' para siempre.

    Los intentos se gastan AL RECLAMAR, así que tres muertes seguidas dejan el
    job en 'running' con `attempts >= max_attempts`: nadie puede volver a
    cogerlo. El documento se queda en 'embedding' sin error y sin final, y la
    consola lo pinta en ámbar («Generando embeddings») indefinidamente. El
    administrador espera algo que no va a pasar.
    """
    monkeypatch.setattr(queue, "ABANDONO", timedelta(seconds=0))

    ruta, sha = await _sembrar_archivo(PROTOCOLO, "abandonado.md")
    document_id = await crear_documento("abandonado.md", ruta, sha, mime="text/markdown")
    job_id = await queue.encolar(document_id)

    try:
        async with connection() as conn:
            await conn.execute(
                """
                UPDATE jobs SET status = 'running', attempts = max_attempts,
                       locked_at = now() - interval '10 minutes', locked_by = 'muerto'
                 WHERE id = %s
                """,
                (job_id,),
            )
            await conn.execute(
                "UPDATE documents SET status = 'embedding' WHERE id = %s", (document_id,)
            )

        async with connection() as conn, conn.transaction():
            assert await queue.tomar_trabajo(conn, worker="relevo") is None, (
                "el job no debería ser reclamable: es el supuesto del test"
            )

        sepultados = await ingest.sepultar_abandonados()
        assert document_id in sepultados

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT status, error_message FROM documents WHERE id = %s", (document_id,)
            )
            doc = await cur.fetchone()
            cur = await conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
            job = await cur.fetchone()

        assert doc["status"] == "failed"
        assert "interrumpió" in doc["error_message"]
        assert "vuelve a subir" in doc["error_message"].lower()
        assert job["status"] == "failed"

        # Idempotente: el barrido corre en cada vuelta del worker ocioso.
        assert await ingest.sepultar_abandonados() == []
    finally:
        await limpiar_documentos(document_id)
        ruta.unlink(missing_ok=True)


async def test_un_documento_largo_no_pierde_su_job_por_tardar(pool, monkeypatch):  # noqa: F811
    """Embeber es la única etapa cuya duración no está acotada.

    Con el latido solo en los cambios de etapa, un protocolo de doscientas
    páginas tarda más que el umbral de abandono y otro worker lo da por muerto
    mientras está trabajando: dos procesos embebiendo el mismo documento,
    compitiendo por la GPU y por la misma fila. Aquí el umbral se baja a un
    segundo y se hace que embeber tarde varios, que es la misma situación con el
    reloj cambiado de escala.
    """
    monkeypatch.setattr(queue, "ABANDONO", timedelta(seconds=1))

    async def _embeber_lento(textos, batch_size=16):
        await asyncio.sleep(0.6)
        return np.random.default_rng(4).standard_normal((len(textos), 1024)).astype(np.float32)

    monkeypatch.setattr(embeddings, "embeber_lote", _embeber_lento)

    # Suficientes fragmentos para varios lotes de 16 y para pasarse del umbral.
    largo = "".join(
        f"## Sección {i}\n\n{'Instrucción postoperatoria detallada. ' * 12}\n\n"
        for i in range(40)
    )
    ruta, sha = await _sembrar_archivo(f"# Protocolo largo\n\n{largo}", "largo.md")
    document_id = await crear_documento("largo.md", ruta, sha, mime="text/markdown")
    job_id = await queue.encolar(document_id)

    reclamado: list = []

    async def _vigilar() -> None:
        """Otro worker intentando robar el job mientras el primero embebe."""
        while True:
            await asyncio.sleep(0.3)
            async with connection() as conn, conn.transaction():
                job = await queue.tomar_trabajo(conn, worker="ladron")
            if job is not None:
                reclamado.append(job)
                return

    try:
        async with connection() as conn, conn.transaction():
            mio = await queue.tomar_trabajo(conn, worker="legitimo")
        assert mio is not None and mio.id == job_id

        vigilante = asyncio.create_task(_vigilar())
        resultado = await ingest.procesar_documento(document_id, job_id=job_id)
        vigilante.cancel()

        assert resultado.ok, resultado.error
        assert resultado.chunks > 16, "hacen falta varios lotes para que el test valga"
        assert not reclamado, (
            "otro worker se llevó el job mientras el primero seguía embebiendo"
        )
    finally:
        await limpiar_documentos(document_id)
        ruta.unlink(missing_ok=True)
