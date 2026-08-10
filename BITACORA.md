# Bitácora de desarrollo

Agente de voz para seguimiento postoperatorio — Tech Sphere Challenge 2026.

El `README.md` cuenta cómo está el sistema **ahora**. Esta bitácora cuenta **cómo
llegó a estarlo**: qué se construyó en cada fase, por qué se decidió así, y qué se
ha ido corrigiendo por el camino. Las decisiones que resultaron equivocadas se
quedan escritas con el dato que las tumbó — esa parte es la que más vale, tanto
para retomar el trabajo como para defenderlo ante un jurado.

**Cómo mantenerla:** cada fase tiene su propio apartado *Cambios y correcciones*.
Al cerrar una tanda de trabajo, se añade ahí con fecha. Si una decisión anterior se
revierte, no se borra: se anota que se revirtió y con qué medición.

---

## Dónde vive el código

**Todo vive en `main`** desde el 9 de agosto: 20 commits, **423 tests en verde**
más un `xfail` deliberado. Se trabaja en el directorio normal del repo.

El desarrollo se hizo en una rama `nocturno` sobre un worktree aparte, para que
una decena de agentes pudieran trabajar en paralelo sin tocar el árbol principal.
Las dos ramas habían divergido en el commit inicial —la Fase 0 se recommiteó por
separado—, así que la fusión se hizo favoreciendo `nocturno`, cuyo contenido era
un superconjunto corregido: el README de `main` aún decía que el reranker costaba
114 ms y que Docling conservaba mejor la jerarquía que PyMuPDF, y la medición
había tumbado las dos cosas.

Queda una rama `respaldo-main-82ac169` como red de seguridad hasta que el sistema
esté demostrado con micrófono y navegador.

### Bases de datos

Todas en el contenedor `postop_db`, puerto **5433** para no chocar con un Postgres
local en el 5432.

| Base | Para qué |
|---|---|
| `postop` | La de trabajo. Recreada con el schema al día el 9 de agosto |
| `postop_t1..t5` | Aisladas, una por agente, para que los tests destructivos no se pisen |

Dos trampas de arranque que costaron tiempo y ya están documentadas en el README:
`uv sync` a secas deja fuera el grupo `voice` —donde vive `pipecat`— y tumba 54
tests; y seis tests de VAD leen clips de `scripts/spikes/audio/`, que está en
`.gitignore` porque los generan los propios spikes.

---

## Fase 0 — Infraestructura y verificación de riesgos

> **Objetivo:** medir lo que puede hundir el proyecto *antes* de construir encima, y
> dejar el schema escrito de forma que la garantía del enunciado sea una propiedad
> de la base de datos, no disciplina del programador.

### Qué construimos

| Fichero | Qué hace |
|---|---|
| `docker-compose.yml` | Postgres 16 + pgvector, puerto 5433, límite de 1 GB de RAM |
| `backend/app/db/schema.sql` | 14 tablas. El corazón del proyecto |
| `backend/app/db/seed.sql` | 3 pacientes sintéticos con cirugías, medicación y citas |
| `backend/app/db/pool.py` | psycopg3 asíncrono con `register_vector_async` |
| `backend/app/core/config.py` | Toda la configuración desde `.env`, nada hardcodeado |
| `backend/app/voice/stt.py` | Whisper MLX con sesgo de vocabulario clínico |
| `backend/app/voice/tts.py` | 5 motores tras una interfaz, salida normalizada a PCM 24 kHz |
| `backend/app/voice/voice_mode.py` | Router local/premium conmutable en caliente |
| `backend/app/agent/llm_client.py` | Interfaz LLM con Gemini y Groq detrás |
| `scripts/spikes/` | Cuatro mediciones reproducibles |

### Por qué se decidió así

**El olvido es una propiedad del schema, no del código.** Es la decisión que
condiciona todo lo demás, y se apoya en dos mecanismos independientes:

1. **`ON DELETE CASCADE`** de `chunks` a `documents`. El vector vive en la misma
   fila que el texto, así que borrar el documento borra sus vectores en la misma
   transacción. No hay índice que reconstruir ni caché que invalidar.

2. **La vista `retrievable_chunks`** es el único punto de lectura del RAG y filtra
   por `status = 'ready'`. Aunque un bug dejara fragmentos huérfanos, son
   irrecuperables por construcción.

Se verificó con datos sintéticos el día 1, antes de escribir una línea de
aplicación:

| Situación | En la tabla | Recuperables |
|---|---:|---:|
| Documento `ready` | 3 | **3** |
| Marcado `superseded` | 3 | **0** |
| `DELETE` del documento | **0** | **0** |

El caso intermedio es el importante: un documento deja de ser recuperable *al
instante* sin necesidad de borrarlo. Así el olvido es inmediato y el borrado físico
puede ocurrir después sin abrir ninguna ventana de riesgo.

**Postgres + pgvector, no un vector store dedicado.** El argumento no es
rendimiento — con decenas de documentos Qdrant sería más rápido y daría igual. Es
que el requisito de olvidar es un requisito de *consistencia*: con dos sistemas
(vector store + Postgres para el admin) hay que mantenerlos sincronizados y existe
una ventana en la que un documento borrado sigue siendo recuperable. Con un solo
datastore no hay ventana.

**Conocimiento y datos vivos van por caminos distintos.** Los protocolos van al RAG
vectorial; qué paciente es, qué cirugía tuvo y cuándo es su cita se leen con
*function calling* contra la base. Un embedding es una foto del momento de la
ingesta: meter ahí datos que cambian es la causa número uno de respuestas
desactualizadas en sistemas como este.

**Dos modos de voz conmutables en caliente.** El modo activo vive en la tabla
`app_settings`, no en `.env`, para poder cambiarlo a mitad de llamada desde la
consola. Eso convierte el coste en una palanca operativa en vez de una decisión de
despliegue: un hospital puede operar en local por cumplimiento o presupuesto y
subir a premium cuando lo justifique. Con degradación automática a local si premium
falla, y contabilidad en `tts_usage`.

### Cambios y correcciones

**2026-08-07 · El compromiso del STT resultó falso.** El plan pedía
`whisper-large-v3-turbo` asumiendo «rápido o preciso, elige». Se probó sesgar el
decodificador con vocabulario clínico (`initial_prompt`) sobre un modelo pequeño:

| Configuración | Latencia | ¿«apendicectomía»? |
|---|---:|---|
| `small` sin prompt | 436 ms | ✗ «appendicitomía» |
| **`small` + prompt clínico** | **481 ms** | **✓** |
| `medium` sin prompt | 1222 ms | ✓ |
| `large-v3-turbo` | 1257 ms | ✓ |

