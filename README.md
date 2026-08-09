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
| LLM | Gemini 2.5 Flash · Groq Llama de repuesto | Mejor español clínico y tool calling fiable del conjunto permitido; medido, Groq solo gana 91 ms de TTFT y su free tier da timeouts con tool calling, así que queda como plan B de disponibilidad, no de latencia |
| Orquestación de voz | Pipecat + `SmallWebRTCTransport` **recomendado**; hoy corre el WebSocket propio | Pipecat gana por solapar el STT con la espera de fin de turno (−379 ms), no por el barge-in — ver abajo. Lo montado hoy es el WebSocket propio, que es el que tiene cliente de navegador |
| VAD / turnos | Silero VAD | ONNX en CPU. Cargado por `app/voice/vad.py`, sin Pipecat, para poder medirlo con audio inyectado; Pipecat lo trae también |
| STT | Whisper `small` (MLX) + sesgo de vocabulario | 481 ms con la misma precisión clínica que `medium` a 1222 ms — ver abajo |
| TTS | **Dos modos**: local (Kokoro) y premium (ElevenLabs) *(hoy solo suena el local: ver abajo)* | Flexibilidad de coste sin recompilar: gratis e ilimitado para operar, voz premium cuando la experiencia lo justifique |
| Vector DB | Postgres 16 + pgvector (HNSW) | Convierte «borrar = olvidar» en una propiedad ACID, no en disciplina del programador |
| Embeddings | `BAAI/bge-m3` sobre MPS | 1024 dims, multilingüe fuerte; 24 ms por consulta |
| Reranker | `bge-reranker-v2-m3`, top-8 | Discrimina nítido (0.993 vs 0.004), pero cuesta 585 ms: decisión abierta, ver abajo |
| Búsqueda | Híbrida: denso + FTS español + RRF | El léxico acierta «cefalexina 500 mg»; el denso acierta «¿me puedo bañar?» |
| Parsing | PyMuPDF en `.pdf`, Docling en `.docx`; cada uno respalda al otro | Lo que decide es la jerarquía de secciones, de la que depende todo el troceado: PyMuPDF acierta 25/25 niveles y Docling 3/25 — al revés de lo que asumía el plan. En `.docx` se invierte, porque Docling lee el nivel del estilo del párrafo en vez de inferirlo del tamaño de letra |
| Cola de ingesta | Postgres `FOR UPDATE SKIP LOCKED` | El estado del job vive en la misma transacción que el documento |
| Backend | FastAPI | Mismo lenguaje que Pipecat y el pipeline RAG |
| Frontend | React + Vite + Tailwind + shadcn/ui | Tres vistas: `/admin`, `/call` y `/calls` |
| Trazas | Tabla `traces` en Postgres *(schema creado, sin escribir todavía)* | Ver «Langfuse» abajo |

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
| **Langfuse self-hosted** | v3 arrastra ClickHouse + Redis + MinIO. Esta máquina tiene 16 GB compartidos con Whisper, bge-m3 y el reranker. Las trazas van a una tabla de Postgres (`traces`): ~0 RAM. La tabla está en el schema y nadie escribe en ella todavía — hoy las latencias por etapa viajan en `call_turns.latencies` y en el `ms` de `/api/rag/query`, que es lo que la consola y la pantalla de llamada pintan |
| **Phi mini local** | Sirve para demostrar operación sin red, pero tool calling frágil y compite por la GPU con Whisper y el TTS |

---

## Presupuesto de latencia

Medido en **MacBook Air M4, 16 GB**, mediana de 3-5 ejecuciones tras calentar.
Todo se midió de verdad, el LLM incluido: el 9 de agosto, contra la API real y con
el prompt de sistema del agente — ver §El LLM más abajo.

| Etapa | Medido | Nota |
|---|---:|---|
| **Fin de turno — Silero VAD** | **626 ms** | la etapa que este presupuesto no contaba |
| STT — Whisper `small` + prompt clínico | **391 ms** | con Pipecat se solapa con la etapa anterior |
| Embedding de la consulta — bge-m3 | **25 ms** | despreciable |
| Retrieval híbrido — Postgres | **3 ms** | pgvector + FTS + RRF sobre 25 fragmentos |
| Reranker — bge-reranker-v2-m3, top-8 | **585 ms** | ver abajo: el mayor bloque del pipeline |
| LLM TTFT — Gemini 2.5 Flash | **462 ms** | con el prompt del agente y el razonamiento apagado — ver abajo |
| TTS 1ª frase — Kokoro `ef_dora` | **196-303 ms** | mejor de lo que se creyó (461 ms) |
| TTS 1ª frase — ElevenLabs Flash | **354 ms** | ganaba al local cuando Kokoro se creía en 461 ms; remedido, el local vuelve a ser el más rápido |
| **Hasta el primer audio, con reranker** | **≈ 2.1 s** | medido con el LLM simulado a 400 ms; con el real son ~60 ms más |
| **Hasta el primer audio, sin reranker** | **≈ 1.5 s** | ídem |

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

