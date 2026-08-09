"""Endpoints de llamadas: pacientes pendientes, inicio, historial y transcripción.

Implementa `docs/CONTRATO_LLAMADAS.md`. La pantalla `/call` y la `/calls` las
construye otro agente contra este contrato, así que la forma de la respuesta es
lo único que no se puede improvisar aquí.

── Dos endpoints que el contrato no pedía ──────────────────────────────────
Están anotados en `docs/CONTRATO_LLAMADAS.md` §Cambios:

- `POST /api/calls/{id}/mensaje` — hablar con el agente **escribiendo**. Es el
  equivalente para la Fase 4 de lo que `python -m app.rag.query` fue para la
  Fase 1: permite probar el guion, las herramientas, el grounding y el
  escalamiento sin micrófono, sin Whisper y sin TTS. Cuando el agente responde
  mal, esta ruta separa en dos segundos «el modelo se equivocó» de «Whisper oyó
  otra cosa», que de otro modo son indistinguibles.
- `POST /api/calls/{id}/fin` — cerrar la llamada y dejar `ended_at`. Sin ella no
  hay forma de que `duracion_s` y `estado` del historial signifiquen algo.

── El mapa de estados ──────────────────────────────────────────────────────
La tabla `calls` tiene cuatro estados y el contrato publica tres. La traducción
está en `_ESTADO` y no es cosmética: una llamada escalada se publica como
`completada` con `escalada: true` porque escalar no es una forma de terminar mal
—es exactamente lo que el sistema debe hacer— y mezclarla con `interrumpida`
haría que el historial contara como fallos los aciertos.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.agent.agente import AgenteLlamada, guardar_turno
from app.api import errores
from app.api.deps import exigir_admin
from app.db.pool import connection

router = APIRouter(tags=["llamadas"], dependencies=[Depends(exigir_admin)])

_ESTADO = {
    "active": "en_curso",
    "completed": "completada",
    "escalated": "completada",
    "failed": "interrumpida",
}
_QUIEN = {"agent": "agente", "patient": "paciente", "system": "sistema"}


def _llamada_no_encontrada(call_id: UUID | str) -> errores.ErrorAPI:
    return errores.ErrorAPI(
        "llamada_no_encontrada", f"No existe ninguna llamada con id {call_id}.", 404
    )


def _paciente_no_encontrado(patient_id: UUID | str) -> errores.ErrorAPI:
    return errores.ErrorAPI(
        "paciente_no_encontrado", f"No existe ningún paciente con id {patient_id}.", 404
    )


def _llamada_cerrada(call_id: UUID | str) -> errores.ErrorAPI:
    return errores.ErrorAPI(
        "llamada_cerrada", f"La llamada {call_id} ya ha terminado.", 409
    )


# ---------------------------------------------------------------------------
# Agentes vivos
# ---------------------------------------------------------------------------
# El agente de una llamada guarda el historial de la conversación, si ya hubo
# alarma y si está esperando la confirmación del paciente. Eso es estado de
# sesión y vive en memoria del proceso.
#
# Se descartó reconstruirlo desde `call_turns` en cada petición: sería más
# robusto ante un reinicio, pero volvería a mandar el historial entero al LLM en
# cada turno y, sobre todo, perdería lo que no está en la transcripción —los
# resultados de las herramientas—, que es justo lo que el modelo necesita para no
# volver a consultarlos. Con una demo de un proceso y llamadas de tres minutos,
# el reinicio a mitad de llamada no es un caso que haya que cubrir; que se note
# si ocurre (404 al turno siguiente) es preferible a cubrirlo a medias.
_AGENTES: dict[str, AgenteLlamada] = {}


class NuevaLlamada(BaseModel):
    patient_id: UUID


class MensajePaciente(BaseModel):
    texto: str = Field(min_length=1, max_length=2000)

    @field_validator("texto")
    @classmethod
    def _no_vacio(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("el mensaje no puede estar vacío")
        return valor.strip()


class FinLlamada(BaseModel):
    motivo: str = "completada"


# ---------------------------------------------------------------------------
# GET /api/patients
# ---------------------------------------------------------------------------
@router.get("/patients")
async def pacientes_pendientes() -> dict[str, Any]:
    """Pacientes con seguimiento pendiente, para la pantalla de inicio de llamada.

    Se ordena por días desde la cirugía descendente —el más reciente primero—
    porque es el orden en que un servicio real llamaría: el postoperatorio
    inmediato es donde están las complicaciones.
    """
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT p.id, p.full_name, p.preferred_name, p.birth_date,
                   s.procedure_name, s.performed_at,
                   (CURRENT_DATE - s.performed_at) AS dias_desde,
                   (SELECT count(*) FROM medications m
                     WHERE m.surgery_id = s.id AND m.active
                       AND (m.ends_on IS NULL OR m.ends_on >= CURRENT_DATE)) AS meds,
                   (SELECT min(a.scheduled_at) FROM appointments a
                     WHERE a.patient_id = p.id AND a.status = 'scheduled'
                       AND a.scheduled_at >= now()) AS proxima_cita,
                   (SELECT max(c.started_at) FROM calls c
                     WHERE c.patient_id = p.id) AS ultima_llamada
            FROM patients p
            JOIN LATERAL (
                SELECT * FROM surgeries WHERE patient_id = p.id
                ORDER BY performed_at DESC LIMIT 1
            ) s ON true
            ORDER BY s.performed_at DESC
            """
        )
        filas = await cur.fetchall()

    return {
        "pacientes": [
            {
                "id": str(f["id"]),
                "nombre": f["full_name"],
                "preferred_name": f["preferred_name"],
                "fecha_nacimiento": f["birth_date"].isoformat() if f["birth_date"] else None,
                "cirugia": {
                    "nombre": f["procedure_name"],
                    "fecha": f["performed_at"].isoformat(),
                    "dias_desde": f["dias_desde"],
                },
                "medicacion_activa": f["meds"],
                "proxima_cita": (
                    f["proxima_cita"].date().isoformat() if f["proxima_cita"] else None
                ),
                "ultima_llamada": (
                    f["ultima_llamada"].isoformat() if f["ultima_llamada"] else None
                ),
            }
            for f in filas
        ]
    }


