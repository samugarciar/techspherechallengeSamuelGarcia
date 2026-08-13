"""Las herramientas del agente: datos vivos por SQL, protocolos por RAG.

**Esta separación es la decisión más importante del proyecto** (README §Las tres
decisiones que necesitan justificación, punto 1) y este fichero es donde se hace
cumplir o se pierde.

- Qué paciente es, qué cirugía tuvo, qué medicación tiene activa y cuándo es su
  cita se leen **con SQL contra Postgres, en el momento de preguntarlo**. Nunca
  por búsqueda vectorial. Un embedding es una foto del instante de la ingesta:
  meter ahí datos que cambian es la causa número uno de respuestas
  desactualizadas en sistemas como éste. Una medicación suspendida ayer seguiría
  recomendándose durante días.
- Qué hacer ante una herida enrojecida, cuándo se puede uno duchar o qué es una
  fiebre preocupante sale **del RAG**, del protocolo que el hospital ha subido a
  la consola. Si el administrador lo borra, `buscar_protocolo` deja de
  encontrarlo en la misma transacción.

── Ninguna herramienta recibe el id del paciente ────────────────────────────
Las tres tools de lectura no tienen argumentos. El paciente y la llamada vienen
del contexto de `Herramientas`, que los recibe de quien abrió la llamada.

La alternativa —`obtener_paciente(patient_id)`— se descartó después de verla
fallar en el primer spike: un modelo al que se le pide un UUID lo inventa con
soltura, y el resultado no es un error visible sino una consulta que devuelve
cero filas y un agente que le dice a María que no consta ninguna cirugía. Con
cero argumentos ese fallo es imposible de cometer. Es el mismo principio que
`retrievable_chunks`: hacer irrepresentable el estado malo en vez de confiar en
que nadie lo escriba.

── El grounding se impone aquí, no en el prompt ─────────────────────────────
Cuando `hay_evidencia()` es falso, `buscar_protocolo` **no devuelve los
fragmentos**. No devuelve fragmentos malos con una advertencia: no devuelve
ninguno. Una regla del prompt es una petición y un modelo puede desatenderla; un
contexto vacío no se puede citar. La regla del prompt sigue estando, pero es el
segundo cinturón, no el primero.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.agent.guion import claves_validas, etiqueta_de
from app.agent.llm_client import LlamadaHerramienta
from app.db.pool import connection

log = logging.getLogger("agente.tools")

_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def fecha_en_palabras(valor: date | datetime | None) -> str | None:
    """`2026-08-05` -> «5 de agosto de 2026».

    El agente lee estas cadenas en voz alta y los TTS en español pronuncian las
    fechas ISO fatal («dos mil veintiséis guion cero ocho…»). Se le da ya
    escrita la forma hablada para que no tenga que convertirla —y para que no se
    equivoque al hacerlo, que es peor.
    """
    if valor is None:
        return None
    d = valor.date() if isinstance(valor, datetime) else valor
    return f"{d.day} de {_MESES[d.month - 1]} de {d.year}"


@dataclass(slots=True)
class Cita:
    """Procedencia de una afirmación clínica. Va a `call_turns.citations`."""

    filename: str
    heading: str | None
    page: int | None

    def a_dict(self) -> dict[str, Any]:
        return {"filename": self.filename, "heading": self.heading, "page": self.page}


@dataclass(slots=True)
class ContextoLlamada:
    """Lo mínimo para abrir la llamada: a quién se llama y de qué la operaron.

    Se carga UNA vez al empezar, y solo esto: el nombre para saludar y la
    etiqueta del protocolo para elegir el guion. Todo lo demás —medicación,
    citas, fechas— se lee con herramientas cuando haga falta, aunque hubiera
    cabido aquí. Precargarlo sería exactamente el error que este diseño evita:
    una foto tomada al principio de la llamada que se va quedando vieja mientras
    se habla.
    """

    call_id: UUID | None
    patient_id: UUID
    nombre: str
    nombre_preferido: str
    procedimiento: str
    protocol_tag: str | None
    dias_postop: int


async def cargar_contexto(patient_id: UUID | str, call_id: UUID | str | None = None):
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT p.full_name, p.preferred_name,
                   s.procedure_name, s.protocol_tag,
                   (CURRENT_DATE - s.performed_at) AS dias
            FROM patients p
            LEFT JOIN LATERAL (
                SELECT * FROM surgeries WHERE patient_id = p.id
                ORDER BY performed_at DESC LIMIT 1
            ) s ON true
            WHERE p.id = %s
            """,
            (patient_id,),
        )
        fila = await cur.fetchone()

    if fila is None:
        raise LookupError(f"no existe el paciente {patient_id}")

    return ContextoLlamada(
        call_id=UUID(str(call_id)) if call_id else None,
        patient_id=UUID(str(patient_id)),
        nombre=fila["full_name"],
        nombre_preferido=fila["preferred_name"] or fila["full_name"].split()[0],
        procedimiento=fila["procedure_name"] or "cirugía",
        protocol_tag=fila["protocol_tag"],
        dias_postop=fila["dias"] if fila["dias"] is not None else 0,
    )


