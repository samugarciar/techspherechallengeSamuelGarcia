"""Fixtures y arneses compartidos por los tests de la API.

No es `conftest.py` a propósito: este worktree lo comparten varios agentes y un
conftest en la raíz de `tests/` es el fichero con más papeletas de que dos
trabajos se pisen. Cada módulo de test importa de aquí lo que necesita.

La base de datos es la de verdad (`postop_t2`), no un doble: lo que se está
comprobando —CASCADE, el índice único parcial, la vista `retrievable_chunks`—
son propiedades de Postgres, y contra un mock no se prueba ninguna.

Las fixtures se declaran con `name=` y la función lleva guion bajo. Sin eso, un
test que importa `bd` y recibe `bd` como parámetro dispara F811 en ruff —el
parámetro tapa al import— y habría que sembrar 90 `# noqa`. Con `name=`, pytest
registra la fixture por el nombre que se le da y el módulo solo exporta `_bd`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db import queue
from app.db.pool import close_pool, connection, get_pool, open_pool
from app.main import app

# `jobs` entra en el TRUNCATE porque la subida encola dentro de su transacción:
# dejar jobs de un test anterior confundiría al worker si estuviera corriendo.
_TABLAS = "documents, document_events, jobs"


@pytest_asyncio.fixture(name="bd")
async def _bd():
    """Pool abierto y tablas de documentos limpias.

    El lifespan de la app NO se ejecuta: httpx.ASGITransport no lo dispara, y es
    justo lo que interesa — así los tests no cargan bge-m3 ni el reranker.
    """
    await open_pool()
    async with connection() as conn:
        await conn.execute(f"TRUNCATE {_TABLAS} RESTART IDENTITY CASCADE")

    almacen = get_settings().storage_dir
    previos = set(almacen.glob("*")) if almacen.exists() else set()
    try:
        yield
    finally:
        for sobrante in set(almacen.glob("*")) - previos:
            sobrante.unlink(missing_ok=True)
        await close_pool()


@pytest_asyncio.fixture(name="cliente")
async def _cliente(bd):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://pruebas"
    ) as c:
        yield c


@pytest.fixture(name="cabeceras")
def _cabeceras() -> dict[str, str]:
    return {"X-Admin-Token": get_settings().admin_token}


@pytest.fixture(name="cola")
def _cola(monkeypatch) -> list[tuple]:
    """Sustituye `encolar` y devuelve lo que se encoló.

    Los tests de la API comprueban que se llama a la cola con el documento
    correcto y con la conexión de la transacción, no lo que la cola hace
    después: eso es cosa del worker y de sus propios tests.
    """
    encolados: list[tuple] = []

    async def _encolar(document_id: UUID, tipo: str = "ingest", conn=None) -> int:
        assert conn is not None, "encolar debe ir en la transacción que crea el documento"
        encolados.append((document_id, tipo))
        return len(encolados)

    monkeypatch.setattr(queue, "encolar", _encolar)
    return encolados


def subida(nombre: str = "protocolo.md", contenido: bytes = b"# Alta\n\nTexto.") -> dict:
    return {"file": (nombre, contenido, "text/markdown")}


async def insertar_documento(
    *, filename: str = "sembrado.md", estado: str = "ready", sha256: str | None = None,
    chunks: int = 0, pages: int | None = None,
) -> UUID:
    """Crea un documento (y opcionalmente sus chunks) sin pasar por la API.

    Sirve para montar el estado que un worker habría dejado, que es lo que
    necesitan los tests de borrado, detalle y SSE.
    """
    document_id = uuid4()
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO documents (id, filename, mime_type, sha256, size_bytes,
                                   storage_path, status, chunks_count, embedded_count,
                                   pages)
            VALUES (%s, %s, 'text/markdown', %s, 42, %s, %s, %s, %s, %s)
            """,
            (
                document_id,
                filename,
                sha256 or uuid4().hex,
                f"/tmp/{document_id}.md",
                estado,
                chunks,
                chunks,
                pages,
            ),
        )
        for i in range(chunks):
            await conn.execute(
                """
                INSERT INTO chunks (document_id, ordinal, content, heading, page)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (document_id, i, f"Contenido del fragmento {i}. " * 4, f"Sección {i}", i + 1),
            )
    return document_id


async def estado_de(document_id: UUID) -> str | None:
    async with connection() as conn:
        cur = await conn.execute("SELECT status FROM documents WHERE id = %s", (document_id,))
        fila = await cur.fetchone()
    return fila["status"] if fila else None


def conexiones_en_uso() -> int:
    """Conexiones del pool que alguien tiene cogidas ahora mismo."""
    stats = get_pool().get_stats()
    return stats.get("pool_size", 0) - stats.get("pool_available", 0)


async def esperar_pool_libre(timeout: float = 5.0) -> None:
    """Espera a que el pool quede sin conexiones prestadas.

    Se comprueba así y no con un `assert` instantáneo porque los flujos SSE
    sondean cada 500 ms y una foto tomada al azar puede pillar uno a mitad de
    consulta. La propiedad que importa no es «nunca coge una conexión» sino «la
    devuelve siempre»: un flujo que se quedara con la conexión —que es lo que
    haría LISTEN— nunca dejaría llegar el contador a cero.
    """
    limite = asyncio.get_running_loop().time() + timeout
    while conexiones_en_uso() > 0:
        assert asyncio.get_running_loop().time() < limite, (
            f"{conexiones_en_uso()} conexiones retenidas tras {timeout}s"
        )
        await asyncio.sleep(0.05)


# --- Arnés SSE ---------------------------------------------------------------


class ClienteSSE:
    """Habla ASGI directamente con la app para poder cortar la conexión a mano.

    httpx.ASGITransport no sirve para esto: su `receive` solo devuelve
    `http.disconnect` DESPUÉS de que la respuesta termine, y una respuesta SSE no
    termina nunca. Con este arnés se decide cuándo llega la desconexión, que es
    exactamente lo que hay que probar.
    """

    def __init__(self, ruta: str, cabeceras: dict[str, str] | None = None) -> None:
        camino, _, consulta = ruta.partition("?")
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": camino,
            "raw_path": camino.encode(),
            "query_string": consulta.encode(),
            "root_path": "",
            "headers": [(b"host", b"pruebas")]
            + [(k.lower().encode(), v.encode()) for k, v in (cabeceras or {}).items()],
            "client": ("127.0.0.1", 51234),
            "server": ("pruebas", 80),
        }
        self._peticion_enviada = False
        self._desconectar = asyncio.Event()
        self._salida: asyncio.Queue = asyncio.Queue()
        self._buffer = b""
        self.tarea: asyncio.Task | None = None
        self.inicio: dict | None = None

    async def _receive(self) -> dict:
        if not self._peticion_enviada:
            self._peticion_enviada = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._desconectar.wait()
        return {"type": "http.disconnect"}

    async def _send(self, mensaje: dict) -> None:
        await self._salida.put(mensaje)

    async def __aenter__(self) -> ClienteSSE:
        self.tarea = asyncio.create_task(app(self._scope, self._receive, self._send))
        self.inicio = await asyncio.wait_for(self._salida.get(), timeout=5)
        return self

    async def __aexit__(self, *_) -> None:
        await self.cerrar()

    async def siguiente(self, timeout: float = 5.0) -> dict:
        """Siguiente evento SSE ya parseado, esperando lo que haga falta."""
        while True:
            if b"\n\n" in self._buffer:
                bruto, _, resto = self._buffer.partition(b"\n\n")
                self._buffer = resto
                evento = _parsear(bruto)
                if evento is not None:
                    return evento
                continue
            mensaje = await asyncio.wait_for(self._salida.get(), timeout=timeout)
            assert mensaje["type"] == "http.response.body", mensaje
            self._buffer += mensaje["body"].replace(b"\r\n", b"\n")

    async def esperar_evento(self, tipo: str, timeout: float = 5.0) -> dict:
        """Descarta latidos y comentarios hasta dar con el evento buscado."""
        limite = asyncio.get_running_loop().time() + timeout
        while True:
            restante = limite - asyncio.get_running_loop().time()
            evento = await self.siguiente(timeout=max(restante, 0.1))
            if evento["event"] == tipo:
                return evento

    async def cerrar(self, timeout: float = 5.0) -> None:
        self._desconectar.set()
        if self.tarea is not None:
            await asyncio.wait_for(self.tarea, timeout=timeout)


def _parsear(bruto: bytes) -> dict | None:
    """Un bloque SSE -> dict. Devuelve None si solo eran comentarios."""
    evento: dict = {"event": "message", "id": None, "data": None}
    util = False
    for linea in bruto.decode().splitlines():
        if not linea or linea.startswith(":"):
            continue
        campo, _, valor = linea.partition(":")
        valor = valor.lstrip()
        if campo == "event":
            evento["event"] = valor
            util = True
        elif campo == "id":
            evento["id"] = valor
        elif campo == "data":
            evento["data"] = json.loads(valor)
            util = True
    return evento if util else None


def borrar_del_almacen(ruta: str) -> None:
    Path(ruta).unlink(missing_ok=True)
