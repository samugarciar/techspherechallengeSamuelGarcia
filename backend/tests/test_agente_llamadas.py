"""La llamada de principio a fin, contra Postgres de verdad y con el LLM doblado.

Qué se dobla y qué no, y por qué:

- **Postgres, real.** Lo que se comprueba —que el escalamiento queda escrito, que
  el `survey` se acumula sin pisarse, que `ended_at` sella la llamada— son
  propiedades de la base. Contra un doble, estos tests pasarían aunque nada de
  eso funcionase.
- **El LLM, doblado y con guion escrito.** Un modelo real haría estos tests
  intermitentes: la misma frase puede salir con `registrar_respuesta` o sin ella.
  Lo que se prueba aquí es el **bucle** —que las herramientas se ejecutan, que su
  resultado vuelve al modelo, que el turno se persiste con sus citas— y para eso
  hace falta saber exactamente qué va a pedir el modelo.
- **El RAG, doblado.** Cargar bge-m3 y el cross-encoder son decenas de segundos y
  varios GB de una máquina de 16 GB. Su calidad se mide en los evals de la Fase
  6. Lo que sí se prueba aquí es la consecuencia de que no haya evidencia, que es
  una decisión de este código y no del retrieval.

`postop_t3`, nunca `postop`. El guardarraíl está en `tests/ayuda_db.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agent import agente as modulo_agente
from app.agent.llm_client import LlamadaHerramienta, LLMClient, Mensaje, RespuestaLLM
from app.api import errores, llamadas
from app.core.config import get_settings
from app.db.pool import connection
from tests.ayuda_db import hay_postgres, pool  # noqa: F401 — pytest registra la fixture

pytestmark = hay_postgres


# ---------------------------------------------------------------------------
# Doble del LLM
# ---------------------------------------------------------------------------
@dataclass
class LLMGuionizado(LLMClient):
    """Devuelve las respuestas de una lista, en orden. Registra lo que recibe.

    Guardar `sistemas` es la mitad del valor: permite comprobar que el prompt del
    turno de alarma llevaba la instrucción de cortar, que es una decisión de
    `agente._sistema_del_turno` y no del modelo.
    """

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


def herramienta(nombre: str, **args: Any) -> RespuestaLLM:
    return RespuestaLLM(
        "", [LlamadaHerramienta(id=f"c_{nombre}", nombre=nombre, argumentos=args)]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
PACIENTE = UUID("dddddddd-0000-0000-0000-0000000000a1")


@pytest_asyncio.fixture(name="paciente")
async def _paciente(pool):  # noqa: F811
    """Un paciente propio, no el de la semilla.

    Los tests borran filas y varios agentes comparten esta base: crear el
    paciente aquí y borrarlo al terminar deja `postop_t3` como estaba. El CASCADE
    de `patients` arrastra cirugía, medicación, citas y llamadas.
    """
    async with connection() as conn:
        await conn.execute("DELETE FROM patients WHERE id = %s", (PACIENTE,))
        await conn.execute(
            """
            INSERT INTO patients (id, full_name, preferred_name, documento_cc, phone)
            VALUES (%s, 'Ana Lucía Serrano Vidal', 'Ana', '1012345678', '+34 600 111 222')
            """,
            (PACIENTE,),
        )
        cur = await conn.execute(
            """
            INSERT INTO surgeries (patient_id, procedure_name, performed_at, surgeon,
                                   protocol_tag, discharge_notes)
            VALUES (%s, 'Colecistectomía laparoscópica', CURRENT_DATE - 4, 'Dr. Peláez',
                    'colecistectomia', 'Sin conversión a cirugía abierta.')
            RETURNING id
            """,
            (PACIENTE,),
        )
        cirugia = (await cur.fetchone())["id"]
        await conn.execute(
            """
            INSERT INTO medications (surgery_id, name, dose, schedule, starts_on,
                                     ends_on, active)
            VALUES (%s, 'Omeprazol', '20 mg', 'una vez al día', CURRENT_DATE - 4,
                    CURRENT_DATE + 7, true),
                   (%s, 'Ibuprofeno', '400 mg', 'cada 12 horas', CURRENT_DATE - 4,
                    CURRENT_DATE - 1, false)
            """,
            (cirugia, cirugia),
        )
        await conn.execute(
            """
            INSERT INTO appointments (patient_id, scheduled_at, location, purpose)
            VALUES (%s, (CURRENT_DATE + 3)::timestamptz + interval '15 hours',
                    'Consultorio 210, Torre A', 'Control postoperatorio')
            """,
            (PACIENTE,),
        )
    try:
        yield PACIENTE
    finally:
        async with connection() as conn:
            await conn.execute("DELETE FROM patients WHERE id = %s", (PACIENTE,))
        llamadas._AGENTES.clear()


@pytest.fixture(name="llm")
def _llm(monkeypatch) -> LLMGuionizado:
    """Inyecta el doble en el agente que crea el endpoint.

    `AgenteLlamada.para_llamada` acepta un `llm`, pero quien lo llama es el
    router. Se sustituye la clase entera por una subclase que lo pasa: así el
    resto del camino —contexto, herramientas, prompt— es el de producción.
    """
    doble = LLMGuionizado()

    class AgenteDeprueba(modulo_agente.AgenteLlamada):
        @classmethod
        async def para_llamada(cls, patient_id, call_id=None, llm=None):
            return await super().para_llamada(patient_id, call_id, llm=doble)

    monkeypatch.setattr(llamadas, "AgenteLlamada", AgenteDeprueba)
    return doble


@pytest.fixture(name="rag")
def _rag(monkeypatch):
    """Doble del RAG. Devuelve un `Resultado` con o sin evidencia a voluntad."""
    from app.rag import query as modulo_query
    from app.rag.retrieval import Fragmento

    estado: dict[str, Any] = {"consultas": []}

    def responder_con(fragmentos: list[Fragmento]) -> None:
        estado["fragmentos"] = fragmentos

    async def _consultar(texto: str, top_k: int | None = None):
        estado["consultas"].append(texto)
        return modulo_query.Resultado(fragmentos=estado.get("fragmentos", []), ms={})

    monkeypatch.setattr(modulo_query, "consultar", _consultar)
    # `hay_evidencia` compara contra el umbral real; los fragmentos del doble
    # llevan un score de verdad para que la decisión la siga tomando el mismo
    # código que en producción.
    estado["responder_con"] = responder_con
    estado["fragmento"] = lambda score: Fragmento(
        chunk_id=str(uuid4()), document_id=str(uuid4()), filename="protocolo_colecist.pdf",
        content="Ante fiebre superior a 38,5 grados, acuda a urgencias el mismo día.",
        heading="Signos de alarma", page=4, score=score,
    )
    return estado


@pytest_asyncio.fixture(name="cliente")
async def _cliente(paciente):
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


async def _abrir(cliente, cabeceras) -> str:
    r = await cliente.post("/api/calls", json={"patient_id": str(PACIENTE)},
                           headers=cabeceras)
    assert r.status_code == 201, r.text
    return r.json()["call_id"]


async def _fila(call_id: str) -> dict:
    async with connection() as conn:
        cur = await conn.execute("SELECT * FROM calls WHERE id = %s", (call_id,))
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# GET /api/patients
# ---------------------------------------------------------------------------
async def test_pacientes_pendientes(cliente, cabeceras) -> None:
    r = await cliente.get("/api/patients", headers=cabeceras)
    assert r.status_code == 200
    ana = next(p for p in r.json()["pacientes"] if p["id"] == str(PACIENTE))

    assert ana["nombre"] == "Ana Lucía Serrano Vidal"
    assert ana["preferred_name"] == "Ana"
    assert ana["cirugia"]["nombre"] == "Colecistectomía laparoscópica"
    assert ana["cirugia"]["dias_desde"] == 4
    # Solo la medicación viva: el ibuprofeno terminó ayer y no cuenta.
    assert ana["medicacion_activa"] == 1
    assert ana["proxima_cita"] is not None
    assert ana["ultima_llamada"] is None


async def test_sin_token_no_se_listan_pacientes(cliente) -> None:
    r = await cliente.get("/api/patients")
    assert r.status_code == 401
    assert r.json()["error"]["codigo"] == "no_autorizado"


# ---------------------------------------------------------------------------
# POST /api/calls
# ---------------------------------------------------------------------------
async def test_abrir_llamada_devuelve_el_saludo_del_ai_act(cliente, cabeceras, llm) -> None:
    """El saludo es constante y sale sin haber ido al LLM ni una vez.

    Que `llm.sistemas` esté vacío es la prueba: la llamada puede empezar a sonar
    en el tiempo del TTS, no en el de la nube."""
    r = await cliente.post("/api/calls", json={"patient_id": str(PACIENTE)},
                           headers=cabeceras)
    assert r.status_code == 201
    cuerpo = r.json()

    assert "asistente automatizado" in cuerpo["saludo"]
    assert "Ana" in cuerpo["saludo"]
    assert cuerpo["ws"] == f"/ws/voz?call_id={cuerpo['call_id']}"
    assert cuerpo["dias_postop"] == 4
    assert llm.sistemas == []

    fila = await _fila(cuerpo["call_id"])
    assert fila["status"] == "active"
    assert fila["ended_at"] is None


async def test_el_saludo_queda_en_la_transcripcion(cliente, cabeceras, llm) -> None:
    call_id = await _abrir(cliente, cabeceras)
    r = await cliente.get(f"/api/calls/{call_id}", headers=cabeceras)
    turnos = r.json()["turnos"]
    assert turnos[0]["quien"] == "agente"
    assert turnos[0]["ordinal"] == 1
    assert "asistente automatizado" in turnos[0]["texto"]


async def test_paciente_inexistente(cliente, cabeceras, llm) -> None:
    r = await cliente.post("/api/calls", json={"patient_id": str(uuid4())},
                           headers=cabeceras)
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "paciente_no_encontrado"


# ---------------------------------------------------------------------------
# El bucle de herramientas
# ---------------------------------------------------------------------------
async def test_el_resultado_de_la_herramienta_vuelve_al_modelo(
    cliente, cabeceras, llm
) -> None:
    """Dos rondas: el modelo pide la cirugía, la lee, y contesta con el dato.

    Se comprueba lo que el arreglo del cliente destapó: que el historial de la
    segunda ronda lleva el `assistant` con su `function_call` y el `tool` con su
    id. Sin eso, Gemini improvisa y Groq devuelve un 400."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [
        herramienta("obtener_cirugia"),
        RespuestaLLM("Le operaron de la vesícula hace cuatro días."),
    ]

    r = await cliente.post(f"/api/calls/{call_id}/mensaje",
                           json={"texto": "¿de qué me operaron?"}, headers=cabeceras)
    assert r.status_code == 200
    assert "vesícula" in r.json()["texto"]

    historial = llm.historiales[-1]
    assert historial[-2].role == "assistant"
    assert historial[-2].herramientas[0].nombre == "obtener_cirugia"
    assert historial[-1].role == "tool"
    assert historial[-1].tool_call_id == historial[-2].herramientas[0].id
    # Y el dato salió de Postgres, no del prompt.
    assert "Colecistectomía laparoscópica" in historial[-1].content


