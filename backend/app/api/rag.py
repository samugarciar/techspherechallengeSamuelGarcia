"""Consulta directa al RAG, sin micrófono.

Es la demostración de aprender/olvidar que no depende de que la voz funcione:
se lanza la misma consulta antes y después de borrar un documento y se ve
`hay_evidencia` pasar de true a false y los fragmentos desaparecer. Ese contraste
es más convincente que cualquier explicación, y se puede repetir delante del
jurado en dos comandos.

El desglose de `ms` por etapa no es decoración: es el presupuesto de latencia del
README verificándose en vivo. Si el reranker se dispara, se ve aquí y se apaga
con RERANK_ENABLED sin tocar nada más.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.api.deps import exigir_admin
from app.core.config import get_settings
from app.rag import embeddings
from app.rag.rerank import hay_evidencia, reordenar
from app.rag.retrieval import buscar

router = APIRouter(tags=["rag"], dependencies=[Depends(exigir_admin)])


class Consulta(BaseModel):
    consulta: str = Field(min_length=1, max_length=1000)
    top_k: int | None = Field(default=None, ge=1, le=20)

    @field_validator("consulta")
    @classmethod
    def _no_vacia(cls, valor: str) -> str:
        # `min_length` no ve los espacios: "   " pasaría y llegaría a bge-m3 y a
        # websearch_to_tsquery, que devolverían basura en vez de un error claro.
        limpia = valor.strip()
        if not limpia:
            raise ValueError("la consulta no puede estar vacía")
        return limpia


@router.post("/query")
async def consultar(cuerpo: Consulta) -> dict[str, Any]:
    ajustes = get_settings()
    top_k = cuerpo.top_k or ajustes.context_top_k

    t0 = perf_counter()
    vector = await embeddings.embeber_consulta(cuerpo.consulta)
    t1 = perf_counter()
    # Se recuperan más candidatos de los que se devuelven para que el
    # cross-encoder tenga entre qué elegir; nunca menos de los pedidos.
    candidatos = await buscar(cuerpo.consulta, vector, max(ajustes.retrieve_top_k, top_k))
    t2 = perf_counter()
    fragmentos = await reordenar(cuerpo.consulta, candidatos, top_k)
    t3 = perf_counter()

    return {
        "fragmentos": [
            {
                "documento_id": f.document_id,
                "filename": f.filename,
                "heading": f.heading,
                "page": f.page,
                "contenido": f.content,
                "score": round(f.score, 4),
                "cita": f.cita(),
            }
            for f in fragmentos
        ],
        # El umbral de grounding vive en rerank.hay_evidencia, no aquí: la misma
        # decisión la toma el agente de voz, y duplicarla sería garantizar que
        # algún día divergen.
        "hay_evidencia": hay_evidencia(fragmentos),
        "ms": {
            "embedding": round((t1 - t0) * 1000),
            "retrieval": round((t2 - t1) * 1000),
            "rerank": round((t3 - t2) * 1000),
            "total": round((t3 - t0) * 1000),
        },
    }