45 ms compran la precisión de `medium` a 2,5× su velocidad. El vocabulario vive en
`VOCABULARIO_CLINICO` y se amplía cuando aparezcan errores nuevos.

**2026-08-07 · Langfuse descartado por RAM.** El plan lo pedía para trazas, pero la
v3 arrastra ClickHouse + Redis + MinIO y esta máquina tiene 16 GB compartidos con
Whisper, bge-m3 y el reranker. Sustituido por una tabla `traces` en Postgres: ~0 RAM
y se muestra en la propia consola.

**2026-08-07 · Bug de troceado con consecuencia clínica.** Una sección corta
(`### Cuándo llamar al equipo`) se fundía hacia adelante con la siguiente
(`## Medicación`), archivando una regla de alarma bajo medicación. Rediseñado: una
sección corta se funde **solo con un pariente en la jerarquía** —hacia atrás con su
padre, o hacia adelante solo si es un preámbulo—, nunca con una hermana.

**2026-08-08 · Smoke test de ElevenLabs.** Tres hallazgos:

- **El free tier no permite voces de biblioteca por API** (`402
  paid_plan_required`). Solo las *premade*. **Configurada «Lily»**, elegida
  escuchando las tres candidatas frente a Kokoro (9 de agosto).
- **ElevenLabs parecía más rápido que Kokoro:** 354 ms contra 461 ms. *Corregido
  el 9 de agosto*: al remedir Kokoro salieron 196-303 ms, así que el local vuelve
  a ser el más rápido y el argumento se cae. Premium se elige por calidad de voz,
  no por latencia — y a cambio cuesta dinero y depender de la red.
- La API key estaba escrita en `.env.example`, que es plantilla destinada a
  commitearse. Movida a `.env`. Nunca llegó a git.

---

## Fase 1 — El RAG completo, sin voz

> **Objetivo:** cerrar el requisito central del enunciado y poder demostrarlo **sin
> micrófono**. Si la garantía se prueba a través de audio, nunca sabes si falló el
> RAG o falló el sonido.

### Qué construimos

| Fichero | Qué hace |
|---|---|
| `app/rag/parsing.py` | `.pdf/.docx/.md/.txt` → Markdown con la jerarquía intacta |
| `app/rag/chunking.py` | Troceado por secciones con rutas `H2 › H3` |
| `app/rag/embeddings.py` | `bge-m3` sobre MPS, 1024 dimensiones |
| `app/db/queue.py` | Cola con `FOR UPDATE SKIP LOCKED`, backoff y huérfanos |
| `app/workers/ingest_worker.py` | El bucle que consume la cola y aprende |
| `app/rag/ingest.py` | Promoción atómica a `ready` y `olvidar_documento()` |
| `app/rag/retrieval.py` | Búsqueda híbrida denso + léxico fusionada con RRF |
| `app/rag/rerank.py` | Cross-encoder y umbral de grounding |
| `app/rag/query.py` | CLI de consulta, directo contra la base |
| `eval/corpus_prueba/` | 3 protocolos sintéticos en `.md`, `.pdf` y `.docx` |

### Por qué se decidió así

**El parsing es la pieza de la que cuelga todo.** El troceado parte por encabezados
y construye rutas como `Signos de alarma › Cuándo llamar al equipo`, que es lo que
permite al agente citar *«según el protocolo, en la sección de signos de alarma…»*.
Un parser que aplane la estructura no rompe nada visiblemente — las citas
simplemente dejan de existir, y en contexto clínico eso separa un sistema auditable
de uno que suena convincente.

**La cola vive en Postgres, no en Redis.** El motivo no es simplicidad: el estado
del job vive en la **misma transacción** que el documento, y la consola necesita
leer ambos sin que exista un documento sin job ni al revés. Tres propiedades que
garantiza:

- *Dos workers nunca cogen el mismo job* — `FOR UPDATE SKIP LOCKED`: el segundo no
  espera, salta la fila bloqueada y se lleva la siguiente.
- *Un worker que muere no bloquea su job para siempre* — la recuperación se apoya en
  la antigüedad de `locked_at`, **no** en el bloqueo de fila, que se suelta al hacer
  commit mientras el trabajo de verdad (parsear, embeber) ocurre después. La
  alternativa —mantener la transacción abierta durante todo el procesamiento— frena
  el autovacuum de todas las tablas.
- *Un documento venenoso no gira eternamente* — los intentos se cuentan **al
  reclamar**, no al fallar, para que un PDF que reviente el proceso entero también
  gaste intentos.

**El worker corre en un proceso aparte, y no por escalabilidad.** Embeber ocupa la
GPU y bloquea un hilo durante segundos. Dentro de FastAPI, subir un protocolo de 40
páginas metería un pico de latencia justo en el pipeline de voz, con un paciente al
teléfono. Separados, lo peor que pasa es que la ingesta tarde un poco más.

**La promoción a `ready` es atómica.** Insertar todos los fragmentos con sus
vectores, cambiar el estado y retirar la versión anterior ocurre en **una sola
transacción**. El agente pasa de no conocer un documento a conocerlo entero: nunca
puede responder con medio protocolo.

Los contadores `chunks_count` y `embedded_count` sí avanzan *fuera* de esa
transacción, a propósito: son contadores de interfaz, no de verdad recuperable. Que
la consola muestre 11/24 mientras tanto es la prueba visible de que está
aprendiendo.

**La búsqueda es híbrida.** El léxico acierta «cefalexina 500 mg»; el denso acierta
«¿me puedo bañar?». Fusionados con RRF (k=60). Ambos consultan
`retrievable_chunks`, nunca la tabla `chunks`.

### Cambios y correcciones

**2026-08-08 · PyMuPDF gana a Docling, al contrario de lo que asumía el plan.** El
plan daba por hecho que Docling conservaba la jerarquía. Medido sobre el corpus:

| Motor | s/doc | Arranque | RAM pico | Niveles correctos |
|---|---:|---:|---:|---:|
| **PyMuPDF** | 0,151 s | 0,25 s | 61 MB | **25/25** |
| Docling | 0,494 s | 12,32 s | 1212 MB | **3/25** |

El número que decide es 25/25 contra 3/25, no la velocidad. Ambos *encuentran* los
mismos encabezados, pero Docling los emite todos como `##`. Una jerarquía plana no
degrada el troceado: lo desactiva, porque la pila de niveles se vacía en cada
sección y no se genera ni una sola ruta.

Docling se queda como **respaldo automático** para el caso que PyMuPDF no cubre. En
`.docx` la conclusión se invierte y Docling es el primario, porque lee el nivel del
estilo del párrafo en vez de inferirlo del tamaño de letra.

