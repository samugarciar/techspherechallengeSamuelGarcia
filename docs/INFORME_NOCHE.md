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

**108 tests en verde** y `ruff` limpio. La consola de React compila sin errores
de TypeScript.

| Fase | Estado |
|---|---|
| 1 — pipeline RAG sin voz | Cerrada: parsing, cola, worker, retrieval híbrido |
| 2 — consola de administración | Cerrada: API con SSE + panel de documentos |
| 3 — loop de voz | Las dos opciones construidas; **falta elegir por medición** |

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

## Lo que queda sin hacer o sin verificar

**La comparativa de voz.** Es el hueco grande. Están construidas las dos
opciones —Pipecat 275 líneas, WebSocket propio 488, VAD 300— pero medirlas se
cortó dos veces. Sin la comparación, tener ambas no sirve todavía. El entregable
`docs/VOZ_COMPARATIVA.md` es lo primero que hay que cerrar.

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

1. **El reranker**, con tu corpus real: `uv run python eval/medir_reranker.py`.
   Si no gana aciertos, apagarlo devuelve 585 ms al presupuesto de voz.
2. **Pipecat o WebSocket propio**, cuando esté `docs/VOZ_COMPARATIVA.md`.
3. **Cuál de las voces de ElevenLabs**: compara `scripts/spikes/out/elevenlabs_*.wav`
   con `kokoro_ef_dora_completa.wav`. Ojo, el free tier solo permite voces
   *premade*; las de biblioteca dan `402`.
4. **La clave de Gemini**, para cerrar el TTFT y probar el tool calling de verdad.

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
