"""Forma única de los errores de la API.

El contrato exige `{"error":{"codigo":…,"mensaje":…}}` en TODA respuesta de
error, nunca el `{"detail":…}` que FastAPI devuelve por defecto. Un solo formato
importa más de lo que parece: la consola enseña `mensaje` tal cual al
administrador, así que una respuesta con otra forma pintaría «undefined» en
pantalla justo en el momento en que algo va mal.

De ahí que haya cuatro manejadores y no uno. `ErrorAPI` cubre lo previsto, pero
por debajo hay tres fuentes de respuestas que nadie escribe a mano y que también
tienen que salir con la forma buena: la validación de Pydantic (cuerpo mal
formado), las `HTTPException` que Starlette genera por su cuenta (ruta
inexistente, método equivocado) y cualquier excepción no capturada.

Sobre el 500: el mensaje que sale es fijo y genérico a propósito. El detalle
—traza, SQL, ruta en disco— se registra en el log del servidor, no se devuelve.
En una consola clínica un stack trace no ayuda a nadie y sí filtra estructura
interna.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("api.errores")

# Los cuatro primeros son los del contrato original. Los tres últimos cubren
# respuestas que genera el framework y que el contrato no contempla; están
# anotados en docs/CONTRATO_API.md §«Cambios sobre el contrato».
TAMANO_MAXIMO_MB = 25


class ErrorAPI(Exception):
    """Error con código estable, mensaje mostrable y estado HTTP."""

    def __init__(self, codigo: str, mensaje: str, http: int = 400) -> None:
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje
        self.http = http

    def a_respuesta(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.http,
            content={"error": {"codigo": self.codigo, "mensaje": self.mensaje}},
        )


# --- Constructores -----------------------------------------------------------
# Funciones y no subclases: cada error se usa en un solo sitio y lo que importa
# es que el texto en español viva junto a su código, no la jerarquía.


def no_autorizado() -> ErrorAPI:
    return ErrorAPI(
        "no_autorizado",
        "Token de administrador ausente o incorrecto.",
        401,
    )


def documento_no_encontrado(document_id: UUID | str) -> ErrorAPI:
    return ErrorAPI(
        "documento_no_encontrado",
        f"No existe ningún documento con id {document_id}.",
        404,
    )


def formato_no_soportado(extension: str) -> ErrorAPI:
    visto = extension or "sin extensión"
    return ErrorAPI(
        "formato_no_soportado",
        f"Formato no soportado ({visto}). Se aceptan archivos .pdf, .docx, .md y .txt.",
        415,
    )


def archivo_vacio() -> ErrorAPI:
    return ErrorAPI(
        "archivo_vacio",
        "El archivo está vacío: no hay nada que aprender.",
        400,
    )


def archivo_demasiado_grande() -> ErrorAPI:
    return ErrorAPI(
        "archivo_demasiado_grande",
        f"El archivo supera el máximo permitido de {TAMANO_MAXIMO_MB} MB.",
        413,
    )


def documento_duplicado(estado: str) -> ErrorAPI:
    return ErrorAPI(
        "documento_duplicado",
        (
            "Este archivo ya se subió y todavía se está procesando "
            f"(estado: {estado}). Espera a que termine antes de volver a subirlo."
        ),
        409,
    )


def peticion_invalida(mensaje: str) -> ErrorAPI:
    return ErrorAPI("peticion_invalida", mensaje, 422)


def error_interno() -> ErrorAPI:
    return ErrorAPI(
        "error_interno",
        "Error interno del servidor. Vuelve a intentarlo; si persiste, revisa los logs.",
        500,
    )


# --- Manejadores -------------------------------------------------------------


async def _manejar_error_api(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ErrorAPI)
    return exc.a_respuesta()


def _campo_legible(loc: tuple[Any, ...]) -> str:
    """Ruta del campo que Pydantic señala, sin el prefijo del origen.

    Pydantic devuelve `("body", "consulta")` o `("query", "limit")`; al usuario
    solo le sirve la última parte, que es el nombre que él escribió.
    """
    partes = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
    return " › ".join(partes) if partes else "cuerpo de la petición"


async def _manejar_validacion(_: Request, exc: Exception) -> JSONResponse:
    """Traduce el 422 de Pydantic a un mensaje en español.

    No se reexpone el texto de Pydantic: viene en inglés y con jerga
    ("field required", "value is not a valid uuid"), que no es mostrable en la
    consola. Se nombra el campo, que es la parte accionable.
    """
    assert isinstance(exc, RequestValidationError)
    fallos = exc.errors()
    campo = _campo_legible(fallos[0]["loc"]) if fallos else "cuerpo de la petición"
    return peticion_invalida(f"La petición no es válida: revisa «{campo}».").a_respuesta()


_POR_ESTADO = {
    401: ("no_autorizado", "Token de administrador ausente o incorrecto."),
    403: ("no_autorizado", "Token de administrador ausente o incorrecto."),
    404: ("no_encontrado", "La ruta solicitada no existe."),
    405: ("metodo_no_permitido", "El método HTTP no está permitido en esta ruta."),
}


async def _manejar_http(_: Request, exc: Exception) -> JSONResponse:
    """Respuestas que genera Starlette sin pasar por nuestro código.

    Son sobre todo 404 de ruta y 405 de método. Sin este manejador saldrían como
    `{"detail":"Not Found"}` y romperían el contrato en el peor momento: cuando
    el frontend ha llamado a una URL que no existe y necesita ver por qué.
    """
    assert isinstance(exc, StarletteHTTPException)
    codigo, mensaje = _POR_ESTADO.get(
        exc.status_code, ("error_interno", str(exc.detail) or "Error inesperado.")
    )
    return ErrorAPI(codigo, mensaje, exc.status_code).a_respuesta()


async def _manejar_inesperado(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Excepción no controlada en %s %s", request.method, request.url.path)
    return error_interno().a_respuesta()


def registrar_manejadores(app: FastAPI) -> None:
    app.add_exception_handler(ErrorAPI, _manejar_error_api)
    app.add_exception_handler(RequestValidationError, _manejar_validacion)
    app.add_exception_handler(StarletteHTTPException, _manejar_http)
    # Starlette envía esta respuesta y RE-LANZA la excepción para que uvicorn la
    # registre. El cliente recibe el JSON correcto y el fallo sigue apareciendo
    # en los logs con su traza: no se pierde ninguna de las dos cosas.
    app.add_exception_handler(Exception, _manejar_inesperado)
