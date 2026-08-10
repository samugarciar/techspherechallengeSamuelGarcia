"""Consulta al RAG por HTTP.

Los modelos están sustituidos: bge-m3 y el reranker tardan decenas de segundos en
cargar y su calidad se mide en los evals de la Fase 6, no aquí. Lo que se prueba
en este fichero es lo que la capa de API sí decide — la forma de la respuesta, el
desglose de latencias, y que el veredicto de grounding sea el de
`rerank.hay_evidencia` y no una regla duplicada.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.api import rag
from app.core.config import get_settings
from app.rag import rerank
from app.rag.retrieval import Fragmento
from tests.test_api_utiles import (  # noqa: F401 — pytest registra las fixtures
    _bd,
    _cabeceras,
    _cliente,
)


def _fragmento(score: float, ordinal: int = 0) -> Fragmento:
    return Fragmento(
        chunk_id=f"c{ordinal}",
        document_id=f"d{ordinal}",
        filename="protocolo.pdf",
        content="Puede ducharse a partir de las 48 horas.",
        heading="Cuidado de la herida",
        page=3,
        score=score,
    )


def _simular(monkeypatch, fragmentos: list[Fragmento]) -> dict:
    visto: dict = {}

    async def _embeber(texto: str):
        visto["consulta_embebida"] = texto
        return np.zeros(1024, dtype=np.float32)

    async def _buscar(consulta, vector, top_k=None):
        visto["top_k_recuperado"] = top_k
        return fragmentos

    async def _reordenar(consulta, candidatos, top_k=None):
        visto["top_k_contexto"] = top_k
        return candidatos[:top_k]

    async def _revalidar(fragmentos):
        # La revalidación final consulta `retrievable_chunks` de verdad, y estos
        # fragmentos son dobles sin fila que los respalde. Que esa etapa cumple lo
        # suyo se prueba en tests/test_robustez_consultas.py, contra Postgres.
        visto["revalidados"] = len(fragmentos)
        return fragmentos

    monkeypatch.setattr(rag.embeddings, "embeber_consulta", _embeber)
    monkeypatch.setattr(rag, "buscar", _buscar)
    monkeypatch.setattr(rag, "reordenar", _reordenar)
    monkeypatch.setattr(rag, "revalidar", _revalidar)
    return visto


async def test_consulta_devuelve_fragmentos_con_cita(cliente, cabeceras, monkeypatch):
    _simular(monkeypatch, [_fragmento(0.93)])

    r = await cliente.post(
        "/api/rag/query", json={"consulta": "¿cuándo puedo ducharme?"}, headers=cabeceras
    )

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["hay_evidencia"] is True

    frag = cuerpo["fragmentos"][0]
    assert set(frag) == {
        "documento_id", "filename", "heading", "page", "contenido", "score", "cita"
    }
    assert frag["cita"] == "protocolo.pdf › Cuidado de la herida › p. 3"
    assert frag["score"] == 0.93

    assert set(cuerpo["ms"]) == {"embedding", "retrieval", "rerank", "total"}
    assert all(isinstance(v, int) for v in cuerpo["ms"].values())
    assert cuerpo["ms"]["total"] >= 0


async def test_sin_evidencia_cuando_el_score_no_llega_al_umbral(cliente, cabeceras, monkeypatch):
    """La señal de «no tengo esa información»: el agente no debe improvisar."""
    _simular(monkeypatch, [_fragmento(0.01)])

    r = await cliente.post("/api/rag/query", json={"consulta": "¿me puedo bañar?"},
                           headers=cabeceras)
    assert r.json()["hay_evidencia"] is False


async def test_sin_fragmentos_no_hay_evidencia(cliente, cabeceras, monkeypatch):
    """Es el estado tras borrar el documento: la misma consulta se queda sin nada."""
    _simular(monkeypatch, [])

    r = await cliente.post("/api/rag/query", json={"consulta": "¿cuándo me ducho?"},
                           headers=cabeceras)
    cuerpo = r.json()
    assert cuerpo["fragmentos"] == []
    assert cuerpo["hay_evidencia"] is False


async def test_top_k_acota_el_contexto_pero_no_el_pool(cliente, cabeceras, monkeypatch):
    """Se recuperan más candidatos de los que se devuelven, nunca menos."""
    visto = _simular(monkeypatch, [_fragmento(0.9, i) for i in range(12)])

    r = await cliente.post(
        "/api/rag/query", json={"consulta": "fiebre", "top_k": 12}, headers=cabeceras
    )
    assert len(r.json()["fragmentos"]) == 12
    assert visto["top_k_contexto"] == 12
    assert visto["top_k_recuperado"] >= 12


async def test_consulta_vacia(cliente, cabeceras):
    r = await cliente.post("/api/rag/query", json={"consulta": "   "}, headers=cabeceras)
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "peticion_invalida"
    assert "consulta" in r.json()["error"]["mensaje"]


async def test_consulta_ausente(cliente, cabeceras):
    r = await cliente.post("/api/rag/query", json={}, headers=cabeceras)
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "peticion_invalida"


async def test_top_k_fuera_de_rango(cliente, cabeceras):
    r = await cliente.post(
        "/api/rag/query", json={"consulta": "fiebre", "top_k": 0}, headers=cabeceras
    )
    assert r.status_code == 422


async def test_la_consulta_llega_recortada_al_modelo(cliente, cabeceras, monkeypatch):
    visto = _simular(monkeypatch, [])
    await cliente.post("/api/rag/query", json={"consulta": "  fiebre alta  "}, headers=cabeceras)
    assert visto["consulta_embebida"] == "fiebre alta"


# --- El interruptor del reranker ---------------------------------------------


async def test_hay_evidencia_sigue_significando_algo_sin_reranker():
    """RERANK_ENABLED=0 es el escape de latencia documentado, y rompe el grounding.

    Las dos ramas de `reordenar()` devuelven escalas distintas y `hay_evidencia`
    compara las dos contra el mismo `MIN_RELEVANCE_SCORE = 0.35`:

      · con reranker  -> el cross-encoder pasa por sigmoide, 0..1. 0,35 discrimina.
      · sin reranker  -> el score es el de RRF, cuyo MÁXIMO TEÓRICO es
                         1/(60+1) + 1/(60+1) = 0,0328. Nunca llega a 0,35.

    O sea: con el reranker apagado, `hay_evidencia` es False para todo, el
    agente contesta «no tengo esa información» a preguntas cuyo protocolo tiene
    delante, y la consola pinta el aviso ámbar sobre una lista de fragmentos
    perfectamente buenos. El fallo no se ve en ningún test porque todos los que
    tocan grounding sustituyen `reordenar`.

    Y es un interruptor que se va a tocar: el reranker está medido en 585 ms, el
    mayor bloque del camino de voz, así que apagarlo es la palanca de latencia más
    tentadora que queda. Las cabeceras de `app/api/rag.py` y `app/rag/rerank.py`
    anunciaban que apagarlo salía gratis; ahora avisan de lo contrario y apuntan
    aquí.

    Se prueba con la función real, sin cargar ningún modelo: la rama de
    `rerank_enabled=False` sale antes de tocar el cross-encoder.
    """
    ajustes = get_settings()
    original = ajustes.rerank_enabled
    ajustes.rerank_enabled = False
    try:
        # El mejor candidato posible: primero en las dos listas de la híbrida.
        mejor = _fragmento(1 / (60 + 1) + 1 / (60 + 1))
        assert rerank.hay_evidencia(await rerank.reordenar("¿cuándo me ducho?", [mejor], 4))
    finally:
        ajustes.rerank_enabled = original
