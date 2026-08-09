"""Búsqueda híbrida: denso (pgvector) + léxico (FTS español), fusionados con RRF.

Por qué híbrida y no solo vectorial: en dominio clínico la mitad de las consultas
giran sobre términos exactos — "cefalexina 500 mg", "dehiscencia", "38.5 grados".
El embedding denso los diluye en el significado general de la frase; el índice
léxico los acierta de lleno. Al revés, "¿me puedo bañar?" no comparte ni una
palabra con "puede ducharse a partir de las 48 horas", y ahí el denso es el que
salva la consulta. Las dos mitades cubren los fallos de la otra.

INVARIANTE: todas las consultas van contra la vista `retrievable_chunks`, nunca
contra la tabla `chunks`. La vista filtra por status='ready', así que un
documento borrado o a medio procesar es irrecuperable por construcción — no por
disciplina del programador. Ver app/db/schema.sql.
"""

from dataclasses import dataclass

import numpy as np

from app.core.config import get_settings
from app.db.pool import connection

# Constante estándar de Reciprocal Rank Fusion. Amortigua el peso de los primeros
# puestos: sin ella, un rank 1 dominaría a un rank 2 de la otra lista.
RRF_K = 60


@dataclass(slots=True)
class Fragmento:
    chunk_id: str
    document_id: str
    filename: str
    content: str
    heading: str | None
    page: int | None
    score: float
    # De dónde vino: útil para depurar y para enseñar en la demo por qué el
    # híbrido encuentra cosas que ninguna mitad sola encontraría.
    dense_rank: int | None = None
    lexical_rank: int | None = None

    def cita(self) -> str:
        partes = [self.filename]
        if self.heading:
            partes.append(self.heading)
        if self.page is not None:
            partes.append(f"p. {self.page}")
        return " › ".join(partes)


# Dos rankings independientes sobre la MISMA vista, fusionados por RRF.
# FULL OUTER JOIN: un chunk que solo aparece en una de las dos listas sigue
# siendo candidato (con la contribución de la otra a 0).
_SQL_HIBRIDO = """
WITH denso AS (
    SELECT id, document_id, filename, content, heading, page,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s) AS rank
    FROM retrievable_chunks
    ORDER BY embedding <=> %(qvec)s
    LIMIT %(pool)s
),
lexico AS (
    SELECT id, document_id, filename, content, heading, page,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank_cd(content_tsv, query) DESC
           ) AS rank
    FROM retrievable_chunks,
         websearch_to_tsquery('spanish', %(qtext)s) AS query
    WHERE content_tsv @@ query
    ORDER BY ts_rank_cd(content_tsv, query) DESC
    LIMIT %(pool)s
)
SELECT
    COALESCE(d.id, l.id)                   AS chunk_id,
    COALESCE(d.document_id, l.document_id) AS document_id,
    COALESCE(d.filename, l.filename)       AS filename,
    COALESCE(d.content, l.content)         AS content,
    COALESCE(d.heading, l.heading)         AS heading,
    COALESCE(d.page, l.page)               AS page,
    d.rank                                 AS dense_rank,
    l.rank                                 AS lexical_rank,
    COALESCE(1.0 / (%(k)s + d.rank), 0.0)
      + COALESCE(1.0 / (%(k)s + l.rank), 0.0) AS score
FROM denso d
FULL OUTER JOIN lexico l ON l.id = d.id
ORDER BY score DESC
LIMIT %(top_k)s
"""


async def buscar(
    consulta: str,
    query_vector: np.ndarray,
    top_k: int | None = None,
) -> list[Fragmento]:
    """Recupera candidatos para una consulta. No aplica reranking ni umbral."""
    s = get_settings()
    top_k = top_k or s.retrieve_top_k

    async with connection() as conn:
        cur = await conn.execute(
            _SQL_HIBRIDO,
            {
                "qvec": query_vector,
                "qtext": consulta,
                # Se piden más candidatos por rama de los que se devuelven, para
                # que RRF tenga material con el que fusionar.
                "pool": top_k * 2,
                "k": RRF_K,
                "top_k": top_k,
            },
        )
        filas = await cur.fetchall()

    return [
        Fragmento(
            chunk_id=str(f["chunk_id"]),
            document_id=str(f["document_id"]),
            filename=f["filename"],
            content=f["content"],
            heading=f["heading"],
            page=f["page"],
            score=float(f["score"]),
            dense_rank=f["dense_rank"],
            lexical_rank=f["lexical_rank"],
        )
        for f in filas
    ]


async def solo_denso(query_vector: np.ndarray, top_k: int = 10) -> list[Fragmento]:
    """Búsqueda puramente vectorial. Existe para comparar contra la híbrida en
    los evals de la Fase 6 — sirve para justificar el híbrido con números."""
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT id AS chunk_id, document_id, filename, content, heading, page,
                   1 - (embedding <=> %(qvec)s) AS score
            FROM retrievable_chunks
            ORDER BY embedding <=> %(qvec)s
            LIMIT %(top_k)s
            """,
            {"qvec": query_vector, "top_k": top_k},
        )
        filas = await cur.fetchall()

    return [
        Fragmento(
            chunk_id=str(f["chunk_id"]),
            document_id=str(f["document_id"]),
            filename=f["filename"],
            content=f["content"],
            heading=f["heading"],
            page=f["page"],
            score=float(f["score"]),
        )
        for f in filas
    ]
