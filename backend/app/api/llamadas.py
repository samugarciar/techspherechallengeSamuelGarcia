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

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.agent import redflags
from app.agent.agente import AgenteLlamada, guardar_turno
from app.agent.llm_client import LLMClient, Mensaje, RespuestaLLM
from app.api import errores
from app.api.deps import exigir_admin
from app.db.pool import connection

if TYPE_CHECKING:
    # Solo para el tipo. Importar `app.voice.pipeline_ws` de verdad aquí
    # arrastraría numpy, el VAD y el motor de TTS a TODOS los despliegues, y este
    # router se monta siempre —sin `VOZ=1`— justamente porque es texto y SQL.
    from app.voice.pipeline_ws import MetricasTurno

log = logging.getLogger("api.llamadas")

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

# Qué llamadas tienen ahora mismo un WebSocket de voz enganchado. Sirve para una
# sola cosa y merece la pena: impedir que dos conexiones compartan el mismo
# `AgenteLlamada`. Pasa con un doble clic en «Llamar» o con una pestaña
# duplicada, y el síntoma —dos turnos entrelazados en un único historial, el
# agente contestando a una pregunta con el contexto de la otra— no se parece en
# nada a su causa.
_VOCES: dict[str, SesionDeVoz] = {}


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
# La costura con el bucle de voz — `/ws/voz?call_id=…`
# ---------------------------------------------------------------------------
EnviarEvento = Callable[[dict[str, Any]], Awaitable[None]]

ESPERA_VACIADO_S = 5.0
"""Cuánto se espera a que termine de escribirse la transcripción al colgar.

Hay cola porque escribir no puede costarle latencia al paciente, y hay tope
porque al colgar ya no hay nadie esperando: si Postgres no responde en cinco
segundos, se pierde la cola y se registra en el log. Dejar el cierre del
WebSocket colgado de una base lenta convertiría un problema de escritura en un
socket zombi por llamada."""


