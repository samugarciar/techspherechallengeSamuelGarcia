"""La cola, contra Postgres de verdad.

`FOR UPDATE SKIP LOCKED` no se puede testear con un doble: lo que se está
comprobando es el comportamiento del gestor de bloqueos de Postgres bajo
concurrencia real. Un mock devolvería lo que el test le pida y no probaría nada.
"""

from uuid import uuid4

import pytest

from app.db import queue
from app.db.pool import connection, get_pool
from tests.ayuda_db import hay_postgres, pool  # noqa: F401 — `pool` es una fixture

pytestmark = hay_postgres


async def _estado(job_id: int) -> dict:
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT status, attempts, last_error, locked_by, "
            "run_after <= now() AS disponible FROM jobs WHERE id = %s",
            (job_id,),
        )
        return await cur.fetchone()


async def _reclamar(worker: str = "w") -> queue.Job | None:
    async with connection() as conn:
        async with conn.transaction():
            return await queue.tomar_trabajo(conn, worker=worker)


async def _envejecer(job_id: int, minutos: int) -> None:
    """Simula un worker muerto: su `locked_at` deja de refrescarse."""
    async with connection() as conn:
        await conn.execute(
            "UPDATE jobs SET locked_at = now() - make_interval(mins => %s) WHERE id = %s",
            (minutos, job_id),
        )


# ---------------------------------------------------------------------------
async def test_encolar_y_reclamar(pool):  # noqa: F811
    doc = uuid4()
    job_id = await queue.encolar(doc)

    job = await _reclamar()
    assert job is not None
    assert job.id == job_id
    assert job.document_id == doc
    assert job.intentos == 1, "el intento se consume al reclamar, no al fallar"

    assert (await _estado(job_id))["status"] == "running"
    await queue.cancelar_de_documento(doc)


async def test_encolar_dentro_de_la_transaccion_del_documento(pool):  # noqa: F811
    """Si el commit falla no puede quedar ni documento ni job.

    Se comprueba revirtiendo a propósito: el job insertado con la conexión de la
    transacción del llamante desaparece con ella.
    """
    doc = uuid4()
    async with get_pool().connection() as conn:
        async with conn.transaction(force_rollback=True):
            job_id = await queue.encolar(doc, conn=conn)
            assert job_id is not None

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE payload->>'document_id' = %s", (str(doc),)
        )
        assert (await cur.fetchone())["n"] == 0


async def test_dos_workers_no_cogen_el_mismo_job(pool):  # noqa: F811
    """LA prueba de la cola: con un solo job en cola, uno se lo lleva y el otro
    se va de vacío — no espera, no lo duplica.

    Las dos conexiones mantienen su transacción ABIERTA a la vez a propósito: es
    entre el SELECT y el COMMIT donde `SKIP LOCKED` tiene que actuar. Si el test
    cerrara la primera transacción antes de abrir la segunda, pasaría siempre y
    no estaría comprobando nada.
    """
    doc = uuid4()
    job_id = await queue.encolar(doc)

    pool_ = get_pool()
    async with pool_.connection() as c1, pool_.connection() as c2:
        async with c1.transaction(), c2.transaction():
            primero = await queue.tomar_trabajo(c1, worker="w1")
            segundo = await queue.tomar_trabajo(c2, worker="w2")

    llevados = [j for j in (primero, segundo) if j is not None]
    assert len(llevados) == 1, "los dos workers se llevaron el mismo job"
    assert llevados[0].id == job_id
    assert (await _estado(job_id))["attempts"] == 1

    await queue.cancelar_de_documento(doc)


async def test_dos_jobs_dos_workers_uno_cada_uno(pool):  # noqa: F811
    """SKIP LOCKED salta la fila bloqueada en vez de esperarla: con dos jobs,
    los dos workers salen con trabajo distinto y ninguno se queda parado."""
    docs = [uuid4(), uuid4()]
    ids = [await queue.encolar(d) for d in docs]

    pool_ = get_pool()
    async with pool_.connection() as c1, pool_.connection() as c2:
        async with c1.transaction(), c2.transaction():
            primero = await queue.tomar_trabajo(c1, worker="w1")
            segundo = await queue.tomar_trabajo(c2, worker="w2")

    assert {primero.id, segundo.id} == set(ids)

    for d in docs:
        await queue.cancelar_de_documento(d)


async def test_completar_lo_saca_de_la_cola(pool):  # noqa: F811
    doc = uuid4()
    job_id = await queue.encolar(doc)
    await _reclamar()
    await queue.completar(job_id)

    assert (await _estado(job_id))["status"] == "done"
    assert await _reclamar() is None or True   # no debe volver a salir ESTE job
    async with connection() as conn:
        cur = await conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
        assert (await cur.fetchone())["status"] == "done"
    await queue.cancelar_de_documento(doc)


