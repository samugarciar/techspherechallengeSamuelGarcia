# Plan aprobado — con las correcciones de la Fase 0

Este es el plan que se aprobó al arrancar, versionado en el repo para poder
contrastarlo con lo que de verdad se construyó. **La Fase 0 lo corrigió en tres
puntos**, y esas correcciones mandan sobre lo que dice más abajo:

| El plan decía | La medición dijo | Dónde vive ahora |
|---|---|---|
| STT Whisper `large-v3-turbo` | `small` + sesgo de vocabulario clínico da la misma precisión en 481 ms frente a 1257 ms. El compromiso «rápido o preciso» resultó falso | `STT_MODEL` en `.env`; el vocabulario en `app/voice/stt.py` |
| Reranker top-20 → top-4 | Top-8 separa igual de nítido que top-20 (0.993 vs 0.004), así que se recorta a 8. **El coste que dijo esta medición era falso**: 114 ms medidos con pasajes de 250 caracteres, 585 ms con los reales de 500-1400. Ver README §«El reranker costaba cinco veces lo presupuestado» | `RETRIEVE_TOP_K=8` |
| Observabilidad con Langfuse self-hosted | v3 arrastra ClickHouse + Redis + MinIO. La máquina tiene 16 GB compartidos con Whisper, bge-m3 y el reranker. Se sustituye por una tabla `traces` | `traces` en `schema.sql` |

Y dos decisiones posteriores de Samuel que el plan no contemplaba:

- **Dos modos de voz** (local Kokoro / premium ElevenLabs) conmutables en caliente
  desde la consola, con degradación automática y contabilidad de coste en
  `tts_usage`. El plan asumía voz local obligatoria.
- **Pipecat frente a WebSocket propio se decide por medición**, no por el plan.
  Ver `docs/VOZ_COMPARATIVA.md`.

Hallazgo del smoke test del 8 de agosto: ElevenLabs Flash responde en **354 ms**
de mediana, *menos* que los 461 ms que el spike de la Fase 0 atribuía a Kokoro. De
ahí salió la conclusión de que premium no costaba latencia sino que la ahorraba —
y **duró un día**: remedido contra el pipeline real, Kokoro tarda 196-303 ms, así
que el local vuelve a ser el más rápido y lo que premium compra es solo voz, a
cambio de dinero y de depender de la red. Además, el free tier **no permite voces
de biblioteca por API** (402 `paid_plan_required`): solo las *premade*.

---

# Agente de voz para seguimiento postoperatorio — Stack y ruta de trabajo

## Contexto

Repositorio greenfield (`techspherechallengeSamuelGarcia`, solo LICENSE + README) para el Tech Sphere Challenge 2026. Hay que construir cuatro piezas que forman un sistema: interfaz de voz bidireccional, RAG sobre documentos y datos vivos, web app de llamada simulada, y consola de administración cuyo contenido alimenta el RAG.

**Restricciones confirmadas:**
- LLM limitado a: Gemini Flash, Llama vía Groq (o local), Phi mini.
- Arquitectura híbrida: LLM en cloud; **voz y RAG obligatoriamente locales**.
- Hardware: solo Mac Apple Silicon (sin CUDA).
- Ventana: 1–2 semanas. Lo que se evalúa es el **demo**.

**Requisito que manda sobre el diseño:** cuando el admin sube un documento el agente debe aprenderlo, y cuando lo borra debe olvidarlo — de forma confiable y verificable. Esto no es una feature del RAG; es la propiedad que decide qué base de datos se usa.

---

## Decisión 1 — LLM: Gemini 2.5 Flash primario, Groq como escape de latencia

| Opción | A favor | En contra |
|---|---|---|
| **Gemini 2.5 Flash** | Mejor español del conjunto permitido; function calling nativo y paralelo; salida estructurada con JSON schema; contexto largo; free tier amplio; API key en 2 minutos | TTFT ~300–600 ms, más alto que Groq |
| **Llama vía Groq** | TTFT ~100–200 ms — el más rápido del mercado, y en voz el TTFT es lo que se percibe como "responde rápido" | Español algo más flojo en matiz clínico; rate limits del free tier; capacidad variable |
| **Phi mini local** | Cero dependencia de red, privacidad total | Tool calling frágil, español débil, compite por la GPU del Mac con Whisper/TTS |

