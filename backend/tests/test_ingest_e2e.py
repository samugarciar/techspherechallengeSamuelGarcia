"""La garantía del enunciado, de punta a punta y contra Postgres de verdad.

    subir -> procesar -> PREGUNTAR y encontrarlo -> borrar -> la misma pregunta
    devuelve CERO

La pregunta se hace con el pipeline de recuperación real (embedding con bge-m3 +
búsqueda híbrida sobre la vista `retrievable_chunks`), no contando filas de la
tabla `chunks`. Contar filas demostraría que el DELETE borró filas, que no es lo
que hay que demostrar: hay que demostrar que **el agente ya no puede encontrarlo**,
que es la afirmación que se hace en la demo.

Estos tests cargan bge-m3 de verdad, así que el primero tarda unos segundos.
"""

import shutil
import time
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.db import queue
from app.db.pool import connection
from app.rag import embeddings, ingest, retrieval
from tests.ayuda_db import (  # noqa: F401 — `pool` es una fixture
    contar_recuperables,
    crear_documento,
    hay_postgres,
    limpiar_documentos,
    pool,
)
from tests.test_parsing import CORPUS, PDF

pytestmark = [
    hay_postgres,
    pytest.mark.skipif(not PDF.exists(), reason="genera antes eval/corpus_prueba"),
]

PREGUNTA_FIEBRE = "¿qué hago si me sube la fiebre después de la operación?"


async def _preguntar(consulta: str, document_id) -> list:
    """Recupera como lo hace el agente y se queda con lo de ESTE documento."""
    vector = await embeddings.embeber_consulta(consulta)
    fragmentos = await retrieval.buscar(consulta, vector, top_k=8)
    return [f for f in fragmentos if f.document_id == str(document_id)]


async def _subir(nombre: str, origen) -> tuple:
    """Coloca el archivo en storage y crea la fila, como haría el endpoint."""
    destino = get_settings().storage_dir / f"{uuid4().hex}_{nombre}"
    shutil.copy(origen, destino)
    sha = ingest.sha256_archivo(destino)
    document_id = await crear_documento(nombre, destino, sha)
    return document_id, destino, sha


# ---------------------------------------------------------------------------
async def test_aprender_y_olvidar(pool):  # noqa: F811
    """El test que sostiene el requisito central del enunciado."""
    document_id, destino, _ = await _subir("protocolo_apendicectomia.pdf", PDF)
    try:
        # --- aprender ------------------------------------------------------
        t0 = time.perf_counter()
        resultado = await ingest.procesar_documento(document_id)
        ms = (time.perf_counter() - t0) * 1000
        assert resultado.ok, resultado.error
        assert resultado.chunks > 0
        print(f"\ningesta: {resultado.chunks} fragmentos en {ms:.0f} ms")

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT status, chunks_count, embedded_count, pages FROM documents "
                "WHERE id = %s",
                (document_id,),
            )
            doc = await cur.fetchone()
        assert doc["status"] == "ready"
        assert doc["chunks_count"] == doc["embedded_count"] == resultado.chunks
        assert doc["pages"] == 2

        # --- el agente lo encuentra ----------------------------------------
        encontrados = await _preguntar(PREGUNTA_FIEBRE, document_id)
        assert encontrados, "el documento se procesó pero el agente no lo recupera"

        # Y encuentra la REGLA, no un fragmento cualquiera: el umbral clínico
        # tiene que llegar entero y citado con su sección.
        alarma = next(
            (f for f in encontrados if "38.5" in f.content), None
        )
        assert alarma is not None, "no recupera la regla de fiebre"
        assert "fiebre superior a 38.5 grados" in alarma.content
        assert "Signos de alarma" in (alarma.heading or "")
        assert "p. " in alarma.cita()
        print(f"cita: {alarma.cita()}")

        # --- olvidar -------------------------------------------------------
        assert await ingest.olvidar_documento(document_id) is True

        despues = await _preguntar(PREGUNTA_FIEBRE, document_id)
        assert despues == [], f"tras borrarlo el agente sigue recuperando {len(despues)}"
        assert await contar_recuperables(document_id) == 0

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM chunks WHERE document_id = %s", (document_id,)
            )
            assert (await cur.fetchone())["n"] == 0, "el CASCADE no se llevó los chunks"

        assert not destino.exists(), "el archivo quedó en disco"

        # El rastro de auditoría SÍ sobrevive: document_events no tiene FK.
        async with connection() as conn:
            cur = await conn.execute(
                "SELECT event FROM document_events WHERE document_id = %s ORDER BY id",
                (document_id,),
            )
            eventos = [f["event"] for f in await cur.fetchall()]
        assert eventos[-1] == "deleted"
        assert "ready" in eventos
    finally:
        await limpiar_documentos(document_id)
        destino.unlink(missing_ok=True)


async def test_nunca_se_lee_medio_documento(pool):  # noqa: F811
    """Mientras se embebe, el documento es invisible para el RAG.

    Es la propiedad que hace que el agente no pueda responder con medio
    protocolo: los contadores de la consola avanzan, pero `retrievable_chunks`
    no devuelve nada hasta el COMMIT que lo pone en 'ready'.
    """
    document_id, destino, _ = await _subir("protocolo_herniorrafia.pdf",
                                           CORPUS / "protocolo_herniorrafia.pdf")
    try:
        async with connection() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'embedding' WHERE id = %s", (document_id,)
            )
        assert await contar_recuperables(document_id) == 0

        await ingest.procesar_documento(document_id)
        assert await contar_recuperables(document_id) > 0

        # Y basta con retirarlo del estado 'ready' para que deje de serlo, sin
        # borrar una sola fila: el olvido es inmediato y el borrado físico puede
        # ir después sin abrir ninguna ventana.
        async with connection() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'superseded' WHERE id = %s", (document_id,)
            )
        assert await contar_recuperables(document_id) == 0
    finally:
        await limpiar_documentos(document_id)
        destino.unlink(missing_ok=True)


