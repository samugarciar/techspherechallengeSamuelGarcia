"""El flujo SSE: lo que hace visible que el agente aprende y olvida.

El test que más importa aquí no es el del camino feliz sino el del cierre. Un
generador SSE que no se entera de que el cliente se fue sigue sondeando para
siempre, retiene el objeto de respuesta y acaba comiéndose el pool de 8
conexiones; con la consola abierta en dos pestañas durante una demo, eso es una
llamada de voz que se queda sin base de datos a mitad.
"""

from __future__ import annotations

from app.api import eventos
from app.core.config import get_settings
from app.db.pool import connection
from tests.test_api_utiles import (  # noqa: F401 — pytest registra las fixtures
    ClienteSSE,
    _bd,
    _cabeceras,
    _cliente,
    conexiones_en_uso,
    esperar_pool_libre,
    insertar_documento,
)


def _ruta(**extra: str) -> str:
    partes = {"token": get_settings().admin_token, **extra}
    return "/api/documents/stream?" + "&".join(f"{k}={v}" for k, v in partes.items())


async def _cambiar_estado(document_id, estado: str, embebidos: int = 0) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE documents SET status = %s, embedded_count = %s WHERE id = %s",
            (estado, embebidos, document_id),
        )


async def _promover_con_paginas(document_id, paginas: int) -> None:
    """Lo que hace el worker al terminar: `ready`, contadores y número de páginas.

    `pages` solo lo conoce el parser, así que se escribe aquí y no antes (ver
    docs/CONTRATO_API.md §Cambios sobre el contrato, punto 1).
    """
    async with connection() as conn:
        await conn.execute(
            "UPDATE documents SET status = 'ready', chunks_count = %s, "
            "embedded_count = %s, pages = %s WHERE id = %s",
            (12, 12, paginas, document_id),
        )


async def test_instantanea_inicial(bd):
    document_id = await insertar_documento(estado="uploaded")

    async with ClienteSSE(_ruta()) as sse:
        assert sse.inicio["status"] == 200
        cabeceras_respuesta = {k.lower(): v for k, v in sse.inicio["headers"]}
        assert cabeceras_respuesta[b"content-type"].startswith(b"text/event-stream")

        evento = await sse.esperar_evento("documento")
        assert evento["data"] == {
            "id": str(document_id),
            "status": "uploaded",
            "chunks_count": 0,
            "embedded_count": 0,
            "pages": None,
            "error": None,
        }
        assert evento["id"], "cada evento lleva marca de tiempo para la reconexión"


async def test_se_ve_avanzar_el_estado_y_los_contadores(bd):
    """El momento demo: los contadores subiendo sin recargar la página."""
    document_id = await insertar_documento(estado="uploaded")

    async with ClienteSSE(_ruta()) as sse:
        await sse.esperar_evento("documento")

        await _cambiar_estado(document_id, "embedding", embebidos=11)
        evento = await sse.esperar_evento("documento")
        assert evento["data"]["status"] == "embedding"
        assert evento["data"]["embedded_count"] == 11

        await _cambiar_estado(document_id, "ready", embebidos=24)
        evento = await sse.esperar_evento("documento")
        assert evento["data"]["status"] == "ready"
        assert evento["data"]["embedded_count"] == 24


async def test_el_evento_trae_las_paginas(bd):
    """`pages` viajaba solo en `GET /api/documents`, nunca por el flujo.

    La consola pinta una columna «Páginas» y la rellena fusionando el evento
    sobre la fila que ya tiene (`useDocumentos.aplicarEvento` lee `evento.pages`).
    Como el evento no lo traía, todo PDF ingerido durante la demo se quedaba con
    un «—» en esa columna hasta que alguien pulsara «Recargar» — y solo se
    notaba con el jurado delante, porque el mock del frontend SÍ lo manda y con
    `VITE_MOCK=1` la columna se rellena sola.

    Es el único dato del `Documento` que cambia durante la ingesta y no estaba
    en el evento; el resto (`filename`, `size_bytes`, `sha256`) se fija al subir
    y la consola ya lo tiene desde el 202.
    """
    document_id = await insertar_documento(estado="chunking")

    async with ClienteSSE(_ruta()) as sse:
        primero = await sse.esperar_evento("documento")
        assert primero["data"]["pages"] is None

        await _promover_con_paginas(document_id, 6)

        evento = await sse.esperar_evento("documento")
        assert evento["data"]["status"] == "ready"
        assert evento["data"]["pages"] == 6


async def test_el_borrado_emite_eliminado(bd, cliente, cabeceras):
    document_id = await insertar_documento(estado="ready", chunks=3)

    async with ClienteSSE(_ruta()) as sse:
        await sse.esperar_evento("documento")

        await cliente.delete(f"/api/documents/{document_id}", headers=cabeceras)

        evento = await sse.esperar_evento("eliminado")
        assert evento["data"] == {"id": str(document_id)}