**Recomendación:** Gemini 2.5 Flash. El agente de seguimiento sigue un guion con preguntas y escucha respuestas — la fiabilidad del tool calling y la calidad del español pesan más que 200 ms de TTFT. Verificar el ID exacto vigente en AI Studio antes de fijarlo.

**Mitigación obligatoria:** todo el acceso al LLM detrás de una interfaz `LLMClient` (`generate`, `generate_stream`, `call_tools`). Cambiar a Groq debe ser una variable de entorno, no una refactorización. Si en la Fase 5 la latencia estorba en la demo, se cambia el mismo día. Phi mini queda solo como demostración de "puede correr offline", no como camino principal.

## Decisión 2 — Orquestación de voz: Pipecat

El problema difícil aquí no es STT ni TTS: es el full-duplex — barge-in (que el paciente interrumpa), detección de fin de turno, resampleo, jitter, transcripciones parciales. Escribir eso a mano cuesta días.

| Opción | A favor | En contra |
|---|---|---|
| **Pipecat** + `SmallWebRTCTransport` | WebRTC P2P sin SFU ni cloud; barge-in e interrupciones resueltos; Python (mismo lenguaje que el RAG); cliente JS oficial; un solo proceso | Framework joven, API en movimiento |
| **LiveKit Agents** | Más maduro; camino documentado a telefonía real (SIP) — buena narrativa de pitch | Exige servidor LiveKit en Docker + emisión de tokens; más infra para 10 días |
| **FastAPI + WebSocket a mano** | Control total, cero lock-in | Reconstruyes barge-in y VAD gating desde cero — riesgo alto en esta ventana |

**Recomendación:** Pipecat con `SmallWebRTCTransport`. Es el camino más corto a una llamada en vivo que funcione en el navegador contra un Mac. Mencionar LiveKit en la presentación como el paso siguiente hacia telefonía real (da profundidad sin costar tiempo).

**VAD / detección de turno:** Silero VAD (ONNX, CPU, ya integrado en Pipecat como `SileroVADAnalyzer`).

## Decisión 3 — STT local: Whisper large-v3-turbo sobre Metal

| Opción | A favor | En contra |
|---|---|---|
| **whisper.cpp / mlx-whisper** (`large-v3-turbo`) | Acelerado por Metal en Apple Silicon; mejor precisión multilingüe disponible; español excelente | Sin streaming nativo real — se trocea por VAD |
| **faster-whisper** (CTranslate2) | Muy usado, API cómoda | CTranslate2 no aprovecha Metal; en Mac cae a CPU |
| **Vosk** | Streaming real, ligerísimo | Precisión en español notablemente inferior — inaceptable para términos clínicos |

**Recomendación:** `large-v3-turbo` vía mlx-whisper o whisper.cpp. Pipecat trae un servicio Whisper local; **verificar en el día 1 si la versión instalada expone binding MLX** — si no, envolver `mlx-whisper` en un `STTService` propio son ~40 líneas.

## Decisión 4 — TTS local en español: Kokoro, con A/B obligatorio el día 1

| Opción | A favor | En contra |
|---|---|---|
| **Kokoro-82M** | Apache-2.0; 82M params — corre rápido en M-series; calidad muy superior a Piper; expone endpoint compatible OpenAI vía Kokoro-FastAPI | Solo 3 voces en español (`ef_dora`, `em_alex`, `em_santa`) y menor calidad que su inglés |
| **Piper** | Latencia mínima, ONNX puro CPU, varias voces `es_ES`/`es_MX`, rock-solid | Suena robótico — se nota en demo |
| **XTTS-v2** | Clonación de voz, español muy bueno | Licencia CPML (no comercial) y lento en Mac — descartado |
| **macOS AVSpeechSynthesizer** (Mónica/Paulina) | Cero setup, cero deps, local por definición | Voz de sistema, poco diferenciador |

**Recomendación:** Kokoro como primario, detrás de un adaptador `TTSAdapter` con Piper como fallback.

**Este es el punto de mayor incertidumbre del plan.** La calidad del español de Kokoro es lo único que puede decepcionar visiblemente en la demo. Acción concreta en el día 1: generar la misma frase clínica en Kokoro-es, Piper-es y `say -v Mónica`, escucharlas, y elegir por oído. 30 minutos que evitan descubrir el problema el día 9.