async def test_la_medicacion_activa_se_lee_fresca(cliente, cabeceras, llm) -> None:
    """El ibuprofeno terminó ayer: no se le recuerda aunque siga en la tabla.

    Es el argumento entero de por qué los datos vivos no van al RAG. Un embedding
    creado el día del alta seguiría recomendándolo."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [herramienta("obtener_medicacion"), RespuestaLLM("Le recuerdo la pauta.")]

    await cliente.post(f"/api/calls/{call_id}/mensaje",
                       json={"texto": "¿qué pastillas tengo que tomar?"}, headers=cabeceras)

    resultado = json.loads(llm.historiales[-1][-1].content)
    nombres = [m["nombre"] for m in resultado["medicacion_activa"]]
    assert nombres == ["Omeprazol"]
    assert "NUNCA la cambies" in resultado["instruccion"]


async def test_registrar_respuesta_acumula_en_el_survey(cliente, cabeceras, llm) -> None:
    call_id = await _abrir(cliente, cabeceras)

    llm.respuestas = [herramienta("registrar_respuesta", clave="dolor", valor="un cuatro"),
                      RespuestaLLM("Anotado. ¿Y la herida?")]
    await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "un cuatro"},
                       headers=cabeceras)

    llm.respuestas = [herramienta("registrar_respuesta", clave="herida", valor="limpia"),
                      RespuestaLLM("Muy bien.")]
    await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "la veo limpia"},
                       headers=cabeceras)

    fila = await _fila(call_id)
    assert fila["survey"] == {"dolor": "un cuatro", "herida": "limpia"}


async def test_una_clave_inventada_se_rechaza_con_las_buenas(cliente, cabeceras, llm) -> None:
    """El modelo corrige y reintenta en el mismo turno; el `survey` no se ensucia."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [
        herramienta("registrar_respuesta", clave="nivel_de_dolor", valor="4"),
        RespuestaLLM("Anotado."),
    ]
    await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "un cuatro"},
                       headers=cabeceras)

    resultado = json.loads(llm.historiales[-1][-1].content)
    assert "no es una pregunta de este guion" in resultado["error"]
    assert "dolor" in resultado["claves_validas"]
    # Y la clave de otra cirugía no aparece entre las válidas.
    assert "transito" not in resultado["claves_validas"]
    assert (await _fila(call_id))["survey"] == {}