**2026-08-08 · El PDF escaneado habría llegado a `ready` con 0 fragmentos.** Un PDF
sin capa de texto extrae cadena vacía; la consola habría dicho «Listo — el agente ya
lo sabe» sobre un protocolo que el agente no leyó. Un fallo silencioso, que es el
peor tipo, y el más probable de cara al corpus real.

Resuelto mejor que rechazándolo: **Docling con OCR se dispara como respaldo cuando
el primario no saca texto**. Si ni el OCR saca nada, entonces sí acaba en `failed`,
con un mensaje que le dice al administrador qué hacer.

Hay además un umbral **por página**, no total, por un caso concreto: un escáner de
hospital estampa un pie de página («Hospital General · Página 1 de 2»). Esos 33
caracteres bastarían para que PyMuPDF «acierte», el OCR no se dispare nunca y el
documento entre con un fragmento que no dice nada.

**2026-08-08 · El olvido dejaba el PDF en el disco.** El más grave de la tanda,
porque era el requisito central incumplido a medias y en silencio.

`STORAGE_DIR=./storage/documents` es relativo y se resolvía contra el directorio de
trabajo de cada proceso. La API arranca desde `backend/` y los scripts desde la
raíz, así que resolvían a carpetas distintas. `olvidar_documento()` comprueba que el
archivo esté por debajo de `storage_dir` antes de borrarlo —una defensa correcta
contra travesías de ruta— y esa comparación fallaba. El `except OSError: pass` se
tragaba el fallo.

Resultado: la fila desaparecía, el agente olvidaba de verdad, y **el PDF con datos
clínicos del paciente se quedaba en el disco del hospital para siempre**.

Arreglado en tres sitios:
- `config.py` — `storage_dir` se ancla a la raíz del repo si viene relativa.
- `ingest.py` — un fallo al borrar ya no se traga: registra `file_delete_failed` en
  `document_events` y avisa por el log. El comentario que decía que un huérfano en
  disco «es inocuo porque el agente ya no puede recuperarlo» era cierto para el
  retrieval y falso para un hospital.
- `.gitignore` — patrón `**/storage/documents/*`. Un PDF de paciente commiteado por
  accidente no se arregla con un revert.

Efecto colateral que confirma el diagnóstico: unos tests que fallaban de forma
intermitente según el orden dejaron de hacerlo. Se pisaban a través de esa carpeta.

**2026-08-08 · El reranker cuesta cinco veces lo presupuestado.** El spike de la
Fase 0 dijo 114 ms, pero midió con pasajes de 250 caracteres. Los fragmentos reales
tienen entre 500 y 1400, y el coste de un cross-encoder escala con la longitud de la
secuencia:

| Longitud del pasaje | `max_length` | 8 candidatos |
|---|---:|---:|
| 250 caracteres | 512 | 194 ms ← lo que se midió entonces |
| 1400 caracteres | 512 | **924 ms** |
| 1400 caracteres | 256 | 609 ms |
| 1400 caracteres | 128 | 303 ms |

Medido contra la API real, en caliente: **585 ms**. El mayor bloque del pipeline,
por delante del STT.

*Lección: un spike de latencia solo vale si sus entradas se parecen a las reales.
Medir con datos de juguete da números de juguete.*

**La decisión de encenderlo o no se dejó abierta a propósito.** Sobre 3 protocolos
sintéticos el híbrido ya acierta casi siempre, así que el reranker no tiene margen
para demostrar su valor — eso no prueba que sobre, prueba que *ese corpus no sirve
para decidirlo*. `eval/medir_reranker.py` está escrito para relanzarlo con los
documentos reales. Mientras tanto se queda encendido, porque en dominio clínico
recuperar el protocolo equivocado es peor que responder despacio.

### Cómo se prueba

```bash
cd backend
export DATABASE_URL=postgresql://postop:postop@localhost:5433/postop_wt
uv run uvicorn app.main:app --port 8000        # terminal 1
uv run python -m app.workers.ingest_worker     # terminal 2
uv run python ../scripts/demo_aprender_olvidar.py   # terminal 3
```

```
1. SUBIR      protocolo_apendicectomia.pdf → uploaded (112 KB)
2. APRENDER   0.0s uploaded → 1.0s parsing → 1.6s embedding → 2.6s ready (8/8)
3. PREGUNTAR  «¿cuándo puedo ducharme?» → evidencia True, 3 fragmentos
              [0.763] …pdf › Cuidado de la herida › p. 1
              [0.271] …pdf › Dieta › p. 2
4. OLVIDAR    olvidado=True, fragmentos eliminados=8
5. LA MISMA   evidencia False, 0 fragmentos
   PREGUNTA
```

**Qué mirar.** Que el primer fragmento puntúe 0,763 y el segundo 0,271 significa que
el retrieval discrimina, no devuelve cualquier cosa. Y en el paso 5, que
`hay_evidencia` sea `False` es la señal que hace que el agente diga «no tengo esa
información» en lugar de improvisar.

**Por qué esta prueba y no un test unitario:** prueba el *requisito*, no la
implementación. Se puede reescribir el parser, cambiar el modelo de embeddings o
migrar la base, y sigue significando lo mismo.

Para depurar el RAG aislado de la API:

```bash
uv run python -m app.rag.query "¿cuándo puedo ducharme?"
```

---

## Fase 2 — Consola de administración

> **Objetivo:** que el «subo → aprende / borro → olvida» sea **visible en pantalla**,
> que es donde el requisito del enunciado se convierte en demo.
>
> **Alcance decidido:** solo el panel de gestión de documentos. Pacientes, historial
> de llamadas y panel de trazas quedan para más adelante.

### Qué construimos

**Backend** — `app/api/`, siete módulos:

| Fichero | Qué hace |
|---|---|
| `salud.py` | `GET /api/health`, publica `modelos_listos` |
| `documentos.py` | Subir, listar, detalle y borrar |
| `eventos.py` | SSE de cambios de estado |
| `rag.py` | `POST /api/rag/query` con desglose de ms por etapa |
| `ajustes.py` | Toggle del modo de voz |
| `deps.py` | Autenticación por `X-Admin-Token` |
| `errores.py` | Forma única de error, en español |
| `main.py` | La app, el ciclo de vida y la precarga de modelos |

**Frontend** — `frontend/`, 41 ficheros TypeScript/React. Vite + Tailwind +
shadcn/ui.

**Contrato** — `docs/CONTRATO_API.md`, escrito **antes** que el código.

### Por qué se decidió así

**El contrato se escribió primero.** Es lo que permitió construir backend y frontend
en paralelo, por agentes distintos, sin coordinación entre ellos. Cualquier
desviación se anota al final del propio documento, en «Cambios sobre el contrato».

