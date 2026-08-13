"""Guion adaptativo, prompt de sistema y traducción de mensajes a cada proveedor.

Nada de esto toca la red ni la base de datos: son funciones puras sobre datos.
Lo que se fija aquí son las tres cosas que se rompen sin hacer ruido:

1. Que a un colecistectomizado se le pregunte por las grasas y a un
   herniorrafiado por los esfuerzos, incluso cuando `protocol_tag` viene a NULL.
2. Que las reglas que hacen seguro al agente —no diagnosticar, no ajustar dosis,
   no leer la fecha de nacimiento— sigan estando en el prompt. Un `git revert`
   descuidado las quita y todo lo demás sigue funcionando igual de bien.
3. Que el historial con herramientas se reconstruya válido para los DOS
   proveedores. Éste es el bug que la primera ejecución del cliente destapó:
   Gemini tolera un historial `usuario → usuario` y Groq lo rechaza, así que sin
   test se descubre el día que se cambia de proveedor.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent import prompts
from app.agent.guion import (
    POR_PROTOCOLO,
    claves_validas,
    como_texto,
    etiqueta_de,
    guion_para,
)
from app.agent.llm_client import GroqClient, LlamadaHerramienta, Mensaje
from app.agent.tools import ContextoLlamada, esquemas, fecha_en_palabras


def _ctx(tag: str | None = "apendicectomia", proc: str = "Apendicectomía laparoscópica"):
    return ContextoLlamada(None, uuid4(), "María Elena Restrepo", "María", proc, tag, 3)


# ---------------------------------------------------------------------------
# Guion
# ---------------------------------------------------------------------------
def test_el_bloque_comun_esta_en_las_tres_cirugias() -> None:
    comunes = {"dolor", "dolor_control", "herida", "fiebre", "medicacion",
               "movilidad", "dudas"}
    for etiqueta in POR_PROTOCOLO:
        claves = {p.clave for p in guion_para(etiqueta)}
        assert comunes <= claves, etiqueta


@pytest.mark.parametrize(
    "etiqueta,clave_propia",
    [
        ("apendicectomia", "transito"),
        ("colecistectomia", "tolerancia_grasas"),
        ("herniorrafia", "esfuerzos"),
    ],
)
def test_cada_cirugia_trae_lo_suyo(etiqueta: str, clave_propia: str) -> None:
    """Decisión 1 del contrato: es lo que separa un seguimiento de un formulario."""
    claves = {p.clave for p in guion_para(etiqueta)}
    assert clave_propia in claves
    # Y no trae lo de las otras.
    otras = {
        c
        for e, preguntas in POR_PROTOCOLO.items()
        if e != etiqueta
        for c in {p.clave for p in preguntas}
    }
    assert not (claves & otras - {p.clave for p in POR_PROTOCOLO[etiqueta]})


def test_las_especificas_van_en_medio_y_no_al_final() -> None:
    """Después de la fiebre y antes de la medicación.

    Colocarlas tras las dudas, con la conversación ya cerrándose, desperdicia el
    único momento en que el paciente percibe que la llamada sabe de lo suyo."""
    claves = [p.clave for p in guion_para("colecistectomia")]
    assert claves.index("fiebre") < claves.index("tolerancia_grasas")
    assert claves.index("tolerancia_grasas") < claves.index("medicacion")
    assert claves[-1] == "dudas"


@pytest.mark.parametrize(
    "nombre,esperada",
    [
        ("Colecistectomía laparoscópica", "colecistectomia"),
        ("Apendicectomía laparoscópica", "apendicectomia"),
        ("Herniorrafia inguinal derecha", "herniorrafia"),
        ("Reparación de hernia umbilical", "herniorrafia"),
    ],
)
def test_sin_protocol_tag_se_deduce_del_nombre(nombre: str, esperada: str) -> None:
    """Un alta cargada a mano deja `protocol_tag` a NULL.

    Sin esta red, esa llamada degradaría al cuestionario genérico en silencio: no
    fallaría nada, simplemente el paciente no oiría las preguntas de su cirugía."""
    assert etiqueta_de(None, nombre) == esperada
    assert "tolerancia_grasas" in {p.clave for p in guion_para(None, nombre)} or (
        esperada != "colecistectomia"
    )


def test_cirugia_desconocida_no_deja_al_paciente_sin_llamada() -> None:
    """Se prefiere un seguimiento genérico a un error: la llamada se hace igual."""
    preguntas = guion_para("gastrectomia", "Gastrectomía total")
    assert {p.clave for p in preguntas} == {
        "dolor", "dolor_control", "herida", "fiebre", "medicacion", "movilidad", "dudas"
    }


def test_las_claves_validas_incluyen_la_libre() -> None:
    """`otros_sintomas` recoge lo que el paciente cuenta y no encaja en nada.

    Sin ella el modelo lo metería en la clave más parecida y contaminaría un
    campo estructurado con texto suelto."""
    claves = claves_validas("herniorrafia")
    assert "otros_sintomas" in claves
    assert "esfuerzos" in claves


def test_el_guion_entra_en_el_prompt_con_su_clave() -> None:
    """El número da el orden y el corchete dice con qué nombre registrar.

    Una segunda lista de claves aparte sería una lista que se desincroniza."""
    texto = como_texto(guion_para("apendicectomia"))
    assert "1. [dolor]" in texto
    assert "[transito]" in texto


# ---------------------------------------------------------------------------
# Prompt de sistema
# ---------------------------------------------------------------------------
def test_el_saludo_declara_que_es_una_maquina() -> None:
    """AI Act art. 50 y decisión 4 del contrato, en la primera frase.

    Es una constante y no la genera el modelo justamente para poder afirmarlo en
    un test: una frase generada saldría distinta cada vez."""
    saludo = prompts.SALUDO.format(nombre="María")
    assert "asistente automatizado" in saludo
    assert "no una persona" in saludo
    assert "María" in saludo


@pytest.mark.parametrize(
    "regla",
    [
        "Nunca diagnostiques",
        "Nunca ajustes medicación",
        "buscar_protocolo",
        "cédula",
    ],
)
def test_las_reglas_duras_siguen_en_el_prompt(regla: str) -> None:
    assert regla in prompts.sistema(_ctx())


def test_el_prompt_no_lleva_datos_del_paciente_mas_alla_del_nombre() -> None:
    """Todo lo demás lo traen las herramientas cuando hace falta.

    Un prompt con la medicación dentro sería una foto tomada al empezar la
    llamada — el error que `app/agent/tools.py` existe para evitar."""
    sistema = prompts.sistema(_ctx())
    assert "Cefalexina" not in sistema
    assert "1978" not in sistema


def test_el_prompt_evita_la_concordancia_de_genero() -> None:
    """`patients` no guarda el sexo, así que «operado» trataría de él a una
    paciente en la primera frase."""
    sistema = prompts.sistema(_ctx())
    assert "a quien operaron" in sistema
    assert "operado hace" not in sistema


def test_el_prompt_de_alarma_lleva_los_cuatro_pasos() -> None:
    texto = prompts.BANDERA_ROJA.format(motivo="fiebre de 39.2 grados", urgencia="urgente")
    assert "Deja el guion" in texto
    assert "buscar_protocolo" in texto
    assert "escalar_a_equipo_clinico" in texto
    assert "entendido" in texto or "claro" in texto
    assert "fiebre de 39.2 grados" in texto


# ---------------------------------------------------------------------------
# Esquemas de herramientas
# ---------------------------------------------------------------------------
def test_ninguna_lectura_pide_el_id_del_paciente() -> None:
    """Un modelo al que se le pide un UUID lo inventa, y el fallo no es visible:
    una consulta que devuelve cero filas y un agente que dice que no consta nada."""
    por_nombre = {e["name"]: e for e in esquemas(["dolor"])}
    for nombre in ("obtener_paciente", "obtener_cirugia", "obtener_medicacion"):
        assert por_nombre[nombre]["parameters"]["properties"] == {}


def test_registrar_respuesta_solo_acepta_claves_del_guion() -> None:
    """El `enum` es lo que impide que el modelo invente el nombre del campo."""
    claves = sorted(claves_validas("apendicectomia"))
    esquema = {e["name"]: e for e in esquemas(claves)}["registrar_respuesta"]
    assert esquema["parameters"]["properties"]["clave"]["enum"] == claves
    assert "tolerancia_grasas" not in claves  # es de la colecistectomía


def test_la_urgencia_es_la_del_check_de_la_tabla() -> None:
    """`calls.escalation_urgency` tiene un CHECK con estos tres valores: si el
    enum del esquema y el CHECK divergen, el INSERT revienta a mitad de llamada."""
    esquema = {e["name"]: e for e in esquemas(["dolor"])}["escalar_a_equipo_clinico"]
    assert esquema["parameters"]["properties"]["urgencia"]["enum"] == [
        "rutina", "prioritaria", "urgente"
    ]


def test_las_fechas_se_dan_ya_habladas() -> None:
    """El TTS pronuncia las fechas ISO fatal, y pedirle al modelo que las
    convierta es pedirle que se equivoque en voz alta."""
    from datetime import date

    assert fecha_en_palabras(date(1978, 4, 12)) == "12 de abril de 1978"
    assert fecha_en_palabras(None) is None


# ---------------------------------------------------------------------------
# Historial con herramientas — el bug que destapó la primera ejecución
# ---------------------------------------------------------------------------
def _conversacion_con_tool() -> list[Mensaje]:
    llamada = LlamadaHerramienta(id="call_1", nombre="obtener_cirugia", argumentos={})
    return [
        Mensaje("user", "¿De qué me operaron?"),
        Mensaje("assistant", "", herramientas=[llamada]),
        Mensaje("tool", '{"procedimiento": "Apendicectomía"}',
                tool_call_id="call_1", name="obtener_cirugia"),
    ]


def test_groq_reconstruye_el_assistant_con_sus_tool_calls() -> None:
    """El contrato de OpenAI —y Groq lo valida— exige que todo `role="tool"` vaya
    precedido del `assistant` que pidió esa herramienta, con el mismo id. Sin él
    la petición se rechaza con un 400 que no dice cuál falta.

    Antes de arreglarlo, `Mensaje` no tenía dónde guardar las llamadas del modelo
    y el historial salía `usuario → usuario`."""
    mensajes = GroqClient._a_messages("eres un agente", _conversacion_con_tool())
    roles = [m["role"] for m in mensajes]
    assert roles == ["system", "user", "assistant", "tool"]

    asistente = mensajes[2]
    assert asistente["tool_calls"][0]["id"] == "call_1"
    assert asistente["tool_calls"][0]["function"]["name"] == "obtener_cirugia"
    # `content` a None y no a "": la API distingue «no dijo nada, solo llamó a la
    # herramienta» de «dijo la cadena vacía».
    assert asistente["content"] is None
    assert mensajes[3]["tool_call_id"] == "call_1"


def test_gemini_traduce_la_tool_a_function_response() -> None:
    """Gemini no tiene rol `tool`: el resultado lo aporta el lado del usuario,
    como `function_response`, y la llamada del modelo va como `function_call`.

    Y nunca una parte de texto vacío: un turno que solo hizo llamadas tiene
    `content=""`, y una parte vacía es basura que confunde al modelo."""
    from app.agent.llm_client import GeminiClient

    cliente = GeminiClient.__new__(GeminiClient)
    from google.genai import types

    cliente._types = types
    contents = cliente._a_contents(_conversacion_con_tool())

    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].function_call.name == "obtener_cirugia"
    assert not [p for p in contents[1].parts if getattr(p, "text", None) == ""]
    respuesta = contents[2].parts[0].function_response
    assert respuesta.name == "obtener_cirugia"
    assert respuesta.response["procedimiento"] == "Apendicectomía"


def test_el_razonamiento_de_gemini_esta_apagado() -> None:
    """Medido: 462 ms contra 956 ms con el prompt real (README §Presupuesto).

    Es la segunda palanca de latencia del sistema y se apaga por defecto. Este
    test está para que subirla sea una decisión y no un descuido."""
    from app.agent import llm_client

    assert llm_client.PENSAMIENTO_DESACTIVADO == 0