async def test_el_modelo_no_puede_pedir_datos_de_otro_paciente(
    cliente, cabeceras, llm
) -> None:
    """Aunque invente un `patient_id`, la herramienta lo ignora: no tiene ese
    argumento. El paciente sale del contexto de la llamada."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [
        herramienta("obtener_paciente", patient_id=str(uuid4())),
        RespuestaLLM("Confirmado."),
    ]
    await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "sí, soy yo"},
                       headers=cabeceras)

    resultado = json.loads(llm.historiales[-1][-1].content)
    assert resultado["nombre_completo"] == "Ana Lucía Serrano Vidal"
    assert resultado["nombre_preferido"] == "Ana"


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------
async def test_sin_evidencia_no_se_le_pasa_ni_un_fragmento(
    cliente, cabeceras, llm, rag
) -> None:
    """La regla del prompt es el segundo cinturón; el primero es que no haya con
    qué inventar. Devolver fragmentos malos con una advertencia sería confiar en
    que el modelo la lea."""
    call_id = await _abrir(cliente, cabeceras)
    rag["responder_con"]([rag["fragmento"](0.05)])   # por debajo del umbral
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="puedo tomar el sol"),
        RespuestaLLM("Eso no lo tengo, se lo paso a su equipo."),
    ]

    r = await cliente.post(f"/api/calls/{call_id}/mensaje",
                           json={"texto": "¿puedo tomar el sol?"}, headers=cabeceras)

    resultado = json.loads(llm.historiales[-1][-1].content)
    assert resultado["hay_evidencia"] is False
    assert resultado["fragmentos"] == []
    assert "no tienes esa información" in resultado["instruccion"]
    assert r.json()["citas"] == []


async def test_con_evidencia_el_turno_publica_sus_citas(cliente, cabeceras, llm, rag) -> None:
    """Las citas van por turno: es lo que permite señalar una frase del agente y
    ver de qué documento y sección salió."""
    call_id = await _abrir(cliente, cabeceras)
    rag["responder_con"]([rag["fragmento"](0.91)])
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="fiebre"),
        RespuestaLLM("El protocolo dice que acuda a urgencias hoy mismo."),
    ]

    r = await cliente.post(f"/api/calls/{call_id}/mensaje",
                           json={"texto": "¿y si me sube la fiebre?"}, headers=cabeceras)

    assert r.json()["citas"] == [
        {"filename": "protocolo_colecist.pdf", "heading": "Signos de alarma", "page": 4}
    ]
    # Y quedan escritas en el turno, no solo en la respuesta HTTP.
    detalle = await cliente.get(f"/api/calls/{call_id}", headers=cabeceras)
    ultimo = detalle.json()["turnos"][-1]
    assert ultimo["citas"][0]["heading"] == "Signos de alarma"


async def test_la_consulta_al_rag_se_sesga_con_el_procedimiento(
    cliente, cabeceras, llm, rag
) -> None:
    """Con tres protocolos en el corpus, «¿cuándo me puedo duchar?» recupera
    cualquiera de los tres. Añadir el procedimiento inclina las dos mitades de la
    búsqueda híbrida hacia el que toca."""
    call_id = await _abrir(cliente, cabeceras)
    rag["responder_con"]([rag["fragmento"](0.9)])
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="cuándo puedo ducharme"),
        RespuestaLLM("A partir de las cuarenta y ocho horas."),
    ]

    await cliente.post(f"/api/calls/{call_id}/mensaje",
                       json={"texto": "¿cuándo me puedo duchar?"}, headers=cabeceras)

    assert rag["consultas"][-1].startswith("Colecistectomía laparoscópica.")


# ---------------------------------------------------------------------------
# Banderas rojas
# ---------------------------------------------------------------------------
async def test_la_alarma_cambia_el_prompt_del_turno(cliente, cabeceras, llm, rag) -> None:
    """Quien decide que hay alarma es el detector, no el modelo.

    Cuando el paciente dice «tengo treinta y nueve», el prompt de ESE turno lleva
    ya la orden de cortar el guion. El modelo no clasifica: redacta."""
    call_id = await _abrir(cliente, cabeceras)
    rag["responder_con"]([rag["fragmento"](0.95)])
    llm.respuestas = [
        herramienta("buscar_protocolo", consulta="fiebre alta"),
        herramienta("escalar_a_equipo_clinico",
                    motivo="fiebre de 39 grados", urgencia="urgente"),
        RespuestaLLM("Acuda hoy a urgencias. ¿Me ha entendido lo que tiene que hacer?"),
    ]

    r = await cliente.post(
        f"/api/calls/{call_id}/mensaje",
        json={"texto": "pues mire, me tomé la temperatura y tengo treinta y nueve"},
        headers=cabeceras,
    )
    cuerpo = r.json()

    assert [b["codigo"] for b in cuerpo["banderas"]] == ["fiebre_alta"]
    assert cuerpo["banderas"][0]["urgencia"] == "urgente"
    assert "ALARMA DETECTADA" in llm.sistemas[-1]
    assert "Deja el guion" in llm.sistemas[-1]

    fila = await _fila(call_id)
    assert fila["escalated"] is True
    assert fila["escalation_urgency"] == "urgente"
    assert "39" in fila["escalation_reason"]


async def test_la_llamada_no_cuelga_hasta_confirmar_que_lo_entendio(
    cliente, cabeceras, llm, rag
) -> None:
    """Decisión 2 del contrato: dar la indicación no basta, hay que comprobar que
    ha llegado. Por eso el corte ocupa dos turnos y no uno."""
    call_id = await _abrir(cliente, cabeceras)
    rag["responder_con"]([rag["fragmento"](0.95)])

    llm.respuestas = [
        herramienta("escalar_a_equipo_clinico", motivo="sangrado activo", urgencia="urgente"),
        RespuestaLLM("Presione la herida y acuda a urgencias. ¿Me ha entendido?"),
    ]
    r1 = await cliente.post(f"/api/calls/{call_id}/mensaje",
                            json={"texto": "la herida no para de sangrar"},
                            headers=cabeceras)
    assert r1.json()["terminar"] is False
    assert (await _fila(call_id))["ended_at"] is None

    llm.respuestas = [RespuestaLLM("Perfecto. Su equipo la llamará enseguida. Cuídese.")]
    r2 = await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "sí, entendido"},
                            headers=cabeceras)
    assert r2.json()["terminar"] is True
    assert "CONFIRMACIÓN" in llm.sistemas[-1].upper() or "entendido" in llm.sistemas[-1]

    fila = await _fila(call_id)
    assert fila["ended_at"] is not None
    assert fila["status"] == "escalated"


async def test_una_respuesta_tranquilizadora_no_escala(cliente, cabeceras, llm) -> None:
    """El contrapeso del test anterior: si el agente escalara con esto, la demo
    acabaría con los tres pacientes derivados y el escalamiento no diría nada."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [RespuestaLLM("Me alegro. ¿Y la herida, cómo la ve?")]

    r = await cliente.post(f"/api/calls/{call_id}/mensaje",
                           json={"texto": "no, fiebre no he tenido nada, todo bien"},
                           headers=cabeceras)

    assert r.json()["banderas"] == []
    assert r.json()["terminar"] is False
    assert (await _fila(call_id))["escalated"] is False


