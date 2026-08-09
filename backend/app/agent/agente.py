"""El agente de seguimiento: un turno de conversación de principio a fin.

Aquí se juntan las cuatro piezas —detector de banderas rojas, prompt, tools y
LLM— y se decide el orden en que actúan. Ese orden es la parte importante:

    texto del paciente
        │
        ├─ 1. redflags.detectar()      determinista, ~0 ms, SIN LLM
        │        └─ si hay alarma, el prompt de ESTE turno cambia
        │
        ├─ 2. bucle de herramientas    LLM ⇄ Postgres / RAG
        │
        └─ 3. texto hablado + citas + escalamiento

El detector va **antes** del modelo y no después, y no es un detalle de
implementación: si el LLM tuviera que decidir si hay alarma, la decisión
dependería de un muestreo. Yendo antes, cuando el modelo entra ya no decide nada
sobre la alarma; solo redacta lo que el protocolo dice que hay que decir.

── Por qué esta clase implementa `LLMClient` ────────────────────────────────
`app/voice/**` es de otro agente y no se puede tocar. `SesionVoz` acepta
cualquier objeto con la interfaz `LLMClient` y hoy recibe un `ClienteLLMFalso`.
Implementando esa misma interfaz, el agente real entra en el bucle de voz
cambiando un argumento —`SesionVoz(llm=AgenteLlamada(...))`— sin una sola línea
nueva en el pipeline.

El precio es una rareza que conviene tener escrita: `stream(sistema, mensajes)`
**ignora** los dos argumentos. El sistema lo construye el agente (depende del
paciente y de si hay alarma) y el historial lo lleva él mismo, porque `SesionVoz`
manda solo el último turno. La firma se respeta; la semántica es «toma lo que ha
dicho el paciente y responde».

── Lo que este bucle todavía no hace ────────────────────────────────────────
No hay streaming real de la respuesta final: se emite por frases en cuanto el
modelo termina, no conforme la escribe. Con herramientas de por medio no hay
alternativa —hasta que la tool no responde no hay nada que decir— pero en los
turnos sin herramientas se está dejando sobre la mesa la diferencia entre el
primer token y la respuesta completa. Está medido en el informe y es la primera
optimización pendiente de la Fase 6.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.agent import prompts, redflags
from app.agent.llm_client import LLMClient, Mensaje, RespuestaLLM, crear_cliente
from app.agent.redflags import BanderaRoja
from app.agent.tools import Cita, ContextoLlamada, Herramientas, cargar_contexto
from app.db.pool import connection

log = logging.getLogger("agente")

MAX_RONDAS = 4
"""Vueltas de herramienta antes de rendirse en un turno.

