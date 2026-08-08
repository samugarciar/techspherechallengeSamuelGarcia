"""Sonda de salud. Único endpoint sin autenticación.

Sirve para dos cosas distintas y conviene no confundirlas: `db` dice si el
backend puede hablar con Postgres, y `modelos_listos` dice si bge-m3 y el
reranker ya están cargados en memoria. Lo segundo no afecta a `ok` — el servidor
lista y borra documentos perfectamente mientras los modelos calientan; solo la
consulta RAG esperaría. Mezclarlas haría que un orquestador reiniciara el
proceso durante un arranque normal.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from app.db.pool import connection

router = APIRouter(tags=["salud"])

_modelos_listos = False


def marcar_modelos_listos() -> None:
    """La llama el lifespan cuando termina la precarga de modelos."""
    global _modelos_listos
    _modelos_listos = True


def _version() -> str:
    try:
        return version("postop-voice-agent")
    except PackageNotFoundError:
        return "0.1.0"


@router.get("/health")
async def salud() -> dict:
    # Nunca lanza: una sonda de salud que devuelve 500 no informa de nada que no
    # informara ya un `ok: false`, y complica la vida a quien la consume.
    try:
        async with connection() as conn:
            await conn.execute("SELECT 1")
        db = True
    except Exception:
        db = False

    return {"ok": db, "db": db, "version": _version(), "modelos_listos": _modelos_listos}