# ---------------------------------------------------------------------------
# Fallos del LLM
# ---------------------------------------------------------------------------
async def test_si_el_llm_revienta_se_escala_en_vez_de_callar(
    cliente, cabeceras, llm, monkeypatch
) -> None:
    """Por teléfono un silencio se lee como que se ha cortado la llamada.

    Y se escala aunque el fallo sea técnico: desde el otro lado no se distingue,
    y una llamada que no se completó es algo que alguien debería revisar."""
    call_id = await _abrir(cliente, cabeceras)

    async def _reventar(*_args, **_kwargs):
        raise RuntimeError("503 desde el proveedor")

    monkeypatch.setattr(llm, "responder", _reventar)

    r = await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "hola"},
                           headers=cabeceras)
    assert r.status_code == 200
    assert "equipo médico" in r.json()["texto"]

    fila = await _fila(call_id)
    assert fila["escalated"] is True
    assert fila["escalation_urgency"] == "prioritaria"


async def test_un_bucle_de_herramientas_se_corta(cliente, cabeceras, llm) -> None:
    """Un modelo que repite la misma llamada porque no le gusta el resultado se
    comería el tiempo de la llamada en silencio."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [herramienta("obtener_cirugia") for _ in range(10)]

    r = await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "hola"},
                           headers=cabeceras)
    assert r.json()["rondas"] if "rondas" in r.json() else True
    assert "equipo médico" in r.json()["texto"]
    assert len(llm.sistemas) == modulo_agente.MAX_RONDAS


async def test_una_respuesta_vacia_no_deja_al_paciente_esperando(
    cliente, cabeceras, llm
) -> None:
    """Pasa con `finish_reason=MAX_TOKENS` y con los filtros de seguridad: la
    respuesta llega vacía y sin error."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [RespuestaLLM("", [], None, "MAX_TOKENS")]

    r = await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "hola"},
                           headers=cabeceras)
    assert "equipo médico" in r.json()["texto"]


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------
async def test_el_historial_publica_la_llamada_escalada_como_completada(
    cliente, cabeceras, llm
) -> None:
    """Escalar no es terminar mal: es lo que el sistema debe hacer. Contarlo como
    `interrumpida` haría que el historial leyera los aciertos como fallos."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [
        herramienta("escalar_a_equipo_clinico", motivo="disnea", urgencia="urgente"),
        RespuestaLLM("Acuda a urgencias. ¿Me ha entendido?"),
    ]
    await cliente.post(f"/api/calls/{call_id}/mensaje",
                       json={"texto": "me falta el aire"}, headers=cabeceras)
    await cliente.post(f"/api/calls/{call_id}/fin", json={"motivo": "completada"},
                       headers=cabeceras)

    r = await cliente.get("/api/calls", headers=cabeceras)
    fila = next(ll for ll in r.json()["llamadas"] if ll["id"] == call_id)
    assert fila["estado"] == "completada"
    assert fila["escalada"] is True
    assert fila["motivo_escalada"] == "disnea"
    assert fila["paciente"] == "Ana Lucía Serrano Vidal"
    assert fila["duracion_s"] >= 0
    assert fila["turnos"] >= 3


async def test_el_detalle_ordena_los_turnos_como_se_dijeron(
    cliente, cabeceras, llm
) -> None:
    """Se ordena por `id` y no por `created_at`: dos turnos del mismo intercambio
    caen en el mismo milisegundo y el agente aparecería contestando antes de la
    pregunta."""
    call_id = await _abrir(cliente, cabeceras)
    llm.respuestas = [RespuestaLLM("Muy bien, ¿y el dolor?")]
    await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "sí, soy yo"},
                       headers=cabeceras)

    r = await cliente.get(f"/api/calls/{call_id}", headers=cabeceras)
    turnos = r.json()["turnos"]
    assert [t["quien"] for t in turnos] == ["agente", "paciente", "agente"]
    assert [t["ordinal"] for t in turnos] == [1, 2, 3]
    assert turnos[1]["texto"] == "sí, soy yo"


async def test_cerrar_dos_veces_no_reabre_la_llamada(cliente, cabeceras, llm) -> None:
    call_id = await _abrir(cliente, cabeceras)
    r1 = await cliente.post(f"/api/calls/{call_id}/fin", json={"motivo": "completada"},
                            headers=cabeceras)
    assert r1.json()["estado"] == "completada"
    r2 = await cliente.post(f"/api/calls/{call_id}/fin", json={"motivo": "cortada"},
                            headers=cabeceras)
    assert r2.status_code == 409
    assert r2.json()["error"]["codigo"] == "llamada_cerrada"


async def test_llamada_inexistente(cliente, cabeceras) -> None:
    for ruta, metodo, cuerpo in [
        (f"/api/calls/{uuid4()}", "get", None),
        (f"/api/calls/{uuid4()}/mensaje", "post", {"texto": "hola"}),
        (f"/api/calls/{uuid4()}/fin", "post", {"motivo": "completada"}),
    ]:
        r = await getattr(cliente, metodo)(
            ruta, headers=cabeceras, **({"json": cuerpo} if cuerpo else {})
        )
        assert r.status_code == 404, ruta
        assert r.json()["error"]["codigo"] == "llamada_no_encontrada"


async def test_mensaje_vacio_da_422_con_la_forma_del_contrato(
    cliente, cabeceras, llm
) -> None:
    call_id = await _abrir(cliente, cabeceras)
    r = await cliente.post(f"/api/calls/{call_id}/mensaje", json={"texto": "   "},
                           headers=cabeceras)
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "peticion_invalida"
