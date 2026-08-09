"""Adaptador de LLM: una interfaz, dos proveedores.

Existe por una razón concreta: si en la demo el TTFT de Gemini estorba, cambiar
a Groq debe ser editar una variable de entorno, no refactorizar el agente. Groq
sirve respuestas con un TTFT bastante menor; Gemini tiene mejor español clínico
y un tool calling más fiable. Cuál gana se decide midiendo, no discutiendo.

Ambos proveedores hablan del mismo par de tipos (`Mensaje`, `RespuestaLLM`) y de
un esquema de herramientas en formato JSON Schema, que es el mínimo común
denominador entre las dos APIs.

── Lo que la primera ejecución rompió ───────────────────────────────────────
Este módulo se escribió sin API key y nunca se había ejecutado. Al ejecutarlo
por primera vez (8 de agosto) salieron tres cosas, y solo una era la que se
sospechaba:

**1. El `await` sobre `generate_content_stream` NO estaba mal.** Era la sospecha
principal y resultó falsa para `google-genai>=2`: ahí `aio.models
.generate_content_stream` es una corrutina que *devuelve* un `AsyncIterator`
(`inspect.iscoroutinefunction(...) is True`), así que hay que esperarla y luego
iterar, que es justo lo que el código hacía. En las versiones 0.x sí era un
generador asíncrono directo y el `await` habría reventado; de ahí la confusión.
Se deja escrito para que nadie lo "arregle" al revés.

**2. Lo que sí estaba roto era el bucle de herramientas**, y de forma
silenciosa. `Mensaje` no tenía dónde guardar las llamadas que emite el modelo,
así que al mandar el resultado de una tool el historial que se reconstruía era
`usuario → usuario`, sin la llamada del modelo en medio. Gemini lo tolera y
responde algo razonable —por eso pasaba desapercibido—, pero Groq (contrato
OpenAI) exige que todo mensaje `role="tool"` vaya precedido del `assistant` con
sus `tool_calls`, y con dos herramientas seguidas el modelo pierde qué pidió y
vuelve a pedirlo. Arreglado con `Mensaje.herramientas` y con
`Part.from_function_response`, que es la forma que la API de Gemini espera para
un resultado de herramienta en vez de un texto suelto.

**3. El razonamiento de Gemini 2.5 Flash está encendido por defecto y cuesta más
que todo el resto del pipeline.** Ver `PENSAMIENTO_DESACTIVADO` abajo.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import get_settings

TIMEOUT_S = 12.0
"""Techo para una respuesta del LLM.

Sin timeout, una petición colgada deja al paciente escuchando silencio hasta que
cuelgue: en una llamada telefónica no hay barra de carga que mirar. 12 s es
holgado para el peor caso con herramientas y muy por debajo de lo que alguien
aguanta callado. Al saltar, el agente cae a su frase de seguridad y escala.
"""

PENSAMIENTO_DESACTIVADO = 0
"""`thinking_budget=0` — la palanca de latencia más grande del camino del LLM.

Gemini 2.5 Flash trae el razonamiento **encendido por defecto** y con
presupuesto dinámico: antes de emitir el primer token se gasta el rato que le
parezca pensando, y ese rato entra íntegro en el TTFT. Medido con el prompt de
sistema real de este agente (README §Presupuesto de latencia): **462 ms sin
razonamiento contra 956 ms con él**, un factor 2,1. Casi medio segundo por turno,
y en una llamada de quince turnos eso son siete segundos de silencio regalados.

Se desactiva porque este agente no razona: sigue un guion, lee datos con
herramientas y repite lo que dice un protocolo. El trabajo difícil —decidir si
hay una bandera roja— es determinista y ni siquiera pasa por el modelo
(`app/agent/redflags.py`). Pagar una cadena de pensamiento para preguntar «¿ha
tenido fiebre?» es gastar el presupuesto de latencia en la parte que no lo
necesita.

