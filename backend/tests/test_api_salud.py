"""Salud, autenticación y forma de los errores.

La forma del error se prueba aquí y no en cada endpoint porque es una propiedad
de la aplicación entera: si un solo camino se escapa con `{"detail":…}`, la
consola pinta «undefined» en el peor momento posible.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.test_api_utiles import (  # noqa: F401 — pytest registra las fixtures
    _bd,
    _cabeceras,
    _cliente,
)


def _error(respuesta) -> dict:
    """Comprueba la forma del contrato y devuelve el objeto de error."""
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"error"}, cuerpo
    assert set(cuerpo["error"]) == {"codigo", "mensaje"}, cuerpo
    assert cuerpo["error"]["mensaje"].strip()
    return cuerpo["error"]


async def test_salud_no_pide_token(cliente):
    r = await cliente.get("/api/health")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["ok"] is True
    assert cuerpo["db"] is True
    assert cuerpo["version"]


async def test_salud_sin_pool_no_revienta(cliente):
    """`ok:false` en vez de un 500: una sonda que falla no informa de nada."""
    from app.db.pool import close_pool, open_pool

    await close_pool()
    try:
        r = await cliente.get("/api/health")
        assert r.status_code == 200
        assert r.json()["db"] is False
    finally:
        await open_pool()


@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [
        ("get", "/api/documents"),
        ("get", "/api/documents/stream"),
        ("post", "/api/rag/query"),
        ("get", "/api/settings/voice-mode"),
    ],
)
async def test_sin_token_es_401(cliente, metodo, ruta):
    r = await getattr(cliente, metodo)(ruta)
    assert r.status_code == 401
    assert _error(r)["codigo"] == "no_autorizado"


async def test_token_incorrecto_es_401(cliente):
    r = await cliente.get("/api/documents", headers={"X-Admin-Token": "no-es"})
    assert r.status_code == 401
    assert _error(r)["codigo"] == "no_autorizado"


async def test_token_de_sse_por_query(cliente):
    r = await cliente.get("/api/documents/stream", params={"token": "no-es"})
    assert r.status_code == 401
    assert _error(r)["codigo"] == "no_autorizado"


async def test_ruta_inexistente_respeta_el_contrato(cliente):
    r = await cliente.get("/api/no-existe")
    assert r.status_code == 404
    assert _error(r)["codigo"] == "no_encontrado"


async def test_metodo_no_permitido_respeta_el_contrato(cliente, cabeceras):
    r = await cliente.patch("/api/documents", headers=cabeceras)
    assert r.status_code == 405
    assert _error(r)["codigo"] == "metodo_no_permitido"


async def test_fallo_inesperado_respeta_el_contrato(bd, cabeceras, monkeypatch):
    """Un 500 tampoco puede salirse del formato: es cuando más falta hace."""
    from app.api import rag

    async def _explotar(*_a, **_k):
        raise RuntimeError("la base de datos se ha ido a dar un paseo")

    monkeypatch.setattr(rag.embeddings, "embeber_consulta", _explotar)

    # raise_app_exceptions=False para observar la respuesta: Starlette manda el
    # JSON y RE-LANZA la excepción para que uvicorn la registre en el log.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://pruebas",
    ) as c:
        r = await c.post("/api/rag/query", json={"consulta": "¿duele?"}, headers=cabeceras)

    assert r.status_code == 500
    error = _error(r)
    assert error["codigo"] == "error_interno"
    # El detalle interno se registra en el log, no se devuelve.
    assert "paseo" not in error["mensaje"]