**Optimización de latencia:** trocear el texto del LLM por frase (puntuación) y sintetizar por frase, no esperar la respuesta completa. Es la diferencia entre 1.5 s y 400 ms hasta el primer audio.

## Decisión 5 — RAG: separar conocimiento de datos vivos

**Esta es la decisión más importante del proyecto.** "Documentos y datos vivos" no son la misma cosa y no deben ir al mismo sitio:

- **(a) Conocimiento no estructurado** — protocolos de alta, guías de cuidado de herida, instrucciones de medicación → **RAG vectorial**. Cambia poco, se consulta semánticamente.
- **(b) Datos vivos estructurados** — qué paciente es, qué cirugía tuvo, en qué fecha, qué medicación tiene activa, cuándo es su cita → **function calling contra la base de datos**, nunca RAG.

Meter datos vivos en un índice vectorial es la causa número uno de respuestas desactualizadas en sistemas como este: el vector es una foto del momento de la ingesta. Si el agente necesita saber la fecha de la cirugía, llama a `obtener_paciente(id)` y lee el valor actual. Si necesita saber qué hacer ante enrojecimiento de la herida, consulta el RAG.

### Base de datos vectorial: Postgres 16 + pgvector

| Opción | A favor | En contra |
|---|---|---|
| **Postgres + pgvector** | Un solo datastore para metadata, chunks, vectores y estado de jobs; **borrado transaccional**; HNSW; `tsvector` español para híbrido sin otro servicio; trivial en Docker en Mac | Menos rápido que un vector store dedicado a gran escala — irrelevante con decenas de documentos |
| **Qdrant** | Filtrado excelente, borrado por filtro, sparse vectors nativos | Sigues necesitando Postgres para el admin → **dos sistemas que se pueden desincronizar**, que es exactamente el riesgo que el requisito de "olvidar" no tolera |
| **Chroma** | El más fácil para prototipar | Semántica de concurrencia y borrado más débil |
| **Milvus / Weaviate** | Escala | Sobredimensionado; más ops de las que hay tiempo |

**Recomendación: Postgres + pgvector.** El argumento no es rendimiento, es que convierte "borrar = olvidar" de un problema de sistemas distribuidos en una propiedad ACID de una sola transacción.

### Embeddings y búsqueda

- **Embeddings: `BAAI/bge-m3`** (local, MPS). Multilingüe fuerte, contexto 8192, bueno en español técnico. Alternativa si la latencia aprieta: `intfloat/multilingual-e5-large`.
- **Búsqueda híbrida: denso (pgvector, coseno, HNSW) + léxico (Postgres FTS con diccionario `spanish`), fusionados con RRF.** El híbrido es donde se gana confiabilidad en dominio clínico: "cefalexina 500 mg" y "dehiscencia" son términos que el léxico acierta y el denso puede diluir.
- **Reranker: `BAAI/bge-reranker-v2-m3`** — cross-encoder multilingüe, top-20 → top-4. Es la palanca individual más grande sobre precisión. Coste: ~200–400 ms en Mac. **Debe ser toggleable por env var** y medirse dentro del presupuesto de latencia de voz. Si no cabe, `jina-reranker-v2-base-multilingual` es la mitad de tamaño.
- **Parsing: Docling** (MIT) para PDF/DOCX → Markdown estructurado con tablas y jerarquía de encabezados. Fallback ligero: PyMuPDF.
- **Chunking por estructura** (secciones/encabezados, ~500–800 tokens, solape), guardando `document_id`, `page` y `heading` en cada chunk. Necesario para que el agente pueda citar: *"según el protocolo de alta, sección cuidado de herida…"*.

## Decisión 6 — Aprender y olvidar: el diseño que hay que demostrar

Requisito literal del enunciado, y el momento más vendible de la demo. Diseño explícito:

**Máquina de estados del documento**
```
uploaded → parsing → chunking → embedding → ready
     ↘ failed                                  ↓
                                    superseded → (borrado)
```

**Cinco garantías:**