Queda como constante y no enterrado en el código porque es reversible: si en los
evals de la Fase 6 el modelo se equivoca eligiendo herramienta, subir esto a 128
o 256 es la primera cosa que probar.
"""

TEMPERATURA = 0.3
"""Baja a propósito. Este agente lee protocolos y hace preguntas de un guion; la
variedad léxica no aporta nada y sí abre la puerta a que reformule una
instrucción clínica «con sus palabras». 0.0 se descartó porque tiende a repetir
la misma muletilla en cada turno y en voz eso se nota mucho."""


@dataclass(slots=True)
class LlamadaHerramienta:
    id: str
    nombre: str
    argumentos: dict[str, Any]


@dataclass(slots=True)
class Mensaje:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    herramientas: list[LlamadaHerramienta] = field(default_factory=list)
    """Solo en `role="assistant"`: las llamadas que el modelo emitió en ese turno.

    Sin esto el historial de una conversación con herramientas es inválido —ver
    el punto 2 de la cabecera del módulo—. Va al final del dataclass porque
    `Mensaje("user", texto)` se construye posicionalmente en `app/voice/**`, que
    es de otro agente y no se puede tocar.
    """


@dataclass(slots=True)
class RespuestaLLM:
    texto: str = ""
    herramientas: list[LlamadaHerramienta] = field(default_factory=list)
    ttft_ms: float | None = None
    motivo_fin: str | None = None
    """`finish_reason` del proveedor. Interesa uno en concreto: `MAX_TOKENS` con
    texto vacío es el síntoma de que el razonamiento se comió el presupuesto de
    salida, y sin este campo se ve como «el modelo no contestó»."""


class LLMClient(ABC):
    """Contrato mínimo que el agente necesita."""

    @abstractmethod
    async def responder(
        self,
        sistema: str,
        mensajes: list[Mensaje],
        herramientas: list[dict[str, Any]] | None = None,
    ) -> RespuestaLLM: ...

    @abstractmethod
    def stream(
        self,
        sistema: str,
        mensajes: list[Mensaje],
    ) -> AsyncIterator[str]:
        """Emite fragmentos de texto conforme llegan.

        En voz esto no es un lujo: permite empezar a sintetizar la primera frase
        mientras el modelo sigue escribiendo el resto.
        """
        ...


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
class GeminiClient(LLMClient):
    def __init__(self, api_key: str, modelo: str) -> None:
        from google import genai
        from google.genai import types

        self._genai = genai
        self._types = types
        self._client = genai.Client(
            api_key=api_key,
            # El timeout de `HttpOptions` va en MILISEGUNDOS. En segundos, el
            # cliente aceptaría el número igual y el techo real sería de 12 ms:
            # todas las peticiones fallarían y parecería un problema de red.
            http_options=types.HttpOptions(timeout=int(TIMEOUT_S * 1000)),
        )
        self._modelo = modelo

    def _a_contents(self, mensajes: list[Mensaje]) -> list[Any]:
        """Historial en el formato de Gemini.

        Tres reglas que no son evidentes:

        - El turno del modelo se reconstruye con `Part(function_call=…)`, no con
          el texto de la llamada. Si se manda como texto, el modelo lee su propia
          petición como si se la hubiera dicho el usuario.
        - El resultado de una herramienta va con rol `user` y
          `Part.from_function_response`. Gemini no tiene rol `tool`: la respuesta
          de la herramienta la aporta el lado del usuario.
        - Nunca se emite una `Part` de texto vacío. Un turno de modelo que solo
          hizo llamadas tiene `content=""`, y una parte vacía es basura que
          confunde al modelo cuando no la rechaza la propia API.
        """
        types = self._types
        contents: list[Any] = []

        for m in mensajes:
            if m.role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=m.name or m.tool_call_id or "herramienta",
                                # `response` tiene que ser un objeto JSON. La
                                # herramienta devuelve su resultado ya serializado,
                                # así que se envuelve; si viniera JSON válido se
                                # reenvía tal cual para que el modelo lo lea con
                                # su estructura y no como una cadena.
                                response=_como_objeto(m.content),
                            )
                        ],
                    )
                )
                continue

            partes: list[Any] = []
            if m.content:
                partes.append(types.Part(text=m.content))
            for llamada in m.herramientas:
                partes.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=llamada.nombre, args=llamada.argumentos
                        )
                    )
                )
            if not partes:
                continue
            contents.append(
                types.Content(role="model" if m.role == "assistant" else "user", parts=partes)
            )

        return contents

    def _config(self, sistema: str, herramientas: list[dict[str, Any]] | None):
        types = self._types
        kwargs: dict[str, Any] = {
            "system_instruction": sistema,
            "temperature": TEMPERATURA,
            "thinking_config": types.ThinkingConfig(
                thinking_budget=PENSAMIENTO_DESACTIVADO
            ),
        }
        if herramientas:
            kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=h["name"],
                            description=h["description"],
                            parameters=h["parameters"],
                        )
                        for h in herramientas
                    ]
                )
            ]
        return types.GenerateContentConfig(**kwargs)

    async def responder(self, sistema, mensajes, herramientas=None) -> RespuestaLLM:
        t0 = time.perf_counter()
        resp = await self._client.aio.models.generate_content(
            model=self._modelo,
            contents=self._a_contents(mensajes),
            config=self._config(sistema, herramientas),
        )
        latencia = (time.perf_counter() - t0) * 1000

        llamadas: list[LlamadaHerramienta] = []
        texto_partes: list[str] = []
        motivo = None
        for cand in resp.candidates or []:
            motivo = motivo or getattr(cand, "finish_reason", None)
            for parte in getattr(cand.content, "parts", None) or []:
                if fn := getattr(parte, "function_call", None):
                    llamadas.append(
                        LlamadaHerramienta(
                            # Gemini rellena `id` solo a veces. El nombre sirve de
                            # identificador porque este agente no pide la misma
                            # herramienta dos veces en el mismo turno; si algún día
                            # lo hiciera, el `id` real de la API manda.
                            id=getattr(fn, "id", None) or fn.name,
                            nombre=fn.name,
                            argumentos=dict(fn.args or {}),
                        )
                    )
                elif txt := getattr(parte, "text", None):
                    texto_partes.append(txt)

        return RespuestaLLM(
            "".join(texto_partes), llamadas, latencia, str(motivo) if motivo else None
        )

    async def stream(self, sistema, mensajes) -> AsyncIterator[str]:
        # OJO: el `await` es correcto en google-genai >= 1. Ahí
        # `generate_content_stream` es una corrutina que devuelve el iterador
        # asíncrono, no un generador asíncrono. Quitarlo rompe el streaming con
        # "async for requires __aiter__, got coroutine". Ver cabecera del módulo.
        it = await self._client.aio.models.generate_content_stream(
            model=self._modelo,
            contents=self._a_contents(mensajes),
            config=self._config(sistema, None),
        )
        async for trozo in it:
            if texto := getattr(trozo, "text", None):
                yield texto


# ---------------------------------------------------------------------------
# Groq — API compatible con OpenAI
# ---------------------------------------------------------------------------
class GroqClient(LLMClient):
    def __init__(self, api_key: str, modelo: str) -> None:
        from groq import AsyncGroq

        self._client = AsyncGroq(api_key=api_key, timeout=TIMEOUT_S, max_retries=1)
        self._modelo = modelo

    @staticmethod
    def _a_messages(sistema: str, mensajes: list[Mensaje]) -> list[dict[str, Any]]:
        """Historial en formato OpenAI.

        La parte delicada es el `assistant` con `tool_calls`: el contrato de
        OpenAI —y Groq lo valida— exige que todo mensaje `role="tool"` vaya
        precedido del `assistant` que pidió esa herramienta, con el mismo
        `tool_call_id`. Sin él la petición se rechaza con un 400 que además no
        dice cuál falta.
        """
        out: list[dict[str, Any]] = [{"role": "system", "content": sistema}]
        for m in mensajes:
            if m.role == "tool":
                out.append(
                    {"role": "tool", "content": m.content, "tool_call_id": m.tool_call_id}
                )
            elif m.role == "assistant" and m.herramientas:
                out.append(
                    {
                        "role": "assistant",
                        # `content` a None y no a "": la API distingue entre «no
                        # dijo nada, solo llamó a la herramienta» y «dijo la
                        # cadena vacía».
                        "content": m.content or None,
                        "tool_calls": [
                            {
                                "id": h.id,
                                "type": "function",
                                "function": {
                                    "name": h.nombre,
                                    "arguments": json.dumps(h.argumentos, ensure_ascii=False),
                                },
                            }
                            for h in m.herramientas
                        ],
                    }
                )
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    async def responder(self, sistema, mensajes, herramientas=None) -> RespuestaLLM:
        kwargs: dict[str, Any] = {
            "model": self._modelo,
            "messages": self._a_messages(sistema, mensajes),
            "temperature": TEMPERATURA,
        }
        if herramientas:
            kwargs["tools"] = [{"type": "function", "function": h} for h in herramientas]

        t0 = time.perf_counter()
        resp = await self._client.chat.completions.create(**kwargs)
        latencia = (time.perf_counter() - t0) * 1000

        eleccion = resp.choices[0]
        msg = eleccion.message
        llamadas = [
            LlamadaHerramienta(
                id=tc.id,
                nombre=tc.function.name,
                argumentos=_como_objeto(tc.function.arguments or "{}"),
            )
            for tc in (msg.tool_calls or [])
        ]
        return RespuestaLLM(msg.content or "", llamadas, latencia, eleccion.finish_reason)

    async def stream(self, sistema, mensajes) -> AsyncIterator[str]:
        it = await self._client.chat.completions.create(
            model=self._modelo,
            messages=self._a_messages(sistema, mensajes),
            temperature=TEMPERATURA,
            stream=True,
        )
        async for trozo in it:
            # El último trozo del stream llega con `choices` vacío y solo las
            # estadísticas de uso. Indexarlo a ciegas revienta con IndexError
            # justo al final de la respuesta, cuando ya se había sintetizado
            # todo: el fallo aparece como un turno que "no termina nunca".
            if not trozo.choices:
                continue
            if delta := trozo.choices[0].delta.content:
                yield delta


def _como_objeto(bruto: str) -> dict[str, Any]:
    """Texto de una herramienta -> objeto JSON, sin confiar en que lo sea.

    Los argumentos que emite un modelo son JSON *casi* siempre; el resultado que
    devuelve una herramienta lo es porque lo serializamos nosotros. En los dos
    casos, un fallo de parseo no debe tumbar la llamada: se envuelve el texto
    crudo y que el modelo lo lea como texto, que es mejor que una excepción a
    mitad de conversación con un paciente.
    """
    try:
        valor = json.loads(bruto)
    except (json.JSONDecodeError, TypeError):
        return {"resultado": bruto}
    return valor if isinstance(valor, dict) else {"resultado": valor}


def crear_cliente() -> LLMClient:
    """Punto único de decisión. Cambiar LLM_PROVIDER conmuta el agente entero."""
    s = get_settings()
    if not s.llm_api_key:
        raise RuntimeError(
            f"Falta la API key de {s.llm_provider}. "
            f"Define {'GEMINI_API_KEY' if s.llm_provider == 'gemini' else 'GROQ_API_KEY'} en .env"
        )
    if s.llm_provider == "gemini":
        return GeminiClient(s.llm_api_key, s.llm_model)
    return GroqClient(s.llm_api_key, s.llm_model)
