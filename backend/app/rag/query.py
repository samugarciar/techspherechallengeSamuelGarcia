"""Consulta al RAG contra la base de datos, sin API y sin micrófono.

    cd backend && uv run python -m app.rag.query "¿cuándo puedo ducharme?"

Existe para depurar el RAG aislado de todo lo demás. Cuando el agente responde mal
hay cinco sospechosos —el STT transcribió otra cosa, el prompt del LLM, el
troceado, el retrieval, el reranker— y esta orden descarta tres de golpe: enseña
exactamente qué fragmentos habría visto el modelo, con qué cita, con qué score y
cuántos milisegundos costó cada etapa. Si lo que sale de aquí es correcto, el
problema está por encima; si no, está aquí.

Va directa a Postgres y no por `POST /api/rag/query` a propósito: no depende de
que el servidor esté levantado ni del token, así que sirve también cuando lo que
está roto es la API.

── La última etapa: revalidar ───────────────────────────────────────────────
Entre que se recuperan los candidatos y se devuelve la respuesta pasan ~120 ms
(el cross-encoder). En esa ventana el administrador puede haber borrado el
documento, y sin una comprobación final la respuesta llevaría fragmentos de un
protocolo que el hospital acaba de retirar. Ninguna de las dos garantías del
schema lo impide: el CASCADE borra las filas, pero la consulta que las leyó ya
había tomado su instantánea (READ COMMITTED: cada sentencia ve el estado que
había cuando ELLA empezó, no el de ahora). Por eso la última cosa que se hace
antes de devolver nada es volver a preguntarle a `retrievable_chunks` si esos
fragmentos siguen siendo recuperables. Ver `retrieval.revalidar()`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from time import perf_counter

from app.core.config import get_settings
from app.db.pool import close_pool, open_pool
from app.rag import embeddings
from app.rag.rerank import hay_evidencia, reordenar
from app.rag.rerank import precalentar as rerank_precalentar
from app.rag.retrieval import Fragmento, buscar, revalidar


@dataclass(slots=True)
class Resultado:
    fragmentos: list[Fragmento]
    ms: dict[str, int] = field(default_factory=dict)
    # Cuántos candidatos se cayeron en la revalidación final. Distinto de cero
    # significa que alguien borró un documento a mitad de esta consulta; es dato
    # de diagnóstico, no un error.
    retirados: int = 0

    @property
    def hay_evidencia(self) -> bool:
        return hay_evidencia(self.fragmentos)


async def consultar(texto: str, top_k: int | None = None) -> Resultado:
    """Pipeline completo de recuperación: embeber, buscar, reordenar, revalidar.

    Es el mismo camino que recorre `POST /api/rag/query` y el que debe recorrer el
    agente de voz. Tenerlo en una función y no repetido en cada llamante es lo que
    evita que uno de los tres se olvide de la revalidación final y acabe citando
    un documento borrado.
    """
    ajustes = get_settings()
    top_k = top_k or ajustes.context_top_k

    t0 = perf_counter()
    vector = await embeddings.embeber_consulta(texto)
    t1 = perf_counter()
    # Más candidatos de los que se devuelven: el cross-encoder necesita entre qué
    # elegir. Nunca menos de los pedidos.
    candidatos = await buscar(texto, vector, max(ajustes.retrieve_top_k, top_k))
    t2 = perf_counter()
    reordenados = await reordenar(texto, candidatos, top_k)
    t3 = perf_counter()
    fragmentos = await revalidar(reordenados)
    t4 = perf_counter()

    return Resultado(
        fragmentos=fragmentos,
        ms={
            "embedding": round((t1 - t0) * 1000),
            "retrieval": round((t2 - t1) * 1000),
            "rerank": round((t3 - t2) * 1000),
            "revalidacion": round((t4 - t3) * 1000),
            "total": round((t4 - t0) * 1000),
        },
        retirados=len(reordenados) - len(fragmentos),
    )


# ---------------------------------------------------------------------------
# Línea de órdenes
# ---------------------------------------------------------------------------
def _imprimir(consulta: str, resultado: Resultado) -> None:
    ms = resultado.ms
    print(f"\n  consulta   {consulta!r}")
    print(f"  evidencia  {resultado.hay_evidencia}   "
          f"(umbral {get_settings().min_relevance_score})")
    print("  latencia   " + "  ".join(f"{etapa} {valor} ms" for etapa, valor in ms.items()))
    if resultado.retirados:
        print(f"  ATENCIÓN   {resultado.retirados} fragmento(s) se borraron "
              f"durante la consulta y se descartaron")

    if not resultado.fragmentos:
        print("\n  (ningún fragmento — el agente responderá que no tiene esa información)")
        return

    for n, f in enumerate(resultado.fragmentos, start=1):
        print(f"\n  {n}. [{f.score:.4f}] {f.cita()}")
        procedencia = []
        if f.dense_rank:
            procedencia.append(f"denso #{f.dense_rank}")
        if f.lexical_rank:
            procedencia.append(f"léxico #{f.lexical_rank}")
        if procedencia:
            print(f"     ({', '.join(procedencia)})")
        for linea in f.content.strip().splitlines():
            print(f"     {linea}")


def _como_json(resultado: Resultado) -> str:
    return json.dumps(
        {
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
                for f in resultado.fragmentos
            ],
            "hay_evidencia": resultado.hay_evidencia,
            "ms": resultado.ms,
        },
        ensure_ascii=False,
        indent=2,
    )


async def _correr(consulta: str, top_k: int | None, en_json: bool, frio: bool) -> int:
    await open_pool()
    try:
        if not frio:
            # Los modelos se cargan ANTES de cronometrar. Sin esto la primera (y
            # única) consulta de la orden mide 10 s de embedding y 6 s de rerank
            # —el peso de bge-m3 y del cross-encoder entrando en memoria— y el
            # desglose de `ms` no se parece en nada al presupuesto de latencia que
            # esta herramienta existe para verificar. Con `--frio` se mide lo otro,
            # que también interesa: lo que le cuesta al worker arrancar en seco.
            t0 = perf_counter()
            await asyncio.to_thread(embeddings.precalentar)
            await asyncio.to_thread(rerank_precalentar)
            if not en_json:
                print(f"  modelos cargados en {perf_counter() - t0:.1f} s")

        resultado = await consultar(consulta, top_k)
    finally:
        await close_pool()

    print(_como_json(resultado) if en_json else "", end="")
    if not en_json:
        _imprimir(consulta, resultado)
    # Código de salida distinto de cero cuando no hay evidencia: así se puede
    # encadenar en un script de comprobación («esta pregunta TIENE que tener
    # respuesta después de subir el protocolo»).
    return 0 if resultado.hay_evidencia else 1


def cli() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m app.rag.query",
        description="Consulta el RAG directamente contra Postgres, sin pasar por la API.",
    )
    ap.add_argument("consulta", help="la pregunta, tal cual la haría el paciente")
    ap.add_argument("--top-k", type=int, default=None,
                    help="fragmentos a devolver (por defecto CONTEXT_TOP_K)")
    ap.add_argument("--json", action="store_true",
                    help="salida en JSON, con la misma forma que POST /api/rag/query")
    ap.add_argument("--frio", action="store_true",
                    help="no precargar los modelos: los ms incluyen su carga (~16 s)")
    args = ap.parse_args()

    sys.exit(asyncio.run(_correr(args.consulta, args.top_k, args.json, args.frio)))


if __name__ == "__main__":
    cli()
