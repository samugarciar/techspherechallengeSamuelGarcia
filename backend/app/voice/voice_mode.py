"""Dos modos de voz conmutables desde la consola, con contabilidad de coste.

    local   -> Kokoro-82M   · gratis, ilimitado, funciona sin red
    premium -> ElevenLabs   · calidad muy superior, coste por carácter

El modo vive en la base de datos, no en .env, por una razón concreta: el admin
puede cambiarlo **en mitad de una llamada** y la frase siguiente ya sale con la
otra voz. Además convierte una restricción técnica en una palanca de negocio —
un hospital puede operar en local por cumplimiento o coste, y subir a premium
cuando la experiencia del paciente lo justifique.

Los dos motores se mantienen cargados: cambiar de modo no debe pagar el arranque
de un modelo en mitad de una conversación.
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from app.core.config import get_settings
from app.db.pool import connection
from app.voice.tts import Audio, TTSEngine, crear_motor

Modo = Literal["local", "premium"]

# Cache en proceso para no consultar la BD en cada frase sintetizada. El TTL es
# corto a propósito: el toggle debe notarse casi al instante, y una consulta
# cada pocos segundos no le pesa a nadie.
_TTL_S = 3.0
_cache: tuple[Modo, float] | None = None
_lock = asyncio.Lock()


async def modo_actual() -> Modo:
    global _cache
    if _cache is not None and (time.monotonic() - _cache[1]) < _TTL_S:
        return _cache[0]

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT value FROM app_settings WHERE key = 'voice_mode'"
        )
        fila = await cur.fetchone()

    modo: Modo = fila["value"] if fila and fila["value"] in ("local", "premium") else "local"
    _cache = (modo, time.monotonic())
    return modo


async def cambiar_modo(modo: Modo, actor: str = "admin") -> None:
    global _cache
    if modo not in ("local", "premium"):
        raise ValueError(f"modo de voz inválido: {modo}")

    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_by, updated_at)
            VALUES ('voice_mode', %s, %s, now())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value,
                   updated_by = EXCLUDED.updated_by,
                   updated_at = now()
            """,
            (modo, actor),
        )
    _cache = (modo, time.monotonic())


class VoiceRouter:
    """Enruta cada síntesis al motor del modo activo y anota el consumo.

    Si el motor premium falla —red caída, cuota agotada, API con problemas—
    cae a local automáticamente en vez de dejar al paciente en silencio. En una
    demo en vivo esa degradación es la diferencia entre un fallo y un detalle
    que nadie nota.
    """

    def __init__(self) -> None:
        self._motores: dict[Modo, TTSEngine] = {}

    async def _motor(self, modo: Modo) -> TTSEngine:
        if modo not in self._motores:
            async with _lock:
                if modo not in self._motores:
                    s = get_settings()
                    nombre = s.tts_engine_local if modo == "local" else s.tts_engine_premium
                    self._motores[modo] = await asyncio.to_thread(crear_motor, nombre)
        return self._motores[modo]

    async def sintetizar(self, texto: str, call_id: str | None = None) -> Audio:
        modo = await modo_actual()
        t0 = time.perf_counter()

        try:
            motor = await self._motor(modo)
            audio = await motor.sintetizar(texto)
        except Exception:
            if modo == "local":
                raise
            # Degradación a local: mejor una voz peor que ninguna voz.
            modo = "local"
            motor = await self._motor(modo)
            audio = await motor.sintetizar(texto)

        await self._anotar(
            call_id, modo, motor.nombre, len(texto),
            (time.perf_counter() - t0) * 1000, audio.duracion_s,
        )
        return audio

    @staticmethod
    async def _anotar(call_id, modo, engine, chars, ms, audio_s) -> None:
        try:
            async with connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO tts_usage (call_id, mode, engine, characters,
                                           duration_ms, audio_s)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (call_id, modo, engine, chars, int(ms), audio_s),
                )
        except Exception:
            # La contabilidad nunca debe tumbar una llamada en curso.
            pass

    async def cerrar(self) -> None:
        for motor in self._motores.values():
            await motor.cerrar()
        self._motores.clear()


async def resumen_consumo() -> list[dict]:
    """Consumo agregado por modo, para el panel de la consola.

    Da al admin lo que necesita para decidir: cuántos caracteres lleva gastados
    en premium y qué latencia media está obteniendo a cambio.
    """
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT mode,
                   count(*)                AS sintesis,
                   sum(characters)         AS caracteres,
                   round(avg(duration_ms)) AS latencia_media_ms,
                   round(sum(audio_s)::numeric, 1) AS audio_total_s
            FROM tts_usage
            GROUP BY mode
            """
        )
        return [dict(f) for f in await cur.fetchall()]
