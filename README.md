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
| Reranker | `bge-reranker-v2-m3`, top-8 | 114 ms y discrimina nítido (0.993 vs 0.004); apagable por env |
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

**3. Pipecat, no un WebSocket propio.**
Lo difícil de la voz en tiempo real no es STT ni TTS: es el barge-in (que el
paciente interrumpa y el agente se calle), la detección de fin de turno, el
resampleo y el jitter. Escribir eso a mano cuesta días que esta ventana no tiene.

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
| STT — Whisper `small` + prompt clínico | **481 ms** | turno de 2-8 s |
| Embedding de la consulta — bge-m3 | **24 ms** | despreciable |
| Retrieval híbrido — Postgres | *pendiente* | se mide en la Fase 1 |
| Reranker — bge-reranker-v2-m3, top-8 | **114 ms** | apagable |
| LLM TTFT — Gemini 2.5 Flash | *~300-600 ms* | sin medir, falta API key |
| TTS 1ª frase — Kokoro `ef_dora` | **461 ms** | se sintetiza por frase |
| **Hasta el primer audio** | **≈ 1.4-1.7 s** | |

Palancas si hay que bajarlo, en orden de coste:

1. Apagar el reranker (`RERANK_ENABLED=false`) → −114 ms
2. Cambiar a Groq (`LLM_PROVIDER=groq`) → −200-400 ms estimados
3. Bajar a `CONTEXT_TOP_K=2` → menos tokens de entrada, LLM más rápido

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
- [ ] **Fase 1** — pipeline RAG completo sin voz
- [ ] **Fase 2** — consola de administración
- [ ] **Fase 3** — loop de voz
- [ ] **Fase 4** — agente de seguimiento y banderas rojas
- [ ] **Fase 5** — web app de llamada
- [ ] **Fase 6** — evals, tuning y guion de demo
