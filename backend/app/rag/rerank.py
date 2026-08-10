"""Reranking con cross-encoder.

Es la mayor palanca de precisión del RAG: el bi-encoder que produce los vectores
comprime consulta y pasaje por separado, mientras que el cross-encoder los lee
juntos y puede juzgar si el pasaje responde de verdad a la pregunta. En dominio
clínico, donde la diferencia entre "puede ducharse a las 48 h" y "mantenga el
apósito seco" decide la respuesta, ese juicio importa.

Es también el componente más caro del camino de voz —585 ms medidos contra la API
real, no los 114 ms que presupuestó la Fase 0 midiendo con pasajes de juguete—, así
que está detrás de un interruptor (RERANK_ENABLED).

Ese interruptor NO está listo para usarse todavía, aunque el nombre invite. Las dos
ramas de `reordenar()` emiten scores en escalas distintas y `hay_evidencia()` las
compara con el mismo umbral, de modo que apagarlo hoy no cuesta precisión: rompe el
grounding entero y en silencio. Ver `docs/REVISION_F2_F3.md` §1.12 y el
`xfail(strict=True)` de `tests/test_api_rag.py`, que se pondrá rojo el día que se
arregle. Encender o apagar el reranker es además una decisión abierta que espera al
corpus real (`eval/medir_reranker.py`).
"""

import asyncio
import threading

from app.core.config import get_settings
from app.rag.retrieval import Fragmento

_modelo = None
_lock = threading.Lock()


def _cargar():
    global _modelo
    if _modelo is None:
        with _lock:
            if _modelo is None:
                import torch
                from sentence_transformers import CrossEncoder

                s = get_settings()
                device = "mps" if torch.backends.mps.is_available() else "cpu"
                _modelo = CrossEncoder(s.rerank_model, device=device, max_length=512)
    return _modelo


def _puntuar(consulta: str, textos: list[str]) -> list[float]:
    return [float(x) for x in _cargar().predict([(consulta, t) for t in textos])]


async def reordenar(
    consulta: str,
    candidatos: list[Fragmento],
    top_k: int | None = None,
) -> list[Fragmento]:
    """Reordena los candidatos de la búsqueda híbrida y se queda con los mejores.

    Con el reranker apagado devuelve el orden de RRF recortado. La *forma* no
    cambia —misma lista de `Fragmento`—, pero la ESCALA de `score` sí: RRF con
    k=60 tiene un máximo teórico de 0,0328 y el cross-encoder llega a 1. Como
    `hay_evidencia()` compara ambas contra el mismo umbral, esa rama es hoy el
    fallo 1.12 y no un modo de funcionamiento válido.
    """
    s = get_settings()
    top_k = top_k or s.context_top_k

    if not candidatos:
        return []
    if not s.rerank_enabled:
        max_rrf = 2.0 / (60 + 1)
        for frag in candidatos:
            frag.score = min(1.0, frag.score / max_rrf)
        return candidatos[:top_k]

    puntuaciones = await asyncio.to_thread(
        _puntuar, consulta, [c.content for c in candidatos]
    )
    for frag, score in zip(candidatos, puntuaciones, strict=True):
        # El score del cross-encoder sustituye al de RRF: son escalas distintas
        # y es éste el que el umbral de grounding compara después.
        frag.score = score

    return sorted(candidatos, key=lambda f: f.score, reverse=True)[:top_k]


def hay_evidencia(fragmentos: list[Fragmento]) -> bool:
    """¿Basta lo recuperado para responder, o el agente debe decir que no sabe?

    Grounding obligatorio: en seguimiento clínico, inventar es peor que callar.
    Si el mejor fragmento no llega al umbral, el agente responde "no tengo esa
    información" y ofrece escalar, en vez de improvisar sobre contexto pobre.
    """
    if not fragmentos:
        return False
    return fragmentos[0].score >= get_settings().min_relevance_score


def precalentar() -> None:
    if get_settings().rerank_enabled:
        _puntuar("precalentamiento", ["texto de precalentamiento"])
