"""Docling contra PyMuPDF sobre los MISMOS PDF, con números.

El plan de la Fase 0 daba por buena a Docling («conserva la jerarquía»). Esto
existe para comprobarlo en vez de creerlo, porque el coste que Docling cobra por
esa jerarquía —modelos de layout residentes— se paga en una máquina de 16 GB que
ya comparte Whisper, bge-m3 y el reranker con el worker de ingesta.

Mide tres cosas y no una: segundos por documento, RSS pico y calidad de la
jerarquía. Un motor que tarde el doble pero saque las secciones bien gana; uno
que sea instantáneo y aplane el documento no sirve para nada, porque sin
encabezados el troceado degrada a un splitter por frases y las citas del agente
desaparecen.

Cada motor se mide en un PROCESO APARTE. Con los dos en el mismo intérprete el
RSS pico sería el del que más gastara, y la pregunta que hay que responder es
exactamente cuánta memoria cuesta cada uno por separado.

    cd backend && uv run python ../scripts/spikes/spike_parsing.py
    cd backend && uv run python ../scripts/spikes/spike_parsing.py --motor pymupdf
"""

from __future__ import annotations

import argparse
import json
import re
import resource
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.rag import parsing  # noqa: E402

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "corpus_prueba"
_ENCABEZADO = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

MOTORES = {
    "pymupdf": parsing.pdf_con_pymupdf,
    "docling": parsing.con_docling,
}


def _rss_mb() -> float:
    """RSS pico del proceso. En macOS ru_maxrss viene en bytes, en Linux en KiB."""
    pico = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return pico / (1024 * 1024) if sys.platform == "darwin" else pico / 1024


def _canonico(titulo: str) -> str:
    """Título comparable entre motores.

    Docling reescribe la raya «—» como guion y PyMuPDF la conserva. Contar eso
    como encabezado perdido mediría la tipografía en vez de lo que se quiere
    medir, que es si el motor encuentra la sección. Se unifican guiones y
    espacios y se compara sin acentos de más ni mayúsculas.
    """
    t = unicodedata.normalize("NFKC", titulo)
    t = re.sub(r"[‐-―−-]+", "-", t)
    return re.sub(r"\s+", " ", t).strip().casefold()


def _jerarquia(markdown: str) -> list[tuple[int, str]]:
    return [(len(m.group(1)), _canonico(m.group(2))) for m in _ENCABEZADO.finditer(markdown)]


def _esperado(pdf: Path) -> list[tuple[int, str]]:
    """Verdad de referencia: los encabezados del .md del que salió el PDF."""
    fuente = pdf.with_suffix(".md")
    return _jerarquia(fuente.read_text(encoding="utf-8")) if fuente.exists() else []


def _comparar(obtenido: list[tuple[int, str]], esperado: list[tuple[int, str]]) -> dict:
    """Cuántos encabezados se recuperan y si la PROFUNDIDAD RELATIVA se conserva.

    Se compara la profundidad relativa, no el nivel absoluto: Docling exporta el
    título del documento como `##` en vez de `#`, y desplazar todos los niveles
    en bloque no rompe nada — `chunking` solo necesita saber quién cuelga de
    quién para construir la ruta «H2 › H3».
    """
    if not esperado:
        return {"aciertos": len(obtenido), "esperados": 0, "niveles_ok": None}

    por_titulo = {t: n for n, t in obtenido}
    base_obt = min((n for n, _ in obtenido), default=0)
    base_esp = min(n for n, _ in esperado)

    aciertos = 0
    niveles_ok = 0
    for nivel, titulo in esperado:
        if titulo in por_titulo:
            aciertos += 1
            if por_titulo[titulo] - base_obt == nivel - base_esp:
                niveles_ok += 1

    return {"aciertos": aciertos, "esperados": len(esperado), "niveles_ok": niveles_ok}


