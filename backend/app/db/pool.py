"""Pool de conexiones a Postgres.

Registra el adaptador de pgvector en cada conexión nueva, para poder pasar y
recibir `numpy.ndarray` directamente en las consultas sin serializar a mano.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings

_pool: AsyncConnectionPool | None = None


async def _configure(conn: AsyncConnection) -> None:
    await register_vector_async(conn)
    conn.row_factory = dict_row


async def open_pool() -> AsyncConnectionPool:
    """Abre el pool. Llamar una vez en el lifespan de FastAPI."""
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=get_settings().database_url,
            min_size=1,
            # 16 GB compartidos con los modelos: pool pequeño a propósito.
            max_size=8,
            configure=_configure,
            open=False,
        )
        await _pool.open(wait=True, timeout=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("El pool no está abierto. Llama a open_pool() primero.")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    """Conexión del pool en autocommit (lecturas y escrituras sueltas)."""
    async with get_pool().connection() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    """Conexión dentro de una transacción explícita.

    Úsalo para todo lo que deba ser atómico — en particular la promoción de un
    documento a 'ready': o se insertan TODOS sus chunks con sus vectores y el
    estado cambia, o no pasa nada. Nunca un documento a medio aprender.
    """
    async with get_pool().connection() as conn:
        async with conn.transaction():
            yield conn
