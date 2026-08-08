"""Gestión de documentos: subir, listar, inspeccionar y olvidar.

Aquí vive la mitad visible del requisito central del enunciado. Subir devuelve
en seguida y deja el trabajo a la cola; borrar es inmediato y devuelve cuántos
vectores se llevó por delante, que es el número que la consola enseña cayendo a
cero. Todo lo que tarda —parsear, trocear, embeber— ocurre en el worker.

Por qué la subida no espera: un PDF de 40 páginas son varios segundos de Docling
más varios de bge-m3. Bloquear la petición haría que el navegador pareciera
colgado justo en la acción que el jurado va a ejecutar en directo. El estado
viaja por SSE (app/api/eventos.py) y el usuario ve avanzar el proceso.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse

from app.api import errores
from app.api.deps import exigir_admin
from app.core.config import get_settings
from app.db import queue
from app.db.pool import connection, transaction
from app.rag.ingest import olvidar_documento

router = APIRouter(tags=["documentos"], dependencies=[Depends(exigir_admin)])

# La extensión manda sobre el `Content-Type` que envía el navegador: para .md y
# .txt los navegadores mandan cualquier cosa (a menudo application/octet-stream),
# y el mime que se guarda aquí es el que app/rag/ingest.py usa para decidir si
# pasa por Docling o lee el texto directamente. Equivocarlo rompe la ingesta.
MIME_POR_EXTENSION = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
}

MAXIMO_BYTES = errores.TAMANO_MAXIMO_MB * 1024 * 1024
_BLOQUE = 1024 * 1024

# Estados en los que una subida previa del mismo contenido todavía está viva.
# 'failed' y 'superseded' no cuentan: volver a subir ese archivo es legítimo.
ESTADOS_EN_CURSO = ("uploaded", "parsing", "chunking", "embedding")

Estado = Literal[
    "uploaded", "parsing", "chunking", "embedding", "ready", "failed", "superseded"
]

_COLUMNAS = """
    d.id, d.filename, d.title, d.mime_type, d.size_bytes, d.sha256,
    d.status, d.error_message, d.chunks_count, d.embedded_count,
    d.pages, d.supersedes_id, d.created_at, d.updated_at