# ---------------------------------------------------------------------------
# POST /api/calls
# ---------------------------------------------------------------------------
@router.post("/calls", status_code=201)
async def iniciar_llamada(cuerpo: NuevaLlamada) -> dict[str, Any]:
    """Crea la llamada y deja el agente preparado con su primera frase.

    El saludo se devuelve ya escrito. Es constante (`prompts.SALUDO`) y contiene
    la declaración de sistema automatizado que exige el AI Act: no se genera con
    el modelo, así que puede salir por el TTS antes de que haya habido ni un
    viaje a la nube. La llamada empieza a sonar en el tiempo del TTS, no en el
    del LLM.
    """
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM patients WHERE id = %s", (cuerpo.patient_id,)
        )
        if await cur.fetchone() is None:
            raise _paciente_no_encontrado(cuerpo.patient_id)

        cur = await conn.execute(
            "INSERT INTO calls (patient_id) VALUES (%s) RETURNING id", (cuerpo.patient_id,)
        )
        call_id = (await cur.fetchone())["id"]

    agente = await AgenteLlamada.para_llamada(cuerpo.patient_id, call_id)
    _AGENTES[str(call_id)] = agente

    saludo = agente.apertura()
    await guardar_turno(call_id, "agent", saludo)

    return {
        "call_id": str(call_id),
        "ws": f"/ws/voz?call_id={call_id}",
        "saludo": saludo,
        "paciente": agente.contexto.nombre,
        "cirugia": agente.contexto.procedimiento,
        "dias_postop": agente.contexto.dias_postop,
    }


# ---------------------------------------------------------------------------
# POST /api/calls/{id}/mensaje  — la llamada sin micrófono
# ---------------------------------------------------------------------------
@router.post("/calls/{call_id}/mensaje")
async def mensaje_del_paciente(call_id: UUID, cuerpo: MensajePaciente) -> dict[str, Any]:
    agente = _AGENTES.get(str(call_id))
    if agente is None:
        raise _llamada_no_encontrada(call_id)

    await guardar_turno(call_id, "patient", cuerpo.texto)
    turno = await agente.responder_paciente(cuerpo.texto)
    await guardar_turno(call_id, "agent", turno.texto, turno.citas, turno.ms)

    if turno.terminar:
        await _cerrar(call_id, "escalada" if turno.escalada else "completada")

    return turno.a_dict()


# ---------------------------------------------------------------------------
# POST /api/calls/{id}/fin
# ---------------------------------------------------------------------------
@router.post("/calls/{call_id}/fin")
async def terminar_llamada(call_id: UUID, cuerpo: FinLlamada) -> dict[str, Any]:
    if not await _existe(call_id):
        raise _llamada_no_encontrada(call_id)
    estado = await _cerrar(call_id, cuerpo.motivo)
    if estado is None:
        raise _llamada_cerrada(call_id)
    _AGENTES.pop(str(call_id), None)
    return {"call_id": str(call_id), "estado": _ESTADO[estado]}


