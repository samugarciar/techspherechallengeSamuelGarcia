"""Adaptador de LLM: una interfaz, dos proveedores.

Existe por una razón concreta: si en la demo el TTFT de Gemini estorba, cambiar
a Groq debe ser editar una variable de entorno, no refactorizar el agente. Groq
sirve respuestas con un TTFT bastante menor; Gemini tiene mejor español clínico
y un tool calling más fiable. Cuál gana se decide midiendo, no discutiendo.

Ambos proveedores hablan del mismo par de tipos (`Mensaje`, `RespuestaLLM`) y de
un esquema de herramientas en formato JSON Schema, que es el mínimo común
denominador entre las dos APIs.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import get_settings


@dataclass(slots=True)
class Mensaje:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(slots=True)
class LlamadaHerramienta:
    id: str
    nombre: str
    argumentos: dict[str, Any]


@dataclass(slots=True)
class RespuestaLLM:
    texto: str = ""
    herramientas: list[LlamadaHerramienta] = field(default_factory=list)
    ttft_ms: float | None = None


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

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._modelo = modelo

    @staticmethod
    def _a_contents(mensajes: list[Mensaje]) -> list[dict[str, Any]]:
        roles = {"user": "user", "assistant": "model", "tool": "user"}
        return [
            {"role": roles[m.role], "parts": [{"text": m.content}]} for m in mensajes
        ]

    def _config(self, sistema: str, herramientas: list[dict[str, Any]] | None):
        from google.genai import types

        kwargs: dict[str, Any] = {"system_instruction": sistema}
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
        import time

        t0 = time.perf_counter()
        resp = await self._client.aio.models.generate_content(
            model=self._modelo,
            contents=self._a_contents(mensajes),
            config=self._config(sistema, herramientas),
        )
        ttft = (time.perf_counter() - t0) * 1000

        llamadas: list[LlamadaHerramienta] = []
        texto_partes: list[str] = []
        for cand in resp.candidates or []:
            for parte in getattr(cand.content, "parts", None) or []:
                if fn := getattr(parte, "function_call", None):
                    llamadas.append(
                        LlamadaHerramienta(
                            id=fn.name, nombre=fn.name, argumentos=dict(fn.args or {})
                        )
                    )
                elif txt := getattr(parte, "text", None):
                    texto_partes.append(txt)

        return RespuestaLLM("".join(texto_partes), llamadas, ttft)

    async def stream(self, sistema, mensajes) -> AsyncIterator[str]:
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

        self._client = AsyncGroq(api_key=api_key)
        self._modelo = modelo

    @staticmethod
    def _a_messages(sistema: str, mensajes: list[Mensaje]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": sistema}]
        for m in mensajes:
            if m.role == "tool":
                out.append(
                    {"role": "tool", "content": m.content, "tool_call_id": m.tool_call_id}
                )
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    async def responder(self, sistema, mensajes, herramientas=None) -> RespuestaLLM:
        import time

        kwargs: dict[str, Any] = {
            "model": self._modelo,
            "messages": self._a_messages(sistema, mensajes),
        }
        if herramientas:
            kwargs["tools"] = [{"type": "function", "function": h} for h in herramientas]

        t0 = time.perf_counter()
        resp = await self._client.chat.completions.create(**kwargs)
        ttft = (time.perf_counter() - t0) * 1000

        msg = resp.choices[0].message
        llamadas = [
            LlamadaHerramienta(
                id=tc.id,
                nombre=tc.function.name,
                argumentos=json.loads(tc.function.arguments or "{}"),
            )
            for tc in (msg.tool_calls or [])
        ]
        return RespuestaLLM(msg.content or "", llamadas, ttft)

    async def stream(self, sistema, mensajes) -> AsyncIterator[str]:
        it = await self._client.chat.completions.create(
            model=self._modelo,
            messages=self._a_messages(sistema, mensajes),
            stream=True,
        )
        async for trozo in it:
            if delta := trozo.choices[0].delta.content:
                yield delta


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