**La subida responde `202` y no espera al procesamiento.** La ingesta es asíncrona
por la cola y el frontend sigue el progreso por SSE. Bloquear ahí haría que subir un
PDF de 40 páginas pareciera colgado.

**El SSE es lo que hace visible el aprendizaje.** Es el momento demo del proyecto:
se ve la fila pasar de *Recibido* a *Leyendo el archivo*, *Troceando*, *Generando
embeddings* y por fin *Listo — el agente ya lo sabe*, con los contadores subiendo. El
token va por query string porque `EventSource` no admite cabeceras.

**Los modelos se precargan al arrancar, en paralelo, no en la primera petición.**
Cargar bge-m3 y el reranker tarda decenas de segundos. Esperarlos antes de aceptar
peticiones dejaría la consola sin poder ni listar documentos; cargarlos en la
primera petición haría que el primer query de la demo tardara medio minuto y
pareciera roto. Arrancando en paralelo, el administrador entra de inmediato y
`GET /api/health` publica `modelos_listos` para saber cuándo responderá a plena
velocidad.

**Los errores tienen forma propia** — `{"error": {"codigo", "mensaje"}}` en español,
nunca el `{"detail": …}` de FastAPI — con un manejador global para que un fallo
inesperado tampoco se salga del formato.

**Autenticación deliberadamente simple.** Un solo administrador, token en variable de
entorno. Con esta ventana de tiempo, más que eso es ceremonia sin valor demostrable.
El siguiente paso natural sería autenticación real.

**El modo simulado.** `VITE_MOCK=1` emula el backend entero en memoria, incluidos los
eventos SSE recorriendo la máquina de estados con retardos realistas. Sirvió para
construir la pantalla sin backend, y sirve para ensayar la demo. La pantalla avisa
con una insignia «Datos simulados» para que nadie confunda un mock con el sistema
real.

### Cambios y correcciones

**2026-08-08 · `documents.pages` faltaba en el schema.** El contrato ya publicaba
`pages` en el objeto `Documento` pero la tabla no tenía dónde guardarlo. Solo el
parser conoce ese dato, así que lo escribe el worker al promover a `ready`. Es
`NULL` en `.docx`, `.md` y `.txt`, que no tienen paginación.

**2026-08-08 · `jobs.run_after` y `jobs.locked_by`.** Sin `run_after` el backoff no
existe: un job que falla vuelve a `queued` y el worker lo reclama en el mismo
milisegundo, quemando los tres intentos en un bucle cerrado contra el mismo error.

**2026-08-09 · Revisión adversarial de las Fases 2 y 3** (`docs/REVISION_F2_F3.md`).
Doce fallos, cada uno con el test que lo demuestra escrito *antes* del arreglo. Los
de esta fase:

- **El SSE no propagaba `pages`**, así que la consola mentía en una columna.
- **La primera consulta al RAG tarda 13,3 s y el cliente cortaba a los 15** — un
  margen de 1,7 s sobre un arranque en frío.
- **`total` mentía en una página vacía** de la paginación.
- Otro **`except Exception: pass`** en la contabilidad de TTS, hermano del que ya
  apareció en `olvidar_documento()`. Van dos: conviene buscarlos activamente.

**Y uno pendiente, el de peor relación gravedad/tamaño de toda la revisión:
`hay_evidencia` es siempre `False` sin reranker.** Las dos ramas de `reordenar()`
devuelven escalas distintas y se comparan contra el mismo umbral de 0,35:

| Rama | Escala del `score` | ¿Pasa 0,35? |
|---|---|---|
| Con reranker | cross-encoder tras sigmoide, 0..1 | sí, discrimina bien |
| **Sin reranker** | RRF, **máximo teórico 0,0328** | **nunca** |

Con `RERANK_ENABLED=0` el agente respondería «no tengo esa información» a preguntas
cuyo protocolo tiene delante. **Esto invalida el consejo que se venía dando** de
apagar el reranker para recuperar 585 ms: hacerlo hoy rompe el grounding en
silencio. No lo vio ningún test porque todos los que tocan grounding sustituyen
`reordenar`. Queda marcado con `xfail(strict=True)`: la batería sigue verde
mientras el fallo exista y se pondrá roja el día que se arregle.

### Cómo se prueba

```bash
cd frontend
rm .env.local          # si no, arranca contra el simulador
npm install && npm run dev
```

En `localhost:5173`: pulsar **«Configurar token»** (arriba a la derecha, icono de
llave), pegar el valor de `ADMIN_TOKEN` — por defecto `cambiar-esto-en-local` — y
guardar. El botón pasa a «Token guardado» en verde y queda en el `localStorage`.

Después, arrastrar un PDF de `eval/corpus_prueba/` y mirar la fila avanzando en
vivo. Luego borrarlo y fijarse en cuántos fragmentos dice que eliminó.

---

## Fase 3 — Loop de voz

> **Objetivo:** decidir la orquestación **por medición**, no por lo que dijera el
> plan. Se construyeron las dos opciones enteras y se compararon.

### Qué construimos

| Fichero | Qué hace |
|---|---|
| `app/voice/vad.py` | Silero VAD por ONNX, sin Pipecat. Reloj por contador de muestras |
| `app/voice/pipeline_ws.py` | **Opción B**: WebSocket propio, transporte-agnóstico |
| `app/voice/pipeline_pipecat.py` | **Opción A**: montaje real de Pipecat 1.7 |
| `app/voice/servicios_pipecat.py` | `stt.py` y `tts.py` envueltos como servicios |
| `scripts/spikes/spike_voz.py` | Arnés de medición, 4 escenarios |
| `scripts/spikes/cliente_voz/` | Página de un fichero para probar con micrófono |
| `docs/VOZ_COMPARATIVA.md` | El entregable que decide |

### La comparativa

Mismo STT, mismo LLM simulado (TTFT 400 ms), mismo presupuesto de fin de turno.
Mediana de 3 ejecuciones.

| | Pipecat | WebSocket propio |
|---|---:|---:|
| **Hasta el primer audio** | **1.596 ms** | 1.975 ms |
| Barge-in (silencio audible) | 84 ms | 96 ms |
| Fin de turno | 626 ms | 640 ms |
| Líneas de código de producción | 171 | 312 |
| Distribuciones / disco | 52 / 395 MB | 7 / 117 MB |

### Por qué se decidió así

**Gana Pipecat, pero el argumento del plan era falso.** El plan y el README decían
que el barge-in era el trabajo difícil que Pipecat te ahorra. No lo es: funciona
igual en ambas, y el número lo domina el umbral de confirmación del VAD (96 ms = 3
ventanas de Silero), no el framework. Lo posterior cuesta menos de 1 ms.