1. **Aprender es atómico.** Los chunks no son visibles hasta que el documento pasa a `ready`. La transición inserta todos los chunks con sus vectores y actualiza el estado **en una sola transacción**. El agente nunca puede leer un documento a medio procesar.
2. **Olvidar es transaccional.** `DELETE FROM documents WHERE id = ?` con FK `ON DELETE CASCADE` sobre `chunks`. El vector vive en la misma fila que el chunk: desaparecen juntos o no desaparece ninguno.
3. **Segundo cinturón en el retrieval.** *Toda* consulta hace `JOIN documents ON ... WHERE d.status = 'ready'`. Aunque quedara un chunk huérfano por un bug, es irrecuperable por construcción. Defensa en profundidad, no confianza en el borrado.
4. **Sin caché entre turnos.** El retrieval se ejecuta fresco en cada turno. El historial de conversación se re-inyecta como *diálogo*, nunca como *conocimiento* — así un documento borrado a mitad de llamada deja de influir de inmediato.
5. **Re-subida = versionado.** Subir de nuevo el mismo documento crea un `document_id` nuevo; el anterior pasa a `superseded` (deja de ser retrievable al instante) y luego se borra. Evita la ventana en que ambas versiones coexisten.

**Auditoría:** tabla `document_events` (evento, actor, timestamp). En contexto médico es lo que convierte un prototipo en algo creíble.

**Verificabilidad en pantalla:** la consola muestra `chunks_count` y `embedded_count` por documento. El guion de demo es: subir → ver los estados avanzar → preguntarle al agente por voz → borrar → ver los contadores a 0 → repetir la misma pregunta → *"no tengo esa información, lo escalo con tu equipo"*.

## Decisión 7 — Cola de ingesta

**Recomendación: cola en Postgres** con `SELECT ... FOR UPDATE SKIP LOCKED` + worker asyncio. Cero servicios extra (Postgres ya está), y el estado del job vive en la misma transacción que el documento — que es justo lo que la consola necesita leer. Redis + arq solo si más adelante escala.

**Estado en vivo en la consola:** SSE desde FastAPI (`GET /api/documents/stream`). Trivial de implementar y se ve muy bien en demo.

## Decisión 8 — Backend, frontend, seguridad clínica

- **Backend: FastAPI** — mismo lenguaje que Pipecat y el pipeline RAG. Dos procesos: API + bot de voz.
- **Frontend: React + Vite + TypeScript + Tailwind + shadcn/ui.** Dos vistas: `/call` y `/admin`.
- **Cliente de voz:** SDK JS de Pipecat (`@pipecat-ai/client-js` + transporte SmallWebRTC).
- **Auth:** un admin, token en variable de entorno. No sobre-ingenierizar con 10 días.
- **Archivos:** disco local `./storage/documents/` + hash SHA-256 para deduplicar.
- **Observabilidad: Langfuse self-hosted** (Docker, open source) para trazas de LLM + RAG. Encaja con la restricción "local".

**Capa de seguridad clínica** — no es opcional, es lo que diferencia el proyecto:
- Detección determinista de banderas rojas por keywords sobre el transcript parcial (fiebre >38.5 °C, sangrado activo, dehiscencia, dolor torácico, disnea). Determinista y barato: **no depender del LLM para esto**.
- Tool `escalar_a_equipo_clinico(motivo, urgencia)` que corta el guion y marca el caso.
- Reglas duras en el system prompt: nunca diagnosticar, nunca ajustar dosis, solo leer protocolo o escalar.
- **Grounding obligatorio:** si el retrieval no trae evidencia suficiente, la respuesta es *"no tengo esa información"* + escalar. Prohibido improvisar.
- Cada llamada se guarda con transcripción y las citas usadas en cada respuesta.

---

## Stack final

| Capa | Elección |
|---|---|
| LLM | Gemini 2.5 Flash (primario) / Groq Llama (fallback latencia), tras `LLMClient` |
| Orquestación de voz | Pipecat + `SmallWebRTCTransport` |
| VAD / turnos | Silero VAD |
| STT | Whisper `large-v3-turbo` local (mlx-whisper / whisper.cpp) |
| TTS | Kokoro-82M local, `TTSAdapter` con Piper de fallback |
| Vector DB | Postgres 16 + pgvector (HNSW) |
| Embeddings | `BAAI/bge-m3` local sobre MPS |
| Reranker | `bge-reranker-v2-m3` (toggleable por env) |
| Búsqueda | Híbrida: denso + FTS español + RRF |
| Parsing | Docling (fallback PyMuPDF) |
| Cola | Postgres `SKIP LOCKED` + worker asyncio |
| Backend | FastAPI |
| Frontend | React + Vite + Tailwind + shadcn/ui |
| Observabilidad | Langfuse self-hosted |

