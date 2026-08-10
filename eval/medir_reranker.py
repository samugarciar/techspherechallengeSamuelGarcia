"""¿Merece la pena el reranker, y a qué coste?

    cd backend && DATABASE_URL=…postop_t5 uv run python ../eval/medir_reranker.py

Existe porque el presupuesto de latencia de la Fase 0 estaba mal. Aquel spike
midió el cross-encoder con pasajes de 250 caracteres y publicó 114 ms; los
fragmentos reales tienen entre 500 y 1400, el coste escala con la longitud, y
medido contra la API real son 585 ms. Cinco veces por encima de lo presupuestado,
y en el camino de voz eso se oye.

Así que hay que responder dos preguntas por separado, porque tienen respuestas
distintas: cuánto CUESTA rerankear, y cuánto MEJORA. Lo primero se mide fácil.
Lo segundo necesita saber qué fragmento es el correcto para cada pregunta, y por
eso este script trae su propio conjunto de preguntas con la sección esperada.

Aviso sobre el alcance: el corpus provisional son 3 protocolos sintéticos. Con
tan pocos documentos el retrieval denso casi no se equivoca, así que el reranker
tiene poco margen para lucirse. Este script está escrito para volver a lanzarlo
con el corpus real y decidir entonces con datos que sí signifiquen algo.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.pool import close_pool, open_pool  # noqa: E402
from app.rag import embeddings, retrieval  # noqa: E402

# Pregunta -> qué documento DEBE responderla. La gracia está en que las tres
# cirugías comparten vocabulario ("herida", "fiebre", "cita"), así que acertar
# el documento exige entender de qué operación habla el paciente, no solo
# reconocer el tema.
def cargar_casos() -> list[tuple[str, str]]:
    import json
    path_golden = Path(__file__).resolve().parent / "golden_set_rag.json"
    if path_golden.exists():
        try:
            data = json.loads(path_golden.read_text("utf-8"))
            return [(q["pregunta"], q["documento_esperado"].replace(".pdf", "")) for q in data]
        except Exception:
            pass
    return CASOS


def _acierta(frags, esperado: str) -> bool:
    return bool(frags) and esperado in frags[0].filename


def _en_contexto(frags, esperado: str, k: int) -> bool:
    """¿Llega el documento correcto al LLM aunque no sea el primero?

    Es la métrica que de verdad importa: al modelo le llegan `context_top_k`
    fragmentos, y con que el bueno esté entre ellos puede responder. Exigir que
    sea el primero es más severo de lo que el sistema necesita.
    """
    return any(esperado in f.filename for f in frags[:k])


async def main() -> int:
    s = get_settings()
    await open_pool()
    try:
        from app.rag import rerank

        casos = cargar_casos()
        print(f"corpus: {len(casos)} preguntas · reranker {s.rerank_model}\n")

        # --- Cuánto mejora ---------------------------------------------------
        sin_top1 = sin_ctx = con_top1 = con_ctx = 0
        t_busq, t_rer = [], []

        for consulta, esperado in casos:
            qv = await embeddings.embeber_consulta(consulta)

            t0 = time.perf_counter()
            cand = await retrieval.buscar(consulta, qv, top_k=s.retrieve_top_k)
            t_busq.append((time.perf_counter() - t0) * 1000)

            sin_top1 += _acierta(cand, esperado)
            sin_ctx += _en_contexto(cand, esperado, s.context_top_k)

            t0 = time.perf_counter()
            ordenados = await rerank.reordenar(consulta, cand, top_k=s.context_top_k)
            t_rer.append((time.perf_counter() - t0) * 1000)

            con_top1 += _acierta(ordenados, esperado)
            con_ctx += _en_contexto(ordenados, esperado, s.context_top_k)

        n = len(casos)
        print(f"{'':<26} {'doc correcto 1º':>16} {f'correcto en top-{s.context_top_k}':>18} {'ms':>8}")
        print(f"{'híbrido sin reranker':<26} {f'{sin_top1}/{n}':>16} {f'{sin_ctx}/{n}':>18} "
              f"{statistics.median(t_busq):>7.0f}")
        print(f"{'híbrido + reranker':<26} {f'{con_top1}/{n}':>16} {f'{con_ctx}/{n}':>18} "
              f"{statistics.median(t_busq) + statistics.median(t_rer):>7.0f}")
        print(f"\ncoste aislado del reranker: {statistics.median(t_rer):.0f} ms "
              f"(top-{s.retrieve_top_k} candidatos, max_length {512})")

        ganancia = con_ctx - sin_ctx
        coste = statistics.median(t_rer)
        print(f"\nveredicto: {ganancia:+d} aciertos por {coste:.0f} ms")
        if ganancia <= 0:
            print("  El reranker no mejora nada sobre ESTE corpus y cuesta el mayor")
            print("  bloque de latencia del pipeline. Sobre 3 protocolos sintéticos")
            print("  el híbrido ya casi no falla, así que esto NO demuestra que sobre:")
            print("  demuestra que este corpus no sirve para decidirlo. Volver a lanzarlo")
            print("  con los documentos reales antes de fijar RERANK_ENABLED.")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