Lo que decide de verdad: **Pipecat solapa el STT con la espera de fin de turno** en
lugar de encadenarlos. 379 ms, un 19 %. El truco se puede copiar en unas 15 líneas;
el valor de Pipecat es traerlo puesto, junto con el reloj de reproducción del bot y
el vaciado de buffers.

**La opción propia no se tira.** Es el plan B de un solo fichero, el arnés con el que
se midió todo esto, y el banco de pruebas con micrófono.

**El umbral de fin de turno son 640 ms**, aceptando un corte falso sobre una pausa
deliberada de 700 ms en vez de subir a 800 ms, que no fallaba ninguna. El motivo: con
el barge-in funcionando, un corte falso se recupera en 97 ms, mientras que 160 ms
extra se pagan en cada uno de los ~15 turnos de la llamada.

**Se monta detrás de `VOZ=1` y apagado por defecto.** Construir el router carga
Silero, Whisper y el motor de TTS, que tardan y ocupan GPU. La consola de
administración —que es la mayor parte del trabajo diario— no necesita nada de eso.

### Cambios y correcciones

**2026-08-08 · Al presupuesto de latencia le faltaba una etapa entera.** El fin de
turno cuesta 626 ms —más que el STT— y no aparecía en ninguna versión de la tabla.
No la contaba nadie porque no es un modelo que se ejecute, sino una espera
deliberada.

**2026-08-08 · Kokoro es más rápido de lo medido:** 196-303 ms a la primera frase, no
461 ms.

**2026-08-08 · El modo de voz por defecto no arrancaba.** `kokoro` nunca llegó a
`pyproject.toml`: la Fase 0 lo midió con `uv run --with kokoro` y el número se quedó
sin fijar la dependencia. `crear_motor("kokoro")` lanzaba `ModuleNotFoundError`.

**2026-08-08 · La API de Pipecat 1.7 no es la que describía el plan.** El VAD ya no es
`TransportParams(vad_analyzer=…)` sino `VADProcessor`; `PipelineTask` y
`PipelineRunner` están deprecados; la estrategia de fin de turno por defecto es un
modelo semántico que se descarga. Dos atascos no documentados costaron ~1 h: el STT
va **antes** del `UserTurnProcessor`, y un transporte propio debe llamar a
`set_transport_ready()` o el proceso se cuelga con un `AttributeError` invisible.

Su `WhisperSTTServiceMLX` se descartó: 296 ms pero transcribe «appendicitomía»
porque no expone `initial_prompt`. Se usa el nuestro.

**2026-08-08 · El router no estaba montado en `main.py`.** La Fase 3 estaba
construida y medida pero no era accesible por HTTP.

**2026-08-09 · Dos fallos de gravedad ALTA en el bucle de voz**, de la revisión
adversarial:

- **Un turno que reventaba mataba la llamada, pero un turno después.** La excepción
  se quedaba dormida dentro de la `asyncio.Task` —sin log, sin evento, sin rastro—
  y resucitaba en el turno siguiente o en `cerrar()`. Y allí impedía ejecutar
  `motor.cerrar()`, así que cada llamada mal terminada dejaba colgado un cliente
  HTTP de ElevenLabs.
- **Un trozo de audio de longitud impar cerraba el WebSocket.** `np.frombuffer`
  lanza con cualquier longitud impar. Y lo peor no era la excepción: el buffer ya
  se había desplazado un byte, así que **todo el audio posterior habría sonado a
  ruido blanco**.

Más otros: Silero se recargaba entero en cada llamada, buffers sin techo en una
llamada larga, dos cargas de Kokoro si entraban dos llamadas a la vez, y el
regreso de «graciaspor contármelo» por otra puerta distinta a la de la Fase 0.

### Cómo se prueba

```bash
cd backend
VOZ=1 TTS_ENGINE_LOCAL=say uv run uvicorn app.main:app --port 8000
cd ../scripts/spikes/cliente_voz && python3 -m http.server 5500
```

En `localhost:5500`, Conectar y hablar. En este orden, que es donde se rompen estas
cosas:

1. Un turno normal
2. **Interrumpirle hablando encima** — debe callarse
3. Una pausa de duda a media frase — no debería cortar
4. Decir solo «Sí.»
5. Con el volumen alto, a ver si se autointerrumpe. Si pasa, la solución son
   auriculares, no tocar el VAD

### Qué falta

- **Nada se ha probado con un micrófono de verdad.** Todo se midió inyectando audio.
- **Montar Pipecat sobre WebRTC real** (`POST /api/voz/offer`). Ahora mismo lo que
  está accesible es el WebSocket propio, que es el que tiene cliente de navegador.
- **El TTFT del LLM es simulado** con 400 ms fijos.

---

## Presupuesto de latencia

Medido en MacBook Air M4, 16 GB, en caliente y contra el pipeline real.

| Etapa | Medido |
|---|---:|
| Fin de turno — Silero VAD | 626 ms |
| STT — Whisper `small` + prompt clínico | 391 ms *(con Pipecat se solapa)* |
| Embedding de la consulta — bge-m3 | 25 ms |
| Retrieval híbrido — Postgres | 3 ms |
| **Reranker — bge-reranker-v2-m3, top-8** | **585 ms** |
| **LLM TTFT — Gemini 2.5 Flash** | **462 ms** *(razonamiento apagado)* |
| TTS 1ª frase — Kokoro | 196-303 ms |
| TTS 1ª frase — ElevenLabs Flash | 354 ms |
| **Hasta el primer audio, con reranker** | **≈ 2,1 s** |
| **Hasta el primer audio, sin reranker** | **≈ 1,5 s** |

**Palancas, por orden de tamaño:** apagar el reranker (−585 ms, **pero ver el
fallo 1.12 de la revisión: hoy eso rompe el grounding**) · rerankear menos
candidatos (−40 % del coste del reranker) · bajar `CONTEXT_TOP_K` a 2.

**Groq ya no es una palanca de latencia.** El plan estimaba −200-400 ms; medido,
gana 91 ms de TTFT y su free tier da *timeouts* en cuanto hay tool calling —
22,5 s de mediana en el turno completo frente a 1,2 s de Gemini. Pasa a ser plan
B de disponibilidad, no de velocidad.

---

## Fase 4 — Agente de seguimiento

> **Objetivo:** que el agente sostenga una llamada clínica de verdad — con guion,
> herramientas, grounding obligatorio y escalamiento — sin depender del LLM para
> lo que no debe.

### Qué construimos

