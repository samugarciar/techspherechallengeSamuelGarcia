# Informe de la noche del 7 al 8 de agosto

Rama `nocturno`, en un worktree aparte. **`main` y la base de datos `postop` no
se han tocado.** Todo el trabajo va contra `postop_wt` y `postop_t1..t5`.

```bash
cd .claude/worktrees/nocturno && git log --oneline
```

---

## Lo que funciona, verificado ejecutándolo

**El requisito central del enunciado está cumplido y comprobado contra la API
real**, no solo en tests unitarios:

```
1. SUBIR      protocolo_apendicectomia.pdf → uploaded (112 KB)
2. APRENDER   0.0s uploaded → 1.0s parsing → 1.5s embedding → 2.6s ready (8/8)
3. PREGUNTAR  «¿cuándo puedo ducharme?» → evidencia: True, 3 fragmentos
              [0.763] protocolo_apendicectomia.pdf › Cuidado de la herida › p. 1
4. OLVIDAR    olvidado=True, fragmentos eliminados=8
5. LA MISMA   evidencia: False, 0 fragmentos
   PREGUNTA   → el agente diría «no tengo esa información»
```

Reproducible en un comando: `uv run python scripts/demo_aprender_olvidar.py`.
Es a la vez el guion de la demo y la prueba de regresión de la garantía.

**213 tests en verde**, ejecutados dos veces sobre bases distintas para descartar
inestabilidad, y `ruff` limpio. La consola de React compila sin errores de
TypeScript.

| Fase | Estado |
|---|---|
| 1 — pipeline RAG sin voz | Cerrada: parsing con OCR de respaldo, cola, worker, retrieval híbrido |
| 2 — consola de administración | Cerrada: API con SSE + panel de documentos |
| 3 — loop de voz | **Decidida por medición: Pipecat.** Falta montarla sobre WebRTC y probarla con micrófono |

---

## La decisión de voz, y un argumento mío que resultó falso

Se construyeron las dos opciones y se midieron con el mismo STT, el mismo LLM
simulado y el mismo presupuesto de fin de turno. Detalle en
[docs/VOZ_COMPARATIVA.md](VOZ_COMPARATIVA.md).

| | Pipecat | WebSocket propio |
|---|---:|---:|
| Hasta el primer audio | **1.596 ms** | 1.975 ms |
| Barge-in (silencio audible) | 84 ms | 96 ms |
| Líneas de código de producción | 171 | 312 |
| Distribuciones / disco | 52 / 395 MB | 7 / 117 MB |

**Gana Pipecat, pero no por el motivo que yo había escrito en el README.** Decía
que el barge-in era el trabajo difícil que Pipecat te ahorra. Es falso: funciona
igual en las dos, y el número lo domina el umbral de confirmación del VAD, no el
framework. Lo que decide de verdad es que Pipecat **solapa el STT con la espera de
fin de turno** en lugar de encadenarlos: 379 ms, un 19 %.

La opción propia no se tira. Es el plan B de un solo fichero, el arnés con el que
se midió todo esto, y el banco de pruebas con micrófono.

**Al presupuesto de latencia le faltaba una etapa entera.** El fin de turno cuesta
626 ms —más que el STT— y no aparecía en ninguna versión de la tabla, porque no es
un modelo que se ejecute sino una espera deliberada. El umbral quedó en 640 ms.

---

## El hallazgo importante: el presupuesto de latencia estaba mal

El README publicaba **114 ms** para el reranker. Era un error de medición mío: el
spike de la Fase 0 usó pasajes de 250 caracteres, y los fragmentos reales tienen
entre 500 y 1400. El coste de un cross-encoder escala con la longitud de la
secuencia. Medido en caliente contra la API real: **585 ms**, el mayor bloque del
pipeline, por delante del STT.

| Longitud del pasaje | `max_length` | 8 candidatos |
|---|---:|---:|
| 250 caracteres | 512 | 194 ms ← lo que se midió en la Fase 0 |
| 1400 caracteres | 512 | **924 ms** |
| 1400 caracteres | 256 | 609 ms |
| 1400 caracteres | 128 | 303 ms |

Con reranker, el primer audio se va a **2,2-2,5 s**, que no es aceptable en una
conversación. Sin él, a 1,6-1,9 s.

**No he tomado la decisión de apagarlo, y creo que es lo correcto.** Sobre el
corpus provisional —3 protocolos sintéticos, 25 fragmentos— el retrieval híbrido
ya acierta el documento correcto casi siempre, así que el reranker no tiene
margen para demostrar su valor. Eso *no* prueba que sobre: prueba que este corpus
no sirve para decidirlo. `eval/medir_reranker.py` está listo para relanzarlo con
tus documentos reales y cerrar la decisión con datos que signifiquen algo.

En dominio clínico, recuperar el protocolo equivocado es peor que responder
despacio, así que el valor por defecto se queda en el conservador hasta tener esa
medición.

---

---

## Tres bugs encontrados atacando el sistema

Detalle completo en [docs/ROBUSTEZ.md](ROBUSTEZ.md).

**1. El olvido dejaba el archivo en disco.** El más grave, porque era el requisito
central incumplido a medias y en silencio. `STORAGE_DIR` era relativo y se
resolvía contra el directorio del proceso; la comprobación anti-travesía de
`olvidar_documento()` fallaba y el `except OSError: pass` se lo tragaba. La fila
desaparecía, el agente olvidaba de verdad, y **el PDF con datos del paciente se
quedaba en el disco del hospital**. Arreglado, y de paso desapareció una
inestabilidad de los tests que tenía la misma causa: se pisaban a través de esa
carpeta mal resuelta.