Un turno normal gasta una o dos: `buscar_protocolo` y a veces
`registrar_respuesta`. Cuatro cubre el caso peor real —alarma: buscar protocolo,
escalar y registrar— y corta los bucles en los que el modelo repite la misma
llamada porque no le gusta el resultado. Sin tope, ese bucle se come el tiempo de
la llamada en silencio, que por teléfono es lo mismo que colgar."""


@dataclass
class TurnoAgente:
    """Todo lo que produce un turno. Es también lo que el WebSocket publica."""

    texto: str
    citas: list[Cita] = field(default_factory=list)
    banderas: list[BanderaRoja] = field(default_factory=list)
    escalada: dict[str, str] | None = None
    terminar: bool = False
    ms: dict[str, int] = field(default_factory=dict)
    rondas: int = 0

    def a_dict(self) -> dict[str, Any]:
        return {
            "texto": self.texto,
            "citas": [c.a_dict() for c in self.citas],
            "banderas": [
                {"codigo": b.codigo, "motivo": b.motivo, "urgencia": b.urgencia,
                 "evidencia": b.evidencia}
                for b in self.banderas
            ],
            "escalada": self.escalada,
            "terminar": self.terminar,
            "ms": self.ms,
        }


class AgenteLlamada(LLMClient):
    def __init__(
        self,
        contexto: ContextoLlamada,
        llm: LLMClient | None = None,
        max_rondas: int = MAX_RONDAS,
    ) -> None:
        self.contexto = contexto
        self.llm = llm or crear_cliente()
        self.herramientas = Herramientas(contexto)
        self.historial: list[Mensaje] = []
        self.max_rondas = max_rondas
        self.alarma: BanderaRoja | None = None
        self.ultimo_turno: TurnoAgente | None = None
        self.saludo: str | None = None
        """Lo que dijo `apertura()`, para quien tenga que pronunciarlo después.

        `POST /api/calls` genera el saludo y lo registra, pero quien lo dice en
        voz alta es el WebSocket, que se conecta más tarde. Sin esto tendría que
        reconstruirlo con `prompts.SALUDO.format(...)` por su cuenta —dos sitios
        generando la frase que el AI Act obliga a poder demostrar— o rebuscar en
        `historial[0]`, que deja de ser el saludo en cuanto alguien cambie el
        orden de los mensajes."""
        self._esperando_confirmacion = False
        self._sistema = prompts.sistema(contexto)

    @classmethod
    async def para_llamada(
        cls,
        patient_id: UUID | str,
        call_id: UUID | str | None = None,
        llm: LLMClient | None = None,
    ) -> AgenteLlamada:
        return cls(await cargar_contexto(patient_id, call_id), llm=llm)

    # -- apertura -----------------------------------------------------------
    def apertura(self) -> str:
        """Primera intervención del agente. Constante, no generada.

        Se registra en el historial como si la hubiera dicho el modelo para que
        el turno siguiente tenga sentido: sin esto, el modelo recibe la respuesta
        del paciente («sí, soy yo») sin la pregunta que la provocó."""
        texto = prompts.SALUDO.format(nombre=self.contexto.nombre_preferido)
        self.historial.append(Mensaje("assistant", texto))
        self.saludo = texto
        return texto

    # -- el turno -----------------------------------------------------------
    async def responder_paciente(self, texto_paciente: str) -> TurnoAgente:
        t0 = time.perf_counter()
        self.herramientas.nuevo_turno()

        # 1. Banderas rojas. Solo sobre lo que dice el PACIENTE: pasarle también
        #    el texto del agente haría que «¿ha tenido fiebre por encima de 38?»
        #    se detectara a sí misma.
        banderas = redflags.detectar(texto_paciente)
        nueva_alarma = banderas and self.alarma is None
        if nueva_alarma:
            self.alarma = redflags.peor(banderas)

        self.historial.append(Mensaje("user", texto_paciente))
        sistema = self._sistema_del_turno(bool(nueva_alarma))

        t_llm = time.perf_counter()
        texto, rondas = await self._bucle_herramientas(sistema)
        ms_llm = (time.perf_counter() - t_llm) * 1000

        turno = TurnoAgente(
            texto=texto,
            citas=list(self.herramientas.citas),
            banderas=banderas,
            escalada=self.herramientas.escalada,
            rondas=rondas,
            ms={"llm": round(ms_llm), "turno": round((time.perf_counter() - t0) * 1000)},
        )

        # Decisión 2 del contrato: ante bandera roja el agente corta. Pero no
        # cuelga en el mismo turno — antes tiene que comprobar que el paciente ha
        # entendido la indicación de urgencia, y eso ocupa un turno más. De ahí
        # los dos estados en vez de un `terminar` inmediato.
        if nueva_alarma:
            self._esperando_confirmacion = True
        elif self._esperando_confirmacion:
            self._esperando_confirmacion = False
            turno.terminar = True

        self.ultimo_turno = turno
        return turno

    def _sistema_del_turno(self, hay_alarma_nueva: bool) -> str:
        """El prompt de sistema cambia con la fase de la llamada.

        Es la forma de dar una orden que solo vale AHORA sin ensuciar el
        historial con mensajes falsos del paciente. Se descartó inyectar la orden
        como un `Mensaje("user", "[sistema] …")`: funciona, pero acaba en la
        transcripción que se le enseña al equipo clínico, donde parece que el
        paciente dijo algo que no dijo.
        """
        if hay_alarma_nueva and self.alarma is not None:
            return (
                self._sistema
                + "\n\n"
                + prompts.BANDERA_ROJA.format(
                    motivo=self.alarma.motivo, urgencia=self.alarma.urgencia
                )
            )
        if self._esperando_confirmacion:
            return self._sistema + "\n\n" + prompts.CONFIRMACION_ENTENDIDO
        return self._sistema

    async def _bucle_herramientas(self, sistema: str) -> tuple[str, int]:
        """LLM ⇄ herramientas hasta que el modelo produzca texto para hablar."""
        esquemas = self.herramientas.esquemas()

        for ronda in range(1, self.max_rondas + 1):
            try:
                resp = await self.llm.responder(sistema, self.historial, esquemas)
            except Exception:
                log.exception("el LLM falló en la ronda %d", ronda)
                return await self._rendirse(), ronda

            if not resp.herramientas:
                texto = resp.texto.strip()
                if not texto:
                    # Pasa con `finish_reason=MAX_TOKENS` y con filtros de
                    # seguridad: la respuesta llega vacía y sin error.
                    log.warning("respuesta vacía del LLM (%s)", resp.motivo_fin)
                    return await self._rendirse(), ronda
                self.historial.append(Mensaje("assistant", texto))
                return texto, ronda

            self.historial.append(
                Mensaje("assistant", resp.texto, herramientas=resp.herramientas)
            )
            for llamada in resp.herramientas:
                resultado = await self.herramientas.ejecutar(llamada)
                self.historial.append(
                    Mensaje("tool", resultado, tool_call_id=llamada.id, name=llamada.nombre)
                )

        log.warning("el modelo agotó %d rondas de herramientas sin responder", self.max_rondas)
        return await self._rendirse(), self.max_rondas

    async def _rendirse(self) -> str:
        """Salida segura: frase fija y escalamiento.

        Un agente clínico que no sabe qué decir no se calla ni improvisa: avisa
        al equipo. Se escala aunque el fallo sea técnico porque desde el otro
        lado del teléfono no se distingue —y porque una llamada que no se
        completó es, en sí misma, algo que alguien debería revisar.
        """
        await self.herramientas.escalar(
            "la llamada automática no pudo completarse", "prioritaria"
        )
        return prompts.FRASE_SEGURIDAD

    # -- interfaz LLMClient (para enchufarlo en SesionVoz sin tocar voice/) ---
    async def responder(
        self,
        sistema: str,
        mensajes: list[Mensaje],
        herramientas: list[dict[str, Any]] | None = None,
    ) -> RespuestaLLM:
        turno = await self.responder_paciente(_ultimo_usuario(mensajes))
        return RespuestaLLM(turno.texto, [], float(turno.ms.get("llm", 0)))

    async def stream(self, sistema: str, mensajes: list[Mensaje]) -> AsyncIterator[str]:
        from app.voice.tts import dividir_en_frases

        turno = await self.responder_paciente(_ultimo_usuario(mensajes))
        self.ultimo_turno = turno
        # Por frases y no de golpe: `SesionVoz` sintetiza cada frase cerrada en
        # cuanto la recibe, así que trocear aquí adelanta el primer audio lo que
        # tarde el TTS en las frases siguientes.
        for frase in dividir_en_frases(turno.texto) or [turno.texto]:
            yield frase + " "


def _ultimo_usuario(mensajes: list[Mensaje]) -> str:
    for m in reversed(mensajes):
        if m.role == "user":
            return m.content
    return ""


# ---------------------------------------------------------------------------
# Persistencia de la conversación
# ---------------------------------------------------------------------------
async def guardar_turno(
    call_id: UUID | str,
    quien: str,
    texto: str,
    citas: list[Cita] | None = None,
    ms: dict[str, int] | None = None,
) -> None:
    """Un turno en `call_turns`, con sus citas y sus latencias.

    Las citas se guardan **por turno** y no por llamada porque es lo que hace
    auditable el sistema: poder señalar una frase concreta del agente y ver de
    qué documento y de qué sección salió. Una lista de citas a nivel de llamada
    no responde a esa pregunta.
    """
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO call_turns (call_id, role, content, citations, latencies)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
            """,
            (
                call_id,
                quien,
                texto,
                json.dumps([c.a_dict() for c in (citas or [])], ensure_ascii=False),
                json.dumps(ms or {}),
            ),
        )
