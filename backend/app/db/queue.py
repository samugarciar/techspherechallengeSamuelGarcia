"""Cola de ingesta sobre Postgres.

Por qué Postgres y no Redis+Celery: el estado del job vive en la MISMA
transacción que el documento. La consola necesita leer ambos a la vez y no puede
permitirse que el documento exista sin su job, ni al revés. `FOR UPDATE SKIP
LOCKED` da concurrencia segura sin un servicio más compitiendo por los 16 GB que
ya comparten Whisper, bge-m3 y el reranker.

Tres propiedades que la cola tiene que garantizar, y cómo:

1. **Dos workers nunca cogen el mismo job.** `SELECT ... FOR UPDATE SKIP LOCKED`
   dentro de una transacción: el segundo worker no espera al primero, salta la
   fila bloqueada y se lleva la siguiente.

2. **Un worker que muere no bloquea su job para siempre.** La reclamación NO se
   apoya en el bloqueo de fila, porque el bloqueo se suelta al hacer commit y el
   trabajo de verdad (parsear, embeber) ocurre después, fuera de la transacción.
   Se apoya en la antigüedad de `locked_at`: pasado `ABANDONO`, un job en
   'running' se considera huérfano y vuelve a estar disponible. El worker vivo se
   defiende de ese barrido refrescando `locked_at` con `latido()` en cada cambio
   de estado. La alternativa —bloquear la fila durante todo el procesamiento—
   mantendría una transacción abierta minutos enteros, y una transacción larga en
   Postgres frena el autovacuum de todas las tablas.

3. **Un documento venenoso no gira eternamente.** `attempts` se incrementa AL
   RECLAMAR, no al fallar, para que un PDF que reviente el proceso entero también
   consuma intentos. Agotados los `max_attempts`, el job pasa a 'failed', que es
   terminal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from app.db.pool import connection, transaction

# Un job 'running' cuyo locked_at supere esto se da por huérfano. Tiene que ser
# holgadamente mayor que el intervalo de latido del worker (que refresca en cada
# cambio de estado) o dos workers procesarían el mismo documento a la vez.
ABANDONO = timedelta(minutes=5)

# Backoff exponencial con techo. El techo importa porque el error típico aquí no
# es transitorio (un PDF corrupto): esperar horas no lo arregla, y mientras tanto
# la consola muestra el documento atascado sin explicación.
BACKOFF_BASE_S = 2
BACKOFF_MAX_S = 300


@dataclass(slots=True)
class Job:
    # bigserial en el schema, no uuid: el orden de llegada ES el id, y un uuid
    # aleatorio obligaría a ordenar por created_at para conservar el FIFO.
    id: int
    document_id: UUID
    tipo: str
    intentos: int


_CAMPOS = "id, kind, payload, attempts"


def _a_job(fila: dict) -> Job:
    payload = fila["payload"]
    if isinstance(payload, str):          # por si la conexión no decodifica jsonb
        payload = json.loads(payload)
    return Job(
        id=fila["id"],
        document_id=UUID(str(payload["document_id"])),
        tipo=fila["kind"],
        intentos=fila["attempts"],
    )


async def encolar(document_id: UUID, tipo: str = "ingest", conn=None) -> int:
    """Encola un documento y devuelve el id del job.

    `conn` es opcional pero es la forma correcta de llamarlo desde la API: pasar
    la conexión de la transacción que acaba de crear la fila del documento hace
    que documento y job entren o no entren juntos. Sin `conn` se abre una
    transacción propia, y entonces existe una ventana —corta, pero real— en la
    que el documento está en 'uploaded' sin nadie que vaya a procesarlo.
    """
    sql = "INSERT INTO jobs (kind, payload) VALUES (%s, %s::jsonb) RETURNING id"
    args = (tipo, json.dumps({"document_id": str(document_id)}))

    if conn is not None:
        cur = await conn.execute(sql, args)
        return (await cur.fetchone())["id"]

    async with transaction() as propia:
        cur = await propia.execute(sql, args)
        return (await cur.fetchone())["id"]


async def tomar_trabajo(conn, worker: str = "worker") -> Job | None:
    """Reclama un job. Devuelve None si no hay nada que hacer.

    DEBE llamarse dentro de una transacción: el bloqueo de `FOR UPDATE` solo vale
    hasta el commit, y es entre el SELECT y el UPDATE cuando protege de que otro
    worker se lleve la misma fila.

    Las dos ramas del WHERE son los dos motivos por los que un job está
    disponible: nunca se ha empezado (y su backoff ya venció), o se empezó y
    quien lo empezó ya no está.
    """
    cur = await conn.execute(
        f"""
        SELECT {_CAMPOS} FROM jobs
         WHERE attempts < max_attempts
           AND (
                 (status = 'queued'  AND run_after <= now())
              OR (status = 'running' AND locked_at < now() - %s::interval)
               )
         ORDER BY id
         FOR UPDATE SKIP LOCKED
         LIMIT 1
        """,
        (ABANDONO,),
    )
    fila = await cur.fetchone()
    if fila is None:
        return None

    await conn.execute(
        """
        UPDATE jobs
           SET status = 'running', attempts = attempts + 1,
               locked_at = now(), locked_by = %s
         WHERE id = %s
        """,
        (worker, fila["id"]),
    )
    # attempts ya incrementado: el worker debe ver el número de intento que está
    # gastando, no el anterior.
    return _a_job({**fila, "attempts": fila["attempts"] + 1})


async def latido(job_id: int) -> None:
    """Refresca `locked_at` para que el barrido de huérfanos no se lleve un job vivo.

    El worker lo llama en cada cambio de estado del documento. Una tarea de fondo
    con su propio temporizador sería más regular, pero las etapas de la ingesta
    (parsear, trocear, embeber) ya marcan el ritmo y no hace falta un reloj más.
    """
    async with connection() as conn:
        await conn.execute(
            "UPDATE jobs SET locked_at = now() WHERE id = %s AND status = 'running'",
            (job_id,),
        )


async def completar(job_id: int) -> None:
    async with connection() as conn:
        await conn.execute(
            """
            UPDATE jobs
               SET status = 'done', last_error = NULL, locked_at = NULL, locked_by = NULL
             WHERE id = %s
            """,
            (job_id,),
        )


async def fallar(job_id: int, error: str, reintentar: bool = True) -> None:
    """Marca el fallo y decide si vuelve a la cola o muere.

    El estado terminal lo decide la propia sentencia comparando `attempts` con
    `max_attempts`, en vez de leer primero y decidir en Python: así dos workers
    que fallaran a la vez sobre el mismo job no pueden llegar a conclusiones
    distintas sobre cuántos intentos quedaban.

    `reintentar=False` es para los errores que no van a mejorar por repetirlos —
    un formato no soportado, un documento que ya no existe.
    """
    async with connection() as conn:
        await conn.execute(
            """
            UPDATE jobs
               SET status = CASE
                       WHEN %(reintentar)s AND attempts < max_attempts THEN 'queued'
                       ELSE 'failed'
                   END,
                   run_after = now() + make_interval(
                       secs => least(%(base)s::float8 ^ attempts, %(techo)s::float8)
                   ),
                   last_error = %(error)s,
                   locked_at = NULL,
                   locked_by = NULL
             WHERE id = %(id)s
            """,
            {
                "id": job_id,
                "error": error[:2000],
                "reintentar": reintentar,
                "base": BACKOFF_BASE_S,
                "techo": BACKOFF_MAX_S,
            },
        )


async def cancelar_de_documento(document_id: UUID, conn=None) -> int:
    """Descarta los jobs pendientes de un documento. Devuelve cuántos.

    Se llama al borrar un documento: sin esto, el worker recoge después un job
    cuyo documento ya no existe, lo reintenta tres veces y lo deja en 'failed'.
    La consola mostraría un error rojo por un borrado que salió perfecto.
    """
    sql = """
        DELETE FROM jobs
         WHERE status IN ('queued', 'running')
           AND payload->>'document_id' = %s
    """
    if conn is not None:
        cur = await conn.execute(sql, (str(document_id),))
        return cur.rowcount

    async with transaction() as propia:
        cur = await propia.execute(sql, (str(document_id),))
        return cur.rowcount