**2. El modo de voz por defecto no arrancaba.** `kokoro` no estaba en
`pyproject.toml`: la Fase 0 lo midió con `uv run --with kokoro` y nunca llegó a
ser dependencia. `crear_motor("kokoro")` lanzaba `ModuleNotFoundError`.

**3. El PDF escaneado habría llegado a `ready` con 0 fragmentos**, con la consola
diciendo «Listo — el agente ya lo sabe» habiendo aprendido nada. Era el riesgo más
probable de cara a tu corpus real. Resuelto mejor que rechazándolo: PyMuPDF de
primario y **Docling con OCR de respaldo**, que se dispara solo cuando la
extracción directa no saca texto.

---

## Lo que queda sin hacer o sin verificar

**La voz no se ha probado con un micrófono.** Está decidida y medida, pero
inyectando audio. Falta montar el transporte WebRTC real (`POST /api/voz/offer`)
y que un humano hable. `scripts/spikes/cliente_voz/index.html` está listo para
eso, en dos clics.

**El TTFT del LLM sigue sin medir** porque `GEMINI_API_KEY` quedó vacía toda la
noche. Es el último número que falta del presupuesto de latencia, y además
`app/agent/llm_client.py` está escrito contra las APIs de google-genai y groq
pero **nunca se ha ejecutado**: es el punto del proyecto con más probabilidad de
tener un error latente.

**El corpus real.** Los tres protocolos de `eval/corpus_prueba/` son sintéticos y
provisionales. Todo lo medido sobre ellos —incluida la decisión del reranker—
hay que rehacerlo con los documentos de verdad.

---

## Dos incidentes de la noche, por si se repiten

**Límite de sesión a las 2:40.** Mató los cuatro agentes de la primera ola a la
vez. No se perdió trabajo porque los ficheros estaban en disco, pero tres murieron
a un paso de terminar y el cuarto —el de voz— justo antes de medir, que era todo
su encargo.

**Disco lleno al 100%.** Durante un rato no se pudo ni ejecutar un comando. La
causa era el caché de modelos: 13 GB. Liberé 5 GB borrando `whisper-medium`,
`whisper-large-v3-turbo` y `multilingual-e5-large`, que la propia Fase 0 había
descartado por medición y que se re-descargan solos si hicieran falta. Quedan
21 GB. Los modelos en uso ocupan 7 GB y no se tocan.

También hay contención de memoria real: con dos agentes cargando modelos más una
API y un worker, los 16 GB se agotan y hasta un `psql` tarda minutos. Conviene no
correr más de dos procesos pesados a la vez.

---

## Qué decidir por la mañana

1. **La clave de Gemini.** Es lo que desbloquea más cosas: cierra el último número
   del presupuesto de latencia y permite ejecutar por primera vez
   `app/agent/llm_client.py`. Aviso concreto: al revisarlo se vio que
   `GeminiClient.stream()` hace `await ...generate_content_stream(...)` y luego
   itera, pero en `google-genai` ese método ya devuelve un iterador asíncrono, así
   que **ese `await` es sospechoso de romper**. Sin clave no se puede confirmar.

2. **El reranker**, con tu corpus real: `uv run python eval/medir_reranker.py`. Si
   no gana aciertos, apagarlo devuelve 585 ms al presupuesto de voz — la palanca
   más grande que queda.

3. **Probar la voz con micrófono.** El único paso que necesita una persona:
   ```bash
   cd backend && TTS_ENGINE_LOCAL=say uv run uvicorn app.main:app --port 8000
   cd scripts/spikes/cliente_voz && python3 -m http.server 5500
   ```
   Y en `localhost:5500`: turno normal → interrumpirle hablando encima → pausa de
   duda a media frase → decir solo «Sí.» → probar con el volumen alto, a ver si se
   autointerrumpe (si pasa, la solución son auriculares, no tocar el VAD).

4. **Cuál de las voces de ElevenLabs**: compara `scripts/spikes/out/elevenlabs_*.wav`
   con `kokoro_ef_dora_completa.wav`. Ojo, el free tier solo permite voces
   *premade*; las de biblioteca dan `402`.

5. **Montar Pipecat sobre WebRTC**: cambiar `EntradaInyectada`/`SalidaMedida` por
   el transporte real y añadir `POST /api/voz/offer`.

## Cómo arrancarlo todo

```bash
cd .claude/worktrees/nocturno
docker compose up -d                                    # Postgres en el 5433

cd backend
DATABASE_URL=postgresql://postop:postop@localhost:5433/postop_wt \
  uv run uvicorn app.main:app --port 8000               # API
DATABASE_URL=postgresql://postop:postop@localhost:5433/postop_wt \
  uv run python -m app.workers.ingest_worker            # worker, en otra terminal

cd ../frontend && npm run dev                           # consola
```

**Aviso sobre la consola:** `frontend/.env.local` trae `VITE_MOCK=1`, así que por
defecto arranca contra un simulador en memoria y no contra el backend. La pantalla
lo avisa con una insignia «Datos simulados». Para hablar con el backend de verdad,
borra esa línea.