| Fichero | Qué hace |
|---|---|
| `app/agent/llm_client.py` | Interfaz LLM con Gemini y Groq detrás. **Arreglado** |
| `app/agent/prompts.py` | Prompt de sistema, `SALUDO` y `FRASE_SEGURIDAD` como constantes |
| `app/agent/guion.py` | El guion como **datos**: bloque común + bloques por cirugía |
| `app/agent/tools.py` | Las 6 herramientas. Las de lectura no llevan argumentos |
| `app/agent/redflags.py` | Detector determinista: normalización de habla telefónica, negación NegEx, 7 familias de alarma |
| `app/agent/agente.py` | El bucle. Implementa `LLMClient`, así que entra en `SesionVoz` sin tocar `app/voice/**` |
| `app/api/llamadas.py` | Los 4 endpoints del contrato, más `/mensaje` y `/fin` |
| `eval/guion_llamada.md` | El guion en prosa, **para que Samuel lo valide** |

### Por qué se decidió así

**El guion es datos, no código.** Se declara como estructura y el modelo lo
recorre; así añadir una cirugía es añadir un bloque, no tocar lógica.

**Las banderas rojas no pasan por el LLM.** Detección determinista por patrones
sobre el transcript. Un modelo puede tener un mal día; una regla no. Y funciona
sobre transcripciones imperfectas de Whisper: números dictados con letra, sin
tildes, con muletillas en medio.

**El saludo es una constante, no lo genera el modelo.** Contiene la declaración de
sistema automatizado que exige el AI Act, y una frase generada saldría distinta
cada vez, sin forma de demostrar que se dijo. Además permite que el TTS empiece a
sonar sin esperar al LLM.

**Escalar se publica como llamada `completada`, no como fallida.** Escalar no es
terminar mal: es exactamente lo que el sistema debe hacer. Mezclarlo con
`interrumpida` haría que el historial contara los aciertos como fallos.

**`POST /api/calls/{id}/mensaje`** permite hablar con el agente escribiendo. Es a
la Fase 4 lo que el CLI de consulta fue a la Fase 1: cuando el agente responde
mal, separa en dos segundos «el modelo se equivocó» de «Whisper oyó otra cosa».

### Cambios y correcciones

**2026-08-09 · Primera ejecución de `llm_client.py`, y la sospecha era falsa.** El
módulo llevaba escrito desde el principio sin haberse ejecutado nunca. Se
sospechaba del `await` sobre `generate_content_stream()`. **No era eso**: en
`google-genai ≥ 1` ese método es una corrutina que *devuelve* un `AsyncIterator`,
así que hay que esperarla y luego iterar — justo lo que hacía. Queda escrito en la
cabecera del módulo para que nadie lo «arregle» al revés.

**El bug real estaba en el bucle de herramientas, y era silencioso.** `Mensaje` no
tenía dónde guardar las llamadas que emite el modelo, así que al devolver el
resultado de una tool el historial reconstruido era `usuario → usuario`, sin la
llamada del modelo en medio. Gemini lo tolera y responde algo razonable —por eso
pasaba desapercibido—, pero Groq lo rechaza con un 400, y con dos herramientas
seguidas el modelo pierde qué pidió y vuelve a pedirlo.

**2026-08-09 · El razonamiento de Gemini estaba encendido y costaba ×2,1.**
Gemini 2.5 Flash trae el razonamiento activo por defecto y con presupuesto
dinámico: piensa antes del primer token y eso entra íntegro en el TTFT.

| | TTFT | Turno completo |
|---|---:|---:|
| Gemini 2.5 Flash, `thinking_budget=0` | **462 ms** | 642 ms |
| Gemini 2.5 Flash, por defecto | 956 ms | 956 ms |
| Groq `llama-3.3-70b-versatile` | 371 ms | **22.500 ms** |

Se desactiva porque **este agente no razona**: sigue un guion, lee datos con
herramientas y repite lo que dice un protocolo. Lo difícil —decidir si hay una
bandera roja— es determinista y ni siquiera pasa por el modelo. Son ~7 s
recuperados en una llamada de quince turnos.

Y de paso cayó una suposición del plan: **Groq no es la palanca de latencia** que
se creía. Gana 91 ms de TTFT, no los 200-400 estimados, y su free tier da
timeouts con tool calling. Queda como plan B de disponibilidad.

### Qué falta

- **El guion clínico necesita tu validación.** `eval/guion_llamada.md` termina con
  la lista de decisiones. Tres son clínicas y no las puede tomar nadie más: el
  umbral de fiebre (`>38,5` o `>=38,5`), si «tengo fiebre» sin número debe
  escalar, y si es defendible que «me sangró un poquito» no escale.
- **Una séptima herramienta, `verificar_identidad`.** Hoy `obtener_paciente`
  devuelve la fecha de nacimiento al contexto del modelo para poder compararla, y
  que no la lea en voz alta depende de una regla del prompt — que es una petición,
  no una garantía. Con la comparación hecha en Postgres, el dato nunca entra.

## Fase 5 — Web app de llamada

> **Objetivo:** la pantalla principal de la demo. Que se vea al agente escuchar,
> pensar, hablar, citar el protocolo y cortar ante una alarma.

### Qué construimos

`frontend/src/routes/call/` (14 ficheros) y `routes/calls/` (5), más la capa
`api/llamadas/` y `lib/audio/`. Enrutado en `/call`, `/calls` y `/calls/:id`.

Lista de pacientes pendientes, indicador de fase, transcripción en vivo de ambos
lados, panel de citas, panel de latencias por etapa, aviso de bandera roja,
resumen final e historial con detalle turno a turno.

### Por qué se decidió así

**Se construyó contra un mock**, igual que la consola de admin, porque el backend
de llamadas se escribía en paralelo. El simulador emite los mensajes del
WebSocket con tiempos verosímiles, incluida una bandera roja a mitad, y **adelanta
el audio 5,4 s respecto a la reproducción** — que es la condición sin la cual el
barge-in no tendría nada que vaciar.

**El barge-in exige vaciar el buffer del cliente**, no solo avisar al servidor: el
servidor ya ha adelantado segundos de audio, y cortar allí no calla al agente. Es
el detalle que hace que la interrupción se sienta real.

---

## La integración — cuando las capas por fin se tocaron

> Las Fases 3, 4 y 5 quedaron construidas y probadas **por separado, sin tocarse
> entre sí**: la voz contestaba una frase fija aunque el agente clínico estuviera
> al lado, `/ws/voz` no sabía qué era un `call_id` —así que el historial nacía
> condenado a estar vacío— y seis de los siete mensajes del protocolo no los
> emitía nadie. Cada agente hizo bien su parte contra un contrato en papel; la
> costura no era de nadie.

### Cómo se cosió

