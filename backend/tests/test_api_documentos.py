"""Subir, listar, inspeccionar y olvidar documentos.

El eje de estos tests es lo que puede salir mal en una subida, no el camino
feliz: el camino feliz lo prueba la demo sola, y lo que arruina una demo es un
.exe aceptado, un archivo de 0 bytes que deja un job envenenado en la cola, o un
duplicado que hace estallar el índice único a mitad de la ingesta.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pytest

from app.api import documentos, errores
from app.db.pool import connection
from tests.test_api_utiles import (  # noqa: F401 — pytest registra las fixtures
    _bd,
    _cabeceras,
    _cliente,
    _cola,
    estado_de,
    insertar_documento,
    subida,
)

# --- Subida ------------------------------------------------------------------


async def test_subida_devuelve_202_y_encola(cliente, cabeceras, cola):
    r = await cliente.post(
        "/api/documents",
        files=subida("protocolo_apendicectomia.md"),
        data={"title": "Protocolo de alta — apendicectomía"},
        headers=cabeceras,
    )

    assert r.status_code == 202
    doc = r.json()
    assert doc["status"] == "uploaded", "la subida no debe esperar al procesamiento"
    assert doc["filename"] == "protocolo_apendicectomia.md"
    assert doc["title"] == "Protocolo de alta — apendicectomía"
    assert doc["mime_type"] == "text/markdown"
    assert doc["chunks_count"] == 0 and doc["embedded_count"] == 0
    assert len(doc["sha256"]) == 64
    assert doc["created_at"].endswith("Z")

    assert [str(d) for d, _ in cola] == [doc["id"]], "debe encolarse el documento creado"


async def test_subida_deja_rastro_de_auditoria(cliente, cabeceras, cola):
    r = await cliente.post("/api/documents", files=subida(), headers=cabeceras)
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT event FROM document_events WHERE document_id = %s", (r.json()["id"],)
        )
        eventos = [f["event"] for f in await cur.fetchall()]
    assert eventos == ["uploaded"]


async def test_subida_escribe_el_archivo_con_nombre_seguro(cliente, cabeceras, cola):
    """El nombre en disco es el uuid: dos «alta.pdf» no se pisan y un
    «../../etc/passwd» no puede salir de storage/."""
    r = await cliente.post(
        "/api/documents", files=subida("../../fuera.md", b"# X\n\ncuerpo"), headers=cabeceras
    )
    doc = r.json()
    assert doc["filename"] == "fuera.md"

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT storage_path FROM documents WHERE id = %s", (doc["id"],)
        )
        ruta = (await cur.fetchone())["storage_path"]
    assert ruta.endswith(f"{doc['id']}.md")


async def test_formato_no_soportado(cliente, cabeceras, cola):
    r = await cliente.post("/api/documents", files=subida("virus.exe"), headers=cabeceras)
    assert r.status_code == 415
    assert r.json()["error"]["codigo"] == "formato_no_soportado"
    assert not cola, "no debe encolarse nada que no se vaya a procesar"


async def test_archivo_sin_extension(cliente, cabeceras, cola):
    r = await cliente.post("/api/documents", files=subida("README"), headers=cabeceras)
    assert r.status_code == 415
    assert r.json()["error"]["codigo"] == "formato_no_soportado"


async def test_archivo_vacio(cliente, cabeceras, cola):
    r = await cliente.post("/api/documents", files=subida("vacio.md", b""), headers=cabeceras)
    assert r.status_code == 400
    assert r.json()["error"]["codigo"] == "archivo_vacio"
    assert not cola


async def test_archivo_demasiado_grande(cliente, cabeceras, cola):
    enorme = b"x" * (documentos.MAXIMO_BYTES + 1)
    r = await cliente.post("/api/documents", files=subida("gordo.pdf", enorme), headers=cabeceras)
    assert r.status_code == 413
    assert r.json()["error"]["codigo"] == "archivo_demasiado_grande"
    assert not cola


async def test_el_tope_se_vigila_tambien_al_leer():
    """Camino sin `size` declarado: el guardia del bucle es el que corta.

    Se prueba aparte porque por HTTP el parser multipart siempre informa del
    tamaño y el atajo se dispara antes; este es el camino que quedaría vivo si
    alguien cambiara de parser.
    """

    class _Chorro:
        size = None          # el parser no supo decir cuánto ocupa

        def __init__(self) -> None:
            self.lecturas = 0

        async def read(self, n: int) -> bytes:
            self.lecturas += 1
            assert self.lecturas < 64, "el guardia del bucle no cortó"
            return b"y" * n

    with pytest.raises(errores.ErrorAPI) as fallo:
        await documentos._leer_validando(_Chorro())
    assert fallo.value.codigo == "archivo_demasiado_grande"


async def test_no_deja_archivo_huerfano_si_falla_la_cola(cliente, cabeceras, monkeypatch):
    """Si la transacción revierte, el fichero escrito tiene que irse con ella."""
    from app.db import queue

    async def _romper(*_a, **_k):
        raise RuntimeError("worker caído")

    monkeypatch.setattr(queue, "encolar", _romper)

    almacen = documentos.get_settings().storage_dir
    antes = set(almacen.glob("*"))
    with pytest.raises(RuntimeError):
        await cliente.post("/api/documents", files=subida(), headers=cabeceras)

    assert set(almacen.glob("*")) == antes, "quedó un archivo sin fila que lo referencie"
    async with connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM documents")
        assert (await cur.fetchone())["n"] == 0


# --- Duplicados y versionado -------------------------------------------------


async def test_mismo_sha256_dos_veces_es_duplicado(cliente, cabeceras, cola):
    """Mientras la primera copia se procesa, la segunda se rechaza.

    Aceptarla crearía dos jobs para el mismo contenido y, al promocionar el
    segundo, chocaría con `documents_active_sha_idx`.
    """
    contenido = b"# Cuidado de la herida\n\nMantenerla seca 48 horas."
    primera = await cliente.post(
        "/api/documents", files=subida("herida.md", contenido), headers=cabeceras
    )
    assert primera.status_code == 202

    segunda = await cliente.post(
        "/api/documents", files=subida("herida.md", contenido), headers=cabeceras
    )
    assert segunda.status_code == 409
    error = segunda.json()["error"]
    assert error["codigo"] == "documento_duplicado"
    assert "uploaded" in error["mensaje"]
    assert len(cola) == 1


async def test_resubir_lo_ya_listo_es_una_version_nueva(cliente, cabeceras, cola):
    """Un protocolo corregido se acepta y anota a quién sustituye."""
    contenido = b"# Alta\n\nRevision 2."
    sha = hashlib.sha256(contenido).hexdigest()
    anterior = await insertar_documento(filename="alta.md", estado="ready", sha256=sha, chunks=3)

    r = await cliente.post("/api/documents", files=subida("alta.md", contenido), headers=cabeceras)
    assert r.status_code == 202
    assert r.json()["supersedes_id"] == str(anterior)
    assert await estado_de(anterior) == "ready", "el anterior sigue vivo hasta que el nuevo lo esté"


async def test_resubir_lo_que_fallo_se_acepta(cliente, cabeceras, cola):
    contenido = b"# Alta\n\nOtra vez."
    await insertar_documento(
        filename="alta.md", estado="failed", sha256=hashlib.sha256(contenido).hexdigest()
    )
    r = await cliente.post("/api/documents", files=subida("alta.md", contenido), headers=cabeceras)
    assert r.status_code == 202
    assert r.json()["supersedes_id"] is None


# --- Listado -----------------------------------------------------------------


async def test_listado_ordena_y_filtra(cliente, cabeceras):
    await insertar_documento(filename="apendicectomia.md", estado="ready", chunks=2)
    await insertar_documento(filename="colecistectomia.md", estado="failed")
    await insertar_documento(filename="hernia.md", estado="ready")

    r = await cliente.get("/api/documents", headers=cabeceras)
    assert r.status_code == 200
    assert r.json()["total"] == 3

    r = await cliente.get("/api/documents", params={"estado": "ready"}, headers=cabeceras)
    assert r.json()["total"] == 2
    assert {d["status"] for d in r.json()["documentos"]} == {"ready"}

    r = await cliente.get("/api/documents", params={"q": "apendic"}, headers=cabeceras)
    assert [d["filename"] for d in r.json()["documentos"]] == ["apendicectomia.md"]

    r = await cliente.get("/api/documents", params={"limit": 2}, headers=cabeceras)
    cuerpo = r.json()
    assert len(cuerpo["documentos"]) == 2
    assert cuerpo["total"] == 3, "el total ignora la paginación"


async def test_el_total_sobrevive_a_una_pagina_vacia(cliente, cabeceras):
    """`count(*) OVER ()` viaja DENTRO de las filas: sin filas, no hay total.

    Con un `offset` más allá del final la consulta devuelve cero filas y el
    endpoint respondía `total: 0` habiendo tres documentos. El contrato dice que
    `total` es el número de documentos que casan con el filtro, no los de la
    página, y quien pagine —o quien pinte «3 de 12»— se lo cree.
    """
    for nombre in ("uno.md", "dos.md", "tres.md"):
        await insertar_documento(filename=nombre, estado="ready")

    r = await cliente.get("/api/documents", params={"limit": 2, "offset": 10}, headers=cabeceras)
    cuerpo = r.json()
    assert cuerpo["documentos"] == []
    assert cuerpo["total"] == 3, "el total no puede depender de que la página traiga filas"

    # Y con un filtro que no casa con nada, el total sí es 0 de verdad.
    r = await cliente.get("/api/documents", params={"q": "no-existe-esto"}, headers=cabeceras)
    assert r.json() == {"documentos": [], "total": 0}


async def test_listado_con_estado_invalido(cliente, cabeceras):
    r = await cliente.get("/api/documents", params={"estado": "inventado"}, headers=cabeceras)
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "peticion_invalida"


async def test_listado_vacio(cliente, cabeceras):
    r = await cliente.get("/api/documents", headers=cabeceras)
    assert r.json() == {"documentos": [], "total": 0}


# --- Detalle -----------------------------------------------------------------


async def test_detalle_muestra_lo_aprendido(cliente, cabeceras):
    document_id = await insertar_documento(estado="ready", chunks=5, pages=6)
    r = await cliente.get(f"/api/documents/{document_id}", headers=cabeceras)

    assert r.status_code == 200
    doc = r.json()
    assert doc["chunks_count"] == 5
    assert doc["pages"] == 6, "lo que contó el parser, no lo que llegó a trocearse"
    preview = doc["chunks_preview"]
    assert len(preview) == 3, "solo los tres primeros"
    assert preview[0]["heading"] == "Sección 0"
    assert len(preview[0]["contenido"]) <= 200


async def test_detalle_de_documento_inexistente(cliente, cabeceras):
    r = await cliente.get(f"/api/documents/{uuid4()}", headers=cabeceras)
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "documento_no_encontrado"


async def test_detalle_con_id_no_uuid(cliente, cabeceras):
    r = await cliente.get("/api/documents/no-soy-un-uuid", headers=cabeceras)
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "peticion_invalida"


# --- Olvido ------------------------------------------------------------------


async def test_borrar_olvida_y_cuenta_los_fragmentos(cliente, cabeceras):
    document_id = await insertar_documento(estado="ready", chunks=7)

    r = await cliente.delete(f"/api/documents/{document_id}", headers=cabeceras)
    assert r.status_code == 200
    assert r.json() == {"olvidado": True, "chunks_eliminados": 7}

    async with connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM chunks WHERE document_id = %s",
                                 (document_id,))
        assert (await cur.fetchone())["n"] == 0, "el CASCADE debió llevárselos"
        cur = await conn.execute(
            "SELECT event FROM document_events WHERE document_id = %s", (document_id,)
        )
        assert [f["event"] for f in await cur.fetchall()] == ["deleted"], (
            "el rastro de auditoría sobrevive al borrado (document_events no tiene FK)"
        )


async def test_borrar_lo_hace_irrecuperable_para_el_rag(cliente, cabeceras):
    """La prueba del olvido a nivel de datos: la vista deja de verlo."""
    document_id = await insertar_documento(estado="ready", chunks=3)
    async with connection() as conn:
        # `retrievable_chunks` exige embedding no nulo; se pone uno cualquiera.
        await conn.execute(
            "UPDATE chunks SET embedding = %s WHERE document_id = %s",
            (np.full(1024, 0.1, dtype=np.float32), document_id),
        )
        cur = await conn.execute(
            "SELECT count(*) AS n FROM retrievable_chunks WHERE document_id = %s", (document_id,)
        )
        assert (await cur.fetchone())["n"] == 3

    await cliente.delete(f"/api/documents/{document_id}", headers=cabeceras)

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM retrievable_chunks WHERE document_id = %s", (document_id,)
        )
        assert (await cur.fetchone())["n"] == 0


async def test_borrar_documento_inexistente(cliente, cabeceras):
    r = await cliente.delete(f"/api/documents/{uuid4()}", headers=cabeceras)
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "documento_no_encontrado"


async def test_borrar_borra_tambien_el_archivo(cliente, cabeceras, cola):
    r = await cliente.post("/api/documents", files=subida(), headers=cabeceras)
    document_id = r.json()["id"]
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT storage_path FROM documents WHERE id = %s", (document_id,)
        )
        ruta = documentos.Path((await cur.fetchone())["storage_path"])
    assert ruta.exists()

    await cliente.delete(f"/api/documents/{document_id}", headers=cabeceras)
    assert not ruta.exists()


def test_serializador_no_inventa_campos():
    """El objeto `Documento` del contrato, exactamente."""
    ahora = datetime.now(UTC)
    fila = {
        "id": uuid4(), "filename": "a.pdf", "title": None, "mime_type": "application/pdf",
        "size_bytes": 10, "sha256": "x", "status": "ready", "error_message": None,
        "chunks_count": 1, "embedded_count": 1, "supersedes_id": None, "pages": 2,
        "created_at": ahora, "updated_at": ahora,
    }
    assert set(documentos._documento(fila)) == {
        "id", "filename", "title", "mime_type", "size_bytes", "sha256", "status",
        "error", "chunks_count", "embedded_count", "pages", "supersedes_id",
        "created_at", "updated_at",
    }