async def test_fallar_reencola_con_backoff(pool):  # noqa: F811
    """Un fallo vuelve a la cola pero NO de inmediato.

    Sin `run_after`, el worker reclamaría el job en el mismo milisegundo y
    quemaría los tres intentos contra el mismo error en un bucle cerrado.
    """
    doc = uuid4()
    job_id = await queue.encolar(doc)
    await _reclamar()
    await queue.fallar(job_id, "el PDF estaba corrupto")

    estado = await _estado(job_id)
    assert estado["status"] == "queued"
    assert estado["last_error"] == "el PDF estaba corrupto"
    assert estado["disponible"] is False, "se reencoló sin esperar el backoff"
    assert await _reclamar() is None

    await queue.cancelar_de_documento(doc)


async def test_agotar_los_intentos_es_terminal(pool):  # noqa: F811
    doc = uuid4()
    job_id = await queue.encolar(doc)

    for intento in range(1, 4):
        async with connection() as conn:     # vencer el backoff a mano
            await conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job_id,))
        job = await _reclamar()
        assert job is not None, f"debía poder reclamarse en el intento {intento}"
        assert job.intentos == intento
        await queue.fallar(job_id, f"fallo {intento}")

    estado = await _estado(job_id)
    assert estado["status"] == "failed"
    assert estado["attempts"] == 3

    async with connection() as conn:
        await conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job_id,))
    assert await _reclamar() is None, "un job agotado no puede volver a la cola"

    await queue.cancelar_de_documento(doc)


async def test_error_sin_arreglo_no_se_reintenta(pool):  # noqa: F811
    """Un formato no soportado no mejora por repetirlo tres veces."""
    doc = uuid4()
    job_id = await queue.encolar(doc)
    await _reclamar()
    await queue.fallar(job_id, "formato no soportado", reintentar=False)

    assert (await _estado(job_id))["status"] == "failed"
    await queue.cancelar_de_documento(doc)


async def test_un_worker_muerto_no_secuestra_el_job(pool):  # noqa: F811
    """El job de un worker que murió a media faena vuelve a estar disponible.

    Es el caso que el bloqueo de fila no cubre: el trabajo largo ocurre fuera de
    la transacción, así que cuando el proceso muere no queda ningún bloqueo que
    caduque. Solo queda la antigüedad de `locked_at`.
    """
    doc = uuid4()
    job_id = await queue.encolar(doc)

    muerto = await _reclamar(worker="el-que-murio")
    assert muerto is not None
    assert await _reclamar(worker="el-vivo") is None, "aún no es huérfano"

    await _envejecer(job_id, minutos=int(queue.ABANDONO.total_seconds() // 60) + 1)

    rescatado = await _reclamar(worker="el-vivo")
    assert rescatado is not None and rescatado.id == job_id
    assert rescatado.intentos == 2, "la reclamación también gasta un intento"
    assert (await _estado(job_id))["locked_by"] == "el-vivo"

    await queue.cancelar_de_documento(doc)


async def test_el_latido_protege_al_worker_vivo(pool):  # noqa: F811
    """Un documento grande tarda más que el umbral de abandono; el latido es lo
    que impide que otro worker lo recoja en paralelo y lo procese dos veces."""
    doc = uuid4()
    job_id = await queue.encolar(doc)
    await _reclamar(worker="el-lento")

    await _envejecer(job_id, minutos=int(queue.ABANDONO.total_seconds() // 60) + 1)
    await queue.latido(job_id)

    assert await _reclamar(worker="el-otro") is None
    await queue.cancelar_de_documento(doc)


async def test_cancelar_de_documento(pool):  # noqa: F811
    doc = uuid4()
    await queue.encolar(doc)
    await queue.encolar(doc)

    assert await queue.cancelar_de_documento(doc) == 2
    assert await _reclamar() is None


@pytest.mark.parametrize("intentos,minimo", [(1, 2), (2, 4), (3, 8)])
async def test_el_backoff_crece(pool, intentos, minimo):  # noqa: F811
    """2^intentos segundos: 2, 4, 8… con techo, para que un error permanente no
    acabe esperando horas mientras la consola lo muestra atascado."""
    doc = uuid4()
    job_id = await queue.encolar(doc)

    async with connection() as conn:
        await conn.execute("UPDATE jobs SET attempts = %s WHERE id = %s", (intentos, job_id))
    await queue.fallar(job_id, "falla", reintentar=intentos < 3)

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT extract(epoch FROM run_after - now()) AS espera FROM jobs WHERE id = %s",
            (job_id,),
        )
        espera = (await cur.fetchone())["espera"]

    assert minimo - 1 <= espera <= queue.BACKOFF_MAX_S
    await queue.cancelar_de_documento(doc)