### El LLM: el último número que faltaba, y la sorpresa que traía dentro

Medido el 9 de agosto contra la API real, con el **prompt de sistema del agente**
(3.354 caracteres, ~840 tokens) y un historial de cuatro turnos — no con un «di
hola», que habría dado un número bonito e inútil. Mediana de 5 ejecuciones para
Gemini y Groq, 4 para la variante con razonamiento, intercaladas para que la
deriva de la red no favorezca a ninguno.

| Configuración | TTFT | Respuesta completa | Peor caso visto |
|---|---:|---:|---:|
| **Gemini 2.5 Flash, `thinking_budget=0`** | **462 ms** | 642 ms | 9.733 ms |
| Gemini 2.5 Flash, razonamiento por defecto | 956 ms | 956 ms | 1.454 ms |
| Groq `llama-3.3-70b-versatile` | **371 ms** | 425 ms | 573 ms |

**El razonamiento de Gemini costaba más que el modelo.** Gemini 2.5 Flash trae el
*thinking* encendido por defecto y con presupuesto dinámico: piensa antes de
emitir el primer token, y ese rato entra íntegro en el TTFT. Apagarlo
(`thinking_budget=0`) es un factor **2,1** — 494 ms por turno, unos 7 segundos en
una llamada de quince turnos. Es la segunda palanca de latencia más grande del
sistema, por detrás del reranker, y no costó nada encontrarla salvo medirla.

Se apaga porque este agente no razona: sigue un guion, lee datos con herramientas
y repite lo que dice un protocolo. La decisión difícil —si hay una bandera roja—
es determinista y ni siquiera pasa por el modelo. La constante que lo controla
está con nombre en `app/agent/llm_client.py` (`PENSAMIENTO_DESACTIVADO`) por si
los evals de la Fase 6 muestran que elige mal la herramienta: subirla a 128 es lo
primero que habría que probar.

**Groq gana el TTFT por 91 ms, y aun así no se cambia.** Era la palanca número 3
de la lista de abajo, con «−200-400 ms estimados». Medido son **−91 ms**, dentro
del ruido de una sola ejecución. Y en la prueba del turno completo —dos rondas de
LLM con *tool calling*, que es lo que de verdad hace el agente— el free tier de
Groq dio timeouts y reintentos: mediana de **22,5 s** frente a **1,2 s** de
Gemini. El escape a Groq sigue existiendo y sigue siendo una variable de entorno,
pero deja de ser la palanca de latencia que se creía: es el plan B para cuando
Gemini no esté disponible, no para cuando Gemini vaya lento.

**El peor caso importa más que la mediana.** Los 9,7 s de Gemini fueron una sola
ejecución de cinco, y con el free tier a 10 peticiones por minuto lo más probable
es que fuera un 429 con reintento. En una demo en vivo eso es un silencio de diez
segundos, así que el cliente tiene `TIMEOUT_S = 12` y el agente una frase de
seguridad (`prompts.FRASE_SEGURIDAD`) que escala en vez de callar. No arregla la
latencia; arregla que se note como una avería.

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

1. ~~Apagar el reranker (`RERANK_ENABLED=false`)~~ → serían −585 ms, la palanca
   grande, pero **hoy no se puede tirar de ella**: las dos ramas de `reordenar()`
   puntúan en escalas distintas y ambas se comparan contra el mismo umbral, así
   que apagarlo deja `hay_evidencia` en `False` para todo y el agente responde «no
   tengo esa información» con el protocolo delante. El fallo está aislado y es
   pequeño (`docs/REVISION_F2_F3.md` §1.12, con un `xfail(strict=True)` que
   avisará cuando se arregle); arreglarlo es lo que desbloquea esta palanca
2. ~~Cambiar a Groq~~ → **−91 ms medidos**, no los −200-400 ms que se estimaron.
   Ya no es una palanca de latencia; es el plan B de disponibilidad
3. Rerankear menos candidatos (`RETRIEVE_TOP_K=5`) → ≈ −40 % del coste del reranker
4. Bajar a `CONTEXT_TOP_K=2` → menos tokens de entrada, LLM más rápido

La palanca que sí apareció al medir —apagar el razonamiento de Gemini, −494 ms—
ya está aplicada por defecto, así que no figura en la lista: está dentro del
462 ms de la tabla.

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
| **Kokoro** `ef_dora` | **196-303 ms** | 987 ms |
| macOS `say` Mónica | 512 ms | 530 ms |
| Piper `es_ES-davefx` | 594 ms | 725 ms |