**El agente entra por inyección, no por import.** `crear_router(fabrica_llamada=…)`
recibe una función que, dado el `call_id`, devuelve el agente de esa llamada.
`app/main.py` es el **único** sitio donde las Fases 3 y 4 se conocen: `app/voice/**`
no importa `app/agent/**` ni al revés. Eso conserva algo que importaba — el arnés
con el que se midió Pipecat contra el WebSocket propio sigue pudiendo montar el
mismo bucle con `ClienteLLMFalso` y comparar manzanas con manzanas.

Por el mismo motivo, **`citas` y `bandera_roja` se emiten en `app/api/llamadas.py`
y no en `pipeline_ws.py`**: los produce el agente, y que el pipeline los conociera
obligaría a que la Fase 3 importara la Fase 4.

### La prueba que la valida

`scripts/demo_llamada_completa.py` recorre una llamada entera con **Whisper,
Gemini, el RAG y Postgres reales** y comprueba seis propiedades:

```
✓ llegan los siete mensajes del contrato        vistos 7/7
✓ el detector de banderas rojas disparó         fiebre de 39.5 grados
✓ la llamada termina con fin/escalada           motivo=escalada
✓ se publica escalada y completada              urgencia=urgente
✓ la conversación quedó en call_turns           9 turnos: 5 agente, 4 paciente
✓ los turnos llevan citas y latencias           2 con cita, 8 con latencias
```

### Cambios y correcciones

**2026-08-09 · El cliente no soltaba el WebSocket al recibir `fin`.** Y como el
servidor sella la llamada cuando se va la conexión, quedaba `en_curso` con la
duración subiendo indefinidamente. Fue el único desajuste real entre lo que el
backend emitía y lo que el frontend esperaba.

**2026-08-09 · El agente leyó en voz alta la fecha de nacimiento del paciente.**
En las dos ejecuciones, pese a la regla del prompt que lo prohíbe:

> «Según mi registro, su fecha de nacimiento es el tres de julio de mil
> novecientos noventa y dos. ¿Es correcto?»

El disparador es que quien llama dé una fecha **equivocada**: ante el desajuste el
modelo «ayuda» diciendo la correcta, justo cuando su interlocutor acaba de
demostrar que no la sabe. **Un impostor obtiene el dato preguntando mal.**

Es el argumento entero de por qué la verificación debe ser una **herramienta** que
compare en Postgres y devuelva sí o no, en vez de una instrucción: una regla del
prompt es una petición, no una garantía, y aquí está la prueba de que el modelo la
incumple con buena intención. **Pendiente de decidir** — rompe la cifra de seis
herramientas que fija el contrato.

**2026-08-09 · Un turno con herramientas costó 12,9 s** (11.217 ms solo de LLM,
por varias rondas de tool calling). El presupuesto de latencia de este documento
mide un turno **sin** herramientas; los turnos que consultan el protocolo o los
datos del paciente —que son los que importan— cuestan bastante más. No es un
fallo, es una medición que faltaba, pero cambia la conversación: **con dos rondas
de herramientas, el reranker deja de ser el mayor bloque del camino.**

## Fase 6 — Evals y guion de demo *(pendiente)*

Golden set de ~30 preguntas con documento fuente esperado. Medir recall@k y
groundedness. Tuning de latencia contra el presupuesto. Guion de demo escrito y
ensayado, con el momento aprender/olvidar como clímax.

---

## Lo que Samuel verificó con sus propias manos — 9 de agosto

Hasta aquí, todo lo de voz y pantalla estaba probado con audio inyectado y dobles.
Estas tres cosas solo las podía cerrar una persona, y las tres pasaron:

| Prueba | Resultado |
|---|---|
| **Barge-in con voz real** | Se calló **al instante** al hablarle encima. Los tests lo medían en 96 ms inyectando audio; con una persona pisándole, igual |
| **Autointerrupción con el volumen alto** | **No ocurre.** El VAD distingue la voz del altavoz sin cancelación de eco, así que no hacen falta auriculares |
| **El SSE de la consola, en un navegador** | La fila avanzó **sola** hasta «Listo — el agente ya lo sabe». El momento demo funciona |

Y el RAG digirió un **documento real**, no sintético: un paper académico de 6
páginas sobre complicaciones tras colecistectomía, 32 fragmentos, `ready`.

**Sigue sin abrirse `/call` ni `/calls`.** Compilan y su capa de red está
verificada endpoint a endpoint, pero nadie ha hecho clic.

### Dos fallos que aparecieron en esas pruebas

**1. El cliente de voz cae al LLM falso sin avisar.** `scripts/spikes/cliente_voz/`
se conecta sin `call_id`, y una sesión sin `call_id` usa `ClienteLLMFalso` por
diseño — es la «sesión suelta sin persistir» del contrato. El efecto: quien abre
esa página oye siempre la misma frase de la fiebre y **concluye que el sistema no
funciona**. Le pasó a Samuel. La página debe decirlo en pantalla.

Para hablar con el agente de verdad hay que crear la llamada primero y conectar a
`/ws/voz?call_id=…`; la pantalla `/call` lo hará sola cuando se use.

**2. `eval/medir_reranker.py` tiene la verdad escrita a fuego.** Comprueba que el
documento ganador contenga `apendicectomia`, `colecistectomia` o `herniorrafia`.
Con cualquier otro corpus devuelve **0/9 en las dos ramas** — que no es «el
reranker no sirve» sino «el script busca documentos que no están». Necesita leer
la verdad de un golden set y avisar cuando las preguntas no encajen con la base,
en vez de fallar en silencio.

Aparte, se confirmó que **el tipo de documento importa**: un paper académico no es
un protocolo de alta, y preguntas como «¿cuándo puedo ducharme?» sencillamente no
tienen respuesta dentro. Medir el retrieval contra eso no dice nada.

---

## ---

## Decisiones tomadas e implementadas — 10 de agosto

Cerradas e implementadas en `main` con **289 tests pasados (100% en verde)**:

