"""Registro de trazas en la tabla `traces` de Postgres.

Sustituye a sistemas pesados como Langfuse, guardando latencias por etapa
(stt, retrieval, rerank, llm, tts) y metadatos de consumo sin penalizar el rendimiento.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from app.db.pool import connection

log = logging.getLogger("db.traces")


async def registrar_traza(
    span: str,
    duration_ms: int | float,
    call_id: UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Inserta una fila de traza en la base de datos de forma asíncrona."""
    try:
        async with connection() as conn:
            await conn.execute(
                """
                INSERT INTO traces (call_id, span, duration_ms, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    call_id,
                    span,
                    int(duration_ms),
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
    except Exception:
        log.warning("no se pudo registrar la traza %s", span, exc_info=True)
