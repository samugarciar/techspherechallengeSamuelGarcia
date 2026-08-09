"""Lo peor que este sistema puede hacer: citar un protocolo ya retirado.

Un documento borrado no puede aparecer en una respuesta. Ni siquiera si el
borrado ocurre a mitad de la consulta, que es cuando de verdad pasa: el
administrador retira un protocolo obsoleto justo mientras el agente está
respondiéndole a un paciente por teléfono. Que la ventana sea de 120 ms no la
hace aceptable — la hace difícil de encontrar.

Las dos garantías del schema NO cubren este caso, y conviene tenerlo claro
porque el README las presenta como suficientes:

  - `ON DELETE CASCADE` borra las filas de verdad, pero la sentencia que ya las
    leyó trabajaba sobre su propia instantánea. En READ COMMITTED cada sentencia
    ve el estado que había cuando ELLA empezó.
  - `retrievable_chunks` filtra en el momento de la lectura, y la lectura ya
    pasó: para cuando el borrado hace COMMIT, los fragmentos llevan 120 ms en
    memoria de Python, donde Postgres no llega.

Lo que cierra el hueco es preguntar otra vez, lo último de todo. Aquí se prueba
que se pregunta, y también la otra mitad del cinturón: que la vista es
irrompible desde abajo (fragmentos de documentos no 'ready', sin vector, o
huérfanos) y que nadie se la salta consultando `chunks` a pelo.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import psycopg
import pytest

from app.api import rag as api_rag
from app.db.pool import connection
from app.rag import ingest, query, retrieval
from tests.ayuda_db import hay_postgres
from tests.test_api_utiles import (  # noqa: F401 — pytest registra las fixtures
    _bd,
    _cabeceras,
    _cliente,
)

pytestmark = hay_postgres

# Un vector cualquiera, pero el MISMO para la consulta y para los fragmentos
# sembrados: así la distancia coseno es 0 y el orden del retrieval es
# determinista sin cargar bge-m3.
VECTOR = np.zeros(1024, dtype=np.float32)
VECTOR[0] = 1.0

TEXTO = (
    "Puede ducharse a partir de las 48 horas de la intervención, retirando el "
    "apósito antes y secando la herida sin frotar. Acuda a urgencias si presenta "
    "fiebre superior a 38.5 grados."
)


async def sembrar_listo(filename: str = "protocolo.pdf", fragmentos: int = 3) -> UUID:
    """Un documento en 'ready' con fragmentos ya vectorizados.

    Sin pasar por la ingesta: lo que se prueba aquí es la lectura, y montar el
    estado a mano evita cargar bge-m3 y deja el test en milisegundos.
    """
    document_id = uuid4()
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO documents (id, filename, mime_type, sha256, size_bytes,
                                   storage_path, status, chunks_count, embedded_count)
            VALUES (%s, %s, 'application/pdf', %s, 1024, %s, 'ready', %s, %s)
            """,
            (document_id, filename, uuid4().hex, f"/tmp/{document_id}.pdf",
             fragmentos, fragmentos),
        )
        for i in range(fragmentos):
            await conn.execute(
                """
                INSERT INTO chunks (document_id, ordinal, content, heading, page, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (document_id, i, TEXTO, "Cuidado de la herida", i + 1, VECTOR),
            )
    return document_id


def _sin_modelo(monkeypatch, modulo) -> None:
    """Sustituye bge-m3 por el vector fijo. La consulta sigue yendo a Postgres."""
    async def _embeber(texto: str):
        return VECTOR

    monkeypatch.setattr(modulo.embeddings, "embeber_consulta", _embeber)


# ---------------------------------------------------------------------------
# Borrado en vuelo
# ---------------------------------------------------------------------------
async def test_borrar_a_mitad_de_consulta_no_deja_citar_lo_borrado(bd, monkeypatch):
    """El borrado cae en la ventana del reranker y la respuesta sale limpia.

    Se coloca el borrado exactamente donde de verdad hace daño: después de que el
    retrieval haya leído las filas y antes de que la respuesta se construya. Es
    la simulación fiel de los ~120 ms que tarda el cross-encoder.
    """
    document_id = await sembrar_listo()
    _sin_modelo(monkeypatch, query)

    async def _reordenar_mientras_el_admin_borra(consulta, candidatos, top_k=None):
        assert candidatos, "el retrieval tenía que haber encontrado el documento"
        await ingest.olvidar_documento(document_id)
        return candidatos[:top_k]

    monkeypatch.setattr(query, "reordenar", _reordenar_mientras_el_admin_borra)

    resultado = await query.consultar("¿cuándo puedo ducharme?", top_k=3)

    assert resultado.fragmentos == [], (
        "la respuesta cita un protocolo que el hospital acaba de retirar"
    )
    assert resultado.hay_evidencia is False, "el agente debe decir que no tiene esa información"
    assert resultado.retirados == 3, "los tres fragmentos tenían que caerse en la revalidación"


async def test_por_http_tampoco_se_cita_un_documento_recien_borrado(
    bd, cliente, cabeceras, monkeypatch
):
    """El mismo ataque contra `POST /api/rag/query`, que es lo que usa la demo."""
    document_id = await sembrar_listo()
    _sin_modelo(monkeypatch, api_rag)

    async def _reordenar_mientras_el_admin_borra(consulta, candidatos, top_k=None):
        await ingest.olvidar_documento(document_id)
        return candidatos[:top_k]

    monkeypatch.setattr(api_rag, "reordenar", _reordenar_mientras_el_admin_borra)

    r = await cliente.post(
        "/api/rag/query", json={"consulta": "¿cuándo puedo ducharme?"}, headers=cabeceras
    )
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["fragmentos"] == []
    assert cuerpo["hay_evidencia"] is False


async def test_reemplazar_la_version_a_mitad_de_consulta_tampoco_la_cita(bd, monkeypatch):
    """Retirar no es borrar, y también tiene que valer al instante.

    Cuando se sube un protocolo corregido, el anterior pasa a 'superseded' y su
    fila y sus fragmentos siguen existiendo. Si la revalidación mirara «¿existe
    la fila?» en vez de preguntarle a la vista, esto pasaría desapercibido y el
    agente citaría la versión que el hospital acaba de sustituir — que en clínica
    es peor que citar una borrada, porque suena plausible.
    """
    document_id = await sembrar_listo()
    _sin_modelo(monkeypatch, query)

    async def _reordenar_mientras_llega_la_version_nueva(consulta, candidatos, top_k=None):
        async with connection() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'superseded' WHERE id = %s", (document_id,)
            )
        return candidatos[:top_k]

    monkeypatch.setattr(query, "reordenar", _reordenar_mientras_llega_la_version_nueva)

    resultado = await query.consultar("¿cuándo puedo ducharme?", top_k=3)
    assert resultado.fragmentos == []
    assert resultado.retirados == 3

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s", (document_id,)
        )
        assert (await cur.fetchone())["n"] == 3, "no se borró nada: solo dejó de ser recuperable"


async def test_lo_que_sigue_vivo_se_devuelve(bd, monkeypatch):
    """El contrapeso: la revalidación no puede tragarse lo que sí es válido.

    Sin este test, `revalidar()` podría devolver siempre la lista vacía y los
    otros tres pasarían igual.
    """
    await sembrar_listo()
    _sin_modelo(monkeypatch, query)

    resultado = await query.consultar("¿cuándo puedo ducharme?", top_k=3)
    assert len(resultado.fragmentos) == 3
    assert resultado.retirados == 0
    assert all("ducharse" in f.content for f in resultado.fragmentos)


# ---------------------------------------------------------------------------
# La vista como último cinturón
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "estado", ["uploaded", "parsing", "chunking", "embedding", "failed", "superseded"]
)
async def test_solo_lo_que_esta_listo_es_recuperable(bd, monkeypatch, estado):
    """Fragmentos perfectos, con vector y todo, de un documento que no está listo.

    Es el escenario de un bug en la ingesta: si alguna vez insertara los chunks
    antes de promover, o dejara los de un intento anterior, la vista los tiene
    que seguir escondiendo.
    """
    document_id = await sembrar_listo()
    async with connection() as conn:
        await conn.execute("UPDATE documents SET status = %s WHERE id = %s",
                           (estado, document_id))

    _sin_modelo(monkeypatch, query)
    resultado = await query.consultar("¿cuándo puedo ducharme?", top_k=3)
    assert resultado.fragmentos == [], f"un documento en '{estado}' resultó recuperable"


async def test_un_fragmento_sin_vector_no_es_recuperable(bd, monkeypatch):
    """Un documento a medio embeber no puede responder con la mitad que ya tiene."""
    document_id = await sembrar_listo()
    async with connection() as conn:
        await conn.execute(
            "UPDATE chunks SET embedding = NULL WHERE document_id = %s AND ordinal > 0",
            (document_id,),
        )

    _sin_modelo(monkeypatch, query)
    resultado = await query.consultar("¿cuándo puedo ducharme?", top_k=8)
    assert len(resultado.fragmentos) == 1, "los fragmentos sin vector no deben salir"


async def test_no_se_puede_crear_un_fragmento_huerfano(bd):
    """La otra mitad del cinturón: el huérfano no se puede ni escribir.

    `chunks.document_id` es NOT NULL con FK, así que el estado «fragmento sin
    documento» —el que haría falta para que el olvido dejara restos— no es
    representable. No es que el código no lo haga: es que Postgres no deja.
    """
    async with connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                """
                INSERT INTO chunks (document_id, ordinal, content, embedding)
                VALUES (%s, 0, 'fragmento sin dueño', %s)
                """,
                (uuid4(), VECTOR),
            )


def test_ningun_codigo_de_retrieval_consulta_la_tabla_chunks():
    """Invariante de diseño, comprobada con grep sobre todo el backend.

    La vista solo protege si nadie la esquiva. Las dos excepciones son legítimas
    y están acotadas: `ingest` escribe (esa es su faena) y `documentos` lee para
    la consola del administrador —la vista previa de lo aprendido y el recuento
    de lo borrado—, que es justo lo que hay que poder ver DE un documento que no
    está listo. Ninguna de las dos participa en responderle a un paciente.

    Este test es el que impide que la excepción se extienda sin que nadie lo note:
    cualquier fichero nuevo que toque `chunks` tiene que pasar por aquí y
    justificarse.
    """
    permitidos = {"app/rag/ingest.py", "app/api/documentos.py"}
    sospechoso = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+chunks\b", re.IGNORECASE)

    raiz = Path(__file__).resolve().parents[1]
    culpables = {
        str(fichero.relative_to(raiz))
        for fichero in (raiz / "app").rglob("*.py")
        if sospechoso.search(fichero.read_text(encoding="utf-8"))
    }

    assert culpables <= permitidos, (
        f"estos ficheros consultan la tabla `chunks` en vez de `retrievable_chunks`: "
        f"{sorted(culpables - permitidos)}"
    )


async def test_la_revalidacion_tambien_consulta_la_vista(bd, monkeypatch):
    """Y la última etapa no es una excepción a la invariante.

    Comprobar «¿sigue existiendo la fila?» contra `chunks` sería más rápido y
    estaría mal: un documento reemplazado conserva sus filas.
    """
    document_id = await sembrar_listo()
    fragmentos = await retrieval.buscar("ducharse", VECTOR, top_k=3)
    assert len(fragmentos) == 3

    async with connection() as conn:
        await conn.execute(
            "UPDATE documents SET status = 'superseded' WHERE id = %s", (document_id,)
        )
    assert await retrieval.revalidar(fragmentos) == []


# ---------------------------------------------------------------------------
# La mitad léxica de la búsqueda híbrida
# ---------------------------------------------------------------------------
async def test_una_pregunta_hablada_activa_tambien_la_mitad_lexica(bd):
    """La búsqueda es híbrida solo si las dos mitades se disparan de verdad.

    `websearch_to_tsquery` une los términos con AND, así que
    «¿qué hago si me sube la fiebre?» se convierte en
    `'hag' & 'si' & 'sub' & 'fiebr'` y no casa con ningún fragmento aunque el
    protocolo diga «fiebre superior a 38.5 grados». El síntoma era silencioso:
    resultados que parecían razonables porque la mitad densa los salvaba, con
    `lexical_rank = None` en los ocho candidatos.
    """
    await sembrar_listo()

    fragmentos = await retrieval.buscar("¿qué hago si me sube la fiebre?", VECTOR, top_k=8)
    assert fragmentos, "ni la mitad densa devolvió nada: el test no vale"
    assert any(f.lexical_rank is not None for f in fragmentos), (
        "la mitad léxica no encontró «fiebre»: la búsqueda no es híbrida, es solo densa"
    )


async def test_una_consulta_por_palabras_clave_sigue_funcionando(bd):
    """Ampliar a OR no puede romper el caso que ya funcionaba."""
    await sembrar_listo()
    fragmentos = await retrieval.buscar("apósito 48 horas", VECTOR, top_k=8)
    assert any(f.lexical_rank is not None for f in fragmentos)


@pytest.mark.parametrize("consulta", ["¿?", "...", "   .  ", "de la"])
async def test_una_consulta_sin_palabras_utiles_no_revienta(bd, consulta):
    """Solo puntuación o solo stopwords: la rama léxica se queda vacía y la densa
    responde sola. `to_tsquery('')` sería un error de sintaxis en SQL."""
    await sembrar_listo()
    fragmentos = await retrieval.buscar(consulta, VECTOR, top_k=4)
    assert all(f.lexical_rank is None for f in fragmentos)
    assert fragmentos, "la mitad densa debería seguir devolviendo candidatos"


# ---------------------------------------------------------------------------
# La orden de consulta (app/rag/query.py)
# ---------------------------------------------------------------------------
async def test_la_orden_de_consulta_ensena_cita_score_y_milisegundos(bd, monkeypatch, capsys):
    """Lo que tiene que salir por pantalla para poder depurar el RAG a solas."""
    await sembrar_listo(filename="protocolo_apendicectomia.pdf")
    _sin_modelo(monkeypatch, query)

    resultado = await query.consultar("¿cuándo puedo ducharme?", top_k=2)
    query._imprimir("¿cuándo puedo ducharme?", resultado)
    salida = capsys.readouterr().out

    assert "protocolo_apendicectomia.pdf › Cuidado de la herida › p. 1" in salida
    assert "ducharse" in salida
    assert set(resultado.ms) == {"embedding", "retrieval", "rerank", "revalidacion", "total"}
    for etapa in resultado.ms:
        assert f"{etapa} {resultado.ms[etapa]} ms" in salida


async def test_la_orden_de_consulta_no_inventa_cuando_no_hay_nada(bd, monkeypatch, capsys):
    """Con la base vacía —o con todo borrado— dice que no sabe, y lo dice claro."""
    _sin_modelo(monkeypatch, query)

    resultado = await query.consultar("¿cuándo puedo ducharme?")
    query._imprimir("¿cuándo puedo ducharme?", resultado)

    assert resultado.fragmentos == []
    assert resultado.hay_evidencia is False
    assert "no tiene esa información" in capsys.readouterr().out
