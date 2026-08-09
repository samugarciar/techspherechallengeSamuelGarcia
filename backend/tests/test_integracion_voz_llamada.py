"""La costura entre las Fases 3, 4 y 5: voz → agente clínico → `call_turns`.

Cada fase estaba probada por separado y ninguna prueba cruzaba la frontera, que
es exactamente donde estaban los tres fallos. Esto es lo que faltaba: audio por
un lado, filas de Postgres y mensajes del contrato por el otro.

Qué es real y qué se dobla, y por qué:

- **Postgres, real.** Lo que se comprueba —que los turnos quedan escritos, con
  sus citas, en el orden en que se dijeron— es una propiedad de la base. Contra
  un doble pasaría aunque no se guardara nada, que es el fallo que había.
- **El agente clínico, real.** `AgenteLlamada` entero: guion, herramientas,
  detector de banderas rojas y máquina de fases. Es la pieza que se está
  enchufando; doblarla sería probar el enchufe contra sí mismo.
- **El LLM y el RAG, doblados.** Un modelo real haría estos tests intermitentes
  —la misma frase sale con `registrar_respuesta` o sin ella— y cargar bge-m3 más
  el cross-encoder son decenas de segundos y varios GB. Lo que se prueba aquí es
  el recorrido, no la calidad de la respuesta.
- **Whisper y el TTS, doblados.** Ya tienen sus propias pruebas y su spike; aquí
  solo añadirían 400 ms y varianza a cada aserción.

`postop_t2`, nunca `postop`. El guardarraíl está en `tests/ayuda_db.py`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_voz_barge_in import STTFalso, TTSFalso, cargar, esperar, inyectar, silencio

from app.agent import agente as modulo_agente
from app.agent.llm_client import LlamadaHerramienta, LLMClient, Mensaje, RespuestaLLM
from app.api import errores, llamadas
from app.core.config import get_settings
from app.db.pool import connection
from app.voice.pipeline_ws import SesionVoz
from tests.ayuda_db import hay_postgres, pool  # noqa: F401 — pytest registra la fixture

pytestmark = hay_postgres


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------
@dataclass
class LLMGuionizado(LLMClient):
    """Devuelve las respuestas de una lista, en orden, y apunta lo que recibe."""

    respuestas: list[RespuestaLLM] = field(default_factory=list)
    sistemas: list[str] = field(default_factory=list)
    historiales: list[list[Mensaje]] = field(default_factory=list)

    async def responder(self, sistema, mensajes, herramientas=None) -> RespuestaLLM:
        self.sistemas.append(sistema)
        self.historiales.append(list(mensajes))
        if not self.respuestas:
            return RespuestaLLM("De acuerdo, lo anoto.")
        return self.respuestas.pop(0)

    async def stream(self, sistema, mensajes):
        resp = await self.responder(sistema, mensajes)
        yield resp.texto

    def dijo(self) -> list[str]:
        """Lo que el paciente le llegó a decir, según su historial."""
        return [m.content for h in self.historiales for m in h if m.role == "user"]


def herramienta(nombre: str, **args: Any) -> RespuestaLLM:
    return RespuestaLLM(
        "", [LlamadaHerramienta(id=f"c_{nombre}", nombre=nombre, argumentos=args)]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
PACIENTES = {
    "ana": UUID("dddddddd-0000-0000-0000-0000000000b1"),
    "bruno": UUID("dddddddd-0000-0000-0000-0000000000b2"),
}


async def _crear_paciente(patient_id: UUID, nombre: str, preferido: str) -> None:
    async with connection() as conn:
        await conn.execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        await conn.execute(
            """
            INSERT INTO patients (id, full_name, preferred_name, birth_date, phone)
            VALUES (%s, %s, %s, '1981-03-09', '+34 600 111 222')
            """,
            (patient_id, nombre, preferido),
        )
        await conn.execute(
            """
            INSERT INTO surgeries (patient_id, procedure_name, performed_at, surgeon,
                                   protocol_tag, discharge_notes)
            VALUES (%s, 'Colecistectomía laparoscópica', CURRENT_DATE - 4, 'Dr. Peláez',
                    'colecistectomia', 'Sin conversión a cirugía abierta.')
            """,
            (patient_id,),
        )


@pytest_asyncio.fixture(name="pacientes")
async def _pacientes(pool):  # noqa: F811
    """Dos pacientes propios. El segundo es el que hace falta para probar que dos
    llamadas simultáneas no se mezclan."""
    await _crear_paciente(PACIENTES["ana"], "Ana Lucía Serrano Vidal", "Ana")
    await _crear_paciente(PACIENTES["bruno"], "Bruno Casal Meira", "Bruno")
    try:
        yield PACIENTES
    finally:
        async with connection() as conn:
            for patient_id in PACIENTES.values():
                await conn.execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        llamadas._AGENTES.clear()
        llamadas._VOCES.clear()


@pytest.fixture(name="llm")
def _llm(monkeypatch) -> LLMGuionizado:
    doble = LLMGuionizado()

    class AgenteDeprueba(modulo_agente.AgenteLlamada):
        @classmethod
        async def para_llamada(cls, patient_id, call_id=None, llm=None):
            return await super().para_llamada(patient_id, call_id, llm=doble)

    monkeypatch.setattr(llamadas, "AgenteLlamada", AgenteDeprueba)
    return doble


@pytest.fixture(name="llm_por_llamada")
def _llm_por_llamada(monkeypatch) -> dict[str, LLMGuionizado]:
    """Un LLM doblado DISTINTO por llamada, indexado por `call_id`.

    Con un solo doble compartido no se podría distinguir «cada agente lleva su
    historial» de «hay un historial único donde caben las dos conversaciones»,
    que es justo lo que hay que demostrar.
    """
    dobles: dict[str, LLMGuionizado] = {}

    class AgenteDeprueba(modulo_agente.AgenteLlamada):
        @classmethod
        async def para_llamada(cls, patient_id, call_id=None, llm=None):
            doble = LLMGuionizado()
            dobles[str(call_id)] = doble
            return await super().para_llamada(patient_id, call_id, llm=doble)

    monkeypatch.setattr(llamadas, "AgenteLlamada", AgenteDeprueba)
    return dobles


@pytest.fixture(name="rag")
def _rag(monkeypatch):
    """Doble del RAG, con o sin evidencia a voluntad."""
    from app.rag import query as modulo_query
    from app.rag.retrieval import Fragmento

    estado: dict[str, Any] = {"fragmentos": []}

    async def _consultar(texto: str, top_k: int | None = None):
        return modulo_query.Resultado(fragmentos=estado["fragmentos"], ms={})

    monkeypatch.setattr(modulo_query, "consultar", _consultar)
    estado["responder_con"] = lambda frs: estado.__setitem__("fragmentos", frs)
    estado["fragmento"] = lambda score: Fragmento(
        chunk_id=str(uuid4()), document_id=str(uuid4()),
        filename="protocolo_colecist.pdf",
        content="Ante fiebre superior a 38,5 grados, acuda a urgencias el mismo día.",
        heading="Signos de alarma", page=4, score=score,
    )
    return estado


@pytest_asyncio.fixture(name="cliente")
async def _cliente(pacientes):
    app = FastAPI()
    errores.registrar_manejadores(app)
    app.include_router(llamadas.router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://pruebas"
    ) as c:
        yield c


@pytest.fixture(name="cabeceras")
def _cabeceras() -> dict[str, str]:
    return {"X-Admin-Token": get_settings().admin_token}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
class Grabadora:
    """Recoge lo que la sesión manda al cliente, sin WebSocket de por medio."""

    def __init__(self) -> None:
        self.eventos: list[dict] = []
        self.bytes_audio = 0

    async def audio(self, pcm16: bytes) -> None:
        self.bytes_audio += len(pcm16)

    async def evento(self, datos: dict) -> None:
        self.eventos.append(datos)

    def tipos(self) -> list[str]:
        return [e["tipo"] for e in self.eventos]

    def primero(self, tipo: str) -> dict | None:
        return next((e for e in self.eventos if e["tipo"] == tipo), None)


async def _abrir_llamada(cliente, cabeceras, patient_id: UUID) -> str:
    r = await cliente.post(
        "/api/calls", json={"patient_id": str(patient_id)}, headers=cabeceras
    )
    assert r.status_code == 201, r.text
    return r.json()["call_id"]


async def _montar_voz(call_id: str, stt: STTFalso) -> tuple[SesionVoz, Grabadora]:
    """Lo mismo que hace el endpoint `/ws/voz?call_id=…`, sin red."""
    grabadora = Grabadora()
    clinica = await llamadas.abrir_sesion_de_voz(call_id, grabadora.evento)
    assert clinica is not None, "no se pudo enganchar la voz a la llamada"
    sesion = SesionVoz(
        enviar_audio=grabadora.audio,
        enviar_evento=grabadora.evento,
        stt=stt,
        motor_tts=TTSFalso(segundos_por_frase=0.05),
        llm=clinica,
        llamada=clinica,
    )
    return sesion, grabadora


async def _turno(sesion: SesionVoz, stt: STTFalso, texto: str) -> None:
    """El paciente dice `texto`. Espera a que el turno termine."""
    stt.texto = texto
    antes = len(sesion.turnos)
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    assert await esperar(lambda: len(sesion.turnos) > antes), f"el turno «{texto}» no terminó"


async def _turnos_guardados(call_id: str) -> list[dict]:
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT role, content, citations, latencies FROM call_turns "
            "WHERE call_id = %s ORDER BY id",
            (call_id,),
        )
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# Los siete mensajes del contrato
# ---------------------------------------------------------------------------
async def test_un_turno_emite_los_mensajes_clinicos_del_contrato(
    cliente, cabeceras, llm, rag
) -> None:
    """`estado`, `transcripcion`, `citas` y `metricas` en un solo turno.

    Los cuatro existían en el contrato y no los emitía nadie: el frontend los
    escuchaba y el pipeline solo mandaba `listo`. `citas` es el que más importa
    —es lo que hace auditable cada afirmación clínica— y el que no se podía
    emitir desde el bucle de voz, porque quien las produce es el agente.
    """
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    rag["responder_con"]([rag["fragmento"](0.91)])
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="cuidado de la herida"),
        RespuestaLLM("El protocolo dice que puede ducharse a las cuarenta y ocho horas."),
    ]

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "¿cuándo me puedo duchar?")

    tipos = grabadora.tipos()
    assert {"estado", "transcripcion", "citas", "metricas"} <= set(tipos)

    # Las fases, en el orden real de un turno.
    fases = [e["fase"] for e in grabadora.eventos if e["tipo"] == "estado"]
    assert fases[:3] == ["escuchando", "pensando", "hablando"]

    transcripcion = grabadora.primero("transcripcion")
    assert transcripcion == {
        "tipo": "transcripcion", "quien": "paciente",
        "texto": "¿cuándo me puedo duchar?", "parcial": False,
    }

    # La cita sale del RAG, con documento y sección: es lo que permite señalar
    # una frase del agente y decir de dónde salió.
    assert grabadora.primero("citas")["citas"] == [
        {"filename": "protocolo_colecist.pdf", "heading": "Signos de alarma", "page": 4}
    ]

    # Y llega ANTES del audio que fundamenta, no después: el cliente pega las
    # citas a la intervención abierta, y tarde se pegarían a la siguiente.
    assert tipos.index("citas") < tipos.index("agente_habla")

    ms = grabadora.primero("metricas")["ms"]
    assert set(ms) == {"stt", "llm", "tts", "total"}
    assert ms["total"] >= 0

    await sesion.cerrar()


async def test_sin_evidencia_no_se_anuncian_citas(cliente, cabeceras, llm, rag) -> None:
    """El contrapeso: si el agente no citó nada, la pantalla no debe inventar un
    panel de citas. Un sistema que siempre muestra fuentes deja de significar que
    las tiene."""
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    rag["responder_con"]([rag["fragmento"](0.05)])  # por debajo del umbral
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="puedo tomar el sol"),
        RespuestaLLM("Eso no lo tengo. Se lo paso a su equipo."),
    ]

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "¿puedo tomar el sol?")

    assert "citas" not in grabadora.tipos()
    await sesion.cerrar()


# ---------------------------------------------------------------------------
# Bandera roja — decisión 2 del contrato
# ---------------------------------------------------------------------------
async def test_la_bandera_roja_llega_al_cliente_y_la_llamada_acaba_escalada(
    cliente, cabeceras, llm, rag
) -> None:
    """El momento clínico de la demo, de punta a punta.

    El paciente dice que tiene 39 de fiebre. El detector —determinista, sin
    LLM— dispara, el cliente recibe `bandera_roja`, el agente da la indicación
    del protocolo y pregunta si se ha entendido. Solo cuando el paciente
    confirma llega `fin` con `motivo: escalada`, que es la decisión 2 del
    contrato: no se cuelga hasta comprobar que la indicación llegó.
    """
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    rag["responder_con"]([rag["fragmento"](0.95)])
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="fiebre alta postoperatoria"),
        herramienta("escalar_a_equipo_clinico",
                    motivo="fiebre de 39 grados", urgencia="urgente"),
        RespuestaLLM("Acuda hoy a urgencias. ¿Me ha entendido lo que tiene que hacer?"),
    ]

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "me tomé la temperatura y tengo treinta y nueve")

    bandera = grabadora.primero("bandera_roja")
    assert bandera is not None, "no se avisó de la bandera roja"
    assert bandera["urgencia"] == "urgente"
    assert "39" in bandera["motivo"]
    # Todavía NO se acaba: falta que el paciente confirme.
    assert "fin" not in grabadora.tipos()
    assert not sesion.terminada

    llm.respuestas = [RespuestaLLM("Perfecto. Su equipo la llamará enseguida. Cuídese.")]
    await _turno(sesion, stt, "sí, entendido, voy para allá")

    fin = grabadora.primero("fin")
    assert fin == {"tipo": "fin", "motivo": "escalada"}
    assert sesion.terminada

    await sesion.cerrar()

    # Y la llamada queda escalada en la base, no solo en la pantalla.
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT status, escalated, escalation_urgency, ended_at FROM calls WHERE id = %s",
            (call_id,),
        )
        fila = await cur.fetchone()
    assert fila["escalated"] is True
    assert fila["status"] == "escalated"
    assert fila["escalation_urgency"] == "urgente"
    assert fila["ended_at"] is not None


async def test_tras_el_fin_el_audio_que_llegue_tarde_no_abre_otro_turno(
    cliente, cabeceras, llm, rag
) -> None:
    """El navegador tarda un momento en soltar el micrófono. Lo que entre en ese
    hueco no puede hacer que el agente hable después de despedirse."""
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    rag["responder_con"]([rag["fragmento"](0.95)])
    llm.respuestas = [
        herramienta("escalar_a_equipo_clinico", motivo="sangrado activo", urgencia="urgente"),
        RespuestaLLM("Presione la herida y acuda a urgencias. ¿Me ha entendido?"),
    ]

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "la herida no para de sangrar")

    llm.respuestas = [RespuestaLLM("Muy bien. Cuídese.")]
    await _turno(sesion, stt, "sí, lo he entendido")
    assert sesion.terminada
    turnos_al_acabar = len(sesion.turnos)

    stt.texto = "¿oiga, sigue ahí?"
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    await asyncio.sleep(0.2)

    assert len(sesion.turnos) == turnos_al_acabar, "el agente contestó tras despedirse"
    await sesion.cerrar()


# ---------------------------------------------------------------------------
# La transcripción
# ---------------------------------------------------------------------------
async def test_la_conversacion_queda_en_call_turns_con_sus_citas(
    cliente, cabeceras, llm, rag
) -> None:
    """El fallo que dejaba el historial de la Fase 5 siempre vacío.

    `/ws/voz` no sabía qué era un `call_id`, así que ninguna palabra dicha por
    voz llegaba nunca a `call_turns`.
    """
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    rag["responder_con"]([rag["fragmento"](0.9)])
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="herida"),
        RespuestaLLM("La herida se cura con suero. Puede ducharse a las cuarenta y ocho horas."),
    ]

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "la herida la tengo un poco roja")
    await sesion.cerrar()

    filas = await _turnos_guardados(call_id)
    # El saludo lo escribió `POST /api/calls`; los dos siguientes son la voz.
    assert [f["role"] for f in filas] == ["agent", "patient", "agent"]
    assert filas[1]["content"] == "la herida la tengo un poco roja"
    assert "suero" in filas[2]["content"]

    # Las citas van en el turno del AGENTE, que es el que hay que poder auditar.
    assert filas[2]["citations"][0]["heading"] == "Signos de alarma"
    assert filas[1]["citations"] == []

    # Y las latencias se reparten según a quién describen.
    assert "stt" in filas[1]["latencies"]
    assert {"llm", "tts", "total"} <= set(filas[2]["latencies"])

    # Y el detalle del contrato lo publica en orden.
    r = await cliente.get(f"/api/calls/{call_id}", headers=cabeceras)
    turnos = r.json()["turnos"]
    assert [t["quien"] for t in turnos] == ["agente", "paciente", "agente"]
    assert [t["ordinal"] for t in turnos] == [1, 2, 3]
    assert turnos[2]["citas"][0]["filename"] == "protocolo_colecist.pdf"


async def test_colgar_sin_que_el_agente_termine_deja_la_llamada_cortada(
    cliente, cabeceras, llm
) -> None:
    """Se va el WebSocket a mitad. La llamada tiene que quedar sellada: si no,
    el historial la enseñaría «en curso» para siempre y con la duración
    subiendo."""
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    llm.respuestas = [RespuestaLLM("Muy bien, ¿y el dolor cómo va?")]

    stt = STTFalso()
    sesion, _ = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "sí, soy yo")
    await sesion.cerrar()

    r = await cliente.get(f"/api/calls/{call_id}", headers=cabeceras)
    assert r.json()["estado"] == "interrumpida"
    assert r.json()["terminada"] is not None


# ---------------------------------------------------------------------------
# Estado por llamada — el riesgo del diccionario en memoria
# ---------------------------------------------------------------------------
async def test_dos_llamadas_a_la_vez_no_comparten_historial(
    cliente, cabeceras, llm_por_llamada, rag
) -> None:
    """El agente guarda historial y fase en memoria del proceso (§Cambios 6).

    Dos llamadas simultáneas por WebSocket no pueden compartir ese estado. Si lo
    compartieran, el agente de Ana vería lo que dijo Bruno —y podría contárselo—,
    que en una llamada clínica es una fuga de datos entre pacientes, no un bug
    de presentación.

    Los turnos se intercalan a propósito: si el estado fuera global, el orden
    A-B-A-B es el que lo destapa.
    """
    call_a = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    call_b = await _abrir_llamada(cliente, cabeceras, PACIENTES["bruno"])
    assert call_a != call_b

    stt_a, stt_b = STTFalso(), STTFalso()
    sesion_a, grab_a = await _montar_voz(call_a, stt_a)
    sesion_b, grab_b = await _montar_voz(call_b, stt_b)

    await _turno(sesion_a, stt_a, "soy Ana, me duele un poco la herida")
    await _turno(sesion_b, stt_b, "soy Bruno, yo estoy perfectamente")
    await _turno(sesion_a, stt_a, "el dolor es un cuatro")
    await _turno(sesion_b, stt_b, "no he tenido nada de fiebre")

    await sesion_a.cerrar()
    await sesion_b.cerrar()

    # 1. Cada agente solo ha oído a su paciente.
    dijo_a = llm_por_llamada[call_a].dijo()
    dijo_b = llm_por_llamada[call_b].dijo()
    assert any("Ana" in t for t in dijo_a)
    assert not any("Bruno" in t for t in dijo_a), "el agente de Ana oyó a Bruno"
    assert any("Bruno" in t for t in dijo_b)
    assert not any("Ana" in t for t in dijo_b), "el agente de Bruno oyó a Ana"

    # 2. Y cada llamada guarda solo sus turnos, en su orden.
    for call_id, quien, otro in ((call_a, "Ana", "Bruno"), (call_b, "Bruno", "Ana")):
        dichos = [f["content"] for f in await _turnos_guardados(call_id)]
        assert any(quien in t for t in dichos), (call_id, dichos)
        assert not any(otro in t for t in dichos), f"{call_id} guardó turnos de {otro}"

    # 3. Y los mensajes tampoco se cruzaron por el cable.
    assert any("Ana" in e.get("texto", "") for e in grab_a.eventos)
    assert not any("Bruno" in e.get("texto", "") for e in grab_a.eventos)
    assert not any("Ana" in e.get("texto", "") for e in grab_b.eventos)


async def test_una_bandera_roja_no_termina_la_llamada_de_al_lado(
    cliente, cabeceras, llm_por_llamada, rag
) -> None:
    """La fase de la llamada también es estado por llamada. Si `terminar` fuera
    global, escalar a un paciente colgaría el teléfono del otro."""
    call_a = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    call_b = await _abrir_llamada(cliente, cabeceras, PACIENTES["bruno"])
    rag["responder_con"]([rag["fragmento"](0.95)])

    stt_a, stt_b = STTFalso(), STTFalso()
    sesion_a, _ = await _montar_voz(call_a, stt_a)
    sesion_b, grab_b = await _montar_voz(call_b, stt_b)

    llm_por_llamada[call_a].respuestas = [
        herramienta("escalar_a_equipo_clinico", motivo="fiebre de 39,2", urgencia="urgente"),
        RespuestaLLM("Acuda a urgencias. ¿Me ha entendido?"),
    ]
    await _turno(sesion_a, stt_a, "tengo treinta y nueve de fiebre")
    llm_por_llamada[call_a].respuestas = [RespuestaLLM("Muy bien. Cuídese.")]
    await _turno(sesion_a, stt_a, "sí, entendido")

    assert sesion_a.terminada
    assert not sesion_b.terminada, "escalar a Ana colgó la llamada de Bruno"
    assert "fin" not in grab_b.tipos()

    # Y Bruno sigue pudiendo hablar.
    llm_por_llamada[call_b].respuestas = [RespuestaLLM("Me alegro. ¿Y la herida?")]
    await _turno(sesion_b, stt_b, "yo estoy bien")

    await sesion_a.cerrar()
    await sesion_b.cerrar()


async def test_no_se_enganchan_dos_websockets_a_la_misma_llamada(
    cliente, cabeceras, llm
) -> None:
    """Un doble clic en «Llamar» o una pestaña duplicada. Dos conexiones sobre el
    mismo `AgenteLlamada` entrelazarían los turnos en un solo historial."""
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])

    primera = await llamadas.abrir_sesion_de_voz(call_id, Grabadora().evento)
    assert primera is not None
    segunda = await llamadas.abrir_sesion_de_voz(call_id, Grabadora().evento)
    assert segunda is None, "se permitió una segunda voz sobre la misma llamada"

    await primera.cerrar()


async def test_un_call_id_sin_llamada_viva_no_engancha(cliente, cabeceras, llm) -> None:
    """Es lo que pasa si el backend se reinicia a mitad de llamada. El endpoint
    lo convierte en un `fin` explícito en vez de en una sesión que suena bien y
    no guarda nada."""
    assert await llamadas.abrir_sesion_de_voz(str(uuid4()), Grabadora().evento) is None
    assert await llamadas.abrir_sesion_de_voz("no-soy-un-uuid", Grabadora().evento) is None


# ---------------------------------------------------------------------------
# Persistir no puede costar latencia ni tumbar la llamada
# ---------------------------------------------------------------------------
async def test_una_base_lenta_no_retrasa_la_respuesta_al_paciente(
    cliente, cabeceras, llm, monkeypatch
) -> None:
    """Escribir en `call_turns` va fuera del camino crítico.

    Se dobla `guardar_turno` con medio segundo de retardo. Si la escritura
    estuviera en línea, el turno siguiente no podría empezar hasta pagarlo —el
    bucle de voz espera a la tarea del turno anterior— y el paciente oiría ese
    silencio.
    """
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    llm.respuestas = [RespuestaLLM("Muy bien."), RespuestaLLM("Estupendo.")]

    escrituras = 0

    async def _lento(*args, **kwargs):
        nonlocal escrituras
        await asyncio.sleep(0.5)
        escrituras += 1

    monkeypatch.setattr(llamadas, "guardar_turno", _lento)

    stt = STTFalso()
    sesion, _ = await _montar_voz(call_id, stt)

    inicio = asyncio.get_running_loop().time()
    await _turno(sesion, stt, "sí, soy yo")
    await _turno(sesion, stt, "todo bien por aquí")
    transcurrido = asyncio.get_running_loop().time() - inicio

    # Cuatro filas a medio segundo serían 2 s si se pagaran en línea.
    assert transcurrido < 1.0, f"la conversación esperó a la base: {transcurrido:.2f}s"
    # Y la prueba directa: la conversación ya ha terminado y las escrituras
    # siguen en vuelo. No es que sean rápidas, es que nadie las espera.
    assert escrituras < 4, "las escrituras se pagaron en línea"

    # Al colgar sí se espera: perder la transcripción por cerrar el socket sería
    # tirar justo lo que la cola existía para proteger.
    await sesion.cerrar()
    assert escrituras == 4


async def test_si_la_base_falla_la_llamada_continua(
    cliente, cabeceras, llm, monkeypatch
) -> None:
    """Perder el registro de un turno es malo; cortarle el teléfono a un paciente
    en seguimiento clínico es peor."""
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    llm.respuestas = [RespuestaLLM("Muy bien."), RespuestaLLM("Estupendo.")]

    async def _reventar(*args, **kwargs):
        raise RuntimeError("la base se cayó")

    monkeypatch.setattr(llamadas, "guardar_turno", _reventar)

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "sí, soy yo")
    await _turno(sesion, stt, "todo bien")

    assert len(sesion.turnos) == 2
    assert all(t.error is None for t in sesion.turnos)
    assert grabadora.bytes_audio > 0, "el paciente se quedó sin oír al agente"
    await sesion.cerrar()


# ---------------------------------------------------------------------------
# El LLM falla a mitad de llamada
# ---------------------------------------------------------------------------
async def test_si_el_llm_revienta_por_voz_el_paciente_oye_la_frase_de_seguridad(
    cliente, cabeceras, llm, monkeypatch
) -> None:
    """Timeout, cuota agotada o 503 del proveedor, con el paciente al teléfono.

    `test_agente_llamadas.py` ya cubre este caso por el camino de TEXTO
    (`POST /calls/{id}/mensaje`), que es donde el agente devuelve su respuesta de
    una pieza. Por voz el recorrido es otro —`SesionDeVoz.stream` envuelve
    `AgenteLlamada.stream`, y en medio están el troceado por frases, el anuncio de
    citas y banderas, la síntesis y la cola de persistencia— así que que uno
    funcione no dice nada del otro. Y es el que importa: por teléfono un silencio
    se interpreta como que se ha cortado la llamada.

    Lo que tiene que pasar: sale la frase de seguridad, SUENA, y la llamada queda
    escalada en la base aunque el fallo sea técnico.
    """
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])

    async def _reventar(*_args, **_kwargs):
        raise TimeoutError("el proveedor no respondió dentro del techo de 12 s")

    monkeypatch.setattr(llm, "responder", _reventar)

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "me duele mucho la herida")

    dicho = [e["texto"] for e in grabadora.eventos if e["tipo"] == "agente_habla"]
    assert dicho, "el agente no dijo nada: el paciente se quedó escuchando silencio"
    assert "equipo médico" in dicho[-1], dicho
    assert grabadora.bytes_audio > 0, "la frase de seguridad no llegó a sonar"

    # Un turno que se rinde no es un turno roto: se emite entero, con sus
    # métricas, y no se marca como fallado.
    assert sesion.turnos[-1].error is None
    assert grabadora.primero("metricas") is not None

    await sesion.cerrar()

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT escalated, escalation_urgency FROM calls WHERE id = %s", (call_id,)
        )
        fila = await cur.fetchone()
    assert fila["escalated"] is True, "un fallo técnico dejó la llamada sin aviso"
    assert fila["escalation_urgency"] == "prioritaria"

    # Y queda en la transcripción que el equipo clínico va a leer.
    dichos = [f["content"] for f in await _turnos_guardados(call_id)]
    assert any("equipo médico" in t for t in dichos), dichos


async def test_una_respuesta_vacia_por_voz_tampoco_deja_al_paciente_esperando(
    cliente, cabeceras, llm
) -> None:
    """`finish_reason=MAX_TOKENS` y los filtros de seguridad: llega vacío y sin error.

    Es el fallo del LLM que NO lanza excepción, así que el `except` del bucle de
    herramientas no lo ve. Por voz tiene además una consecuencia propia: un turno
    sin texto no produce ninguna frase que sintetizar, y sin la rendición el
    pipeline emitiría cero bytes de audio sin que nada pareciera haber fallado.
    """
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    llm.respuestas = [RespuestaLLM("", [], None, "MAX_TOKENS")]

    stt = STTFalso()
    sesion, grabadora = await _montar_voz(call_id, stt)
    await _turno(sesion, stt, "y de la medicación, ¿sigo igual?")

    assert grabadora.bytes_audio > 0, "el paciente se quedó sin oír nada"
    dicho = [e["texto"] for e in grabadora.eventos if e["tipo"] == "agente_habla"]
    assert dicho and "equipo médico" in dicho[-1], dicho

    await sesion.cerrar()


class TTSLento(TTSFalso):
    """Como `TTSFalso`, pero tarda en sintetizar cada frase.

    Hace falta para que una interrupción caiga DENTRO de la emisión. Con el TTS
    instantáneo, las cuatro frases salen del servidor en microsegundos —el
    cliente las encola y las va sonando— y no hay forma de cancelar a mitad:
    lo que se estaría probando es otra cosa.
    """

    def __init__(self, segundos_por_frase: float = 0.05, retardo_s: float = 0.4) -> None:
        super().__init__(segundos_por_frase)
        self.retardo_s = retardo_s

    async def sintetizar(self, texto: str):
        await asyncio.sleep(self.retardo_s)
        return await super().sintetizar(texto)


async def test_un_turno_cortado_por_barge_in_deja_rastro(
    cliente, cabeceras, llm
) -> None:
    """El paciente interrumpe al agente a media respuesta.

    Antes, `texto_agente` se asignaba de una vez al terminar de emitir, así que
    un turno cancelado se registraba **vacío**: la transcripción decía que el
    agente no había dicho nada cuando el paciente le había oído hablar. Ahora se
    acumula frase a frase y queda escrito lo que sí llegó a salir.

    Lo que NO se afirma aquí: que lo registrado sea exactamente lo que sonó por
    el altavoz. El servidor adelanta audio que el cliente todavía tiene en cola,
    y cuánto de eso llegó a oírse lo dicen `sesion.barge_ins`, no la
    transcripción.
    """
    call_id = await _abrir_llamada(cliente, cabeceras, PACIENTES["ana"])
    llm.respuestas = [
        RespuestaLLM("Primera frase. Segunda frase. Tercera frase. Cuarta frase."),
        RespuestaLLM("Claro, dígame."),
    ]

    stt = STTFalso("cuénteme")
    sesion, _ = await _montar_voz(call_id, stt)
    sesion._motor = TTSLento(segundos_por_frase=2.0, retardo_s=0.4)

    # No se espera a que el turno TERMINE: hay que pisarlo mientras habla.
    await inyectar(sesion, np.concatenate((cargar("turno_corto"), silencio(800))))
    assert await esperar(lambda: sesion.agente_hablando), "nunca empezó a hablar"

    stt.texto = "perdone, una cosa"
    await inyectar(sesion, cargar("interrupcion"), en_tiempo_real=True)
    assert len(sesion.barge_ins) == 1, "no se detectó la interrupción"

    await inyectar(sesion, silencio(800))
    assert await esperar(lambda: len(sesion.turnos) >= 2)
    await sesion.cerrar()

    dichos = [f["content"] for f in await _turnos_guardados(call_id)]
    assert "cuénteme" in dichos
    # El turno cortado dejó rastro de lo que había dicho hasta el corte...
    assert any("Primera frase" in t for t in dichos), dichos
    # ...y no de lo que el corte impidió sintetizar.
    assert not any("Cuarta frase" in t for t in dichos), dichos
    # Y la interrupción es un turno más, no el final de la llamada.
    assert any("perdone" in t for t in dichos), dichos
