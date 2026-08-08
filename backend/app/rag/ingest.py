"""Ingesta de documentos: archivo -> markdown -> trozos -> vectores -> 'ready'.

La propiedad que importa: **el documento se vuelve visible para el agente en un
único instante**. Los trozos se insertan, la versión anterior se retira y el
estado pasa a 'ready' dentro de la misma transacción, así que no existe un
momento intermedio en el que el agente pueda leer medio documento y responder con
información incompleta.

Si algo falla a mitad, la transacción revierte y el documento queda en 'failed'
con el error visible en la consola. Nunca a medias.

El progreso sí es observable, y a propósito: `chunks_count` y `embedded_count` se
van escribiendo fuera de la transacción final mientras el lote se embebe. Son
contadores de UI, no de verdad recuperable — la vista `retrievable_chunks` sigue
sin devolver nada hasta el COMMIT. Que la consola muestre 11/24 mientras tanto es
la prueba visible de que el agente está aprendiendo.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np

from app.core.config import get_settings
from app.db import queue
from app.db.pool import connection, transaction
from app.rag import embeddings, parsing
from app.rag.chunking import Trozo, texto_a_embeber, trocear

# Tamaño del lote de embedding. No es solo rendimiento: es la resolución con la
# que avanza el contador de la consola. Lotes de 64 irían algo más rápido y
# darían un salto de 0 a 64, que se lee como un cuelgue.
LOTE_EMBEDDING = 16


@dataclass(slots=True)
class ResultadoIngesta:
    document_id: UUID
    chunks: int
    ok: bool
    error: str | None = None
    # El documento desapareció mientras se procesaba: el admin lo borró. No es un
    # fallo del pipeline y el worker no debe reintentarlo.
    desaparecido: bool = False


def sha256_archivo(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


async def _registrar(conn, document_id: UUID, filename: str, evento: str,
                     detalle: str | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO document_events (document_id, filename, event, detail)
        VALUES (%s, %s, %s, %s)
        """,
        (document_id, filename, evento, detalle),
    )


async def _marcar_estado(conn, document_id: UUID, filename: str, estado: str,
                         error: str | None = None) -> None:
    await conn.execute(
        "UPDATE documents SET status = %s, error_message = %s WHERE id = %s",
        (estado, error, document_id),
    )
    await _registrar(conn, document_id, filename, estado, error)


async def _existe(document_id: UUID) -> bool:
    async with connection() as conn:
        cur = await conn.execute("SELECT 1 FROM documents WHERE id = %s", (document_id,))
        return await cur.fetchone() is not None


def _paginar(doc: parsing.Documento, trozos: list[Trozo]) -> None:
    """Etiqueta cada trozo con la página del original donde empieza.

    Es lo que convierte la cita en «protocolo.pdf › Cuidado de la herida › p. 3»,
    que es lo que el contrato de API publica y lo que permite a un clínico ir a
    comprobar la frase. Se localiza el trozo por su prefijo en vez de por su
    texto completo porque el troceado reescribe el contenido: funde secciones
    cortas y arrastra solape entre piezas, así que la cadena exacta ya no está en
    el markdown. Aproximado a la baja y sin inventar: si el prefijo no aparece,
    el trozo se queda sin página en lugar de heredar una equivocada.
    """
    if not doc.saltos:
        return
    desde = 0
    for trozo in trozos:
        prefijo = trozo.content[:60].strip()
        pos = doc.markdown.find(prefijo, desde) if prefijo else -1
        if pos == -1:
            pos = doc.markdown.find(prefijo) if prefijo else -1
        if pos != -1:
            trozo.page = parsing.pagina_de(doc, pos)
            desde = pos


async def _embeber_con_progreso(document_id: UUID, trozos: list[Trozo]):
    """Embebe por lotes actualizando `embedded_count` entre uno y otro."""
    partes = []
    hechos = 0
    for inicio in range(0, len(trozos), LOTE_EMBEDDING):
        lote = trozos[inicio : inicio + LOTE_EMBEDDING]
        partes.append(await embeddings.embeber_lote([texto_a_embeber(t) for t in lote]))
        hechos += len(lote)

        async with connection() as conn:
            await conn.execute(
                "UPDATE documents SET embedded_count = %s WHERE id = %s",
                (hechos, document_id),
            )
    return np.vstack(partes)


