"""Consulta directa al RAG, sin micrófono.

Es la demostración de aprender/olvidar que no depende de que la voz funcione:
se lanza la misma consulta antes y después de borrar un documento y se ve
`hay_evidencia` pasar de true a false y los fragmentos desaparecer. Ese contraste
es más convincente que cualquier explicación, y se puede repetir delante del
jurado en dos comandos.

El desglose de `ms` por etapa no es decoración: es el presupuesto de latencia del
README verificándose en vivo, y es donde se ve que el reranker cuesta 585 ms.

CUIDADO con la conclusión fácil. Este comentario decía que apagar el reranker con
`RERANK_ENABLED` era gratis, «sin tocar nada más». Es falso y era la peor clase de
consejo: las dos ramas de `reordenar()` devuelven escalas distintas y ambas se
comparan contra el mismo umbral, así que hoy apagarlo deja `hay_evidencia` en
`False` para todo y el agente contesta «no tengo esa información» con el protocolo
delante. Está documentado en `docs/REVISION_F2_F3.md` §1.12 y vigilado por el
`xfail(strict=True)` de `tests/test_api_rag.py`.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.api.deps import exigir_admin
from app.core.config import get_settings
from app.db.traces import registrar_traza
from app.rag import embeddings
from app.rag.rerank import hay_evidencia, reordenar
from app.rag.retrieval import buscar, revalidar

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
    reordenados = await reordenar(cuerpo.consulta, candidatos, top_k)
    t3 = perf_counter()
    # Última etapa, y no es opcional: durante los ~585 ms del cross-encoder el
    # administrador puede haber borrado el documento, y devolver aquí un fragmento
    # suyo sería citarle a un paciente un protocolo ya retirado. Ver
    # `retrieval.revalidar()` para por qué el ON DELETE CASCADE no basta.
    fragmentos = await revalidar(reordenados)
    t4 = perf_counter()

    duracion_ms = round((t4 - t0) * 1000)
    evidencia_ok = hay_evidencia(fragmentos)

    await registrar_traza(
        span="retrieval",
        duration_ms=duracion_ms,
        metadata={
            "consulta": cuerpo.consulta,
            "hay_evidencia": evidencia_ok,
            "fragmentos_count": len(fragmentos),
            "documentos": [f.filename for f in fragmentos],
            "ms": {
                "embedding": round((t1 - t0) * 1000),
                "retrieval": round((t2 - t1) * 1000),
                "rerank": round((t3 - t2) * 1000),
                "total": duracion_ms,
            },
        },
    )

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
        "hay_evidencia": evidencia_ok,
        # El contrato publica estas cuatro claves y la consola las pinta; la
        # revalidación (~1 ms, una consulta por clave primaria) va dentro de
        # `total` en vez de añadir una quinta que el frontend no espera.
        "ms": {
            "embedding": round((t1 - t0) * 1000),
            "retrieval": round((t2 - t1) * 1000),
            "rerank": round((t3 - t2) * 1000),
            "total": duracion_ms,
        },
    }
