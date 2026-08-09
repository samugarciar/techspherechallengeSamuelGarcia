"""Los bordes de la API: sin token, con un id que no lo es, y a lo bestia.

Lo que se defiende aquí no es el camino feliz de la consola sino el perímetro. En
particular tres cosas:

  - **Ninguna ruta se queda sin autenticar por descuido.** El test no enumera
    endpoints a mano: los saca del router, así que una ruta nueva que alguien
    añada sin la dependencia de admin aparece aquí sola. Es la única forma de que
    esta comprobación siga siendo cierta dentro de dos fases.
  - **Ningún error rompe la forma del contrato.** La consola muestra
    `error.mensaje` tal cual; una respuesta con otra forma pinta «undefined» en
    pantalla justo cuando algo va mal.
  - **Nada agota el pool.** Son 8 conexiones compartidas con el pipeline de voz.
    Doce pestañas de la consola abiertas no pueden dejar al agente sin base de
    datos en mitad de una llamada.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from starlette.routing import Route

from app.api import documentos
from app.core.config import get_settings
from app.db import queue
from app.db.pool import connection
from app.main import app
from tests.ayuda_db import hay_postgres
from tests.test_api_utiles import (  # noqa: F401 — pytest registra las fixtures
    ClienteSSE,
    _bd,
    _cabeceras,
    _cliente,
    _cola,
    conexiones_en_uso,
    esperar_pool_libre,
    insertar_documento,
    subida,
)

pytestmark = hay_postgres

# `/api/health` es pública a propósito: es la sonda de arranque y no revela nada.
SIN_TOKEN = {"/api/health"}


def _rutas_protegidas() -> list[tuple[str, str]]:
    """(método, ruta) de todo `/api/*` que debería exigir token.

    Enumerar esto cuesta más de lo que parece: esta versión de FastAPI no deja las
    rutas de un `include_router` colgando de `app.routes`, sino envueltas en un
    `_IncludedRouter` sin `.path` que expone las suyas por `effective_candidates()`
    ya con el prefijo aplicado. Recorrer solo `app.routes` devuelve una lista
    VACÍA, y una lista vacía en un `parametrize` no falla: pytest marca el test
    como saltado y el fichero se queda verde sin haber comprobado nada. De ahí el
    `assert` del final.
    """
    salida = []
    pendientes = list(app.routes)
    while pendientes:
        ruta = pendientes.pop()
        candidatos = getattr(ruta, "effective_candidates", None)
        if callable(candidatos):
            pendientes.extend(candidatos())
            continue

        camino = getattr(ruta, "path", None)
        metodos = getattr(ruta, "methods", None)
        if not camino or not metodos or not camino.startswith("/api"):
            continue
        if not isinstance(ruta, Route) and candidatos is not None:
            continue
        if camino in SIN_TOKEN:
            continue
        salida.extend((m, camino) for m in sorted(metodos - {"HEAD", "OPTIONS"}))

    assert salida, "no se encontró ninguna ruta que enumerar: el recorrido está roto"
    return sorted(set(salida))


def _concretar(ruta: str) -> str:
    return ruta.replace("{document_id}", str(uuid4()))


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("metodo,ruta", _rutas_protegidas())
async def test_ninguna_ruta_responde_sin_token(cliente, metodo, ruta):
    """Enumeradas desde el router: una ruta nueva sin auth cae aquí sola."""
    r = await cliente.request(metodo, _concretar(ruta), json={})
    assert r.status_code == 401, f"{metodo} {ruta} respondió {r.status_code} sin token"
    assert r.json()["error"]["codigo"] == "no_autorizado"


@pytest.mark.parametrize("metodo,ruta", _rutas_protegidas())
async def test_ninguna_ruta_acepta_un_token_equivocado(cliente, metodo, ruta):
    """Y con un token que se le parece tampoco: la comparación es exacta."""
    casi = get_settings().admin_token + "x"
    r = await cliente.request(
        metodo, _concretar(ruta), json={}, headers={"X-Admin-Token": casi}
    )
    assert r.status_code == 401, f"{metodo} {ruta} aceptó un token equivocado"


async def test_el_flujo_sse_tambien_exige_token(cliente):
    """El token va por query porque `EventSource` no admite cabeceras. Eso no lo
    rebaja: sin él o con uno falso, 401 y ni un byte de `text/event-stream`."""
    for consulta in ("", "?token=", "?token=equivocado"):
        r = await cliente.get(f"/api/documents/stream{consulta}")
        assert r.status_code == 401, f"el flujo se abrió con «{consulta}»"
        assert r.json()["error"]["codigo"] == "no_autorizado"
        assert "text/event-stream" not in r.headers.get("content-type", "")


async def test_la_cabecera_no_sirve_para_el_flujo_ni_la_query_para_el_resto(cliente, cabeceras):
    """Cada endpoint valida por su vía y solo por la suya.

    Aceptar las dos en todas partes sería más cómodo y dejaría el token de la
    consola en los logs de acceso de peticiones que no lo necesitan.
    """
    r = await cliente.get("/api/documents/stream", headers=cabeceras)
    assert r.status_code == 401, "el flujo aceptó la cabecera en vez de la query"

    r = await cliente.get(f"/api/documents?token={get_settings().admin_token}")
    assert r.status_code == 401, "el listado aceptó el token por query"


# ---------------------------------------------------------------------------
# Identificadores y formas de error
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "id_malo",
    [
        "no-soy-un-uuid",
        "123",
        "00000000-0000-0000-0000-00000000000",   # un dígito de menos
        "0000000%2D0000",                        # separador escapado
        "' OR 1=1 --",
    ],
)
async def test_un_id_que_no_es_uuid_da_un_error_con_la_forma_del_contrato(
    cliente, cabeceras, id_malo
):
    """Nada que no sea un uuid llega al handler, y el error tiene la forma buena.

    Importa por dos motivos distintos: la consola pinta `error.mensaje` tal cual,
    y el tipado a `UUID` es lo que hace que ninguna de estas cadenas llegue nunca
    a interpolarse en una consulta ni a construirse una ruta de disco.
    """
    for metodo in ("GET", "DELETE"):
        r = await cliente.request(metodo, f"/api/documents/{id_malo}", headers=cabeceras)
        assert r.status_code == 422, f"{metodo} {id_malo} -> {r.status_code}"
        error = r.json()["error"]
        assert error["codigo"] == "peticion_invalida"
        assert error["mensaje"], "el mensaje llega vacío a la consola"


@pytest.mark.parametrize(
    "ruta",
    [
        "/api/documentos",                        # errata de nombre
        "/api/documents/../../etc/passwd",        # travesía; el cliente la normaliza
        "/api/documents/x/y/z",
    ],
)
async def test_una_ruta_inexistente_tambien_respeta_el_contrato(cliente, cabeceras, ruta):
    """Los 404 y 405 los genera Starlette, no nuestro código.

    Sin el manejador de `HTTPException` saldrían como `{"detail":"Not Found"}` y
    la consola pintaría «undefined» justo cuando el frontend ha llamado a una URL
    que no existe y necesita ver por qué.
    """
    r = await cliente.get(ruta, headers=cabeceras)
    assert r.status_code == 404, ruta
    assert set(r.json()["error"]) == {"codigo", "mensaje"}
    assert r.json()["error"]["mensaje"]


# ---------------------------------------------------------------------------
# Tamaño
# ---------------------------------------------------------------------------
async def test_el_limite_de_tamano_se_aplica_al_byte(cliente, cabeceras, cola):
    """25 MB entran; 25 MB y un byte, no.

    Se prueban los dos lados del corte porque un `>=` mal puesto rechazaría
    archivos legítimos y no se notaría hasta que alguien subiera el protocolo
    grande de verdad.
    """
    justo = b"# Protocolo\n\n" + b"a" * (documentos.MAXIMO_BYTES - 13)
    assert len(justo) == documentos.MAXIMO_BYTES

    r = await cliente.post("/api/documents", files=subida("justo.md", justo), headers=cabeceras)
    assert r.status_code == 202, r.text

    r = await cliente.post(
        "/api/documents", files=subida("pasado.md", justo + b"a"), headers=cabeceras
    )
    assert r.status_code == 413
    assert r.json()["error"]["codigo"] == "archivo_demasiado_grande"
    assert len(cola) == 1, "solo el que cabía debía encolarse"


# ---------------------------------------------------------------------------
# Borrados simultáneos
# ---------------------------------------------------------------------------
async def test_dos_borrados_del_mismo_id_solo_olvidan_una_vez(cliente, cabeceras, monkeypatch):
    """Doble clic en la consola, o el frontend reintentando.

    Sin cerrojo los dos leen la fila, los dos la borran —el segundo sin afectar a
    nada—, los dos responden «olvidado: true» y la auditoría registra DOS
    borrados del mismo documento. En un expediente clínico el rastro tiene que
    decir lo que pasó: un borrado.

    El solape se fuerza con una pausa DENTRO de la transacción, justo detrás del
    SELECT. Sin ella los dos borrados se serializan por casualidad —el primero
    termina antes de que el segundo empiece— y el test pasa igual con el cerrojo
    quitado, que es como decir que no prueba nada. Se descartó una barrera de
    asyncio: con el cerrojo puesto el segundo se queda bloqueado en el SELECT y
    nunca llegaría a la barrera, así que el propio test se abrazaría.
    """
    document_id = await insertar_documento(estado="ready", chunks=4)

    original = queue.cancelar_de_documento

    async def _cancelar_despacio(doc_id, conn=None):
        await asyncio.sleep(0.3)
        return await original(doc_id, conn=conn)

    monkeypatch.setattr(queue, "cancelar_de_documento", _cancelar_despacio)

    respuestas = await asyncio.gather(
        cliente.delete(f"/api/documents/{document_id}", headers=cabeceras),
        cliente.delete(f"/api/documents/{document_id}", headers=cabeceras),
    )
    codigos = sorted(r.status_code for r in respuestas)
    assert codigos == [200, 404], codigos

    exito = next(r for r in respuestas if r.status_code == 200).json()
    assert exito == {"olvidado": True, "chunks_eliminados": 4}

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM document_events "
            " WHERE document_id = %s AND event = 'deleted'",
            (document_id,),
        )
        assert (await cur.fetchone())["n"] == 1, "la auditoría registró dos borrados"


async def test_dos_subidas_simultaneas_del_mismo_archivo_no_crean_dos_documentos(
    cliente, cabeceras, cola, monkeypatch
):
    """El 409 del contrato tiene que valer también cuando las dos llegan a la vez.

    La comprobación de duplicado lee `documents` y luego inserta, y entre las dos
    cosas hay un `await`: dos peticiones simultáneas no se ven la una a la otra y
    ambas se aceptan. El resultado no es catastrófico —el cerrojo por contenido de
    la promoción las ordena y acaban 'ready' + 'superseded'— pero sí es un
    documento de más en la consola, embebido para nada, y marcado como reemplazado
    antes de haber existido.

    Igual que en los borrados simultáneos, el solape se fuerza: se ensancha la
    ventana entre la comprobación y el INSERT. Sin ensancharla las dos subidas se
    ordenan por casualidad y el test pasa aunque no haya nada que lo garantice.
    """
    original = documentos._version_anterior

    async def _comprobar_y_esperar(conn, sha256):
        anterior = await original(conn, sha256)
        await asyncio.sleep(0.3)
        return anterior

    monkeypatch.setattr(documentos, "_version_anterior", _comprobar_y_esperar)

    contenido = b"# Cuidado de la herida\n\nMantenerla seca 48 horas."
    respuestas = await asyncio.gather(
        cliente.post("/api/documents", files=subida("herida.md", contenido), headers=cabeceras),
        cliente.post("/api/documents", files=subida("herida.md", contenido), headers=cabeceras),
    )
    codigos = sorted(r.status_code for r in respuestas)
    assert codigos == [202, 409], f"se aceptaron dos subidas del mismo contenido: {codigos}"

    error = next(r for r in respuestas if r.status_code == 409).json()["error"]
    assert error["codigo"] == "documento_duplicado"

    async with connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM documents")
        assert (await cur.fetchone())["n"] == 1
    assert len(cola) == 1


# ---------------------------------------------------------------------------
# Muchos flujos SSE a la vez
# ---------------------------------------------------------------------------
async def test_doce_flujos_abiertos_no_dejan_sin_base_de_datos_al_agente(bd, cliente, cabeceras):
    """Más pestañas que conexiones tiene el pool (8).

    Es el motivo por el que el flujo sondea en vez de usar LISTEN/NOTIFY: LISTEN
    inmoviliza una conexión por flujo y la novena pestaña dejaría al pipeline de
    voz sin base de datos en mitad de una llamada. Aquí se comprueba lo que esa
    decisión compra: doce flujos abiertos, y el resto de la API sigue
    respondiendo.
    """
    await insertar_documento(estado="uploaded")

    flujos = [ClienteSSE(f"/api/documents/stream?token={get_settings().admin_token}")
              for _ in range(12)]
    try:
        for f in flujos:
            await f.__aenter__()
            await f.esperar_evento("documento")

        r = await cliente.get("/api/documents", headers=cabeceras)
        assert r.status_code == 200, "con doce flujos abiertos, el listado dejó de responder"

        # Y en reposo no retienen ninguna: cogen una para sondear y la devuelven.
        await esperar_pool_libre(timeout=10)
    finally:
        for f in flujos:
            await f.cerrar()

    assert conexiones_en_uso() == 0


async def test_un_flujo_cortado_a_lo_bruto_devuelve_su_conexion(bd):
    """Cerrar el navegador a mitad de un sondeo no puede filtrar la conexión."""
    await insertar_documento(estado="uploaded")

    flujo = ClienteSSE(f"/api/documents/stream?token={get_settings().admin_token}")
    await flujo.__aenter__()
    await flujo.esperar_evento("documento")
    # Sin esperar a un punto tranquilo: la desconexión llega cuando llega.
    await flujo.cerrar()

    await esperar_pool_libre(timeout=10)