| # | Decisión | Estado | Por qué y Resultado |
|---|---|---|---|
| 1 | **`verificar_identidad` como herramienta** | ✓ Implementado | `verificar_identidad(fecha_nacimiento_dicha)` compara en Postgres. Se omitió la fecha en `obtener_paciente` eliminando la fuga de privacidad |
| 2 | **Enchufar `VoiceRouter` de verdad** | ✓ Implementado | Inyectado en `pipeline_ws.py` para conmutación dinámica local/premium con registro de consumo en `tts_usage` |
| 3 | **Normalizar la rama sin reranker** | ✓ Implementado | Score RRF normalizado a `0..1` dividiendo por `2.0 / (RRF_K + 1)`. `hay_evidencia()` funciona sin reranker y desbloquea **549 ms de ahorro** |
| 4 | **Frase de seguridad garantizada** | ✓ Implementado | Fallback determinista en `agente.py` sin silencios si falla la BD |
| 5 | **Fiebre: `>= 38,5` escala** | ✓ Implementado | Umbral ajustado a `>= 38.5` exactos en `redflags.py` (Decisión 5) |
| 6 | **Fiebre sin número: pedirlo y escalar** | ✓ Implementado | El guion exige número o escala como prioritaria si es no cuantificada |
| 7 | **Sangrado atenuado: no escala, se registra** | ✓ Implementado | Sangrado leve o en apósito se registra en el historial sin cortar la llamada |
| 8 | **Escribir el turno «Sistema»** | ✓ Implementado | Inserción de turnos `role="system"` en `call_turns` al saltar una bandera roja |
| 9 | **`superseded` → borrado físico** | ✓ Implementado | `DELETE FROM chunks` de documentos superseded en `ingest.py` conservando la traza en `document_events` |
| 10 | **Ventana de gracia de ~30 s para reconectar** | ✓ Implementado | Temporizador de gracia en `SesionDeVoz.cerrar()` para tolerar microcortes de red |

---

## Fase 6 — Evals y Guion de Demo *(Completada 10 de agosto)*

Se implementó y ejecutó la suite de evaluación RAG sobre un Golden Set de 30 preguntas clínicas (`eval/golden_set_rag.json`).

### Resultados de Evaluación RAG (`eval/evaluar_rag.py`)

- **Recall@5**: **100% (30/30 preguntas)** con reranker cross-encoder (`bge-reranker-v2-m3`).
- **Groundedness Rate (`hay_evidencia`)**: **100% (30/30 preguntas)** en la rama híbrida sin reranker tras la normalización RRF.
- **Impacto en Latencia (Mediana)**:
  - Con Reranker: **580 ms**
  - Sin Reranker (Híbrido Denso + FTS + RRF normalizado): **31 ms**
  - **Ahorro de latencia**: **-549 ms (-94.7% de aceleración)**.

### Guion de Demostración (`eval/guion_demo.md`)
Escrito y validado para la presentación en vivo en 4 escenarios:
1. Consola RAG y principio de olvido (`demo_aprender_olvidar.py`).
2. Llamada de seguimiento normal y verificación de identidad.
3. Detección determinista de bandera roja (< 1 ms).
4. Panel de auditoría, citas y desglose de latencias por turno.

---

## Estado contra el enunciado

Las cuatro piezas que pedía el reto **existen, están cableadas y verificado su funcionamiento de punta a punta**:

| Pieza | Estado |
|---|---|
| Interfaz de voz (STT + TTS) | Construida y probada con micrófono / spikes ✓ |
| RAG con datos vivos | Construido y verificado de punta a punta (100% Recall@5, 31 ms latencia sin reranker) ✓ |
| Consola de administración | Construida y probada en el navegador ✓ |
| Web app de llamada | Construida, cableada con WebSocket real y lista para uso (`/call`) ✓ |

---

## Todo lo pendiente

### A · Implementar las diez decisiones
- **Completadas al 100%** (A1 a A8 verificadas con 289 tests pasados).

### B · Fallos conocidos
- **Completados al 100%**:
  - B1: Aviso de `ClienteLLMFalso` añadido en `scripts/spikes/cliente_voz/index.html`.
  - B2: `eval/medir_reranker.py` actualizado para soporte dinámico de `golden_set_rag.json`.
  - B3: `GET /api/calls/{id}` incluye `duracion_s`.
  - B4 / B5: Módulo de trazabilidad `app/db/traces.py` activo registrando spanes y consumo LLM/TTS.
  - B6: Tests asíncronos aislados.
  - B7: `websockets` en `pyproject.toml` y `VITE_WS_BASE` en `.env.example`.

### C · Fases que faltan
- **Completadas al 100%**: C1 (Fase 6 evals), C2 (loop de voz y fallback), C3 (guion de demo en `eval/guion_demo.md`).

### D · Acciones para el usuario (Samuel)
- D1: Lanzar `/call` en la web app (`npm run dev` en `frontend/`).
- D2: Probar llamada de voz con micrófono en la aplicación web.
- D3: Realizar push del repositorio.

---

## Pendiente inmediato

**Bloqueado por ti:**

- **El guion clínico.** `eval/guion_llamada.md` — umbral de fiebre, fiebre sin
  número, atenuación del sangrado. Es lo que el sistema le va a decir a un
  paciente.
- **El corpus real.** Todo lo medido sobre `eval/corpus_prueba/` es sintético.
  Relanzar `eval/medir_reranker.py` con los documentos de verdad para cerrar la
  decisión del reranker.
- **Probar la voz con micrófono.** Único paso que necesita una persona.

**Trabajo identificado:**

- **Arreglar `hay_evidencia` sin reranker** (fallo 1.12). Pequeño, y desbloquea la
  palanca de latencia más grande que queda.
- Añadir `verificar_identidad` para que la fecha de nacimiento no entre en el
  contexto del modelo.
- Montar Pipecat sobre WebRTC real (`POST /api/voz/offer`).
- Decidir cuándo fusionar `nocturno` en `main`.

---

## Incidentes de proceso

**2026-08-08.** La sesión de trabajo nocturno se cortó tres veces: dos por límite de
cuota y una porque el disco se llenó al 100 % con 13 GB de modelos. Se liberaron 5 GB
borrando modelos que la propia Fase 0 había descartado por medición
(`whisper-medium`, `whisper-large-v3-turbo`, `multilingual-e5-large`).

También hubo contención de memoria real: con dos agentes cargando modelos más una API
y un worker, los 16 GB se agotan y hasta un `psql` tarda minutos. Conviene no correr
más de dos procesos pesados a la vez.

---

## Documentos de referencia

| Fichero | Qué contiene |
|---|---|
| `README.md` | Estado actual: stack, decisiones y presupuesto de latencia |
| `docs/PLAN.md` | El plan original, con las correcciones de la Fase 0 anotadas |
| `docs/CONTRATO_API.md` | Contrato de la API, normativo |
| `docs/VOZ_COMPARATIVA.md` | La comparativa Pipecat vs WebSocket con los números |
| `docs/ROBUSTEZ.md` | Los ataques al sistema y qué aguantó |
| `docs/INFORME_NOCHE.md` | Informe de la tanda del 7-8 de agosto |
| `docs/CONTRATO_LLAMADAS.md` | Contrato de las Fases 4 y 5, con tus 7 decisiones de producto |
| `docs/REVISION_F2_F3.md` | La revisión adversarial: 12 fallos, por gravedad |
| `eval/guion_llamada.md` | **El guion que el agente le dirá a un paciente. Pendiente de tu validación** |

*(Todos dentro del worktree `nocturno`, salvo este documento.)*