async def _cerrar(call_id: UUID, motivo: str) -> str | None:
    """Marca la llamada terminada. Devuelve None si ya lo estaba.

    El estado final lo decide la propia fila, no el llamante: si la llamada tiene
    `escalated`, termina como `escalated` aunque quien cuelgue diga
    «completada». Fiarse del cliente aquí significaría que un cierre desde la UI
    borrase el hecho de que hubo un escalamiento.
    """
    esperado = {"completada": "completed", "escalada": "escalated"}.get(motivo, "failed")
    async with connection() as conn:
        cur = await conn.execute(
            """
            UPDATE calls
               SET status = CASE WHEN escalated THEN 'escalated' ELSE %s END,
                   ended_at = now()
             WHERE id = %s AND ended_at IS NULL
            RETURNING status
            """,
            (esperado, call_id),
        )
        fila = await cur.fetchone()
    return fila["status"] if fila else None


async def _existe(call_id: UUID) -> bool:
    async with connection() as conn:
        cur = await conn.execute("SELECT 1 FROM calls WHERE id = %s", (call_id,))
        return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# GET /api/calls
# ---------------------------------------------------------------------------
@router.get("/calls")
async def historial() -> dict[str, Any]:
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT c.id, c.started_at, c.ended_at, c.status, c.escalated,
                   c.escalation_reason, c.escalation_urgency,
                   p.full_name,
                   (SELECT s.procedure_name FROM surgeries s
                     WHERE s.patient_id = p.id
                     ORDER BY s.performed_at DESC LIMIT 1) AS procedimiento,
                   (SELECT count(*) FROM call_turns t WHERE t.call_id = c.id) AS turnos,
                   EXTRACT(EPOCH FROM (coalesce(c.ended_at, now()) - c.started_at))
                       AS duracion
            FROM calls c
            LEFT JOIN patients p ON p.id = c.patient_id
            ORDER BY c.started_at DESC
            """
        )
        filas = await cur.fetchall()

    return {
        "llamadas": [
            {
                "id": str(f["id"]),
                "paciente": f["full_name"],
                "cirugia": f["procedimiento"],
                "iniciada": f["started_at"].isoformat(),
                "duracion_s": int(f["duracion"] or 0),
                "estado": _ESTADO.get(f["status"], f["status"]),
                "escalada": f["escalated"],
                "motivo_escalada": f["escalation_reason"],
                "urgencia_escalada": f["escalation_urgency"],
                "turnos": f["turnos"],
            }
            for f in filas
        ]
    }


# ---------------------------------------------------------------------------
# GET /api/calls/{id}
# ---------------------------------------------------------------------------
@router.get("/calls/{call_id}")
async def detalle(call_id: UUID) -> dict[str, Any]:
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT c.id, c.started_at, c.ended_at, c.status, c.escalated,
                   c.escalation_reason, c.escalation_urgency, c.survey,
                   p.full_name,
                   (SELECT s.procedure_name FROM surgeries s
                     WHERE s.patient_id = p.id
                     ORDER BY s.performed_at DESC LIMIT 1) AS procedimiento
            FROM calls c
            LEFT JOIN patients p ON p.id = c.patient_id
            WHERE c.id = %s
            """,
            (call_id,),
        )
        llamada = await cur.fetchone()
        if llamada is None:
            raise _llamada_no_encontrada(call_id)

        # Se ordena por `id` y no por `created_at`: dos turnos del mismo turno de
        # conversación caen en el mismo milisegundo y `created_at` los devolvería
        # en cualquier orden, con el agente contestando antes de la pregunta.
        cur = await conn.execute(
            """
            SELECT id, role, content, citations, latencies, created_at
            FROM call_turns WHERE call_id = %s ORDER BY id
            """,
            (call_id,),
        )
        turnos = await cur.fetchall()

    return {
        "id": str(llamada["id"]),
        "paciente": llamada["full_name"],
        "cirugia": llamada["procedimiento"],
        "iniciada": llamada["started_at"].isoformat(),
        "terminada": llamada["ended_at"].isoformat() if llamada["ended_at"] else None,
        "estado": _ESTADO.get(llamada["status"], llamada["status"]),
        "escalada": llamada["escalated"],
        "motivo_escalada": llamada["escalation_reason"],
        "urgencia_escalada": llamada["escalation_urgency"],
        "respuestas": llamada["survey"],
        "turnos": [
            {
                "ordinal": n,
                "quien": _QUIEN.get(t["role"], t["role"]),
                "texto": t["content"],
                "citas": t["citations"],
                "ms": t["latencies"],
            }
            for n, t in enumerate(turnos, start=1)
        ],
    }
