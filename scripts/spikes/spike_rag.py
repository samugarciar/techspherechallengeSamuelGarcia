"""Spike 3 — latencia de embeddings y reranker sobre MPS.

Preguntas que responde:
  a) ¿Cuánto cuesta embeber la *query* en el camino de voz? (se paga por turno)
  b) ¿Cabe el reranker cross-encoder en el presupuesto, o se queda fuera?

Nota de memoria: esta máquina tiene 16 GB unificados compartidos con Whisper,
Kokoro y Postgres. Se mide también la RSS del proceso para saber cuánto queda.
"""

import gc
import statistics
import time

import torch
from sentence_transformers import CrossEncoder, SentenceTransformer

DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
RUNS = 5

QUERY = "¿cuándo puedo ducharme después de la operación?"

# Pasajes plausibles de un protocolo postoperatorio: el reranker debe subir el
# primero y hundir el último.
PASSAGES = [
    "Puede ducharse a partir de las 48 horas de la intervención, secando la herida "
    "con una toalla limpia sin frotar.",
    "Mantenga el apósito seco. Si se moja, cámbielo lo antes posible.",
    "La cefalexina de 500 mg se toma cada 8 horas durante siete días.",
    "El dolor leve es normal durante la primera semana tras la cirugía.",
    "Acuda a urgencias si presenta fiebre superior a 38.5 grados.",
    "La cita de revisión será a los diez días en consultas externas.",
    "No levante peso superior a cinco kilos durante las primeras dos semanas.",
    "El aparcamiento del hospital tiene tarifa reducida para pacientes.",
]


def rss_mb() -> float:
    import resource

    # En macOS ru_maxrss viene en bytes
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def bench_embeddings(model_id: str, dims_expected: int) -> None:
    print(f"\n{'=' * 68}\nEMBEDDINGS  {model_id}\n{'=' * 68}")
    t = time.perf_counter()
    try:
        model = SentenceTransformer(model_id, device=DEVICE)
    except Exception as exc:  # noqa: BLE001
        print(f"  FALLÓ al cargar: {exc}")
        return
    print(f"  carga: {time.perf_counter() - t:.1f}s   RSS: {rss_mb():.0f} MB")

    dim = model.get_sentence_embedding_dimension()
    flag = "OK" if dim == dims_expected else f"¡OJO! schema espera vector({dims_expected})"
    print(f"  dimensiones: {dim}  [{flag}]")

    model.encode([QUERY], normalize_embeddings=True)  # warmup

    # (a) query única — esto se paga en CADA turno de voz
    times = []
    for _ in range(RUNS):
        t = time.perf_counter()
        model.encode([QUERY], normalize_embeddings=True)
        times.append((time.perf_counter() - t) * 1000)
    print(f"  query (1 texto)   : {statistics.median(times):6.1f} ms   <-- camino de voz")

    # (b) lote de ingesta — fuera del camino de voz, solo informativo
    batch = PASSAGES * 8  # 64 chunks
    t = time.perf_counter()
    model.encode(batch, batch_size=16, normalize_embeddings=True)
    el = (time.perf_counter() - t) * 1000
    print(f"  ingesta (64 chunks): {el:6.0f} ms  ({el / len(batch):.1f} ms/chunk)")

    del model
    gc.collect()


def bench_reranker(model_id: str) -> None:
    print(f"\n{'=' * 68}\nRERANKER  {model_id}\n{'=' * 68}")
    t = time.perf_counter()
    try:
        ce = CrossEncoder(model_id, device=DEVICE, max_length=512)
    except Exception as exc:  # noqa: BLE001
        print(f"  FALLÓ al cargar: {exc}")
        return
    print(f"  carga: {time.perf_counter() - t:.1f}s   RSS: {rss_mb():.0f} MB")

    for n in (8, 20):
        pairs = [(QUERY, p) for p in (PASSAGES * 3)[:n]]
        ce.predict(pairs)  # warmup
        times = []
        for _ in range(RUNS):
            t = time.perf_counter()
            scores = ce.predict(pairs)
            times.append((time.perf_counter() - t) * 1000)
        med = statistics.median(times)
        verdict = "cabe" if med < 250 else "NO cabe en el camino de voz"
        print(f"  top-{n:<2} candidatos : {med:6.0f} ms   [{verdict}]")

    # ¿Ordena bien? El pasaje 0 responde literalmente la pregunta.
    scores = ce.predict([(QUERY, p) for p in PASSAGES])
    ranked = sorted(zip(scores, PASSAGES, strict=True), reverse=True, key=lambda x: x[0])
    print("\n  ranking (debe ganar el de la ducha a las 48 h):")
    for s, p in ranked[:3]:
        print(f"    {s:+.3f}  {p[:66]}")
    print(f"    ...\n    {ranked[-1][0]:+.3f}  {ranked[-1][1][:66]}   <-- peor")

    del ce
    gc.collect()


if __name__ == "__main__":
    print(f"device = {DEVICE}")
    bench_embeddings("BAAI/bge-m3", 1024)
    bench_embeddings("intfloat/multilingual-e5-large", 1024)
    bench_reranker("BAAI/bge-reranker-v2-m3")
    bench_reranker("jinaai/jina-reranker-v2-base-multilingual")
    print(f"\nRSS pico del proceso: {rss_mb():.0f} MB  (de 16 GB compartidos)")