"""


def _iso(momento: datetime | None) -> str | None:
    """UTC con sufijo Z, como en el contrato. `isoformat()` a secas da +00:00."""
    if momento is None:
        return None
    return momento.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _documento(fila: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(fila["id"]),
        "filename": fila["filename"],
        "title": fila["title"],
        "mime_type": fila["mime_type"],
        "size_bytes": fila["size_bytes"],
        "sha256": fila["sha256"],
        "status": fila["status"],
        "error": fila["error_message"],
        "chunks_count": fila["chunks_count"],
        "embedded_count": fila["embedded_count"],
        "pages": fila.get("pages"),
        "supersedes_id": str(fila["supersedes_id"]) if fila["supersedes_id"] else None,
        "created_at": _iso(fila["created_at"]),
        "updated_at": _iso(fila["updated_at"]),
    }


async def _leer_documento(conn, document_id: UUID) -> dict[str, Any] | None:
    cur = await conn.execute(
        f"SELECT {_COLUMNAS} FROM documents d WHERE d.id = %s", (document_id,)
    )
    return await cur.fetchone()


# --- Listado -----------------------------------------------------------------


@router.get("")
async def listar(
    estado: Annotated[Estado | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Documentos ordenados por fecha de subida, del más reciente al más antiguo."""
    patron = f"%{q.strip()}%" if q and q.strip() else None

    async with connection() as conn:
        cur = await conn.execute(
            # count(*) OVER () trae el total sin paginar en la misma consulta.
            # La alternativa (un SELECT count(*) aparte) son dos viajes y una
            # ventana en la que el total y la página no se corresponden.
            f"""
            SELECT {_COLUMNAS}, count(*) OVER () AS total
            FROM documents d
            WHERE (%(estado)s::text IS NULL OR d.status = %(estado)s)
              AND (%(patron)s::text IS NULL
                   OR d.filename ILIKE %(patron)s
                   OR coalesce(d.title, '') ILIKE %(patron)s)
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"estado": estado, "patron": patron, "limit": limit, "offset": offset},
        )
        filas = await cur.fetchall()

    total = filas[0]["total"] if filas else 0
    return {"documentos": [_documento(f) for f in filas], "total": total}


# --- Subida ------------------------------------------------------------------


async def _leer_validando(archivo: UploadFile) -> bytearray:
    """Carga el archivo en memoria comprobando el tamaño mientras lo lee.

    Se valida ANTES de escribir en `storage/`: un archivo de 300 MB o de 0 bytes
    no debe llegar a tocar el disco del que luego hay que limpiarlo. El tope de
    25 MB acota lo que se sostiene en memoria, así que leerlo entero es seguro y
    permite calcular el sha256 sin un segundo paso por el fichero.
    """
    declarado = getattr(archivo, "size", None)
    if declarado is not None and declarado > MAXIMO_BYTES:
        # Atajo: el parser multipart ya sabe el tamaño, así que se rechaza sin
        # leer nada. Sin esto habría que consumir 25 MB para decir que no.
        raise errores.archivo_demasiado_grande()

    datos = bytearray()
    while bloque := await archivo.read(_BLOQUE):
        datos.extend(bloque)
        if len(datos) > MAXIMO_BYTES:
            raise errores.archivo_demasiado_grande()

    if not datos:
        raise errores.archivo_vacio()
    return datos


async def _version_anterior(conn, sha256: str) -> UUID | None:
    """Comprueba si este contenido ya está en el sistema y decide qué hacer.

    Dos casos distintos que el contrato trata de forma opuesta:

      - Hay una subida del mismo archivo todavía procesándose -> 409. Aceptarla
        crearía dos jobs compitiendo por el mismo contenido y, cuando el segundo
        promocionara, chocaría con el índice único parcial de `documents`.
      - Hay una versión ya 'ready' -> se acepta como versión nueva y se anota a
        quién sustituye. Es el camino de "resubir el protocolo corregido".
    """
    cur = await conn.execute(
        """
        SELECT id, status FROM documents
         WHERE sha256 = %s AND status = ANY(%s)
        """,
        (sha256, [*ESTADOS_EN_CURSO, "ready"]),
    )
    filas = await cur.fetchall()

    en_curso = next((f for f in filas if f["status"] != "ready"), None)
    if en_curso is not None:
        raise errores.documento_duplicado(en_curso["status"])

    anterior = next((f for f in filas if f["status"] == "ready"), None)
    return anterior["id"] if anterior else None


@router.post("", status_code=202)
async def subir(
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Guarda, registra y encola. Devuelve 202 sin esperar al procesamiento."""
    ajustes = get_settings()

    nombre = Path(file.filename or "").name
    extension = Path(nombre).suffix.lower()
    if extension not in MIME_POR_EXTENSION:
        raise errores.formato_no_soportado(extension)

    datos = await _leer_validando(file)
    sha256 = hashlib.sha256(datos).hexdigest()

    document_id = uuid4()
    # El nombre en disco es el uuid, no el que mandó el cliente: así no hay
    # colisiones entre subidas homónimas ni forma de escapar de storage/ con un
    # "../". El nombre original se conserva en la columna `filename`, que es la
    # que se muestra y la que se cita en voz.
    destino = (ajustes.storage_dir / f"{document_id}{extension}").resolve()

    escrito = False
    try:
        async with transaction() as conn:
            supersedes_id = await _version_anterior(conn, sha256)

            # Escribir en un hilo: 25 MB de write() bloquearían el bucle de
            # eventos y con él todos los flujos SSE abiertos.
            await asyncio.to_thread(destino.write_bytes, bytes(datos))
            escrito = True

            await conn.execute(
                """
                INSERT INTO documents (id, filename, mime_type, sha256, size_bytes,
                                       storage_path, title, supersedes_id, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'uploaded')
                """,
                (
                    document_id,
                    nombre,
                    MIME_POR_EXTENSION[extension],
                    sha256,
                    len(datos),
                    str(destino),
                    (title or "").strip() or None,
                    supersedes_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO document_events (document_id, filename, event, detail)
                VALUES (%s, %s, 'uploaded', %s)
                """,
                (document_id, nombre, f"{len(datos)} bytes"),
            )

            # Se le pasa ESTA conexión, no una suya: así el documento y su job
            # entran o no entran juntos. Sin `conn` la cola abriría transacción
            # propia y quedaría una ventana —corta, pero real— con el documento
            # en 'uploaded' y nadie que fuera a procesarlo.
            await queue.encolar(document_id, conn=conn)

            fila = await _leer_documento(conn, document_id)

    except Exception:
        # El archivo se escribe dentro de la transacción, así que un rollback lo
        # dejaría huérfano en disco. Se limpia aquí; si el unlink también falla,
        # queda un fichero inerte que ya no referencia ninguna fila.
        if escrito:
            destino.unlink(missing_ok=True)
        raise

    assert fila is not None
    return JSONResponse(status_code=202, content=_documento(fila))


# --- Detalle -----------------------------------------------------------------


@router.get("/{document_id}")
async def detalle(document_id: UUID) -> dict[str, Any]:
    """El documento más una muestra de lo que el agente aprendió de él.

    `chunks_preview` es la respuesta a "vale, dices que lo sabe: enséñamelo".
    Tres trozos con su encabezado bastan para que se vea que el troceado respetó
    las secciones del protocolo y no cortó a ciegas.
    """
    async with connection() as conn:
        fila = await _leer_documento(conn, document_id)
        if fila is None:
            raise errores.documento_no_encontrado(document_id)

        cur = await conn.execute(
            """
            SELECT ordinal, heading, page, left(content, 200) AS contenido
            FROM chunks WHERE document_id = %s
            ORDER BY ordinal LIMIT 3
            """,
            (document_id,),
        )
        trozos = await cur.fetchall()

    return {**_documento(fila), "chunks_preview": [dict(t) for t in trozos]}


# --- Olvido ------------------------------------------------------------------


@router.delete("/{document_id}")
async def borrar(document_id: UUID) -> dict[str, Any]:
    """Borrado real e inmediato: el documento y todos sus vectores.

    El trabajo lo hace `olvidar_documento()`, que se apoya en el ON DELETE
    CASCADE del schema. No hay índice que reconstruir ni caché que invalidar:
    `retrievable_chunks` deja de verlos en el mismo COMMIT.

    Se cuentan los chunks reales en vez de leer `documents.chunks_count` porque
    ese contador lo escribe el worker al promocionar y podría ir por detrás de la
    realidad si se borra a mitad de ingesta. El número que se devuelve sale en
    pantalla como prueba del olvido, así que tiene que ser el de verdad.
    """
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s", (document_id,)
        )
        fila = await cur.fetchone()
    eliminados = int(fila["n"]) if fila else 0

    if not await olvidar_documento(document_id):
        raise errores.documento_no_encontrado(document_id)

    return {"olvidado": True, "chunks_eliminados": eliminados}
