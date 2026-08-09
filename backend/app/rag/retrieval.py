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

# La consulta léxica, en OR y no en AND. Es un arreglo, no una preferencia:
# `websearch_to_tsquery('spanish', '¿qué hago si me sube la fiebre?')` produce
# `'hag' & 'si' & 'sub' & 'fiebr'` —AND de todos los términos— y sobre el corpus
# de protocolos eso da CERO filas, aunque el fragmento diga «fiebre superior a
# 38.5 grados». Con AND la mitad léxica de la búsqueda híbrida solo se activa
# cuando el usuario escribe justo las palabras del documento y ninguna de más, es
# decir: nunca, porque un paciente por teléfono habla en preguntas. Medido sobre
# `protocolo_apendicectomia.pdf`, ocho candidatos y `lexical_rank = None` en los
# ocho. La mitad que el README vende como la que acierta «cefalexina 500 mg»
# estaba muerta para todo lo que no fuera una consulta de palabras clave.
#
# El OR se construye tokenizando la consulta con `to_tsvector` —que ya quita
# stopwords y lematiza con el diccionario español— y uniendo los lexemas con `|`.
# No hace falta escapar nada porque los lexemas salen ya normalizados por el
# propio Postgres. Con la consulta vacía o solo signos de puntuación, el array
# queda vacío, `to_tsquery` recibe NULL por el NULLIF y la rama léxica devuelve
# cero filas en vez de reventar: la densa sigue respondiendo sola.
#
# No se pierde precisión al ampliar a OR porque el orden lo pone `ts_rank_cd`,
# que puntúa más alto a quien contiene MÁS términos de la consulta y más juntos.
# Lo que sí se pierde es la búsqueda por frase exacta entre comillas de
# `websearch_to_tsquery`; nadie le dicta comillas a un agente de voz.
_TSQUERY_OR = """
    to_tsquery('spanish',
        NULLIF(array_to_string(
            tsvector_to_array(to_tsvector('spanish', %(qtext)s)), ' | '), ''))
"""


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
_SQL_HIBRIDO = f"""
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
         {_TSQUERY_OR} AS query
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


async def revalidar(fragmentos: list[Fragmento]) -> list[Fragmento]:
    """Descarta los fragmentos que han dejado de ser recuperables mientras tanto.

    Es la última etapa de toda consulta y protege del peor fallo que este sistema
    puede cometer: citarle a un paciente un protocolo que el hospital acaba de
    retirar.

    El agujero no está en el schema, está en el tiempo. Entre que `buscar()` lee y
    que la respuesta sale pasan ~585 ms del cross-encoder, y en esa ventana el
    administrador puede borrar el documento. Ninguna de las dos garantías del
    schema lo tapa: el CASCADE borra las filas de verdad, pero la sentencia que ya
    las leyó trabajaba sobre su propia instantánea —READ COMMITTED: cada sentencia
    ve el estado que había cuando ELLA empezó— y los `Fragmento` ya están en
    memoria de Python, donde Postgres no llega. El borrado es correcto y la
    respuesta lleva igualmente el texto retirado.

    Se descartó la alternativa de recuperar dentro de una transacción REPEATABLE
    READ que se mantuviera abierta hasta responder: agrava el problema en vez de
    arreglarlo, porque congela la instantánea todavía más atrás, y además retiene
    una de las 8 conexiones del pool durante todo el rerank.

    Lo que esto garantiza es acotado y hay que decirlo con precisión: que ningún
    fragmento devuelto estaba borrado en el momento de esta comprobación. La
    ventana no desaparece —es imposible—, pasa de ~585 ms a la latencia de una
    consulta por clave primaria, que es el mínimo teórico.
    """
    if not fragmentos:
        return []

    async with connection() as conn:
        cur = await conn.execute(
            # Contra la vista, no contra `chunks`: la invariante del diseño es que
            # el retrieval no mira la tabla ni siquiera para comprobar. Un chunk
            # cuyo documento pasó a 'superseded' sigue en `chunks` y ya no debe
            # devolverse.
            "SELECT id FROM retrievable_chunks WHERE id = ANY(%s::uuid[])",
            ([f.chunk_id for f in fragmentos],),
        )
        vivos = {str(f["id"]) for f in await cur.fetchall()}

    return [f for f in fragmentos if f.chunk_id in vivos]


async def solo_denso(query_vector: np.ndarray, top_k: int = 10) -> list[Fragmento]:
    """Búsqueda puramente vectorial. **No la llama nadie todavía.**

    Existe para comparar contra la híbrida en los evals de la Fase 6: el argumento
    de que el híbrido hace falta —«el léxico acierta *cefalexina 500 mg*, el denso
    acierta *¿me puedo bañar?*»— es hoy una afirmación razonable y sin medir, y
    esta función es la mitad que falta para convertirla en un número.
    """
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
