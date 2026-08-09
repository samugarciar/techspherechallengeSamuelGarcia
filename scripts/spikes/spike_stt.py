"""Spike 2 — latencia de Whisper en Apple Silicon.

Pregunta que responde: ¿cabe `large-v3-turbo` en el presupuesto de latencia de
una conversación de voz en un M4 de 16 GB, o hay que bajar a `medium`?

Referencia: en un turno conversacional la transcripción arranca cuando el VAD
detecta fin de habla, así que su latencia se suma íntegra al tiempo de respuesta
percibido. Objetivo: < 300 ms para un turno típico de 3-5 s.
"""

import statistics
import time
import wave
from pathlib import Path

import mlx_whisper

AUDIO_DIR = Path(__file__).parent / "audio"
RUNS = 3

MODELS = [
    ("large-v3-turbo", "mlx-community/whisper-large-v3-turbo"),
    ("medium", "mlx-community/whisper-medium-mlx"),
    ("small", "mlx-community/whisper-small-mlx"),
]


def duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def bench(label: str, repo: str, clips: list[Path]) -> None:
    print(f"\n{'=' * 68}\n{label}  ({repo})\n{'=' * 68}")

    # Primera pasada: incluye descarga + carga del modelo. No cuenta como latencia.
    t0 = time.perf_counter()
    try:
        mlx_whisper.transcribe(str(clips[0]), path_or_hf_repo=repo, language="es")
    except Exception as exc:  # noqa: BLE001
        print(f"  FALLÓ: {exc}")
        return
    print(f"  carga + primera pasada (frío): {time.perf_counter() - t0:.2f}s")

    for clip in clips:
        secs = duration(clip)
        times, text = [], ""
        for _ in range(RUNS):
            t = time.perf_counter()
            out = mlx_whisper.transcribe(str(clip), path_or_hf_repo=repo, language="es")
            times.append((time.perf_counter() - t) * 1000)
            text = out["text"].strip()

        med = statistics.median(times)
        print(f"\n  clip {clip.name}  ({secs:.2f}s de audio)")
        print(f"    mediana : {med:7.0f} ms   (min {min(times):.0f} / max {max(times):.0f})")
        print(f"    RTF     : {med / 1000 / secs:7.2f}x   (<1 = más rápido que tiempo real)")
        print(f"    texto   : {text[:100]}")


if __name__ == "__main__":
    clips = sorted(AUDIO_DIR.glob("*.wav"))
    if not clips:
        raise SystemExit(f"No hay audio en {AUDIO_DIR}. Genera con: say -v Monica -o ... ")
    print(f"Clips: {[c.name for c in clips]}")
    for label, repo in MODELS:
        bench(label, repo, clips)
