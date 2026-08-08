# Agente de voz para seguimiento postoperatorio

Tech Sphere Challenge 2026 — Samuel García

Sistema que llama a pacientes operados para hacerles el seguimiento clínico: los
escucha, consulta protocolos y datos vivos del paciente, y escala al equipo
médico cuando detecta signos de alarma. La voz y el RAG corren **en local**;
solo el texto sale a un LLM en la nube.

Una consola de administración alimenta la base de conocimiento: **lo que se sube
el agente lo aprende, lo que se borra lo olvida** — de forma verificable.

---

## Stack y decisiones

| Capa | Elección | Por qué |
|---|---|---|
| LLM | Gemini 2.5 Flash · Groq Llama de repuesto | Mejor español clínico y tool calling fiable del conjunto permitido; Groq detrás de la misma interfaz si el TTFT estorba |
| Orquestación de voz | Pipecat + `SmallWebRTCTransport` | Resuelve barge-in y fin de turno, que es el trabajo difícil; WebRTC P2P sin SFU ni nube |
| VAD / turnos | Silero VAD | ONNX en CPU, ya integrado en Pipecat |
| STT | Whisper `small` (MLX) + sesgo de vocabulario | 481 ms con la misma precisión clínica que `medium` a 1222 ms — ver abajo |
| TTS | **Dos modos**: local (Kokoro) y premium (ElevenLabs), conmutables desde la consola | Flexibilidad de coste sin recompilar: gratis e ilimitado para operar, voz premium cuando la experiencia lo justifique |
| Vector DB | Postgres 16 + pgvector (HNSW) | Convierte «borrar = olvidar» en una propiedad ACID, no en disciplina del programador |
| Embeddings | `BAAI/bge-m3` sobre MPS | 1024 dims, multilingüe fuerte; 24 ms por consulta |
| Reranker | `bge-reranker-v2-m3`, top-8 | Discrimina nítido (0.993 vs 0.004), pero cuesta 585 ms: decisión abierta, ver abajo |
| Búsqueda | Híbrida: denso + FTS español + RRF | El léxico acierta «cefalexina 500 mg»; el denso acierta «¿me puedo bañar?» |
| Parsing | Docling (PyMuPDF de respaldo) | Conserva la jerarquía de secciones, de la que depende todo el troceado |
| Cola de ingesta | Postgres `FOR UPDATE SKIP LOCKED` | El estado del job vive en la misma transacción que el documento |
| Backend | FastAPI | Mismo lenguaje que Pipecat y el pipeline RAG |
| Frontend | React + Vite + Tailwind + shadcn/ui | Dos vistas: `/call` y `/admin` |
| Trazas | Tabla `traces` en Postgres | Ver «Langfuse» abajo |

### Las tres decisiones que necesitan justificación

**1. El conocimiento y los datos vivos van por caminos distintos.**
Los protocolos y guías de alta van al RAG vectorial. Pero qué paciente es, qué
cirugía tuvo, qué medicación tiene activa y cuándo es su cita se leen con
*function calling* contra la base de datos, nunca por búsqueda vectorial. Un
embedding es una foto del momento de la ingesta: meter ahí datos que cambian es
la causa número uno de respuestas desactualizadas en sistemas como éste.

**2. Postgres + pgvector, no un vector store dedicado.**
El argumento no es rendimiento — con decenas de documentos Qdrant sería más
rápido y daría igual. Es que el requisito de olvidar es un requisito de
*consistencia*: con dos sistemas (vector store + Postgres para el admin) hay que
mantenerlos sincronizados y existe una ventana en la que un documento borrado
sigue siendo recuperable. Con un solo datastore, `DELETE FROM documents` arrastra
los vectores por `ON DELETE CASCADE` en la misma transacción. No hay ventana.

**3. Pipecat, no un WebSocket propio — pero no por el motivo que parecía.**
La primera versión de este apartado decía que lo difícil era el barge-in y que
Pipecat lo ahorraba. **Se construyeron las dos opciones y se midieron, y ese
argumento resultó falso**: el barge-in funciona igual en ambas (84 ms contra
96 ms), y el número lo domina el umbral de confirmación del VAD, no el framework.
Escribirlo a mano costó 312 líneas frente a 171.