## Estructura de repositorio

```
backend/
  app/
    api/          routers FastAPI: documents, calls, health, sse
    rag/          ingest.py, chunking.py, embeddings.py, retrieval.py, rerank.py
    voice/        pipeline.py (Pipecat), stt.py, tts.py, vad.py
    agent/        prompts.py, tools.py, redflags.py, llm_client.py
    db/           models.py, migrations/, queue.py
    core/         config.py, logging.py, telemetry.py
  tests/
frontend/
  src/routes/     call/, admin/
  src/components/
storage/documents/
docker-compose.yml   (postgres+pgvector, langfuse)
eval/               golden_set.jsonl, run_evals.py
```

---

## Ruta de trabajo (10–12 días)

El orden no es por capas sino **por riesgo**: primero lo que puede fallar y arruinar la demo, último lo cosmético.

### Fase 0 — Andamiaje y verificación de riesgos (día 1)
- `docker-compose` con Postgres 16 + pgvector y Langfuse. Esqueleto FastAPI + React.
- Schema SQL: `documents`, `chunks` (con `vector` y `tsvector`), `document_events`, `patients`, `surgeries`, `calls`, `jobs`. FK `ON DELETE CASCADE` de `chunks` a `documents` desde el minuto uno.
- **Los tres spikes de riesgo, hoy, no después:**
  1. A/B de voz: Kokoro-es vs Piper-es vs `say -v Mónica` con la misma frase clínica. Elegir por oído.
  2. Whisper `large-v3-turbo` en el Mac: medir latencia de transcripción de un clip de 5 s. Confirmar si Pipecat expone binding MLX o hay que envolverlo.
  3. `bge-m3` + `bge-reranker-v2-m3` sobre MPS: medir ms por query. Decide si el reranker entra en el camino de voz.
- **`README.md`: sección "Stack y decisiones"** (ver abajo) escrita ya en este día, con las elecciones y su razón en una línea cada una. Se actualiza al cierre de cada fase si algo cambia.
- **Entregable:** presupuesto de latencia por etapa escrito en el README con números reales medidos, y las decisiones de stack documentadas.

**Contenido de la sección "Stack y decisiones" del README** — breve, una tabla `Capa | Elección | Por qué` reutilizando la tabla de Stack final de este plan, más tres párrafos cortos para las decisiones que necesitan justificación:
- Por qué conocimiento y datos vivos van por caminos distintos (RAG vs function calling).
- Por qué Postgres + pgvector y no un vector store dedicado (borrado transaccional).
- Por qué Pipecat y no un WebSocket propio (barge-in y detección de turno).

Más una nota de las alternativas descartadas con el motivo en media línea (Qdrant, XTTS-v2, Vosk, LiveKit), porque el jurado va a preguntar exactamente eso.

### Fase 1 — Pipeline RAG sin voz (días 2–3)
- Ingesta con Docling → chunking por encabezados → embeddings `bge-m3` → inserción transaccional.
- Retrieval híbrido: pgvector + FTS español + RRF + reranker opcional.
- CLI de prueba: `python -m app.rag.query "¿cuándo puedo ducharme tras la cirugía?"` → devuelve chunks con cita y score.
- **Aquí se prueba la garantía de olvido**, antes de que haya voz: test que sube un doc, consulta, borra, y verifica que el mismo query devuelve cero resultados.
- **Entregable:** el corazón del sistema funcionando, testeable sin micrófono.

### Fase 2 — Consola de administración (días 3–4)
- Endpoints: upload (multipart), list, delete, get status. Worker de cola sobre `SKIP LOCKED`.
- SSE de estados; UI con tabla de documentos: nombre, estado, `chunks_count`, `embedded_count`, fecha, acciones.
- Estados visibles avanzando en tiempo real. Auditoría en `document_events`.
- **Entregable:** el momento demo "subo → aprende / borro → olvida" ya es demostrable end-to-end, sin voz.

