"""Ajustes conmutables en caliente. Hoy solo el modo de voz.

El modo vive en `app_settings` y no en `.env` para que se pueda cambiar en mitad
de una llamada. Este router es la puerta HTTP a lo que `app/voice/voice_mode.py`
ya resuelve; no duplica ninguna regla, ni siquiera la validación del modo.

Sin UI todavía —la consola de la Fase 2 cubre solo documentos— pero el endpoint
existe porque el toggle es un argumento de diseño del proyecto y tiene que ser
demostrable con curl aunque no haya botón.

Lo que este endpoint NO hace hoy, y no se ve desde aquí: que la frase siguiente
salga con la otra voz. Escribe la fila y la lee `voice_mode.modo_actual()`, pero
el único que consulta esa función para sintetizar es `VoiceRouter`, que no lo
construye nadie; las dos rutas de voz usan `settings.tts_engine_local` fijo. O
sea que hoy este PUT cambia lo que devuelve el GET de al lado y nada más.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import exigir_admin
from app.core.config import get_settings
from app.voice.voice_mode import Modo, cambiar_modo, modo_actual

router = APIRouter(tags=["ajustes"], dependencies=[Depends(exigir_admin)])


class ModoVoz(BaseModel):
    modo: Modo


def _respuesta(modo: Modo) -> dict[str, Any]:
    ajustes = get_settings()
    # Qué motor implementa cada modo sí es configuración de despliegue (.env);
    # cuál de los dos modos está activo es una decisión operativa (base de
    # datos). Se devuelven juntos porque la consola necesita enseñar ambos.
    motor = ajustes.tts_engine_local if modo == "local" else ajustes.tts_engine_premium
    return {"modo": modo, "motor": motor}


@router.get("/voice-mode")
async def leer_modo() -> dict[str, Any]:
    return _respuesta(await modo_actual())


@router.put("/voice-mode")
async def escribir_modo(cuerpo: ModoVoz) -> dict[str, Any]:
    await cambiar_modo(cuerpo.modo)
    return _respuesta(cuerpo.modo)
