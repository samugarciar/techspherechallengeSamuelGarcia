"""Flujo SSE con el estado de los documentos.

Es lo que hace VISIBLE el aprendizaje: la consola ve avanzar los estados y subir
los contadores sin recargar, y ve desaparecer la fila cuando se borra. Como es
el momento demo del proyecto, aquí importa más la robustez que la elegancia.

── Sondeo y no LISTEN/NOTIFY ────────────────────────────────────────────────
Se evaluaron las dos. NOTIFY se descartó por dos razones concretas, no por
comodidad:

  1. Nadie emitiría el NOTIFY. Quien cambia `documents.status` es el worker de
     ingesta, no esta capa. Hacerlo funcionar exige o un trigger en
     `schema.sql` —fichero de otro agente, y las bases de datos ya están
     creadas, así que haría falta una migración— o llamadas a `pg_notify()`
     dentro de `app/rag/ingest.py`, que también es de otro. El sondeo no
     necesita que nadie coopere.
  2. LISTEN inmoviliza una conexión del pool durante toda la vida del flujo, y
     el pool es de 8 (app/db/pool.py, dimensionado así porque los 16 GB se
     comparten con Whisper, bge-m3 y el reranker). Nueve pestañas de la consola
     abiertas dejarían al agente sin conexiones en mitad de una llamada. El
     sondeo coge una conexión, consulta y la devuelve: un flujo abierto
     consume, en reposo, cero conexiones.

Lo que se pierde: dos cambios de estado separados por menos de 500 ms se
fusionan y solo se ve el segundo. Es aceptable porque el modo de fallo del
sondeo es el bueno — se comparan instantáneas, no se consume una cola, así que
puede perderse un estado intermedio pero NUNCA el último. El contador siempre
converge al valor real. En la práctica se ven todas las transiciones: parsear
con Docling y embeber con bge-m3 tardan segundos, no milisegundos.

── Reconexión ──────────────────────────────────────────────────────────────
Cada evento lleva `id` con la marca de tiempo del servidor. Si la conexión se
corta, `EventSource` reconecta solo y manda `Last-Event-ID`; entonces se emite
todo lo cambiado desde ese instante en vez de una instantánea completa. Los
borrados ocurridos durante el corte se recuperan de `document_events`, que no
tiene FK justo para sobrevivir al borrado de su documento.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from sse_starlette.event import JSONServerSentEvent, ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from app.api.deps import exigir_admin_por_query
from app.db.pool import connection

log = logging.getLogger("api.eventos")

router = APIRouter(tags=["documentos"])

INTERVALO_SONDEO_S = 0.5
LATIDO_S = 15
# Lo que se le sugiere al navegador que espere antes de reconectar. Por defecto
# EventSource usa 3 s; se fija explícitamente para no depender del navegador.
RECONEXION_MS = 2000

# Solo para diagnóstico: un flujo que no baja este contador al cerrarse es
# exactamente el fallo que agotaría el pool. Los tests lo comprueban.
_abiertos = 0


def flujos_abiertos() -> int:
    return _abiertos


_SQL_ESTADO = """
SELECT t.ahora,
       d.id, d.status, d.chunks_count, d.embedded_count, d.pages,
       d.error_message, d.updated_at
FROM (SELECT now() AS ahora) t
LEFT JOIN documents d ON true
"""


def _iso(momento: datetime) -> str:
    """Con microsegundos: dos cambios dentro del mismo segundo son frecuentes y
    truncar aquí haría que la reconexión se saltara uno."""
    return momento.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _instante(texto: str | None) -> datetime | None:
    if not texto:
        return None
    try:
        momento = datetime.fromisoformat(texto)
    except ValueError:
        # Un Last-Event-ID corrupto no debe tumbar la conexión: se degrada a
        # instantánea completa, que siempre es correcta.
        return None
    return momento if momento.tzinfo else momento.replace(tzinfo=UTC)


def _visible(fila: dict[str, Any]) -> tuple:
    """Los campos que el cliente ve. La comparación se hace sobre esto y no
    sobre `updated_at`: así un UPDATE que no cambia nada observable no genera
    un evento, y la consola no parpadea sin motivo.

    `pages` está aquí porque es el ÚNICO campo del `Documento` que cambia durante
    la ingesta y no se conoce al subir: lo escribe el worker al promover a
    `ready`, cuando el parser ya ha abierto el PDF. Sin él, la columna «Páginas»
    de la consola se quedaba en «—» para todo lo que se ingiriera en vivo hasta
    que alguien recargara. El resto de campos —`filename`, `size_bytes`,
    `sha256`, `title`— se fijan en el 202 de la subida y la consola ya los tiene,
    así que mandarlos en cada sondeo sería tráfico sin información.
    """
    return (
        fila["status"],
        fila["chunks_count"],
        fila["embedded_count"],
        fila["pages"],
        fila["error_message"],
    )


def _evento(document_id: str, estado: tuple, marca: str) -> ServerSentEvent:
    status, chunks, embebidos, paginas, error = estado
    # JSONServerSentEvent y no ServerSentEvent: éste último serializa con
    # str(dict), que produce comillas simples y `None` — repr de Python, no JSON.
    # El navegador haría JSON.parse y fallaría en cada evento.
    return JSONServerSentEvent(
        event="documento",
        id=marca,
        data={
            "id": document_id,
            "status": status,
            "chunks_count": chunks,
            "embedded_count": embebidos,
            "pages": paginas,
            "error": error,
        },
    )


async def _leer_estado() -> tuple[datetime, dict[str, tuple]]:
    """Instantánea del estado de todos los documentos, con el reloj del servidor.

    El `now()` viene de Postgres y no de Python porque es el mismo reloj que
    escribe `updated_at`; compararlos con relojes distintos abriría una ventana
    en la que la reconexión se salta cambios.
    """
    async with connection() as conn:
        cur = await conn.execute(_SQL_ESTADO)
        filas = await cur.fetchall()

    ahora = filas[0]["ahora"]
    return ahora, {str(f["id"]): _visible(f) for f in filas if f["id"] is not None}


async def _cambios_desde(desde: datetime) -> tuple[list[dict], list[str]]:
    """Qué pasó mientras el cliente estaba desconectado."""
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, status, chunks_count, embedded_count, pages, error_message
            FROM documents WHERE updated_at >= %s
            """,
            (desde,),
        )
        cambiados = await cur.fetchall()

        # Los borrados no se pueden leer de `documents` —la fila ya no está— y
        # por eso salen del registro de auditoría, que sobrevive al CASCADE.
        cur = await conn.execute(
            """
            SELECT DISTINCT document_id FROM document_events
             WHERE event = 'deleted' AND created_at >= %s
            """,
            (desde,),
        )
        borrados = await cur.fetchall()

    return list(cambiados), [str(f["document_id"]) for f in borrados]


