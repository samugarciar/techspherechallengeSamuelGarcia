"""Aplicación FastAPI: consola de administración y consulta del RAG.

El ciclo de vida hace dos cosas, y la segunda merece explicación.

Abrir el pool es obvio. Precargar bge-m3 y el reranker en un `create_task` en
lugar de hacerlo antes de aceptar peticiones es la decisión: cargarlos tarda
decenas de segundos, y esperarlos dejaría la consola sin poder ni listar
documentos durante ese rato. Arrancando en paralelo, el administrador entra de
inmediato, la ingesta —que es lo primero que hace— ya encuentra el modelo
caliente, y `GET /api/health` publica `modelos_listos` para saber cuándo la
consulta RAG responderá a plena velocidad. La alternativa que se descartó,
cargar en la primera petición, hace que el primer `/api/rag/query` de la demo
tarde medio minuto y parezca roto.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import ajustes, documentos, errores, eventos, rag, salud
from app.db.pool import close_pool, open_pool

log = logging.getLogger("api")

# Vite sirve la consola en el 5173 y el cliente de prueba de voz en el 5500. Se
# listan los dos nombres de cada uno porque el navegador manda como Origin
# exactamente el que se escribió en la barra, y 127.0.0.1 y localhost son
# orígenes distintos para CORS.
#
# El 5500 hace falta para algo concreto: cuando /ws/voz falla, un WebSocket no
# expone el código HTTP del rechazo al JavaScript de la página, así que «no hay
# backend» y «el backend está arrancado sin VOZ=1» se ven idénticos. El cliente
# lo distingue preguntando por HTTP a /api/health, y sin este origen esa
# consulta la bloquearía el navegador y el diagnóstico saldría al revés.
ORIGENES = [
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:5500", "http://127.0.0.1:5500",
]


def _precarga_activa() -> bool:
    return os.environ.get("PRECARGAR_MODELOS", "1").lower() not in ("0", "false", "no")


async def _precargar_modelos() -> None:
    from app.rag import embeddings, rerank

    try:
        await asyncio.to_thread(embeddings.precalentar)
        await asyncio.to_thread(rerank.precalentar)
        salud.marcar_modelos_listos()
        log.info("modelos de RAG precargados")
    except asyncio.CancelledError:
        raise
    except Exception:
        # Que falle la precarga no debe impedir arrancar: sin modelos la consola
        # sigue subiendo, listando y borrando, que es la mayor parte de la Fase 2.
        # La carga perezosa de app/rag/* volverá a intentarlo en la primera consulta.
        log.warning("no se pudieron precargar los modelos de RAG", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await open_pool()
    tarea = asyncio.create_task(_precargar_modelos()) if _precarga_activa() else None
    try:
        yield
    finally:
        if tarea is not None:
            tarea.cancel()
            await asyncio.gather(tarea, return_exceptions=True)
        await close_pool()


app = FastAPI(
    title="Agente de seguimiento postoperatorio — API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES,
    # No hay cookies ni sesión: la autenticación es una cabecera explícita, así
    # que no hace falta credentials y el navegador no arrastra nada por su cuenta.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

errores.registrar_manejadores(app)

app.include_router(salud.router, prefix="/api")
# /stream ANTES que /{document_id}: FastAPI resuelve por orden de registro y
# `document_id` está tipado como UUID, así que "stream" no caería por su propio
# pie a la ruta siguiente — daría un 422 confuso en el endpoint más importante
# de la demo.
app.include_router(eventos.router, prefix="/api/documents")
app.include_router(documentos.router, prefix="/api/documents")
app.include_router(rag.router, prefix="/api/rag")
app.include_router(ajustes.router, prefix="/api/settings")


# --- Voz -------------------------------------------------------------------
# Se monta detrás de una variable de entorno y APAGADO por defecto, no por
# capricho: construir el router carga Silero, Whisper y el motor de TTS, que
# tardan y ocupan GPU. La consola de administración no necesita nada de eso, y
# la mayor parte del trabajo con ella —subir, listar, borrar, consultar— iría
# más lenta a cambio de nada.
#
#     VOZ=1 uv run uvicorn app.main:app --port 8000
#
# El transporte es el WebSocket propio (`/ws/voz`), que es el que tiene cliente
# de navegador listo en scripts/spikes/cliente_voz/. La comparativa recomienda
# Pipecat para producción por solapar el STT con la espera de fin de turno
# (1.596 ms contra 1.975 ms hasta el primer audio), pero le falta el transporte
# WebRTC real. Ver docs/VOZ_COMPARATIVA.md.
if os.environ.get("VOZ", "0").lower() in ("1", "true", "si", "sí"):
    from app.voice.pipeline_ws import crear_router

    app.include_router(crear_router())
    log.info("router de voz montado en /ws/voz")