### Fase 3 — Loop de voz (días 5–6)
- Pipeline Pipecat: transporte SmallWebRTC → Silero VAD → Whisper → `LLMClient` → Kokoro → salida.
- Primero eco simple (hablo, me transcribe, me responde una constante) para validar el transporte. Después enchufar el agente real.
- Barge-in funcionando: si el usuario habla, el TTS se corta.
- Streaming por frase en el TTS.
- **Entregable:** conversación en vivo por navegador contra el Mac.

### Fase 4 — Agente de seguimiento postoperatorio (días 7–8)
- System prompt + guion de llamada (identificación, dolor, herida, fiebre, medicación, movilidad, dudas).
- Tools: `obtener_paciente`, `obtener_cirugia`, `buscar_protocolo` (RAG), `registrar_respuesta`, `escalar_a_equipo_clinico`.
- Detector determinista de banderas rojas sobre transcript parcial.
- Grounding obligatorio y respuesta de fallback cuando no hay evidencia.
- Datos semilla: 3 pacientes sintéticos con cirugías y medicación distintas.
- **Entregable:** llamada completa de seguimiento con escalamiento funcionando.

### Fase 5 — Web app de llamada (días 9–10)
- UI `/call`: botón de llamar, indicador de estado (escuchando / pensando / hablando), transcripción en vivo de ambos lados.
- **Panel de latencias en pantalla** (STT ms, retrieval ms, LLM TTFT, TTS primer chunk). Es barato y demuestra rigor de ingeniería ante el jurado.
- Historial de llamadas con transcripción y citas usadas por respuesta.
- **Entregable:** demo presentable.

### Fase 6 — Evals, tuning y guion de demo (días 11–12)
- Golden set: ~30 preguntas en español con documento fuente esperado. Medir recall@k y groundedness.
- Tuning de latencia contra el presupuesto de la Fase 0. Si no cierra: reranker off, Whisper `medium`, o LLM a Groq.
- Guion de demo escrito y ensayado, con el momento aprender/olvidar como clímax.
- Cierre del README: diagrama de arquitectura, instrucciones de arranque, y revisión de la sección "Stack y decisiones" contra lo que realmente se construyó (si el A/B de voz eligió Piper, o el reranker quedó apagado por latencia, el README lo refleja con el número que lo motivó).

---

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| **Calidad del TTS en español decepciona** — el mayor riesgo visible | A/B en el día 1, no en el día 9. `TTSAdapter` hace el cambio trivial |
| **Contención de recursos en el Mac**: Whisper + Kokoro + embeddings + reranker compitiendo | Embeddings solo en ingesta (fuera del camino de voz); reranker toggleable; medir en Fase 0 |
| **Latencia total >1.5 s y la conversación se siente muerta** | Presupuesto medido en día 1; palancas ordenadas: TTS por frase → reranker off → Groq → Whisper medium |
| **Pipecat con API en movimiento** | Fijar versiones en `requirements.txt` el día 1; eco simple en Fase 3 antes de integrar nada |
| **Se va el tiempo en el RAG y la voz queda a medias** | Fases 1–2 tienen fecha dura. Si el día 4 el RAG no está cerrado, se recorta el reranker y se pasa a voz |
| **El jurado pregunta por telefonía real** | Respuesta preparada: LiveKit Agents + SIP es el siguiente paso; la arquitectura de agente no cambia, solo el transporte |

## Verificación end-to-end

1. `docker compose up` → Postgres y Langfuse arriba.
2. `pytest backend/tests` → incluye el test de aprender/olvidar (subir → consultar → borrar → consultar → cero resultados).
3. `python eval/run_evals.py` → recall@k y groundedness sobre el golden set.
4. Abrir `/admin`, subir un PDF de protocolo postoperatorio, ver los estados avanzar hasta `ready` con `chunks_count > 0`.
5. Abrir `/call`, iniciar llamada, preguntar algo cubierto por ese PDF → el agente responde citando el documento.
6. Volver a `/admin`, borrar el documento → `chunks_count` a 0.
7. En la misma llamada, repetir la pregunta → el agente responde que no tiene esa información y ofrece escalar.
8. Decir una frase con bandera roja ("tengo fiebre de 39 y la herida está sangrando") → el agente corta el guion, da instrucción de urgencia y registra el escalamiento.
9. Revisar el panel de latencias y las trazas en Langfuse.