La primera fila se midió dos veces: el spike de la Fase 0 dio 461 ms para Kokoro y
esa cifra estuvo un tiempo en esta tabla; remedido contra el pipeline real son
196-303 ms. Importa porque el número viejo era el que sostenía la conclusión
contraria: con 461 ms, ElevenLabs (354 ms) hacía que premium *ahorrase* latencia.
Con el número bueno vuelve a costarla, y lo que premium compra es solo voz.

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

El modo activo **no vive en `.env`**: vive en la tabla `app_settings` y se conmuta
por `PUT /api/settings/voice-mode`. La idea es que el coste sea una palanca
operativa en vez de una decisión de despliegue — un hospital puede operar en local
por cumplimiento o presupuesto y subir a premium cuando lo justifique.

**Lo que de esto está construido hoy, y lo que no.** La pieza que lo une —
`voice_mode.VoiceRouter`, que elige motor según el modo, degrada a local si el
premium falla y anota cada síntesis en `tts_usage`— está escrita y **no la
construye nadie**. Los dos caminos que sintetizan de verdad (`pipeline_ws.py` y
`servicios_pipecat.py`) llaman a `crear_motor(TTS_ENGINE_LOCAL)` sin preguntar por
el modo. Consecuencias, medidas en una llamada completa:

| Pieza | Estado |
|---|---|
| Los cinco motores tras una interfaz | **hecho** — Kokoro, Piper, `say`, ElevenLabs, Cartesia |
| El modo activo en `app_settings`, con endpoint | **hecho** — `GET`/`PUT /api/settings/voice-mode` |
| Que el modo activo decida la voz de una llamada | **no** — hoy siempre suena `TTS_ENGINE_LOCAL` |
| Degradación automática a local si premium falla | **no** — vive en `VoiceRouter`, que nadie instancia |
| Contabilidad en `tts_usage` | **no** — cero filas tras una llamada con cinco síntesis |
| Botón en la consola y panel de consumo | **no** — la Fase 2 se cerró en documentos |

Enchufarlo es una línea en cada uno de esos dos ficheros, pero empieza a gastar
voz de pago, así que es una decisión y no un olvido. Mientras tanto, cambiar de
motor es cambiar `TTS_ENGINE_LOCAL` y reiniciar.

Lo que sí se sostiene sin nada de eso: **el desarrollo no toca el free tier**. Son
~10.000 caracteres al mes; una tarde iterando sobre el guion se los come. Por eso
el modo por defecto es local y premium se reserva para las pruebas finales.

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
  voice/      vad · stt · tts · los DOS pipelines (ws y pipecat)
  agent/      prompts · guion · tools · banderas rojas · llm_client
  db/         schema.sql · pool · cola de jobs
  core/       config
frontend/     React: /call, /calls y /admin
scripts/spikes/  las mediciones que sostienen las decisiones de aquí (reproducibles)
eval/         corpus de prueba, guion de llamada y evaluación del RAG
```

## Estado

- [x] **Fase 0** — infraestructura, schema verificado, presupuesto de latencia medido
- [x] **Fase 1** — pipeline RAG completo sin voz: parsing, cola, worker, retrieval híbrido
- [x] **Fase 2** — consola de administración: API con SSE + panel de documentos en React
- [x] **Fase 3** — loop de voz *(las dos opciones construidas y medidas; gana Pipecat,
  pero lo montado y accesible hoy es el WebSocket propio, que es el que tiene cliente
  de navegador. Falta probarlo con un micrófono de verdad)*
- [x] **Fase 4** — agente de seguimiento y banderas rojas *(guion adaptativo, seis
  herramientas, detector determinista y grounding; el guion clínico está en
  [eval/guion_llamada.md](eval/guion_llamada.md) pendiente de revisión de Samuel)*
- [x] **Fase 5** — web app de llamada: `/call`, `/calls` y `/calls/:id`, cosida al
  bucle de voz y al agente
- [ ] **Fase 6** — evals, tuning y guion de demo

La garantía central se demuestra sin micrófono en un solo comando:

```bash
cd backend && uv run python ../scripts/demo_aprender_olvidar.py
```

Desde `backend/`, que es donde vive el `pyproject.toml`: lanzado desde la raíz,
`uv run` monta un entorno efímero sin `httpx` y el script muere en el import.

Sube un protocolo, espera a que el agente lo aprenda, le pregunta algo que solo
ese documento responde, lo borra y vuelve a preguntar. Medido: **2,6 s** de la
subida a `ready`, y la segunda consulta devuelve cero fragmentos.
