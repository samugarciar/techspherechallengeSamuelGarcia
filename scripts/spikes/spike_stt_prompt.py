"""Spike 2b — ¿el sesgo de vocabulario rescata a Whisper `small`?

`small` es 2-3x más rápido que `medium` pero transcribió "apendicectomía" como
"appendicitomía". Whisper acepta `initial_prompt` para sesgar el decodificador
hacia un vocabulario. Si eso corrige los términos clínicos, nos quedamos con la
velocidad de `small` sin pagar el error.

Si funciona, el prompt clínico entra en producción como constante del STT.
"""

import statistics
import time
from pathlib import Path

import mlx_whisper

AUDIO = Path(__file__).parent / "audio"
RUNS = 3

# El léxico donde un modelo pequeño se rompe: procedimientos, fármacos,
# complicaciones. No es una frase, es un cebo de vocabulario.
PROMPT_CLINICO = (
    "Seguimiento postoperatorio. Procedimientos: apendicectomía, colecistectomía, "
    "herniorrafia inguinal, laparoscopia. Complicaciones: dehiscencia de la herida, "
    "hematoma, seroma, infección del sitio quirúrgico, fiebre. "
    "Medicamentos: cefalexina, acetaminofén, ibuprofeno, omeprazol, dipirona."
)

MODELS = [
    ("small", "mlx-community/whisper-small-mlx"),
    ("medium", "mlx-community/whisper-medium-mlx"),
]

# El clip que falló contiene "apendicectomía".
CLAVE = "apendicectomía"


def run(repo: str, clip: Path, prompt: str | None) -> tuple[float, str]:
    kwargs = {"path_or_hf_repo": repo, "language": "es"}
    if prompt:
        kwargs["initial_prompt"] = prompt
    mlx_whisper.transcribe(str(clip), **kwargs)  # warmup
    times, text = [], ""
    for _ in range(RUNS):
        t = time.perf_counter()
        out = mlx_whisper.transcribe(str(clip), **kwargs)
        times.append((time.perf_counter() - t) * 1000)
        text = out["text"].strip()
    return statistics.median(times), text


if __name__ == "__main__":
    clip = AUDIO / "test_es.wav"
    print(f"Clip: {clip.name}   término clave: «{CLAVE}»\n")

    for label, repo in MODELS:
        print(f"{'=' * 70}\n{label}\n{'=' * 70}")
        for tag, prompt in (("SIN prompt", None), ("CON prompt clínico", PROMPT_CLINICO)):
            ms, text = run(repo, clip, prompt)
            ok = "OK " if CLAVE.lower() in text.lower() else "MAL"
            print(f"  {tag:22} {ms:6.0f} ms   [{ok}]")
            print(f"  {'':22} {text[:110]}")
        print()