async def procesar_documento(document_id: UUID, job_id: int | None = None) -> ResultadoIngesta:
    """Pipeline completo para un documento ya registrado en estado 'uploaded'.

    `job_id` sirve solo para dar latido a la cola en cada etapa: sin él, un
    documento grande tardaría más que el umbral de abandono y otro worker lo
    recogería en paralelo.
    """
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT filename, mime_type, storage_path FROM documents WHERE id = %s",
            (document_id,),
        )
        doc = await cur.fetchone()

    if doc is None:
        return ResultadoIngesta(document_id, 0, False, "documento inexistente",
                                desaparecido=True)

    filename, mime, storage_path = doc["filename"], doc["mime_type"], doc["storage_path"]

    async def _etapa(estado: str) -> None:
        if job_id is not None:
            await queue.latido(job_id)
        async with connection() as conn:
            await _marcar_estado(conn, document_id, filename, estado)

    try:
        # --- parsing -------------------------------------------------------
        await _etapa("parsing")
        analizado = parsing.parsear(Path(storage_path), mime)
        if not analizado.markdown.strip():
            raise ValueError("el documento no contiene texto extraíble")

        # --- chunking ------------------------------------------------------
        await _etapa("chunking")
        trozos: list[Trozo] = trocear(analizado.markdown)
        if not trozos:
            raise ValueError("el troceado no produjo ningún fragmento")
        _paginar(analizado, trozos)

        async with connection() as conn:
            await conn.execute(
                "UPDATE documents SET chunks_count = %s, embedded_count = 0, pages = %s "
                "WHERE id = %s",
                (len(trozos), analizado.paginas, document_id),
            )

        # --- embedding -----------------------------------------------------
        await _etapa("embedding")
        vectores = await _embeber_con_progreso(document_id, trozos)

        # --- promoción atómica a 'ready' ------------------------------------
        # Todo lo de abajo ocurre en UNA transacción. Hasta que hace COMMIT, la
        # vista `retrievable_chunks` no devuelve nada de este documento porque su
        # status sigue siendo 'embedding'. El agente pasa de no conocerlo a
        # conocerlo entero, sin estados intermedios observables.
        if job_id is not None:
            await queue.latido(job_id)

        async with transaction() as conn:
            # El orden NO es indiferente. `documents_active_sha_idx` es UNIQUE
            # sobre sha256 con status='ready', y Postgres lo valida por
            # sentencia, no al final de la transacción: promover primero y
            # superseder después reventaría con violación de unicidad en cuanto
            # se resubiera un archivo ya conocido — o sea, exactamente en el
            # camino de "nueva versión" que el contrato de API describe.
            cur = await conn.execute(
                """
                UPDATE documents SET status = 'superseded'
                 WHERE sha256 = (SELECT sha256 FROM documents WHERE id = %s)
                   AND id <> %s AND status = 'ready'
                RETURNING id, filename
                """,
                (document_id, document_id),
            )
            anteriores = await cur.fetchall()
            for previo in anteriores:
                await _registrar(conn, previo["id"], previo["filename"], "superseded",
                                 f"reemplazado por {document_id}")

            await conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))

            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO chunks
                        (document_id, ordinal, content, heading, page, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (document_id, t.ordinal, t.content, t.heading, t.page, v)
                        for t, v in zip(trozos, vectores, strict=True)
                    ],
                )

            cur = await conn.execute(
                """
                UPDATE documents
                   SET status = 'ready', chunks_count = %s, embedded_count = %s,
                       pages = %s, error_message = NULL,
                       supersedes_id = COALESCE(%s, supersedes_id)
                 WHERE id = %s
                """,
                (len(trozos), len(trozos), analizado.paginas,
                 anteriores[0]["id"] if anteriores else None, document_id),
            )
            if cur.rowcount == 0:
                # Lo borraron mientras se embebía. Se revierte todo: no tiene
                # sentido dejar chunks de un documento que ya no existe.
                raise LookupError("el documento se borró durante la ingesta")

            await _registrar(conn, document_id, filename, "ready",
                             f"{len(trozos)} fragmentos · {analizado.motor}")

        return ResultadoIngesta(document_id, len(trozos), True)

    except LookupError as exc:
        return ResultadoIngesta(document_id, 0, False, str(exc), desaparecido=True)

    except Exception as exc:  # noqa: BLE001 — cualquier fallo deja el doc en 'failed'
        if not await _existe(document_id):
            return ResultadoIngesta(document_id, 0, False, str(exc), desaparecido=True)
        async with connection() as conn:
            await _marcar_estado(conn, document_id, filename, "failed", str(exc)[:500])
        return ResultadoIngesta(document_id, 0, False, str(exc))


async def olvidar_documento(document_id: UUID) -> bool:
    """Borra un documento y, con él, todos sus vectores.

    El ON DELETE CASCADE del schema hace el trabajo: los chunks y sus embeddings
    desaparecen en la misma transacción que la fila del documento. No hay
    limpieza diferida, ni índice que reconstruir, ni caché que invalidar — el
    retrieval consulta `retrievable_chunks`, que deja de verlos al instante.

    El registro de auditoría sobrevive a propósito (document_events no tiene FK).
    """
    settings = get_settings()

    async with transaction() as conn:
        cur = await conn.execute(
            "SELECT filename, storage_path, chunks_count FROM documents WHERE id = %s",
            (document_id,),
        )
        doc = await cur.fetchone()
        if doc is None:
            return False

        # En la misma transacción que el borrado: si el job sobreviviera, el
        # worker lo recogería, no encontraría el documento y lo marcaría 'failed'.
        await queue.cancelar_de_documento(document_id, conn=conn)

        await _registrar(conn, document_id, doc["filename"], "deleted",
                         f"{doc['chunks_count']} fragmentos eliminados")
        await conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))

    # El archivo físico se borra fuera de la transacción: si esto falla queda un
    # huérfano en disco, que es inocuo — el agente ya no puede recuperarlo.
    try:
        ruta = Path(doc["storage_path"]).resolve()
        if ruta.is_relative_to(settings.storage_dir.resolve()) and ruta.exists():
            ruta.unlink()
    except (OSError, ValueError):
        pass

    return True