async def test_reconexion_recupera_lo_perdido(bd):
    """Con `Last-Event-ID` se reanuda en vez de repetir la instantánea entera.

    Se comprueba lo difícil: que un borrado ocurrido mientras el cliente estaba
    desconectado también se recupere. Sale de `document_events`, que no tiene FK
    justo para sobrevivir al CASCADE.
    """
    sobrevive = await insertar_documento(filename="queda.md", estado="uploaded")
    condenado = await insertar_documento(filename="se_va.md", estado="ready")

    async with ClienteSSE(_ruta()) as sse:
        primero = await sse.esperar_evento("documento")
        marca = primero["id"]

    # Desconectado: pasan cosas.
    await _cambiar_estado(sobrevive, "ready", embebidos=5)
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO document_events (document_id, filename, event)
            VALUES (%s, 'se_va.md', 'deleted')
            """,
            (condenado,),
        )
        await conn.execute("DELETE FROM documents WHERE id = %s", (condenado,))

    async with ClienteSSE(_ruta(), cabeceras={"Last-Event-ID": marca}) as sse:
        recuperados: dict[str, dict] = {}
        for _ in range(2):
            evento = await sse.siguiente()
            recuperados[evento["event"]] = evento["data"]

        assert recuperados["documento"]["id"] == str(sobrevive)
        assert recuperados["documento"]["status"] == "ready"
        assert recuperados["eliminado"] == {"id": str(condenado)}


async def test_el_cierre_del_cliente_libera_todo(bd):
    """Ni generadores vivos ni conexiones retenidas cuando el cliente se va."""
    await insertar_documento(estado="uploaded")

    sse = ClienteSSE(_ruta())
    await sse.__aenter__()
    await sse.esperar_evento("documento")
    assert eventos.flujos_abiertos() == 1

    # Un flujo abierto no debe costar una conexión del pool: coge una para
    # sondear y la devuelve. Es lo que descartó LISTEN/NOTIFY.
    await esperar_pool_libre()

    await sse.cerrar()

    assert eventos.flujos_abiertos() == 0, "el generador SSE quedó vivo tras la desconexión"
    assert conexiones_en_uso() == 0


async def test_varios_flujos_a_la_vez_no_agotan_el_pool(bd):
    """Cuatro pestañas de la consola abiertas: el pool tiene 8 conexiones."""
    await insertar_documento(estado="uploaded")

    flujos = [ClienteSSE(_ruta()) for _ in range(4)]
    for f in flujos:
        await f.__aenter__()
        await f.esperar_evento("documento")

    assert eventos.flujos_abiertos() == 4
    await esperar_pool_libre()

    for f in flujos:
        await f.cerrar()
    assert eventos.flujos_abiertos() == 0
    assert conexiones_en_uso() == 0


async def test_llega_el_latido(bd, monkeypatch):
    """El contrato promete un `latido` cada 15 s y nada lo comprobaba.

    Importa por el otro lado del cable: `frontend/src/api/flujo.ts` monta un
    vigilante que fuerza la reconexión si pasan 45 s sin recibir NADA. Un flujo
    correcto pero callado —que es lo normal: entre subida y subida no pasa nada—
    se reconectaría en bucle cada 45 s si el latido no saliera.

    `ping` se lee de `LATIDO_S` en cada petición, así que se puede acortar sin
    tocar el módulo. 15 s de espera real en la batería no los paga nadie.
    """
    await insertar_documento(estado="uploaded")
    monkeypatch.setattr(eventos, "LATIDO_S", 1)

    async with ClienteSSE(_ruta()) as sse:
        await sse.esperar_evento("documento")
        latido = await sse.esperar_evento("latido", timeout=6)
        assert latido["data"] == {}
        assert latido["id"], "el latido lleva marca de tiempo para acortar la reanudación"


async def test_marca_corrupta_degrada_a_instantanea(bd):
    """Un Last-Event-ID basura no puede tumbar la conexión."""
    document_id = await insertar_documento(estado="uploaded")

    async with ClienteSSE(_ruta(), cabeceras={"Last-Event-ID": "esto-no-es-una-fecha"}) as sse:
        evento = await sse.esperar_evento("documento")
        assert evento["data"]["id"] == str(document_id)


async def test_el_sondeo_sobrevive_a_un_fallo_de_la_base(bd, monkeypatch):
    """Un hipo de Postgres no debe cerrar el flujo: el cliente solo vería un
    corte y reconectaría, perdiendo la ventana de eventos del corte."""
    document_id = await insertar_documento(estado="uploaded")
    original = eventos._leer_estado
    fallos = {"restantes": 2}

    async def _inestable():
        if fallos["restantes"] > 0:
            fallos["restantes"] -= 1
            raise RuntimeError("conexión perdida")
        return await original()

    async with ClienteSSE(_ruta()) as sse:
        await sse.esperar_evento("documento")
        monkeypatch.setattr(eventos, "_leer_estado", _inestable)
        await _cambiar_estado(document_id, "ready", embebidos=4)

        evento = await sse.esperar_evento("documento", timeout=8)
        assert evento["data"]["status"] == "ready"
        assert fallos["restantes"] == 0, "los dos fallos debieron consumirse"
