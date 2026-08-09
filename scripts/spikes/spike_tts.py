"""Spike 1 — A/B de TTS en español.

Éste es el riesgo más visible del proyecto: si la voz suena mal, la demo sufre
por mucho que el RAG sea impecable. Se resuelve el día 1, por oído, no el día 9.

Genera la MISMA frase clínica con los tres motores y mide el tiempo hasta el
primer audio (lo que el paciente percibe como "tarda en contestar").

Salida en scripts/spikes/out/ — escuchar con:  afplay scripts/spikes/out/<f>.wav
"""

import subprocess
import time
import wave
from pathlib import Path

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

# Frase representativa: saludo + término clínico + pregunta. Los números y
# "apendicectomía" son justo donde los TTS pequeños se rompen.
FRASE = (
    "Buenos días, le llamo del hospital para el seguimiento de su apendicectomía. "
    "¿Ha tenido fiebre por encima de treinta y ocho grados en las últimas cuarenta y ocho horas?"
)

# En voz real no se sintetiza el párrafo entero: se trocea por frase para que el
# primer audio salga antes. Esto mide esa primera frase.
PRIMERA_FRASE = "Buenos días, le llamo del hospital para el seguimiento de su apendicectomía."


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:  # noqa: BLE001
        return 0.0


RUNS = 3


def medir(fn, path: Path) -> float:
    """Calienta y luego mide la mediana.

    Sin el calentamiento la primera llamada de cada motor incluye la carga de
    pesos y sale peor que las siguientes — que fue justo el artefacto que
    ensució la primera pasada de este spike.
    """
    import statistics

    fn()  # calentamiento, descartado
    tiempos = []
    for _ in range(RUNS):
        t = time.perf_counter()
        fn()
        tiempos.append((time.perf_counter() - t) * 1000)
    return statistics.median(tiempos)


def report(motor: str, path: Path, elapsed_ms: float) -> None:
    secs = wav_seconds(path)
    rtf = (elapsed_ms / 1000 / secs) if secs else float("nan")
    print(f"  {motor:34} {elapsed_ms:7.0f} ms  ->  {secs:.2f}s audio  (RTF {rtf:.2f}x)")


def bench_say() -> None:
    """Baseline: voz del sistema macOS. Cero dependencias, siempre funciona."""
    print("\n--- macOS say (Mónica, es_ES) ---")
    for label, texto in (("frase1", PRIMERA_FRASE), ("completa", FRASE)):
        out = OUT / f"say_{label}.wav"

        def sintetizar(o=out, t=texto):
            subprocess.run(
                ["say", "-v", "Monica", "-o", str(o), "--data-format=LEI16@24000", t],
                check=True,
            )

        report(f"say/{label}", out, medir(sintetizar, out))


def bench_kokoro() -> None:
    print("\n--- Kokoro-82M (es) ---")
    try:
        import soundfile as sf
        from kokoro import KPipeline
    except Exception as exc:  # noqa: BLE001
        print(f"  NO DISPONIBLE: {exc}")
        return

    t = time.perf_counter()
    try:
        # lang_code 'e' = español en misaki
        pipe = KPipeline(lang_code="e")
    except Exception as exc:  # noqa: BLE001
        print(f"  FALLÓ al inicializar: {exc}")
        return
    print(f"  carga del pipeline: {time.perf_counter() - t:.1f}s")

    import numpy as np

    for voz in ("ef_dora", "em_alex"):
        for label, texto in (("frase1", PRIMERA_FRASE), ("completa", FRASE)):
            out = OUT / f"kokoro_{voz}_{label}.wav"

            def sintetizar(o=out, t=texto, v=voz, lb=label):
                audio = []
                for _, _, chunk in pipe(t, voice=v, speed=1.0):
                    audio.append(chunk)
                    if lb == "frase1":
                        break  # hasta el PRIMER chunk: lo que importa en voz
                if audio:
                    sf.write(o, np.concatenate(audio), 24000)

            try:
                report(f"kokoro/{voz}/{label}", out, medir(sintetizar, out))
            except Exception as exc:  # noqa: BLE001
                print(f"  kokoro/{voz}/{label} FALLÓ: {exc}")


def bench_piper() -> None:
    print("\n--- Piper (es_ES) ---")
    voces = list(Path.home().glob(".local/share/piper/*.onnx")) + list(
        (Path(__file__).parent / "piper_voices").glob("*.onnx")
    )
    if not voces:
        print("  SIN MODELO. Descargar una voz es_ES, p.ej.:")
        print("    mkdir -p scripts/spikes/piper_voices && cd scripts/spikes/piper_voices")
        print("    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "es/es_ES/davefx/medium/es_ES-davefx-medium.onnx")
        print("    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "es/es_ES/davefx/medium/es_ES-davefx-medium.onnx.json")
        return

    for modelo in voces[:2]:
        for label, texto in (("frase1", PRIMERA_FRASE), ("completa", FRASE)):
            out = OUT / f"piper_{modelo.stem}_{label}.wav"

            def sintetizar(o=out, t=texto, m=modelo):
                subprocess.run(
                    ["piper", "-m", str(m), "-f", str(o)],
                    input=t.encode(), check=True, capture_output=True,
                )

            try:
                report(f"piper/{modelo.stem}/{label}", out, medir(sintetizar, out))
            except Exception as exc:  # noqa: BLE001
                print(f"  piper/{modelo.stem}/{label} FALLÓ: {exc}")


if __name__ == "__main__":
    print(f"Frase de prueba:\n  {FRASE}\n")
    bench_say()
    bench_kokoro()
    bench_piper()
    print(f"\n{'=' * 70}")
    print("Escuchar y decidir POR OÍDO (el RTF solo descarta lo inviable):")
    print(f"  for f in {OUT}/*completa*.wav; do echo \"$f\"; afplay \"$f\"; done")
    print("=" * 70)
