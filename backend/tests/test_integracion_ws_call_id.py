"""`/ws/voz?call_id=…` visto desde el cable, con un WebSocket de verdad.

`test_integracion_voz_llamada.py` prueba la costura con Postgres y el agente
clínico reales, pero monta la sesión a mano. Lo que falta comprobar —y es donde
fallan estas cosas— es lo que hay entre la query string y esa sesión: que FastAPI
resuelve `call_id` como parámetro de query y no como cuerpo, que sin él la sesión
sigue siendo la suelta de siempre, y que un `call_id` que no existe se cierra
diciendo por qué en vez de aparentar que funciona.

Aquí no hay base de datos ni agente: la fábrica es un doble. Lo que se prueba es
el contrato del endpoint.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_voz_barge_in import STTFalso, TTSFalso

from app.agent.llm_client import LLMClient, RespuestaLLM
from app.voice.pipeline_ws import crear_router


class LlamadaFalsa(LLMClient):
    """Lo que devolvería `abrir_sesion_de_voz`, sin Postgres ni agente detrás."""

    def __init__(self, saludo: str | None = None) -> None:
        self.saludo = saludo
        self.turnos_registrados = 0
        self.cerrada = False
        self.motivo: str | None = None

    # -- LLMClient ----------------------------------------------------------
    async def responder(self, sistema, mensajes, herramientas=None) -> RespuestaLLM:
        return RespuestaLLM("Ya le he entendido.")

    async def stream(self, sistema, mensajes):
        yield "Ya le he entendido."

    # -- LlamadaEnCurso -----------------------------------------------------
    def saludo_inicial(self) -> str | None:
        return self.saludo

    def turno_terminado(self, metricas) -> None:
        self.turnos_registrados += 1

    def motivo_de_fin(self) -> str | None:
        return self.motivo

    async def cerrar(self) -> None:
        self.cerrada = True


class Fabrica:
    """Doble de `abrir_sesion_de_voz`. Apunta con qué `call_id` la llamaron."""

    def __init__(self, llamada: LlamadaFalsa | None = None) -> None:
        self.llamada = llamada
        self.recibidos: list[str] = []

    async def __call__(self, call_id: str, emitir):
        self.recibidos.append(call_id)
        return self.llamada


def _app(fabrica: Fabrica | None, **kwargs) -> FastAPI:
    app = FastAPI()
    app.include_router(
        crear_router(
            stt=STTFalso(),
            motor_tts=TTSFalso(segundos_por_frase=0.05),
            fabrica_llamada=fabrica,
            **kwargs,
        )
    )
    return app


def _leer_eventos(ws, limite: int = 200) -> list[dict]:
    """Todo lo que el servidor mande hasta que se cierre o se agote el límite."""
    eventos: list[dict] = []
    for _ in range(limite):
        try:
            mensaje = ws.receive()
        except Exception:
            break
        if mensaje.get("type") == "websocket.close":
            break
        if (texto := mensaje.get("text")) is not None:
            eventos.append(json.loads(texto))
    return eventos


# ---------------------------------------------------------------------------
def test_el_call_id_de_la_query_llega_a_la_fabrica():
    """El cable 2 entero en una aserción: el contrato dice que `/ws/voz` acepta
    `?call_id=…` y hasta ahora `pipeline_ws.py` no lo mencionaba ni una vez."""
    fabrica = Fabrica(LlamadaFalsa())
    with TestClient(_app(fabrica)) as cliente:
        with cliente.websocket_connect("/ws/voz?call_id=abc-123") as ws:
            assert ws.receive_json()["tipo"] == "listo"

    assert fabrica.recibidos == ["abc-123"]


def test_sin_call_id_la_sesion_es_suelta_y_no_se_persiste():
    """«Sin él, sesión suelta sin persistir», dice el contrato.

    Es el modo de `scripts/spikes/cliente_voz/` y del arnés de medición: tiene
    que seguir arrancando sin base de datos ni agente clínico."""
    fabrica = Fabrica(LlamadaFalsa())
    with TestClient(_app(fabrica)) as cliente:
        with cliente.websocket_connect("/ws/voz") as ws:
            assert ws.receive_json()["tipo"] == "listo"

    assert fabrica.recibidos == [], "se intentó persistir una sesión sin call_id"


def test_un_call_id_desconocido_cierra_la_conexion_diciendo_por_que():
    """El caso real es un backend reiniciado a mitad de llamada.

    Seguir con el LLM falso daría una llamada que suena bien, no guarda ni un
    turno y no se parece a lo que la pantalla dice que pasa. El fallo silencioso
    es justo lo que este proyecto lleva evitando desde la Fase 1."""
    fabrica = Fabrica(None)   # no hay agente vivo para ese id
    with TestClient(_app(fabrica)) as cliente:
        with cliente.websocket_connect("/ws/voz?call_id=fantasma") as ws:
            eventos = _leer_eventos(ws)

    assert [e["tipo"] for e in eventos] == ["listo", "fin"]
    assert eventos[-1]["motivo"] == "cortada"


def test_una_fabrica_que_revienta_no_deja_la_llamada_a_medias():
    """Si abrir la sesión falla, se cierra igual que si no existiera: lo que no
    puede pasar es que el WebSocket quede abierto con un LLM que no es el que la
    pantalla cree."""

    async def _reventar(call_id, emitir):
        raise RuntimeError("Postgres no responde")

    with TestClient(_app(_reventar)) as cliente:
        with cliente.websocket_connect("/ws/voz?call_id=abc") as ws:
            eventos = _leer_eventos(ws)

    assert [e["tipo"] for e in eventos] == ["listo", "fin"]


def test_el_saludo_del_ai_act_se_dice_al_conectar():
    """La primera intervención tiene que SONAR, no solo estar escrita.

    `POST /api/calls` devuelve el saludo —constante, con la declaración de
    sistema automatizado que exige el AI Act— pero el navegador no puede
    sintetizarlo: el TTS está en el servidor. Sin esto la llamada empieza en
    silencio y el paciente no sabe ni que hay alguien al aparato.
    """
    saludo = "Buenos días. Soy un asistente automatizado. ¿Hablo con Ana?"
    fabrica = Fabrica(LlamadaFalsa(saludo=saludo))
    with TestClient(_app(fabrica)) as cliente:
        with cliente.websocket_connect("/ws/voz?call_id=abc") as ws:
            assert ws.receive_json()["tipo"] == "listo"
            eventos, bytes_audio = [], 0
            for _ in range(400):
                mensaje = ws.receive()
                if mensaje.get("bytes") is not None:
                    bytes_audio += len(mensaje["bytes"])
                elif (texto := mensaje.get("text")) is not None:
                    eventos.append(json.loads(texto))
                    if eventos[-1]["tipo"] == "fin_audio":
                        break

    assert bytes_audio > 0, "el saludo no sonó"
    assert eventos[-1]["texto"] == saludo
    assert {"estado", "agente_habla"} <= {e["tipo"] for e in eventos}


def test_sin_saludo_no_se_dice_nada_al_conectar():
    fabrica = Fabrica(LlamadaFalsa(saludo=None))
    with TestClient(_app(fabrica)) as cliente:
        with cliente.websocket_connect("/ws/voz?call_id=abc") as ws:
            assert ws.receive_json()["tipo"] == "listo"
            ws.close()

    assert fabrica.llamada.cerrada, "no se cerró la sesión clínica al colgar"


@pytest.mark.parametrize("motivo", ["completada", "escalada", "cortada"])
def test_al_colgar_se_avisa_a_la_sesion_clinica(motivo):
    """`cerrar()` es donde se vacía la transcripción pendiente y se sella la
    llamada. Si el endpoint no lo llamara, cada llamada dejaría filas sin
    escribir y una fila de `calls` eternamente «en curso»."""
    llamada = LlamadaFalsa()
    llamada.motivo = motivo
    with TestClient(_app(Fabrica(llamada))) as cliente:
        with cliente.websocket_connect("/ws/voz?call_id=abc") as ws:
            assert ws.receive_json()["tipo"] == "listo"

    assert llamada.cerrada


def test_el_router_sin_fabrica_sigue_funcionando_como_antes():
    """Montar `crear_router()` a secas —lo que hacía `main.py` hasta ahora, y lo
    que siguen haciendo los tests de voz— no puede haber cambiado de forma."""
    with TestClient(_app(None)) as cliente:
        with cliente.websocket_connect("/ws/voz?call_id=abc") as ws:
            # Sin fábrica no hay a quién preguntar: se avisa por el log y la
            # sesión sigue, suelta.
            assert ws.receive_json()["tipo"] == "listo"
