"""Demuestra el requisito central del enunciado contra la API real, sin micrófono.

    python scripts/demo_aprender_olvidar.py

Sube un protocolo, espera a que el agente lo aprenda, le pregunta algo que solo
ese documento puede responder, lo borra, y vuelve a preguntar lo mismo. Si el
sistema es correcto, la segunda respuesta no tiene evidencia y el agente diría
«no tengo esa información» en vez de improvisar — que en dominio clínico es la
única respuesta aceptable cuando no se sabe.

Es el guion de la demo y a la vez la prueba de regresión: si alguien rompe la
garantía de olvido, esto se pone en rojo.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parents[1]
PREGUNTA = "¿cuándo puedo ducharme después de la operación?"


def _consultar(c: httpx.Client, api: str, momento: str) -> dict:
    r = c.post(f"{api}/rag/query", json={"consulta": PREGUNTA, "top_k": 3})
    r.raise_for_status()
    d = r.json()
    ms = d["ms"]
    print(f"\n  {momento}")
    print(f"    evidencia: {d['hay_evidencia']}   fragmentos: {len(d['fragmentos'])}")
    print(f"    latencia:  {ms['total']} ms  (embedding {ms['embedding']}, "
          f"retrieval {ms['retrieval']}, rerank {ms['rerank']})")
    for f in d["fragmentos"][:2]:
        print(f"    [{f['score']:.3f}] {f['cita']}")
        print(f"            {f['contenido'][:88].strip()}…")
    if not d["fragmentos"]:
        print("    (ninguno — el agente responderá que no tiene esa información)")
    return d


def main() -> int:
    p = argparse.ArgumentParser()
    # 8000 es el puerto de la instrucción de arranque del README. No inventar
    # otro aquí: un guion de demo que apunta a un puerto distinto del que
    # documenta el proyecto falla en el paso 1 con un muro de traza de httpx, y
    # el fallo no se parece en nada a su causa.
    p.add_argument("--api", default="http://localhost:8000/api")
    p.add_argument("--token", default="cambiar-esto-en-local")
    p.add_argument("--doc", default=str(RAIZ / "eval/corpus_prueba/protocolo_apendicectomia.pdf"))
    a = p.parse_args()

    with httpx.Client(headers={"X-Admin-Token": a.token}, timeout=60.0) as c:
        # Comprobar que hay alguien al otro lado ANTES de subir nada. Sin esto,
        # el primer error visible es una excepción de conexión a media subida.
        try:
            salud = c.get(f"{a.api}/health", timeout=5.0).json()
        except httpx.HTTPError:
            print(f"No hay ninguna API escuchando en {a.api}.\n"
                  f"Levántala con:\n"
                  f"  cd backend && DATABASE_URL=postgresql://postop:postop@"
                  f"localhost:5433/postop_wt uv run uvicorn app.main:app --port 8000\n"
                  f"Y el worker en otra terminal:\n"
                  f"  cd backend && DATABASE_URL=… uv run python -m app.workers.ingest_worker")
            return 1
        if not salud.get("modelos_listos"):
            print("  (aviso: bge-m3 y el reranker aún se están cargando; la primera\n"
                  "   consulta irá lenta, pero el resultado es el mismo)\n")

        print("1. SUBIR")
        with open(a.doc, "rb") as fh:
            r = c.post(f"{a.api}/documents", files={"file": (Path(a.doc).name, fh)})
        r.raise_for_status()
        doc = r.json()
        print(f"    {doc['filename']}  ->  {doc['status']}  ({doc['size_bytes']:,} bytes)")

        print("\n2. APRENDER")
        t0, visto = time.time(), None
        while time.time() - t0 < 180:
            d = c.get(f"{a.api}/documents/{doc['id']}").json()
            clave = (d["status"], d["chunks_count"], d["embedded_count"])
            if clave != visto:
                print(f"    {time.time() - t0:5.1f}s  {d['status']:<10} "
                      f"fragmentos {d['embedded_count']}/{d['chunks_count']}")
                visto = clave
            if d["status"] == "ready":
                break
            if d["status"] == "failed":
                print(f"    FALLÓ: {d['error']}")
                return 1
            time.sleep(0.5)

        antes = _consultar(c, a.api, "3. PREGUNTAR — con el documento presente")

        print("\n4. OLVIDAR")
        borrado = c.delete(f"{a.api}/documents/{doc['id']}").json()
        print(f"    olvidado={borrado['olvidado']}  "
              f"fragmentos eliminados={borrado['chunks_eliminados']}")

        despues = _consultar(c, a.api, "5. LA MISMA PREGUNTA — ya borrado")

    ok = antes["hay_evidencia"] and not despues["hay_evidencia"] and not despues["fragmentos"]
    print("\n" + ("  ✓ GARANTÍA CUMPLIDA: lo aprendió y lo olvidó."
                  if ok else
                  "  ✗ GARANTÍA ROTA — revisar antes de demostrar nada."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
