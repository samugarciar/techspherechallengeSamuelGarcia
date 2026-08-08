"""Utilidades para los tests que hablan con Postgres de verdad.

No es un conftest.py a propósito: ese fichero lo comparten todos los tests del
proyecto y hay más agentes escribiendo aquí a la vez. Como módulo, cada test
importa lo que necesita y nadie pisa a nadie.

Estos tests NO usan mocks. Toda la garantía de «subir = aprender / borrar =
olvidar» es una propiedad del schema —`ON DELETE CASCADE`, la vista
`retrievable_chunks`, el índice único parcial sobre sha256— y un doble de prueba
no tiene ninguna de esas cosas. Un test con mock de la base de datos aquí
demostraría que el código llama a las funciones que el propio test espera, que es
exactamente lo que no hace falta demostrar.

    cd backend && DATABASE_URL=postgresql://postop:postop@localhost:5433/postop_t1 uv run pytest
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.db.pool import close_pool, connection, open_pool

# Guardarraíl: `postop` es la base de la demo de Samuel y estos tests borran
# filas. Equivocarse de DATABASE_URL le costaría el corpus el día de la
# presentación, así que el test se niega a correr en vez de avisar.
BASE_PROHIBIDA = "/postop"


def _url() -> str:
    return os.environ.get("DATABASE_URL", "")


hay_postgres = pytest.mark.skipif(
    not _url(),
    reason="define DATABASE_URL (usa la base de pruebas, no la de la demo)",
)


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[None]:
    """Abre el pool para un test y lo cierra al terminar.

    Por test y no por sesión porque pytest-asyncio crea un bucle de eventos nuevo
    en cada test, y las conexiones de psycopg quedan atadas al bucle en el que se
    abrieron: un pool de sesión daría errores de bucle cerrado en el segundo test.
    """
    url = _url()
    if url.rstrip("/").endswith(BASE_PROHIBIDA):
        pytest.fail(
            "DATABASE_URL apunta a la base 'postop', que es la de la demo. "
            "Usa una base de pruebas: .../postop_t1"
        )

    await open_pool()
    try:
        yield
    finally:
        await close_pool()


async def limpiar_documentos(*ids) -> None:
    """Borra documentos de prueba y, por cascada, sus chunks."""
    async with connection() as conn:
        for document_id in ids:
            await conn.execute("DELETE FROM jobs WHERE payload->>'document_id' = %s",
                               (str(document_id),))
            await conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))


async def crear_documento(filename: str, storage_path, sha256: str,
                          mime: str = "application/pdf"):
    """Inserta la fila que normalmente crearía el endpoint de subida."""
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO documents (filename, mime_type, sha256, size_bytes, storage_path)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (filename, mime, sha256, os.path.getsize(storage_path), str(storage_path)),
        )
        return (await cur.fetchone())["id"]


async def contar_recuperables(document_id) -> int:
    """Fragmentos de un documento visibles para el RAG.

    Consulta `retrievable_chunks` y no `chunks` a propósito: es el único punto de
    lectura del retrieval, así que es lo que hay que comprobar. Contra la tabla
    `chunks` el test pasaría aunque el olvido no funcionara.
    """
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM retrievable_chunks WHERE document_id = %s",
            (document_id,),
        )
        return (await cur.fetchone())["n"]
