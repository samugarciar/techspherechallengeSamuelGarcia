"""Ingesta de documentos: archivo -> markdown -> trozos -> vectores -> 'ready'.

La propiedad que importa: **el documento se vuelve visible para el agente en un
único instante**. Los trozos se insertan y el estado pasa a 'ready' dentro de la
misma transacción, así que no existe un momento intermedio en el que el agente
pueda leer medio documento y responder con información incompleta.

Si algo falla a mitad, la transacción revierte y el documento queda en 'failed'
con el error visible en la consola. Nunca a medias.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.core.config import get_settings
from app.db.pool import connection, transaction
from app.rag import embeddings
from app.rag.chunking import Trozo, texto_a_embeber, trocear


@dataclass(slots=True)
class ResultadoIngesta:
    document_id: UUID
    chunks: int
    ok: bool
    error: str | None = None


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


def _a_markdown(path: Path, mime: str) -> str:
    """Extrae markdown estructurado. Docling conserva la jerarquía de secciones,
    que es de lo que depende todo el troceado; PyMuPDF es el plan B."""
    if mime in ("text/markdown", "text/plain"):
        return path.read_text(encoding="utf-8", errors="replace")

    try:
        from docling.document_converter import DocumentConverter

        return DocumentConverter().convert(str(path)).document.export_to_markdown()
    except Exception:
        # Sin estructura de encabezados el troceado cae a un splitter por frases:
        # peor recuperación, pero el documento entra igual.
        import fitz

        with fitz.open(path) as doc:
            return "\n\n".join(pagina.get_text() for pagina in doc)


async def procesar_documento(document_id: UUID) -> ResultadoIngesta:
    """Pipeline completo para un documento ya registrado en estado 'uploaded'."""
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT filename, mime_type, storage_path FROM documents WHERE id = %s",
            (document_id,),
        )
        doc = await cur.fetchone()

    if doc is None:
        return ResultadoIngesta(document_id, 0, False, "documento inexistente")

    filename, mime, storage_path = doc["filename"], doc["mime_type"], doc["storage_path"]

    try:
        # --- parsing -------------------------------------------------------
        async with connection() as conn:
            await _marcar_estado(conn, document_id, filename, "parsing")

        markdown = _a_markdown(Path(storage_path), mime)
        if not markdown.strip():
            raise ValueError("el documento no contiene texto extraíble")

        # --- chunking ------------------------------------------------------
        async with connection() as conn:
            await _marcar_estado(conn, document_id, filename, "chunking")

        trozos: list[Trozo] = trocear(markdown)
        if not trozos:
            raise ValueError("el troceado no produjo ningún fragmento")

        # --- embedding -----------------------------------------------------
        async with connection() as conn:
            await _marcar_estado(conn, document_id, filename, "embedding")

        vectores = await embeddings.embeber_lote([texto_a_embeber(t) for t in trozos])

        # --- promoción atómica a 'ready' ------------------------------------
        # Todo lo de abajo ocurre en UNA transacción. Hasta que hace COMMIT, la
        # vista `retrievable_chunks` no devuelve nada de este documento porque
        # su status sigue siendo 'embedding'. El agente pasa de no conocerlo a
        # conocerlo entero, sin estados intermedios observables.
        async with transaction() as conn:
            await conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))

            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO chunks (document_id, ordinal, content, heading, page, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (document_id, t.ordinal, t.content, t.heading, t.page, v)
                        for t, v in zip(trozos, vectores, strict=True)
                    ],
                )

            await conn.execute(
                """
                UPDATE documents
                   SET status = 'ready', chunks_count = %s, embedded_count = %s,
                       error_message = NULL
                 WHERE id = %s
                """,
                (len(trozos), len(trozos), document_id),
            )
            await _registrar(conn, document_id, filename, "ready",
                             f"{len(trozos)} fragmentos")

            # Versionado: la subida anterior del mismo contenido deja de ser
            # recuperable en este mismo COMMIT. Nunca coexisten dos versiones.
            await conn.execute(
                """
                UPDATE documents SET status = 'superseded'
                 WHERE sha256 = (SELECT sha256 FROM documents WHERE id = %s)
                   AND id <> %s AND status = 'ready'
                """,
                (document_id, document_id),
            )

        return ResultadoIngesta(document_id, len(trozos), True)

    except Exception as exc:  # noqa: BLE001
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

        await _registrar(conn, document_id, doc["filename"], "deleted",
                         f"{doc['chunks_count']} fragmentos eliminados")
        await conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))

    # El archivo físico se borra fuera de la transacción: si esto falla queda un
    # huérfano en disco, que es inocuo — el agente ya no puede recuperarlo.
    try:
        ruta = Path(doc["storage_path"])
        if ruta.is_relative_to(settings.storage_dir.resolve()) and ruta.exists():
            ruta.unlink()
    except (OSError, ValueError):
        pass

    return True
