"""Embeddings locales sobre MPS (Apple Silicon).

El modelo se carga una sola vez y se reutiliza. En una máquina de 16 GB
compartidos con Whisper y Postgres, cargarlo por petición sería fatal.

Nota sobre bge-m3: NO usa prefijos de instrucción ("query:" / "passage:"), a
diferencia de la familia e5. Si se cambia el modelo a multilingual-e5-*, hay que
añadir esos prefijos o la calidad de recuperación cae de forma silenciosa.
"""

import asyncio
import threading

import numpy as np

from app.core.config import get_settings

_modelo = None
_lock = threading.Lock()


def _cargar():
    """Carga perezosa y protegida: varias corrutinas pueden pedirlo a la vez."""
    global _modelo
    if _modelo is None:
        with _lock:
            if _modelo is None:
                import torch
                from sentence_transformers import SentenceTransformer

                s = get_settings()
                device = "mps" if torch.backends.mps.is_available() else "cpu"
                m = SentenceTransformer(s.embedding_model, device=device)

                # sentence-transformers 5.x renombró el método; se soportan ambos
                # para no atarse a una versión concreta de la librería.
                obtener_dim = getattr(
                    m, "get_embedding_dimension", None
                ) or m.get_sentence_embedding_dimension
                dim = obtener_dim()
                if dim != s.embedding_dim:
                    raise RuntimeError(
                        f"{s.embedding_model} produce {dim} dimensiones pero el schema "
                        f"declara vector({s.embedding_dim}). Cambiar el modelo exige una "
                        f"migración de la columna chunks.embedding y RE-EMBEBER todo el "
                        f"corpus: los vectores viejos no son comparables con los nuevos."
                    )
                _modelo = m
    return _modelo


def _encode(textos: list[str], batch_size: int) -> np.ndarray:
    # normalize_embeddings=True deja los vectores en norma 1, que es lo que la
    # distancia coseno de pgvector (<=>) asume para comportarse como producto punto.
    return _cargar().encode(
        textos,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )


async def embeber_consulta(texto: str) -> np.ndarray:
    """Un solo texto. Se paga en CADA turno de voz — mantener barato."""
    vecs = await asyncio.to_thread(_encode, [texto], 1)
    return vecs[0]


async def embeber_lote(textos: list[str], batch_size: int = 16) -> np.ndarray:
    """Lote de ingesta. Fuera del camino de voz, puede ser lento sin molestar."""
    if not textos:
        return np.empty((0, get_settings().embedding_dim), dtype=np.float32)
    return await asyncio.to_thread(_encode, textos, batch_size)


def precalentar() -> None:
    """Carga el modelo al arrancar, no en la primera consulta del jurado."""
    _encode(["precalentamiento"], 1)