async def _flujo(desde: datetime | None, cerrado: asyncio.Event, marca: dict) -> AsyncIterator:
    """Generador del flujo. Su `finally` es la parte que no puede fallar."""
    global _abiertos
    _abiertos += 1
    try:
        yield ServerSentEvent(comment="flujo de documentos abierto", retry=RECONEXION_MS)

        ahora, previo = await _leer_estado()
        marca["id"] = _iso(ahora)

        if desde is None:
            # Primera conexión: instantánea completa. Hace el flujo autosuficiente
            # —la consola podría pintar la tabla sin llamar a GET /api/documents— y
            # es idempotente si ya la había pedido.
            for document_id, estado in previo.items():
                yield _evento(document_id, estado, marca["id"])
        else:
            cambiados, borrados = await _cambios_desde(desde)
            for fila in cambiados:
                yield _evento(str(fila["id"]), _visible(fila), marca["id"])
            for document_id in borrados:
                yield JSONServerSentEvent(
                    event="eliminado", id=marca["id"], data={"id": document_id}
                )

        while not cerrado.is_set():
            await asyncio.sleep(INTERVALO_SONDEO_S)
            if cerrado.is_set():
                break

            try:
                ahora, actual = await _leer_estado()
            except Exception:
                # Un hipo de la base de datos no debe cerrar el flujo: el cliente
                # tendría que reconectar y el sondeo siguiente probablemente
                # funcione. Si el pool está cerrado (apagado del servidor) el
                # error se repite y la cancelación llega igualmente.
                log.warning("sondeo de documentos fallido", exc_info=True)
                continue

            marca["id"] = _iso(ahora)

            for document_id, estado in actual.items():
                if previo.get(document_id) != estado:
                    yield _evento(document_id, estado, marca["id"])

            for document_id in previo.keys() - actual.keys():
                yield JSONServerSentEvent(
                    event="eliminado", id=marca["id"], data={"id": document_id}
                )

            previo = actual
    finally:
        _abiertos -= 1


@router.get("/stream", dependencies=[Depends(exigir_admin_por_query)])
async def flujo_documentos(
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    desde: Annotated[str | None, Query()] = None,
) -> EventSourceResponse:
    """`text/event-stream` con los estados de los documentos en tiempo real.

    `desde` existe además de `Last-Event-ID` para poder reanudar a mano desde
    curl, que no manda esa cabecera. La cabecera tiene prioridad porque es la
    que pone el navegador.
    """
    cerrado = asyncio.Event()
    # Compartido entre el generador y el latido: así el latido también lleva la
    # marca de tiempo del último sondeo y, tras una reconexión, la ventana que
    # hay que recuperar es de 15 s como mucho en vez de "desde el último cambio".
    marca: dict[str, str | None] = {"id": None}

    async def _al_cerrar(_mensaje) -> None:
        cerrado.set()

    def _latido() -> ServerSentEvent:
        return JSONServerSentEvent(event="latido", id=marca["id"], data={})

    log.info("flujo SSE abierto desde %s", request.client.host if request.client else "?")

    return EventSourceResponse(
        _flujo(_instante(last_event_id or desde), cerrado, marca),
        ping=LATIDO_S,
        ping_message_factory=_latido,
        # sse_starlette cancela el generador al detectar http.disconnect, pero
        # además se avisa por este Event: si el generador despierta del sleep
        # antes de que llegue la cancelación, sale solo. Dos caminos hacia el
        # mismo sitio, porque una conexión SSE colgada retiene el objeto de
        # respuesta y, con el tiempo, conexiones del pool.
        client_close_handler_callable=_al_cerrar,
    )
