# Cliente de prueba de voz — micrófono real, dos clics

Banco de pruebas manual para el bucle de voz. No es la web app: es una página de
un fichero, sin build ni dependencias, para comprobar con oídos y micrófono lo
que el spike ya mide con audio inyectado (`scripts/spikes/spike_voz.py`).

## Arranque

**1. Montar el router del WebSocket.** Aún no está en `app/main.py` (ese fichero
es de otro agente; está anotado en `docs/CONTRATO_API.md` §Cambios sobre el
contrato, punto 5). Dos líneas:

```python
from app.voice.pipeline_ws import crear_router
app.include_router(crear_router())
```

**2. Levantar el backend.**

```bash
cd backend && uv run uvicorn app.main:app --reload --port 8000
```

Si `app/main.py` todavía no existe o no monta el router, se puede levantar solo
el bucle de voz sin tocar nada de nadie:

```bash
cd backend && uv run python -c "
from fastapi import FastAPI
from app.voice.pipeline_ws import crear_router
import uvicorn
app = FastAPI(); app.include_router(crear_router())
uvicorn.run(app, host='127.0.0.1', port=8000)
"
```

**3. Abrir la página.** El micrófono exige un origen seguro, y `localhost`
cuenta como tal — pero `file://` no siempre. Lo fiable:

```bash
cd scripts/spikes/cliente_voz && python3 -m http.server 5500
# luego: http://localhost:5500
```

**4. En la página**: `Conectar` → `Hablar`. Habla, calla medio segundo y espera.

## Qué comprobar, en este orden

| Prueba | Qué hacer | Qué debe pasar |
|---|---|---|
| Turno normal | «Me hicieron una apendicectomía hace tres días.» Callar. | El texto sale bien escrito (*apendicectomía*, no *appendicitomía*) y el agente contesta en ~2 s |
| **Barge-in** | Mientras el agente habla, decir «perdone, una pregunta» | Se calla **de golpe**, no al final de la frase. En el registro aparece `(interrumpido)` |
| Fin de turno | Frase con una pausa de pensar por medio: «Pues no sé… la herida creo que está bien» | No debe cortarte en la pausa |
| Respuesta corta | Solo «Sí.» | Lo tiene que coger: dura 200 ms |
| Eco | Subir el volumen del altavoz | El agente **no** debe interrumpirse a sí mismo |

La última es la que no se puede medir sin un humano: el cancelador de eco lo
pone el navegador (`echoCancellation: true`), y su eficacia depende del equipo.
Si el agente se interrumpe solo, la solución no es tocar el VAD sino usar
auriculares para la demo — y anotarlo.

## Modo de voz

El TTS local por defecto (`TTS_ENGINE_LOCAL=kokoro`) **no está instalado**: el
paquete `kokoro` no figura en `backend/pyproject.toml`. Hasta que se añada, para
probar con voz hay dos caminos:

```bash
# a) motor del sistema, cero instalación
TTS_ENGINE_LOCAL=say uv run uvicorn ...

# b) Kokoro en un entorno superpuesto, sin tocar pyproject.toml
uv run --with kokoro uvicorn ...
```

No se usa ElevenLabs para estas pruebas: cobra por carácter y quedan ~9.500
caracteres del free tier.