Lo que Pipecat sí aporta, y decide: **solapa el STT con la espera de fin de
turno** en vez de encadenarlos. Eso son 1.596 ms hasta el primer audio frente a
1.975 ms — 379 ms, un 19 %. El truco se puede copiar en unas 15 líneas; el valor
de Pipecat es traerlo puesto junto con el reloj de reproducción y el vaciado de
buffers. Detalle completo en [docs/VOZ_COMPARATIVA.md](docs/VOZ_COMPARATIVA.md).

La opción propia no se tira: es el plan B de un solo fichero, el arnés con el que
se midió todo esto, y el banco de pruebas con micrófono.

### Alternativas descartadas

| Descartado | Motivo |
|---|---|
| **Qdrant** | Obliga a mantener Postgres en paralelo para el admin → dos sistemas que desincronizar, justo el riesgo que el requisito de olvidar no tolera |
| **Chroma** | Semántica de concurrencia y borrado más débil de la que este caso necesita |
| **XTTS-v2** | Licencia CPML (no comercial) y lento en Apple Silicon |
| **Vosk** | Streaming real, pero precisión en español muy inferior — inaceptable con terminología clínica |
| **LiveKit Agents** | Más maduro y con camino a telefonía real (SIP), pero exige servidor propio y emisión de tokens: demasiada infra para la ventana. Es el siguiente paso natural |
| **Langfuse self-hosted** | v3 arrastra ClickHouse + Redis + MinIO. Esta máquina tiene 16 GB compartidos con Whisper, bge-m3 y el reranker. Las trazas van a una tabla de Postgres: ~0 RAM y se muestran en la propia consola |
| **Phi mini local** | Sirve para demostrar operación sin red, pero tool calling frágil y compite por la GPU con Whisper y el TTS |

---

## Presupuesto de latencia

Medido en **MacBook Air M4, 16 GB**, mediana de 3-5 ejecuciones tras calentar.
Todo lo local se midió de verdad; el LLM es una estimación hasta tener API key.

| Etapa | Medido | Nota |
|---|---:|---|
| **Fin de turno — Silero VAD** | **626 ms** | la etapa que este presupuesto no contaba |
| STT — Whisper `small` + prompt clínico | **391 ms** | con Pipecat se solapa con la etapa anterior |
| Embedding de la consulta — bge-m3 | **25 ms** | despreciable |
| Retrieval híbrido — Postgres | **3 ms** | pgvector + FTS + RRF sobre 25 fragmentos |
| Reranker — bge-reranker-v2-m3, top-8 | **585 ms** | ver abajo: el mayor bloque del pipeline |
| LLM TTFT — Gemini 2.5 Flash | *~400 ms* | **el único número sin medir**; falta API key |
| TTS 1ª frase — Kokoro `ef_dora` | **196-303 ms** | mejor de lo que se creyó (461 ms) |
| TTS 1ª frase — ElevenLabs Flash | **354 ms** | más rápido que el local, con red |
| **Hasta el primer audio, con reranker** | **≈ 2.1 s** | no aceptable para conversar |
| **Hasta el primer audio, sin reranker** | **≈ 1.5 s** | |

Medido en caliente contra la API y el pipeline reales, no en bancos de pruebas
aparte. Dos correcciones que este presupuesto necesitó, y las dos son
instructivas:

**Faltaba una etapa entera.** El fin de turno —el tiempo que el sistema espera
para estar seguro de que el paciente terminó de hablar— cuesta 626 ms y no
aparecía en ninguna versión anterior de esta tabla. Es la segunda etapa más cara
del camino, por delante del STT, y no la había contado nadie porque no es un
modelo que se ejecute: es una espera deliberada.

El umbral está en **640 ms**, y se eligió aceptando un corte falso sobre una pausa
deliberada de 700 ms en vez de subir a 800 ms, que no fallaba ninguna. El motivo:
con el barge-in funcionando, un corte falso se recupera en 97 ms, mientras que
160 ms extra se pagan en los ~15 turnos de la llamada.

**El TTS local era mejor de lo medido.** Kokoro tarda 196-303 ms a la primera
frase, no 461 ms. Y de paso apareció que `kokoro` no estaba en `pyproject.toml`:
la Fase 0 lo midió con `uv run --with kokoro` y nunca llegó a ser dependencia del
proyecto, así que el modo de voz **por defecto** llevaba todo el tiempo sin
arrancar. Corregido.

### El reranker costaba cinco veces lo presupuestado