# ---------------------------------------------------------------------------
# Esquemas — el mínimo común denominador entre Gemini y Groq
# ---------------------------------------------------------------------------
_SIN_ARGUMENTOS: dict[str, Any] = {"type": "object", "properties": {}}


def esquemas(claves_survey: list[str]) -> list[dict[str, Any]]:
    """Declaración de las seis herramientas, en JSON Schema.

    `claves_survey` entra como `enum` de `registrar_respuesta`. Es la parte que
    más rinde de todo el esquema: sin ella el modelo inventa el nombre del campo
    —`dolor`, `nivel_dolor`, `dolor_escala` en tres llamadas distintas— y el
    `survey` de la tabla `calls` deja de ser consultable. Con el enum, un nombre
    inventado ni siquiera llega a salir del modelo en la mayoría de los casos, y
    si llega lo rechaza `registrar_respuesta`.
    """
    return [
        {
            "name": "obtener_paciente",
            "description": (
                "Datos identificativos del paciente al que estás llamando: nombre "
                "completo, nombre preferido y próxima cita. "
                "Úsala al principio antes de verificar la identidad."
            ),
            "parameters": _SIN_ARGUMENTOS,
        },
        {
            "name": "verificar_identidad",
            "description": (
                "Verifica la identidad del paciente comparando la cédula o número de documento "
                "dicho por el paciente con los registros del sistema. Devuelve coincide=true/false."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "documento_dicho": {
                        "type": "string",
                        "description": (
                            "El número de cédula o documento expresado por el paciente "
                            "(ejemplo: '1012345678' o '1.012.345.678')."
                        ),
                    }
                },
                "required": ["documento_dicho"],
            },
        },
        {
            "name": "obtener_cirugia",
            "description": (
                "La cirugía de este paciente: procedimiento, fecha, días transcurridos, "
                "cirujano y notas del alta. Úsala antes de preguntar nada clínico."
            ),
            "parameters": _SIN_ARGUMENTOS,
        },
        {
            "name": "obtener_medicacion",
            "description": (
                "Medicación ACTIVA hoy de este paciente, con dosis y pauta. Úsala "
                "siempre que el paciente pregunte por sus pastillas; nunca respondas "
                "de memoria."
            ),
            "parameters": _SIN_ARGUMENTOS,
        },
        {
            "name": "buscar_protocolo",
            "description": (
                "Busca en los protocolos clínicos del hospital. Úsala SIEMPRE antes de "
                "dar cualquier indicación clínica. Si devuelve hay_evidencia=false, no "
                "tienes la información y debes decirlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": (
                            "Qué buscar, en lenguaje natural. Por ejemplo: "
                            "'cuidado de la herida y cuándo puede ducharse'."
                        ),
                    }
                },
                "required": ["consulta"],
            },
        },
        {
            "name": "registrar_respuesta",
            "description": (
                "Anota la respuesta del paciente a una pregunta del guion. Llámala en "
                "cuanto tengas la respuesta, antes de pasar a la pregunta siguiente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clave": {
                        "type": "string",
                        "description": "Qué pregunta del guion se está anotando.",
                        "enum": claves_survey,
                    },
                    "valor": {
                        "type": "string",
                        "description": "Lo que ha contestado el paciente, resumido.",
                    },
                },
                "required": ["clave", "valor"],
            },
        },
        {
            "name": "escalar_a_equipo_clinico",
            "description": (
                "Deriva el caso al equipo clínico. Úsala ante cualquier signo de alarma "
                "y también cuando el paciente pregunte algo que los protocolos no cubren."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {
                        "type": "string",
                        "description": "Qué has detectado, en una frase clínica breve.",
                    },
                    "urgencia": {
                        "type": "string",
                        "enum": ["rutina", "prioritaria", "urgente"],
                        "description": (
                            "urgente: requiere atención inmediata. prioritaria: hoy. "
                            "rutina: el equipo lo revisa cuando pueda."
                        ),
                    },
                },
                "required": ["motivo", "urgencia"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Ejecutor
# ---------------------------------------------------------------------------
@dataclass
class Herramientas:
    """Ejecuta las llamadas del modelo contra la base y el RAG.

    Es un objeto con estado por llamada telefónica y no un módulo de funciones
    sueltas porque tiene que acumular dos cosas que la llamada necesita al
    cerrar: las citas usadas en cada turno y si hubo escalamiento.
    """

    contexto: ContextoLlamada
    citas: list[Cita] = field(default_factory=list)
    escalada: dict[str, str] | None = None
    survey: dict[str, str] = field(default_factory=dict)

    def esquemas(self) -> list[dict[str, Any]]:
        claves = sorted(
            claves_validas(self.contexto.protocol_tag, self.contexto.procedimiento)
        )
        return esquemas(claves)

    def nuevo_turno(self) -> None:
        """Vacía las citas del turno anterior.

        Las citas se publican **por turno** (`call_turns.citations`), no por
        llamada: el contrato pide poder señalar de dónde salió *esta* frase. Sin
        este vaciado, el turno 8 aparecería citando los protocolos del turno 2 y
        la auditoría dejaría de significar nada.
        """
        self.citas.clear()

    async def ejecutar(self, llamada: LlamadaHerramienta) -> str:
        """Ejecuta una herramienta y devuelve su resultado serializado en JSON.

        Nunca lanza: un fallo se devuelve como `{"error": …}` para que el modelo
        pueda decir «no he podido consultarlo, se lo paso a su equipo» en vez de
        que se caiga la llamada. En una conversación telefónica una excepción no
        capturada es un silencio, y un silencio es peor que un error dicho.
        """
        try:
            resultado = await self._despachar(llamada.nombre, llamada.argumentos)
        except Exception:
            log.exception("fallo ejecutando la herramienta %s", llamada.nombre)
            resultado = {
                "error": "no se ha podido consultar ese dato",
                "instruccion": (
                    "Dile al paciente que no has podido consultarlo y ofrécele "
                    "escalarlo con su equipo. No inventes el dato."
                ),
            }
        return json.dumps(resultado, ensure_ascii=False, default=str)

    async def _despachar(self, nombre: str, args: dict[str, Any]) -> dict[str, Any]:
        match nombre:
            case "obtener_paciente":
                return await self._paciente()
            case "verificar_identidad":
                return await self._verificar_identidad(
                    str(args.get("documento_dicho", "") or args.get("fecha_nacimiento_dicha", ""))
                )
            case "obtener_cirugia":
                return await self._cirugia()
            case "obtener_medicacion":
                return await self._medicacion()
            case "buscar_protocolo":
                return await self._protocolo(str(args.get("consulta", "")).strip())
            case "registrar_respuesta":
                return await self._registrar(
                    str(args.get("clave", "")), str(args.get("valor", ""))
                )
            case "escalar_a_equipo_clinico":
                return await self.escalar(
                    str(args.get("motivo", "")), str(args.get("urgencia", "prioritaria"))
                )
        return {"error": f"herramienta desconocida: {nombre}"}

    # -- datos vivos --------------------------------------------------------
    async def _paciente(self) -> dict[str, Any]:
        async with connection() as conn:
            cur = await conn.execute(
                """
                SELECT p.full_name, p.preferred_name, p.documento_cc,
                       (SELECT min(scheduled_at) FROM appointments a
                         WHERE a.patient_id = p.id AND a.status = 'scheduled'
                           AND a.scheduled_at >= now()) AS proxima_cita,
                       (SELECT location FROM appointments a
                         WHERE a.patient_id = p.id AND a.status = 'scheduled'
                           AND a.scheduled_at >= now()
                         ORDER BY a.scheduled_at LIMIT 1) AS lugar_cita
                FROM patients p WHERE p.id = %s
                """,
                (self.contexto.patient_id,),
            )
            fila = await cur.fetchone()
        if fila is None:
            return {"error": "el paciente no consta en la base de datos"}

        return {
            "nombre_completo": fila["full_name"],
            "nombre_preferido": fila["preferred_name"],
            "proxima_cita": fecha_en_palabras(fila["proxima_cita"]),
            "proxima_cita_hora": (
                fila["proxima_cita"].strftime("%H:%M") if fila["proxima_cita"] else None
            ),
            "lugar_cita": fila["lugar_cita"],
        }

    async def _verificar_identidad(self, documento_dicho: str) -> dict[str, Any]:
        async with connection() as conn:
            cur = await conn.execute(
                "SELECT documento_cc FROM patients WHERE id = %s",
                (self.contexto.patient_id,),
            )
            fila = await cur.fetchone()
        if fila is None or not fila["documento_cc"]:
            return {"coincide": False, "error": "paciente no encontrado"}

        doc_db = "".join(filter(str.isdigit, str(fila["documento_cc"])))
        doc_dicho = "".join(filter(str.isdigit, str(documento_dicho)))

        coincide = bool(
            doc_db
            and doc_dicho
            and (doc_db == doc_dicho or doc_db in doc_dicho or doc_dicho in doc_db)
        )

        if coincide:
            return {
                "coincide": True,
                "instruccion": (
                    "Identidad VERIFICADA correctamente. Procede a llamar a obtener_cirugia "
                    "y explicarle al paciente de qué le llamas."
                ),
            }

        return {
            "coincide": False,
            "instruccion": (
                "El número de cédula NO coincide con el del sistema. No des ningún "
                "dato personal ni clínico, di amablemente que volverás a llamar en otro "
                "momento y despídete."
            ),
        }

    async def _cirugia(self) -> dict[str, Any]:
        async with connection() as conn:
            cur = await conn.execute(
                """
                SELECT procedure_name, performed_at, surgeon, discharge_notes,
                       protocol_tag, (CURRENT_DATE - performed_at) AS dias
                FROM surgeries WHERE patient_id = %s
                ORDER BY performed_at DESC LIMIT 1
                """,
                (self.contexto.patient_id,),
            )
            fila = await cur.fetchone()
        if fila is None:
            return {"error": "no consta ninguna cirugía para este paciente"}

        return {
            "procedimiento": fila["procedure_name"],
            "fecha": fecha_en_palabras(fila["performed_at"]),
            "dias_desde_la_cirugia": fila["dias"],
            "cirujano": fila["surgeon"],
            "notas_del_alta": fila["discharge_notes"],
        }

    async def _medicacion(self) -> dict[str, Any]:
        async with connection() as conn:
            cur = await conn.execute(
                """
                SELECT m.name, m.dose, m.schedule, m.ends_on
                FROM medications m
                JOIN surgeries s ON s.id = m.surgery_id
                WHERE s.patient_id = %s
                  AND m.active
                  -- El flag `active` es lo que el hospital marcó; la fecha es lo
                  -- que manda hoy. Se exigen las dos: una pauta que terminó ayer
                  -- y que nadie desmarcó no se le recuerda a nadie.
                  AND (m.ends_on IS NULL OR m.ends_on >= CURRENT_DATE)
                ORDER BY m.name
                """,
                (self.contexto.patient_id,),
            )
            filas = await cur.fetchall()

        return {
            "medicacion_activa": [
                {
                    "nombre": f["name"],
                    "dosis": f["dose"],
                    "pauta": f["schedule"],
                    "hasta": fecha_en_palabras(f["ends_on"]),
                }
                for f in filas
            ],
            "instruccion": (
                "Puedes recordarle esta pauta tal cual. NUNCA la cambies, ni subas "
                "ni bajes dosis, ni sugieras suspender nada: eso lo decide su médico."
            ),
        }

    # -- conocimiento -------------------------------------------------------
    async def _protocolo(self, consulta: str) -> dict[str, Any]:
        """RAG con grounding obligatorio.

        La consulta se enriquece con el procedimiento del paciente antes de
        embeberla. No es un filtro —`retrieval.buscar` no filtra por etiqueta—
        sino un sesgo: con tres protocolos en el corpus, «¿cuándo me puedo
        duchar?» recupera indistintamente el de apendicectomía o el de hernia, y
        añadir «apendicectomía» a la consulta inclina las dos mitades de la
        búsqueda híbrida hacia el que toca. Un filtro duro por etiqueta sería más
        preciso y está pendiente: exige columna y `WHERE` en `retrievable_chunks`,
        que es de la Fase 1 y no es mía.
        """
        from app.db.traces import registrar_traza
        from app.rag.query import consultar

        if not consulta:
            return {"hay_evidencia": False, "fragmentos": [], "instruccion": _SIN_EVIDENCIA}

        etiqueta = etiqueta_de(self.contexto.protocol_tag, self.contexto.procedimiento)
        enriquecida = f"{self.contexto.procedimiento}. {consulta}" if etiqueta else consulta
        resultado = await consultar(enriquecida)

        await registrar_traza(
            span="retrieval",
            duration_ms=resultado.ms.get("total", 0),
            call_id=self.contexto.call_id,
            metadata={
                "consulta": consulta,
                "enriquecida": enriquecida,
                "hay_evidencia": resultado.hay_evidencia,
                "fragmentos_count": len(resultado.fragmentos),
                "ms": resultado.ms,
            },
        )

        if not resultado.hay_evidencia:
            # Sin fragmentos, ni siquiera los malos. Ver la cabecera del módulo.
            return {"hay_evidencia": False, "fragmentos": [], "instruccion": _SIN_EVIDENCIA}

        for f in resultado.fragmentos:
            cita = Cita(f.filename, f.heading, f.page)
            if cita.a_dict() not in [c.a_dict() for c in self.citas]:
                self.citas.append(cita)

        return {
            "hay_evidencia": True,
            "fragmentos": [
                {"texto": f.content, "documento": f.filename, "seccion": f.heading}
                for f in resultado.fragmentos
            ],
            "instruccion": (
                "Responde ÚNICAMENTE con lo que digan estos fragmentos, en una o dos "
                "frases habladas. Si no responden a la pregunta, dilo y ofrece escalar."
            ),
        }

    # -- escritura ----------------------------------------------------------
    async def _registrar(self, clave: str, valor: str) -> dict[str, Any]:
        validas = claves_validas(self.contexto.protocol_tag, self.contexto.procedimiento)
        if clave not in validas:
            # Se le devuelven las claves buenas en vez de un error a secas: el
            # modelo corrige y reintenta en el mismo turno.
            return {
                "error": f"«{clave}» no es una pregunta de este guion",
                "claves_validas": sorted(validas),
            }

        self.survey[clave] = valor
        if self.contexto.call_id is None:
            return {"registrado": clave}

        async with connection() as conn:
            # `||` sobre jsonb en una sola sentencia: dos turnos que registren a
            # la vez no se pisan, cosa que un read-modify-write en Python sí
            # haría. La llamada es de un solo hilo hoy, pero el coste de hacerlo
            # bien aquí es cero.
            await conn.execute(
                "UPDATE calls SET survey = survey || %s::jsonb WHERE id = %s",
                (json.dumps({clave: valor}, ensure_ascii=False), self.contexto.call_id),
            )
        return {"registrado": clave}

    async def escalar(self, motivo: str, urgencia: str) -> dict[str, Any]:
        """Pública, y no `_escalar`, porque el orquestador también escala.

        Cuando el LLM falla o agota las rondas, `agente._rendirse()` escala sin
        que el modelo haya pedido nada. Que esa vía exista es deliberado: un
        fallo técnico en una llamada clínica tiene que dejar rastro y aviso, no
        terminar en un «hasta luego» y silencio.
        """
        if urgencia not in ("rutina", "prioritaria", "urgente"):
            urgencia = "prioritaria"
        self.escalada = {"motivo": motivo, "urgencia": urgencia}

        if self.contexto.call_id is not None:
            async with connection() as conn:
                await conn.execute(
                    """
                    UPDATE calls
                       SET escalated = true,
                           escalation_reason = %s,
                           escalation_urgency = %s
                     WHERE id = %s
                    """,
                    (motivo, urgencia, self.contexto.call_id),
                )

        return {
            "escalado": True,
            "urgencia": urgencia,
            "instruccion": (
                "El caso ya está registrado. Dile al paciente que su equipo médico "
                "va a recibir el aviso, dale la indicación del protocolo y "
                "asegúrate de que la ha entendido antes de despedirte."
            ),
        }


_SIN_EVIDENCIA = (
    "No hay ningún protocolo que responda a esto. Dile literalmente que no tienes "
    "esa información, ofrécele consultarlo con su equipo médico y usa "
    "escalar_a_equipo_clinico. NO respondas con conocimiento general de medicina."
)
