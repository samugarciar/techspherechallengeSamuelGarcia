"""Pruebas de trazabilidad RAG en la tabla `traces`.

Verifica que toda consulta RAG (tanto por API como por herramientas del agente)
registre un span 'retrieval' con metadatos completos y latencias.
"""

import pytest
from app.db.pool import connection
from app.db.traces import registrar_traza
from tests.test_api_utiles import _bd, _cabeceras, _cliente  # noqa: F401


@pytest.mark.asyncio
async def test_registrar_traza_directo(bd):
    """Valida la inserción asíncrona en la tabla traces."""
    await registrar_traza(
        span="retrieval",
        duration_ms=45,
        metadata={"consulta": "prueba de trazabilidad", "hay_evidencia": True},
    )

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT span, duration_ms, metadata FROM traces WHERE span = 'retrieval' ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["span"] == "retrieval"
        assert row["duration_ms"] == 45
        assert row["metadata"]["consulta"] == "prueba de trazabilidad"


@pytest.mark.asyncio
async def test_consulta_rag_genera_traza_en_db(cliente, cabeceras):
    """Valida que POST /api/rag/query registre una traza en la BD."""
    resp = await cliente.post(
        "/api/rag/query",
        json={"consulta": "¿cuándo me puedo duchar?"},
        headers=cabeceras,
    )
    assert resp.status_code == 200

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT span, metadata FROM traces WHERE span = 'retrieval' AND metadata->>'consulta' = '¿cuándo me puedo duchar?'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["metadata"]["hay_evidencia"] in (True, False)
