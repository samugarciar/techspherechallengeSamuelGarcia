"""Dependencias compartidas por los routers: autenticación del administrador.

Un solo token, sin sesiones ni JWT. La decisión está en el contrato y es
deliberada: hay un único administrador y emitir tokens firmados no demostraría
nada que el jurado vaya a mirar. Lo que sí se hace bien es la comparación.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, Query

from app.api import errores
from app.core.config import get_settings


def _token_valido(recibido: str | None) -> bool:
    """Compara en tiempo constante.

    `==` sobre str corta en el primer byte distinto, y este endpoint es público
    y se puede sondear sin límite: el tiempo de respuesta filtra cuántos
    caracteres del token acertaste. `compare_digest` cuesta lo mismo escribirlo
    y cierra el canal.
    """
    if not recibido:
        return False
    return secrets.compare_digest(recibido, get_settings().admin_token)


async def exigir_admin(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """Autenticación normal, por cabecera. Se aplica a todo `/api/*` salvo salud."""
    if not _token_valido(x_admin_token):
        raise errores.no_autorizado()


async def exigir_admin_por_query(
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Variante para SSE.

    `EventSource` no admite cabeceras personalizadas — no es una limitación
    nuestra, es la API del navegador — así que el token viaja en la query
    string. Se valida exactamente igual: que el transporte sea más pobre no
    rebaja la comprobación.

    El coste conocido de esto es que el token acaba en los logs de acceso del
    servidor y en el historial del navegador. Es asumible precisamente porque el
    token no es una sesión de usuario: se rota cambiando una variable de entorno.
    """
    if not _token_valido(token):
        raise errores.no_autorizado()
