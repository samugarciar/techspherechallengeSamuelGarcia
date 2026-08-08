"""Reconocimiento de voz local — Whisper sobre MLX (Apple Silicon).

Decisión de la Fase 0, medida en M4 con clips de 2-8 s:

    modelo                  latencia   "apendicectomía"
    small  sin prompt         436 ms   MAL ("appendicitomía")
    small  + prompt clínico   481 ms   OK
    medium sin prompt        1222 ms   OK
    large-v3-turbo           1257 ms   OK

45 ms de sesgo de vocabulario compran la precisión de `medium` a 2.5x su
velocidad. En un turno de voz la transcripción se paga entera, así que ese
ahorro de ~740 ms es la diferencia entre una conversación viva y una muerta.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

import mlx_whisper

from app.core.config import get_settings

# Cebo de vocabulario para el decodificador. No es una instrucción ni una frase
# de ejemplo: es la lista de términos donde un modelo pequeño se rompe.
# Ampliar cuando aparezcan errores nuevos en transcripciones reales.
VOCABULARIO_CLINICO = (
    "Seguimiento postoperatorio. Procedimientos: apendicectomía, colecistectomía, "
    "herniorrafia inguinal, laparoscopia, histerectomía, artroscopia. "
    "Complicaciones: dehiscencia de la herida, hematoma, seroma, eritema, "
    "infección del sitio quirúrgico, fiebre, supuración. "
    "Medicamentos: cefalexina, acetaminofén, ibuprofeno, omeprazol, dipirona, "
    "enoxaparina, tramadol."
)


@dataclass(slots=True)
class Transcripcion:
    texto: str
    idioma: str
    duracion_ms: float


class WhisperSTT:
    """Envuelve mlx-whisper. Sin estado entre llamadas: cada turno es independiente."""

    def __init__(self, modelo: str | None = None, prompt: str | None = None) -> None:
        s = get_settings()
        self.modelo = modelo or s.stt_model
        self.prompt = VOCABULARIO_CLINICO if prompt is None else prompt

    def _transcribir_sync(self, audio: str | Path) -> Transcripcion:
        import time

        t0 = time.perf_counter()
        out = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=self.modelo,
            language="es",
            initial_prompt=self.prompt,
        )
        return Transcripcion(
            texto=out["text"].strip(),
            idioma=out.get("language", "es"),
            duracion_ms=(time.perf_counter() - t0) * 1000,
        )

    async def transcribir(self, audio: str | Path) -> Transcripcion:
        """MLX bloquea el hilo; se saca del event loop para no congelar el pipeline."""
        return await asyncio.to_thread(self._transcribir_sync, audio)

    def precalentar(self) -> None:
        """Carga el modelo en memoria al arrancar.

        Sin esto, la primera transcripción de la demo paga ~2 s de carga y el
        arranque de la llamada se siente roto.
        """
        import numpy as np
        import soundfile as sf

        tmp = Path("/tmp/_warmup_stt.wav")
        sf.write(tmp, np.zeros(16000, dtype="float32"), 16000)
        try:
            self._transcribir_sync(tmp)
        finally:
            tmp.unlink(missing_ok=True)