async def test_resubir_el_mismo_archivo_reemplaza_la_version(pool):  # noqa: F811
    """Nunca coexisten dos versiones consultables del mismo contenido.

    Este camino fallaba antes: `documents_active_sha_idx` es UNIQUE sobre sha256
    con status='ready' y Postgres lo valida por sentencia, así que promover a
    'ready' ANTES de superseder a la anterior reventaba con violación de
    unicidad. El orden dentro de la transacción es lo que se comprueba aquí.
    """
    primera, ruta1, sha = await _subir("protocolo_colecistectomia.pdf",
                                       CORPUS / "protocolo_colecistectomia.pdf")
    segunda, ruta2, _ = await _subir("protocolo_colecistectomia.pdf",
                                     CORPUS / "protocolo_colecistectomia.pdf")
    try:
        assert (await ingest.procesar_documento(primera)).ok
        recuperables_v1 = await contar_recuperables(primera)
        assert recuperables_v1 > 0

        resultado = await ingest.procesar_documento(segunda)
        assert resultado.ok, f"la re-subida falló: {resultado.error}"

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT id, status, supersedes_id FROM documents WHERE sha256 = %s "
                "ORDER BY created_at",
                (sha,),
            )
            filas = await cur.fetchall()

        estados = {f["id"]: f["status"] for f in filas}
        assert estados[primera] == "superseded"
        assert estados[segunda] == "ready"
        assert await contar_recuperables(primera) == 0
        assert await contar_recuperables(segunda) == recuperables_v1

        nueva = next(f for f in filas if f["id"] == segunda)
        assert nueva["supersedes_id"] == primera
    finally:
        await limpiar_documentos(primera, segunda)
        ruta1.unlink(missing_ok=True)
        ruta2.unlink(missing_ok=True)


async def test_un_documento_ilegible_queda_en_failed(pool):  # noqa: F811
    """Fallar visiblemente, no aprender basura en silencio."""
    roto = get_settings().storage_dir / f"{uuid4().hex}_roto.pdf"
    roto.write_bytes(b"%PDF-1.4 esto no es un PDF")
    document_id = await crear_documento("roto.pdf", roto, "sha-roto")
    try:
        resultado = await ingest.procesar_documento(document_id)
        assert not resultado.ok

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT status, error_message FROM documents WHERE id = %s", (document_id,)
            )
            doc = await cur.fetchone()
        assert doc["status"] == "failed"
        assert doc["error_message"]
        assert await contar_recuperables(document_id) == 0
    finally:
        await limpiar_documentos(document_id)
        roto.unlink(missing_ok=True)


async def test_borrar_mientras_se_procesa_no_deja_un_error_falso(pool):  # noqa: F811
    """Si el admin borra el documento a media ingesta, eso no es un fallo.

    Sin este camino el worker reintentaría tres veces un documento que ya no
    existe y acabaría pintando un error rojo en la consola por un borrado que
    salió perfecto.
    """
    document_id, destino, _ = await _subir("protocolo_herniorrafia.pdf",
                                           CORPUS / "protocolo_herniorrafia.pdf")
    await limpiar_documentos(document_id)
    destino.unlink(missing_ok=True)

    resultado = await ingest.procesar_documento(document_id)
    assert not resultado.ok
    assert resultado.desaparecido is True


async def test_el_worker_recorre_la_cola_entera(pool):  # noqa: F811
    """Del job encolado al documento 'ready', pasando por el worker."""
    from app.workers.ingest_worker import _procesar

    document_id, destino, _ = await _subir("protocolo_apendicectomia.pdf", PDF)
    try:
        job_id = await queue.encolar(document_id)

        async with connection() as conn:
            async with conn.transaction():
                job = await queue.tomar_trabajo(conn, worker="test")
        assert job is not None and job.id == job_id

        await _procesar(job, "test")

        async with connection() as conn:
            cur = await conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
            assert (await cur.fetchone())["status"] == "done"
            cur = await conn.execute(
                "SELECT status FROM documents WHERE id = %s", (document_id,)
            )
            assert (await cur.fetchone())["status"] == "ready"

        assert await contar_recuperables(document_id) > 0
    finally:
        await limpiar_documentos(document_id)
        destino.unlink(missing_ok=True)


async def test_el_worker_marca_failed_lo_que_no_puede_leer(pool):  # noqa: F811
    from app.workers.ingest_worker import _procesar

    roto = get_settings().storage_dir / f"{uuid4().hex}_roto.docx"
    roto.write_bytes(b"tampoco soy un docx")
    document_id = await crear_documento("roto.docx", roto, "sha-roto-2")
    try:
        job_id = await queue.encolar(document_id)
        async with connection() as conn:
            async with conn.transaction():
                job = await queue.tomar_trabajo(conn, worker="test")

        await _procesar(job, "test")

        async with connection() as conn:
            cur = await conn.execute(
                "SELECT status, last_error FROM jobs WHERE id = %s", (job_id,)
            )
            estado = await cur.fetchone()
        # Primer fallo de tres: vuelve a la cola con backoff, no muere todavía.
        assert estado["status"] == "queued"
        assert estado["last_error"]
    finally:
        await limpiar_documentos(document_id)
        roto.unlink(missing_ok=True)