class SesionDeVoz(LLMClient):
    """El agente clínico de UNA llamada, hablando por el WebSocket de voz.

    Es la pieza que faltaba entre las Fases 3, 4 y 5, y hace exactamente tres
    cosas que ninguna de las tres podía hacer sola:

    1. **Presenta al agente como un `LLMClient`**, que es lo único que `SesionVoz`
       sabe consumir. `AgenteLlamada` ya implementaba esa interfaz a propósito;
       aquí se envuelve para poder mirar el turno por dentro al pasar.
    2. **Publica lo que el agente produce** —citas y banderas rojas— en el
       instante en que las produce, antes de que salga el audio de esa respuesta.
       Después sería reconstruirlas: el cliente pega las citas a la intervención
       que fundamentan, y si llegan tarde se pegan a la siguiente.
    3. **Registra la conversación en `call_turns`** por una cola, fuera del
       camino crítico.

    ── Por qué la persistencia va por una cola ─────────────────────────────
    Dos propiedades que un `await conn.execute(...)` en línea no da:

    - *No cuesta latencia.* El registro se encola en microsegundos y el turno
      siguiente empieza sin esperar a la base. Una escritura lenta retrasaría el
      audio, que es lo único que el paciente percibe.
    - *Un fallo de base no corta la llamada.* El trabajador registra el error y
      sigue. Perder la transcripción de un turno es malo; colgarle el teléfono a
      un paciente en seguimiento clínico es peor.

    Y un consumidor único, no `create_task` por escritura: `call_turns` se ordena
    por `id` (`GET /api/calls/{id}`), así que dos inserciones en vuelo a la vez
    pueden aterrizar cambiadas y dejar al agente contestando antes de la pregunta.
    Con un solo trabajador el orden es el de la conversación por construcción.
    """

    def __init__(self, call_id: UUID, agente: AgenteLlamada, emitir: EnviarEvento) -> None:
        self.call_id = call_id
        self.agente = agente
        self._emitir = emitir
        self._motivo_fin: str | None = None
        self._citas: list[Any] = []
        self._cerrada = False
        self._cola: asyncio.Queue[Callable[[], Awaitable[None]] | None] = asyncio.Queue()
        self._escritor = asyncio.create_task(self._escribir_pendientes())

    # -- interfaz LLMClient: lo que `SesionVoz` consume ---------------------
    async def stream(self, sistema: str, mensajes: list[Mensaje]) -> AsyncIterator[str]:
        """Un turno del agente, troceado en frases para que el TTS arranque antes.

        El troceado no se repite aquí: lo hace `AgenteLlamada.stream`, y este
        método se limita a mirar el turno al pasar. Duplicar el troceado sería
        tener dos sitios decidiendo dónde se corta una frase clínica.
        """
        anunciado = False
        async for trozo in self.agente.stream(sistema, mensajes):
            if not anunciado:
                # En la primera frase, no al final: el `stream` se consume a la
                # vez que se sintetiza, así que anunciar al terminar dejaría las
                # citas llegando después del audio que fundamentan.
                anunciado = True
                await self._anunciar(self.agente.ultimo_turno)
            yield trozo
        if not anunciado:
            # Un turno sin texto no debería ocurrir —`_rendirse()` siempre dice
            # algo— pero si ocurriera, una bandera roja no puede perderse por no
            # haber tenido audio al que engancharse.
            await self._anunciar(self.agente.ultimo_turno)

    async def responder(
        self,
        sistema: str,
        mensajes: list[Mensaje],
        herramientas: list[dict[str, Any]] | None = None,
    ) -> RespuestaLLM:
        """Presente por la interfaz; el bucle de voz solo usa `stream`."""
        respuesta = await self.agente.responder(sistema, mensajes, herramientas)
        await self._anunciar(self.agente.ultimo_turno)
        return respuesta

    async def _anunciar(self, turno: Any | None) -> None:
        if turno is None:
            return

        # La bandera roja va ANTES que las citas: es el titular de la pantalla y
        # el momento clínico de la demo. Que llegue primero es gratis y evita que
        # se pinte detrás de un panel de documentos.
        if turno.banderas:
            peor = redflags.peor(turno.banderas)
            await self._emitir(
                {"tipo": "bandera_roja", "motivo": peor.motivo, "urgencia": peor.urgencia}
            )
        if turno.citas:
            await self._emitir(
                {"tipo": "citas", "citas": [c.a_dict() for c in turno.citas]}
            )

        # Se guardan para `turno_terminado`, que es quien persiste: allí ya no se
        # puede preguntar por ellas porque el agente habrá empezado el turno
        # siguiente y `nuevo_turno()` las habrá vaciado.
        self._citas = list(turno.citas)

        if turno.terminar:
            # Decisión 2 del contrato: la alarma se dio, el paciente confirmó que
            # la entendió, y aquí se acaba la llamada.
            self._motivo_fin = "escalada" if turno.escalada else "completada"

    # -- interfaz LlamadaEnCurso: lo que el pipeline consulta ---------------
    def saludo_inicial(self) -> str | None:
        return self.agente.saludo

    def motivo_de_fin(self) -> str | None:
        return self._motivo_fin

    def turno_terminado(self, metricas: MetricasTurno) -> None:
        """Encola la transcripción del turno. Síncrona y sin `await` a propósito.

        Se llama desde el `finally` del turno de voz, a veces bajo cancelación
        (un barge-in), donde esperar a nada es inseguro. Ver `LlamadaEnCurso` en
        `app/voice/pipeline_ws.py`.

        Las latencias se reparten entre las dos filas según a quién describen: el
        `stt` es el coste de entender al paciente y va en su turno; el resto es
        el coste de contestarle y va en el del agente. Meterlas todas en una fila
        haría que el panel del historial atribuyera al agente el tiempo de
        Whisper.
        """
        if metricas.texto_paciente:
            self._encolar(
                partial(
                    guardar_turno, self.call_id, "patient", metricas.texto_paciente,
                    None, {"stt": round(metricas.stt_ms)},
                )
            )
        if metricas.texto_agente:
            self._encolar(
                partial(
                    guardar_turno, self.call_id, "agent", metricas.texto_agente,
                    self._citas,
                    {
                        "llm": round(metricas.llm_ttft_ms),
                        "tts": round(metricas.tts_primera_frase_ms),
                        "total": round(metricas.primer_audio_ms),
                    },
                )
            )

    async def cerrar(self) -> None:
        """Vacía la cola y sella la llamada. Idempotente."""
        if self._cerrada:
            return
        self._cerrada = True

        await self._cola.put(None)
        try:
            await asyncio.wait_for(self._escritor, ESPERA_VACIADO_S)
        except TimeoutError:
            log.warning(
                "la transcripción de %s no terminó de escribirse en %.0f s; se descarta",
                self.call_id, ESPERA_VACIADO_S,
            )
            self._escritor.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fallo vaciando la transcripción de %s", self.call_id)

        try:
            # `cortada` es el caso por defecto —se fue el WebSocket— y no
            # `completada`: si el agente hubiera dado la llamada por terminada,
            # `_motivo_fin` lo diría. `_cerrar` no toca una llamada ya cerrada,
            # así que colgar después de una escalada no la reescribe.
            await _cerrar(self.call_id, self._motivo_fin or "cortada")
        except Exception:
            log.exception("no se pudo sellar la llamada %s al colgar", self.call_id)

        _AGENTES.pop(str(self.call_id), None)
        _VOCES.pop(str(self.call_id), None)

    # -- la cola ------------------------------------------------------------
    def _encolar(self, trabajo: Callable[[], Awaitable[None]]) -> None:
        if self._cerrada:
            return
        self._cola.put_nowait(trabajo)

    async def _escribir_pendientes(self) -> None:
        while (trabajo := await self._cola.get()) is not None:
            try:
                await trabajo()
            except Exception:
                # Se registra y se sigue con el siguiente. Un turno perdido no
                # justifica perder los que vengan detrás.
                log.exception("no se pudo registrar un turno de la llamada %s", self.call_id)


async def abrir_sesion_de_voz(call_id: str, emitir: EnviarEvento) -> SesionDeVoz | None:
    """Fábrica que `app/main.py` le pasa a `crear_router()`.

    Devuelve `None` —y el WebSocket se cierra diciendo por qué— en los tres casos
    en que enganchar la voz a esta llamada sería mentir:

    - el `call_id` no es un UUID;
    - no hay agente vivo para él, que en la práctica es «el backend se reinició a
      mitad de llamada» (§Cambios punto 6 del contrato);
    - ya hay otro WebSocket hablando con esa misma llamada.
    """
    try:
        uuid = UUID(call_id)
    except ValueError:
        log.warning("call_id no es un UUID: %r", call_id)
        return None

    agente = _AGENTES.get(str(uuid))
    if agente is None:
        log.warning(
            "no hay ninguna llamada viva con id %s; ¿se reinició el backend a mitad?", uuid
        )
        return None

    if str(uuid) in _VOCES:
        log.warning("la llamada %s ya tiene un WebSocket de voz enganchado", uuid)
        return None

    sesion = SesionDeVoz(uuid, agente, emitir)
    _VOCES[str(uuid)] = sesion
    return sesion


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