La primera versión de esta tabla decía 114 ms. **Era un error de medición mío**, y
conviene dejarlo escrito porque explica cómo se toma bien esta clase de decisión.

El spike de la Fase 0 midió el cross-encoder con pasajes de 250 caracteres. Los
fragmentos reales tienen entre 500 y 1400, y el coste de un cross-encoder escala
con la longitud de la secuencia, no con el número de pasajes. Medido de verdad:

| Longitud del pasaje | `max_length` | 8 candidatos |
|---|---:|---:|
| 250 caracteres | 512 | 194 ms ← lo que se midió en la Fase 0 |
| 1400 caracteres | 512 | **924 ms** |
| 1400 caracteres | 256 | 609 ms |
| 1400 caracteres | 192 | 452 ms |
| 1400 caracteres | 128 | 303 ms |

La lección: un spike de latencia solo vale si sus entradas se parecen a las
reales. Medir con datos de juguete da números de juguete.

**La decisión queda abierta a propósito.** Sobre el corpus provisional —3
protocolos sintéticos, 25 fragmentos— el retrieval híbrido ya acierta el
documento correcto casi siempre, así que el reranker no tiene margen para
demostrar nada: eso no prueba que sobre, prueba que *este corpus no sirve para
decidirlo*. `eval/medir_reranker.py` está escrito para volver a lanzarlo con los
documentos reales y cerrar la decisión con datos que signifiquen algo.

Mientras tanto el valor por defecto es el conservador, porque en dominio clínico
recuperar el protocolo equivocado es peor que responder despacio.

Palancas si hay que bajar la latencia, en orden de coste:

1. Apagar el reranker (`RERANK_ENABLED=false`) → **−585 ms**, la palanca grande
2. Rerankear menos candidatos (`RETRIEVE_TOP_K=5`) → ≈ −40 % del coste del reranker
3. Cambiar a Groq (`LLM_PROVIDER=groq`) → −200-400 ms estimados
4. Bajar a `CONTEXT_TOP_K=2` → menos tokens de entrada, LLM más rápido

### Cómo se eligió el modelo de STT

`small` era 2.5× más rápido que `medium` pero transcribía *«apendicectomía»*
como *«appendicitomía»* — justo la clase de error que hace inútil un agente
clínico. En vez de aceptar el intercambio, se probó sesgar el decodificador con
un vocabulario clínico (`initial_prompt`):

| Configuración | Latencia | «apendicectomía» |
|---|---:|---|
| `small` sin prompt | 436 ms | ✗ «appendicitomía» |
| **`small` + prompt clínico** | **481 ms** | **✓** |
| `medium` sin prompt | 1222 ms | ✓ |
| `large-v3-turbo` | 1257 ms | ✓ |

45 ms compran la precisión de `medium` a 2.5× su velocidad. El vocabulario vive
en `backend/app/voice/stt.py` (`VOCABULARIO_CLINICO`) y se amplía cuando
aparezcan errores nuevos en transcripciones reales.

### Comparativa de TTS en español

| Motor | 1ª frase | Frase completa |
|---|---:|---:|
| **Kokoro** `ef_dora` | **461 ms** | 987 ms |
| macOS `say` Mónica | 512 ms | 530 ms |
| Piper `es_ES-davefx` | 594 ms | 725 ms |

Los tres caben en el presupuesto, así que la latencia no decide entre ellos.
Ninguno alcanza la naturalidad de un motor de nube, y de ahí salen los dos modos.

```bash
for f in scripts/spikes/out/*completa*.wav; do echo "$f"; afplay "$f"; done
```

### Dos modos de voz

| Modo | Motor | Coste | Red | Uso |
|---|---|---|---|---|
| `local` | Kokoro-82M | gratis, ilimitado | no necesita | desarrollo y operación por defecto |
| `premium` | ElevenLabs Flash | por carácter | obligatoria | pruebas finales y demo |

El modo activo **no vive en `.env`**: vive en la tabla `app_settings` y se
conmuta desde la consola de administración. Eso permite cambiarlo en caliente,
incluso a mitad de una llamada, y hace del coste una palanca operativa en vez de
una decisión de despliegue — un hospital puede operar en local por cumplimiento
o presupuesto y subir a premium cuando lo justifique.

Tres consecuencias de diseño:

