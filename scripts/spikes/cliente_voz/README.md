# Cliente de prueba de voz — micrófono real, dos clics

Banco de pruebas manual para el bucle de voz. No es la web app: es una página de
un fichero, sin build ni dependencias, para comprobar con oídos y micrófono lo
que el spike ya mide con audio inyectado (`scripts/spikes/spike_voz.py`).

## Arranque

**1. Levantar el backend con voz.** `app/main.py` monta `/ws/voz`, pero solo con
`VOZ=1`: construir el router carga Silero, Whisper y el motor de TTS, y la consola
de administración no necesita nada de eso.

```bash
cd backend && VOZ=1 uv run uvicorn app.main:app --reload --port 8000
```

Sin `?call_id=…` en la URL del WebSocket —que es el caso de esta página— la sesión
no busca ningún agente clínico y cae a `ClienteLLMFalso`: es la «sesión suelta sin
persistir» del contrato. Se contesta una frase fija, que es justo lo que hace falta
para juzgar el VAD, el barge-in y el TTS sin gastar llamadas al LLM.

**2. Abrir la página.** El micrófono exige un origen seguro, y `localhost`
cuenta como tal — pero `file://` no siempre. Lo fiable:

```bash
cd scripts/spikes/cliente_voz && python3 -m http.server 5500
# luego: http://localhost:5500
```

**3. En la página**: `Conectar` → `Hablar`. Habla, calla medio segundo y espera.

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

El TTS local por defecto es Kokoro y **ya es dependencia del proyecto** (durante un
tiempo no lo fue: la Fase 0 lo midió con `uv run --with kokoro` y el paquete nunca
llegó a `pyproject.toml`, así que el modo por defecto no arrancaba). Con `uv sync`
funciona sin más.

Para iterar rápido sin esperar a que cargue Kokoro, el motor del sistema sirve:

```bash
cd backend && VOZ=1 TTS_ENGINE_LOCAL=say uv run uvicorn app.main:app --port 8000
```

No se usa ElevenLabs para estas pruebas: cobra por carácter y quedan ~9.500
caracteres del free tier.