def medir(motor: str) -> dict:
    """Modo hijo: mide un solo motor y devuelve el informe como dict."""
    parsear = MOTORES[motor]
    pdfs = sorted(CORPUS.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no hay PDFs en {CORPUS} — corre antes eval/corpus_prueba/generar.py")

    # El arranque se mide aparte porque es coste que se paga UNA vez por proceso:
    # mezclarlo con el primer documento haría parecer que Docling tarda 14 s por
    # documento cuando en realidad tarda 11 s en arrancar y 3 s en trabajar.
    t0 = time.perf_counter()
    parsear(pdfs[0])
    arranque = time.perf_counter() - t0

    documentos = []
    for pdf in pdfs:
        t0 = time.perf_counter()
        doc = parsear(pdf)
        segundos = time.perf_counter() - t0
        documentos.append(
            {
                "archivo": pdf.name,
                "s": round(segundos, 3),
                "chars": len(doc.markdown),
                "paginas": doc.paginas,
                **_comparar(_jerarquia(doc.markdown), _esperado(pdf)),
            }
        )

    return {
        "motor": motor,
        "arranque_s": round(arranque, 2),
        "rss_mb": round(_rss_mb(), 1),
        "s_por_doc": round(sum(d["s"] for d in documentos) / len(documentos), 3),
        "documentos": documentos,
    }


def _lanzar_hijo(motor: str) -> dict:
    salida = subprocess.run(
        [sys.executable, __file__, "--motor", motor, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if salida.returncode != 0:
        print(salida.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"el motor {motor} falló")
    # La última línea es el JSON; lo anterior puede ser ruido de las librerías.
    return json.loads(salida.stdout.strip().splitlines()[-1])


def informe(resultados: list[dict]) -> None:
    print("\n## Por documento\n")
    print("| Motor | Documento | s | chars | pág. | Encabezados | Niveles ok |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for r in resultados:
        for d in r["documentos"]:
            print(
                f"| {r['motor']} | {d['archivo']} | {d['s']:.3f} | {d['chars']} | "
                f"{d['paginas']} | {d['aciertos']}/{d['esperados']} | "
                f"{d['niveles_ok']}/{d['esperados']} |"
            )

    print("\n## Resumen\n")
    print("| Motor | s/doc | Arranque | RSS pico | Encabezados | Niveles ok |")
    print("|---|---:|---:|---:|---:|---:|")
    for r in resultados:
        aciertos = sum(d["aciertos"] for d in r["documentos"])
        total = sum(d["esperados"] for d in r["documentos"])
        niveles = sum(d["niveles_ok"] or 0 for d in r["documentos"])
        print(
            f"| {r['motor']} | {r['s_por_doc']:.3f} s | {r['arranque_s']:.2f} s | "
            f"{r['rss_mb']:.0f} MB | {aciertos}/{total} | {niveles}/{total} |"
        )

    rapido = min(resultados, key=lambda r: r["s_por_doc"])
    ligero = min(resultados, key=lambda r: r["rss_mb"])
    lento = max(resultados, key=lambda r: r["s_por_doc"])
    pesado = max(resultados, key=lambda r: r["rss_mb"])
    print(
        f"\n{rapido['motor']} es {lento['s_por_doc'] / max(rapido['s_por_doc'], 1e-6):.0f}× "
        f"más rápido; {ligero['motor']} usa "
        f"{pesado['rss_mb'] - ligero['rss_mb']:.0f} MB menos de RSS pico."
    )
    print("Si los encabezados empatan, decide la memoria: el worker de ingesta corre")
    print("al lado del pipeline de voz en la misma máquina de 16 GB.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--motor", choices=sorted(MOTORES), help="mide solo este motor")
    ap.add_argument("--json", action="store_true", help="salida cruda para el proceso padre")
    args = ap.parse_args()

    if args.motor:
        r = medir(args.motor)
        print(json.dumps(r) if args.json else json.dumps(r, indent=2, ensure_ascii=False))
        return

    informe([_lanzar_hijo(m) for m in sorted(MOTORES)])


if __name__ == "__main__":
    main()