- **Degradación automática.** Si el motor premium falla (red caída, cuota
  agotada, API con problemas), `VoiceRouter` cae a local en vez de dejar al
  paciente en silencio. En una demo en vivo eso convierte un fallo en un detalle
  que nadie nota.
- **Contabilidad.** Cada síntesis se anota en `tts_usage` con modo, caracteres,
  latencia y segundos de audio. Sin esto el toggle sería un interruptor a
  ciegas; con esto la consola muestra qué está costando premium y qué latencia
  se obtiene a cambio.
- **El desarrollo no toca el free tier.** Son ~10.000 caracteres al mes; una
  tarde iterando sobre el guion se los come. Por eso el modo por defecto es
  local y premium se reserva para las pruebas finales.

Añadir un motor es implementar `TTSEngine` en `backend/app/voice/tts.py`;
Cartesia ya está incluido para compararlo con ElevenLabs en la ronda final.

---

## Aprender y olvidar

Es el requisito central del enunciado, y está implementado **en el schema**, no
en la lógica de aplicación. Dos mecanismos independientes:

**1. `ON DELETE CASCADE`.** El vector vive en la misma fila que el fragmento de
texto. Borrar el documento borra sus vectores en la misma transacción.

**2. La vista `retrievable_chunks`.** Es el único punto de lectura del RAG y
filtra por `status = 'ready'`. Aunque un bug dejara fragmentos huérfanos, son
irrecuperables por construcción — el código de retrieval nunca consulta la tabla
`chunks` directamente.

Verificado con datos sintéticos:

| Situación | En tabla | Recuperables |
|---|---:|---:|
| Documento `ready` | 3 | **3** |
| Marcado `superseded` | 3 | **0** |
| `DELETE` del documento | **0** | **0** |

El caso intermedio es el importante: un documento deja de ser recuperable *al
instante* sin necesidad de borrarlo, así que el olvido es inmediato y el borrado
físico puede ocurrir después sin abrir ninguna ventana de riesgo.

Además, la promoción a `ready` inserta todos los fragmentos y cambia el estado en
**una sola transacción**: el agente pasa de no conocer un documento a conocerlo
entero, sin estados intermedios observables. Nunca responde con medio protocolo.

---

## Arranque

```bash
# 1. Base de datos (crea el schema en el primer arranque)
docker compose up -d

# 2. Backend
cd backend && uv sync
cp ../.env.example ../.env      # y rellenar GEMINI_API_KEY

# 3. Datos sintéticos de pacientes
docker exec -i postop_db psql -U postop -d postop < backend/app/db/seed.sql

# 4. Tests
uv run pytest
```

Requisitos del sistema: `ffmpeg` (Whisper) y `espeak-ng` (Kokoro en español),
ambos por Homebrew. Postgres escucha en el **5433** para no chocar con una
instalación local en el 5432.

---

## Estructura

```
backend/app/
  api/        routers FastAPI: documentos, llamadas, SSE de estado
  rag/        ingest · chunking · embeddings · retrieval · rerank
  voice/      pipeline Pipecat · stt · tts · vad
  agent/      prompts · tools · banderas rojas · llm_client
  db/         schema.sql · pool · cola de jobs
  core/       config
frontend/     React: /call y /admin
scripts/spikes/  mediciones de la Fase 0 (reproducibles)
eval/         golden set y evaluación del RAG
```

## Estado

- [x] **Fase 0** — infraestructura, schema verificado, presupuesto de latencia medido
- [x] **Fase 1** — pipeline RAG completo sin voz: parsing, cola, worker, retrieval híbrido
- [x] **Fase 2** — consola de administración: API con SSE + panel de documentos en React
- [ ] **Fase 3** — loop de voz *(las dos opciones construidas; falta elegir por medición)*
- [ ] **Fase 4** — agente de seguimiento y banderas rojas *(bloqueado: falta API key)*
- [ ] **Fase 5** — web app de llamada
- [ ] **Fase 6** — evals, tuning y guion de demo

La garantía central se demuestra sin micrófono en un solo comando:

```bash
uv run python scripts/demo_aprender_olvidar.py
```

Sube un protocolo, espera a que el agente lo aprenda, le pregunta algo que solo
ese documento responde, lo borra y vuelve a preguntar. Medido: **2,6 s** de la
subida a `ready`, y la segunda consulta devuelve cero fragmentos.
