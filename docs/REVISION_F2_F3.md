# Revisión crítica de las Fases 2 y 3

Fases 2 (consola de documentos) y 3 (bucle de voz), construidas en paralelo por
agentes que no se hablaban, cada uno contra `docs/CONTRATO_API.md`. Esto es lo
que se rompió por esa costura y lo que aguantó.

Método: cada fallo tiene un test que lo demuestra, escrito **antes** del arreglo
y comprobado en rojo con el código roto. Los que no se pudieron arreglar dentro
del alcance quedan marcados como tales, no escondidos.

Ámbito revisado: `app/api/*`, `app/voice/*`, `frontend/src/**` (salvo lo de las
Fases 4 y 5, que otros dos agentes estaban escribiendo mientras esto se
revisaba). Fuera de ámbito: `app/agent/**`, `app/api/llamadas.py`,
`frontend/src/routes/call*/**`, `frontend/src/api/llamadas/**`, `app/rag/**`.

---

## 1. Fallos encontrados, por gravedad

### 1.1 · ALTA · Un turno de voz que falla mataba la llamada entera, un turno después

`SesionVoz._responder()` corre en una `asyncio.Task`. Si lanzaba —Whisper caído,
el motor de TTS sin modelo, o el navegador que se va a media síntesis y
`ws.send_bytes` falla— la excepción se quedaba **dormida dentro de la tarea**:
sin log, sin evento al cliente, sin rastro.

El único sitio que hacía `await` sobre esa tarea era `_cancelar_turno()`, y
`contextlib.suppress(asyncio.CancelledError)` no suprime un `RuntimeError`. O
sea que la excepción resucitaba:

- en el **turno siguiente** (`_dejo_de_hablar` → `_cancelar_turno`), matando el
  WebSocket con un error de la pregunta anterior; o
- en **`cerrar()`**, que es el `finally` del endpoint — y entonces
  `motor.cerrar()` no llegaba a ejecutarse, dejando un cliente HTTP de
  ElevenLabs/Cartesia colgado por cada llamada que terminara mal.

Reproducido: el segundo turno moría con `RuntimeError: Whisper se cayó`.

**Tests** `test_voz_robustez.py::test_un_turno_que_revienta_no_mata_la_sesion`,
`::test_un_turno_que_revienta_queda_registrado`,
`::test_cerrar_funciona_aunque_el_ultimo_turno_fallara`.

**Arreglo** (`app/voice/pipeline_ws.py`): `_responder` captura, registra con
`log.exception` y anota el motivo en `MetricasTurno.error` (campo nuevo);
`_cancelar_turno` pone `self._tarea = None` **antes** del `await`, recoge el
resultado y distingue la cancelación de la tarea de la cancelación propia
(`if not tarea.cancelled(): raise`), que era otro camino a un cierre a medias.

Se descartó avisar al cliente con un mensaje de control nuevo: los siete tipos de
`/ws/voz` están fijados en el contrato y en la página de prueba, y un octavo que
nadie maneja no arregla nada.

### 1.2 · ALTA · Un trozo de audio de longitud impar cerraba el WebSocket

`vad.procesar_pcm16` hace `np.frombuffer(datos, dtype="<i2")`, que lanza
`ValueError: buffer size must be a multiple of element size` con cualquier
longitud impar. Verificado extremo a extremo: `ws.send_bytes(b"\x01\x02\x03")`
cierra la conexión con una traza en el servidor y nada útil en el cliente.

Peor que la excepción: `self._buffer += pcm16` ya había ocurrido, así que aunque
se capturara el error el buffer quedaba desplazado un byte y **todo el audio
posterior sonaría a ruido blanco**.

**Tests** `::test_un_trozo_de_longitud_impar_no_tumba_la_llamada`,
`::test_basura_binaria_por_el_websocket_no_cierra_el_socket`,
`::test_un_trozo_vacio_no_hace_nada`.

**Arreglo**: `recibir_audio()` valida antes de tocar nada — descarta el byte
suelto, lo registra y sigue. Un trozo partido por el transporte no puede costar
la llamada. (Texto en vez de binario ya se ignoraba correctamente.)

### 1.3 · MEDIA-ALTA · El SSE no propagaba `pages`: la consola mentía en una columna

`GET /api/documents/stream` mandaba `status`, `chunks_count`, `embedded_count` y
`error`, pero no `pages`. La consola pinta una columna «Páginas» y la rellena
fusionando el evento sobre la fila (`useDocumentos.aplicarEvento` lee
`evento.pages`), así que **todo PDF ingerido en vivo se quedaba con «—» hasta que
alguien pulsara «Recargar»**.

`pages` es el único campo del `Documento` que cambia durante la ingesta y no se
conoce al subir: lo escribe el worker al promover a `ready`, cuando el parser ya
abrió el PDF.

Y es exactamente el patrón que el encargo predecía: **el mock del frontend SÍ
manda `pages`** en su evento simulado. Con `VITE_MOCK=1` la columna se rellena
sola; contra el backend real, no. La pantalla se probó entera contra el mock.

**Test** `test_api_eventos.py::test_el_evento_trae_las_paginas` (rojo con
`KeyError: 'pages'`).

**Arreglo** (`app/api/eventos.py`): `pages` entra en `_visible()`, en `_evento()`,
en `_SQL_ESTADO` y en la consulta de reanudación. Verificado extremo a extremo
con el código real del frontend contra el backend real (§4).

### 1.4 · MEDIA-ALTA · La primera consulta al RAG tarda 13,3 s y el cliente corta a los 15

Medido contra el backend real, en frío: `POST /api/rag/query` devuelve
`ms.embedding = 13333` la primera vez (carga de bge-m3), y **37 ms** la segunda.
El límite de `pedirJson` era 15 s para todo.

Cabía por los pelos. Deja de caber en cuanto el reranker también tenga que
cargarse, cosa que ocurre solo cuando hay candidatos — o sea justo cuando hay
documentos que enseñar. El síntoma sería «El servidor tardó demasiado en
responder. Puede estar procesando o caído» en el minuto exacto de la demo.

Y el backend ya publica el dato que lo explica: `GET /api/health` devuelve
`modelos_listos`. **El frontend ni lo declaraba en su tipo `Salud`.**

**Arreglo** (frontend): `pedirJson` acepta `tiempoLimiteMs`; `consultarRag` usa
60 s con el porqué al lado. `Salud.modelos_listos` declarado, y «Probar conexión»
lo dice: «los modelos del RAG ya están cargados» / «todavía se están cargando: la
primera consulta tardará unos segundos». Un backend que no mande el campo se
trata como listo.

### 1.5 · MEDIA · Silero se recargaba entero en cada llamada

`crear_router()` documenta que el STT y el TTS se crean una sola vez «porque
cargar Kokoro o Whisper en el `accept()` del WebSocket añadiría segundos al
inicio de cada llamada». El VAD es el tercer modelo y se le escapó: `SesionVoz`
construye un `DetectorTurnos`, que construía una `onnxruntime.InferenceSession`
propia. Contado: **una sesión de ONNX nueva por conexión** (4 conexiones → 4
cargas), ~27 ms y una arena de memoria propia cada una, en una máquina de 16 GB
que ya sostiene Whisper, bge-m3 y el reranker.

**Test** `::test_el_modelo_de_vad_no_se_recarga_por_conexion` (contaba 4).

**Arreglo** (`app/voice/vad.py`): caché de sesiones por ruta de modelo, con
cerrojo solo en la construcción. Es seguro y no por suerte: el estado del LSTM
viaja explícitamente como entrada y salida de `run()` (`state`), no vive dentro
de la sesión, así que dos llamadas simultáneas conservan cada una su memoria.

### 1.6 · MEDIA · Vuelve «graciaspor contármelo», por otra puerta

`_hablar()` recorta el buffer pendiente con `pendiente.rfind(cola)` precisamente
para conservar el espacio final del texto crudo — hay un test de regresión de
2023 sobre eso. Pero `cola` sale de `dividir_en_frases()`, que **fusiona las
muletillas con un espacio simple**: si el LLM escribió un `\n` ahí, `cola` deja
de ser un substring literal, `rfind` devuelve `-1`, y el camino de respaldo
(`pendiente = cola`) reintroduce el bug exacto que el `rfind` existía para
evitar, porque `cola` viene con `strip()`.

Reproducido con `"Ahora dígame cómo se encuentra. Sí.\nBien. Muchas gracias por
su tiempo."` troceado de 14 en 14 caracteres: el TTS recibe **«Bien.Muchas
gracias»** y lo pronuncia como una sola palabra. Un LLM escribe saltos de línea
constantemente.

**Tests** `::test_el_troceado_normaliza_los_saltos_de_linea` (documenta la causa)
y `::test_el_troceado_no_pega_palabras_con_saltos_de_linea` (el fallo).

**Arreglo**: el camino de respaldo devuelve el blanco final del texto crudo
(`cola + pendiente[len(pendiente.rstrip()):]`), que era lo único que se perdía.

### 1.7 · MEDIA · Buffers sin techo en una llamada larga

Dos, los dos crecen mientras la llamada dure:

- **`SesionVoz._buffer`**: `_recortar_a_preroll()` solo recorta mientras el
  paciente **no** habla —correcto, hay que conservar el turno para Whisper— así
  que con voz sostenida crece a 32 kB/s sin tope. Medido: 40 repeticiones del
  clip de prueba dejan **92 s de audio (2,9 MB)** y sigue subiendo. Un micrófono
  abierto en una sala con gente no genera nunca los 640 ms de silencio que
  cierran el turno. Y lo caro no es la RAM: cuando por fin llegue el silencio,
  ese buffer entero se escribe a un WAV y se le pasa a Whisper.
- **`Metricas.latencias_ventana_ms`**: un `float` por ventana de 32 ms, 31 por
  segundo, en una lista que solo crecía.

**Tests** `::test_el_buffer_del_turno_tiene_tope`,
`::test_las_metricas_del_vad_no_crecen_sin_fin`.

**Arreglo**: `MS_TURNO_MAXIMO = 60_000` (se descarta lo más viejo; **no** se
fuerza un fin de turno artificial, que haría al agente interrumpir a un paciente
que está contando algo largo) y `deque(maxlen=4096)` para las latencias, con el
contador total intacto.

### 1.8 · MEDIA · `except Exception: pass` en la contabilidad de TTS — hermano del de `olvidar_documento()`

`voice_mode.VoiceRouter._anotar()`. La decisión de no tumbar la llamada por un
fallo de contabilidad es correcta y se mantiene; lo que estaba mal era el `pass` a
secas. `tts_usage` es lo que el administrador mira para decidir si sigue en
premium: si deja de escribirse, el panel dice «0 caracteres gastados» y la
factura dice otra cosa. Ahora registra con `log.warning`.

Segundo `except` en el mismo fichero, también mudo: la **degradación de premium a
local**. Si la clave de ElevenLabs caducó, la demo entera suena en Kokoro y nadie
se entera. Ahora avisa.

Barrido completo de `except`/`suppress` en `app/api/**` y `app/voice/**`: no
quedan más silenciosos. El de `WebSocketDisconnect: pass` en `pipeline_ws` es
correcto (es el cierre normal).

### 1.9 · MEDIA-BAJA · `total` mentía en una página vacía

`count(*) OVER ()` viaja dentro de las filas, así que un `offset` pasado del
final devolvía `total: 0` habiendo tres documentos. El contrato dice que `total`
cuenta lo que casa con el filtro, no lo que trae la página.

**Test** `test_api_documentos.py::test_el_total_sobrevive_a_una_pagina_vacia`.
**Arreglo**: consulta de conteo aparte, y solo en ese caso.

### 1.10 · BAJA · Dos cargas de Kokoro si entran dos llamadas a la vez

`KokoroTTS._cargar()` era un perezoso sin cerrojo, y `sintetizar` va a un hilo
con `asyncio.to_thread`: dos llamadas simultáneas ejecutan `_cargar()` en
paralelo de verdad, las dos ven `_pipe is None` y construyen un `KPipeline` cada
una. Dos copias de Kokoro-82M en una máquina de 16 GB. Arreglado con
`threading.Lock` y doble comprobación.

### 1.11 · BAJA · El sample rate equivocado no dejaba ni un rastro

El contrato fija 16 kHz de subida y nada lo verificaba. Un `AudioContext` sin
`sampleRate` fijado manda 48 kHz: el VAD ve el triple de audio del que hay, los
umbrales de 96 y 640 ms se cumplen en un tercio del tiempo real, y Whisper
transcribe una grabación acelerada 3x. **No hay excepción, no hay error**, y el
síntoma que llega es «el agente no me entiende».

No se remuestrea ni se rechaza —el servidor no puede distinguir «otra frecuencia»
de «una ráfaga de audio grabado», que es legítima— pero ahora se mide el ritmo
sobre una ventana de 4 s y se denuncia una vez en el log si supera 1,5x.
**Tests** `::test_el_sample_rate_equivocado_se_denuncia` y
`::test_el_ritmo_normal_no_genera_ruido_en_el_log` (una alarma que salta siempre
es peor que no tenerla).

### 1.12 · Pendiente, fuera de alcance · `hay_evidencia` es siempre `False` sin reranker

**Es el fallo con peor relación gravedad/tamaño de arreglo de toda la revisión, y
no está arreglado porque `app/rag/rerank.py` no entra en este encargo.**

Las dos ramas de `reordenar()` devuelven escalas distintas y `hay_evidencia`
compara las dos contra el mismo `MIN_RELEVANCE_SCORE = 0.35`:

| rama | escala del `score` | ¿pasa 0,35? |
|---|---|---|
| con reranker | cross-encoder tras sigmoide, 0..1 | sí, discrimina bien |
| **sin reranker** | RRF, **máximo teórico 1/61 + 1/61 = 0,0328** | **nunca** |

Con `RERANK_ENABLED=0`, el agente responde «no tengo esa información» a preguntas
cuyo protocolo tiene delante, y la consola pinta el aviso ámbar «Sin evidencia
suficiente» sobre una lista de fragmentos perfectamente buenos.

No lo ve ningún test porque todos los que tocan grounding sustituyen `reordenar`.
Y es un interruptor que se va a tocar: `app/api/rag.py` lo anuncia («se apaga con
RERANK_ENABLED sin tocar nada más») y el reranker está medido en 585 ms, el mayor
bloque del camino de voz.

Queda como `test_api_rag.py::test_hay_evidencia_sigue_significando_algo_sin_reranker`
con `xfail(strict=True)`: la batería sigue verde mientras el fallo exista y se
pondrá roja el día que alguien lo arregle, avisando de que hay que quitar el
marcador. Ver §5.

---

## 2. Desajustes entre contrato, backend y frontend

Anotados en `docs/CONTRATO_API.md` §«Revisión de las Fases 2 y 3».

| # | Qué | Estado |
|---|---|---|
| 1 | El evento SSE no traía `pages`; **el mock del frontend sí lo mandaba** | Arreglado (§1.3) y fijado en el contrato |
| 2 | `chunks_preview` usa `contenido`; el contrato no nombraba el campo, el mock mandaba `content` y el frontend aceptaba cuatro nombres | Fijado en el contrato. El normalizador se deja: degrada en vez de romper |
| 3 | `GET /api/health` devuelve `modelos_listos`, sin documentar y sin consumir | Documentado y ya consumido (§1.4) |
| 4 | Tres códigos de error de más (`no_encontrado`, `metodo_no_permitido`, `peticion_invalida`). `errores.py` decía que estaban anotados en el contrato: **no lo estaban** | Documentados |
| 5 | `total` con página vacía | Arreglado (§1.9) |
| 6 | El mock manda `filename`, `title` y `updated_at` en el evento SSE; el backend real, no | **No es un fallo**: `useDocumentos` no se fía del evento para esos campos y recarga la lista si el documento le es desconocido. Se documenta para que nadie «arregle» el backend copiando al mock |
| 7 | El comentario de `cliente.ts` decía que el contrato «no numera los eventos ni admite `Last-Event-ID`». Es falso: sí lo hace | Comentario corregido, comportamiento intacto (la recarga completa recupera además lo que el flujo no publica) |

Diferencias de comportamiento mock/real que **no** son fallos y conviene conocer
antes de ensayar con `VITE_MOCK=1`: el mock inventa un `title` a partir del
nombre del archivo (el backend deja `null`, y la UI cae a `filename`), borra
físicamente la versión reemplazada tras 2,5 s (el backend la deja en
`superseded` a propósito, para conservar el rastro clínico — decisión pendiente,
§5) y devuelve `score` de solape léxico, no del cross-encoder.

---

## 3. Lo que se revisó y está BIEN

Acotado a propósito: esto ya no hay que volver a mirarlo.

**El cierre del SSE en el servidor. Verificado dos veces y de dos formas.** Con
la batería (`test_el_cierre_del_cliente_libera_todo`, `test_varios_flujos_a_la_vez_no_agotan_el_pool`)
y a mano contra un servidor real: cinco flujos abiertos, los cinco clientes
`kill -9`, y las cinco conexiones del pool quedaron con su última actividad
congelada en el instante del corte. El generador se entera y para. El sondeo cada
500 ms coge una conexión y la devuelve; un flujo en reposo cuesta cero
conexiones, que es lo que descartaba LISTEN/NOTIFY.

**La reconexión del frontend. Ejercitada contra un servidor SSE que se puede
tirar a voluntad**, no leída:
- socket destruido a lo bruto → `reconectando` y reconexión sola;
- servidor caído 9 s → **3 intentos**, no uno por segundo: el backoff funciona y
  el contador sube (llegó a 4);
- servidor de vuelta → se recupera sin tocar nada y el contador vuelve a 0;
- `cerrar()` → estado `cerrado` y **cero aperturas** en los 6 s siguientes, aun
  destruyendo el socket a propósito. No hay `EventSource` huérfano, así que
  desmontar el componente no deja nada vivo (`useFlujoDocumentos` llama a
  `cerrar()` en su cleanup, y los manejadores viven en una ref para que la
  conexión no se reabra en cada render).

**El latido.** Llega, lleva marca de tiempo, y ahora tiene test
(`test_llega_el_latido`, con `LATIDO_S` acortado). Importa por el otro lado: el
vigilante de silencio del frontend fuerza la reconexión a los 45 s sin recibir
nada, y sin latido un flujo correcto pero callado se reconectaría en bucle.
**El vigilante también está verificado en vivo** contra un servidor que acepta la
conexión y se calla: saltó a los 45 s.

**La reanudación con `Last-Event-ID`**, incluidos los borrados ocurridos durante
el corte (salen de `document_events`, que no tiene FK a propósito), y la
degradación a instantánea completa con una marca corrupta.

**La forma de los errores, endpoint por endpoint, contra el servidor real**: 401
sin token, 404 de ruta, 405 de método, 415 de formato, 400 de archivo vacío, 409
de duplicado en curso, 422 de id no-UUID y de estado inválido. Todos con
`{"error":{"codigo","mensaje"}}` y mensaje en español mostrable. Ninguno devuelve
el `{"detail":…}` de FastAPI.

**El camino completo de la consola contra el backend real** (§4): `salud`,
`listar`, `subir` con barra de progreso, `detalle`, `consultarRag`, `eliminar` y
el flujo SSE, ejecutando el código real de `src/api/`, no una reimplementación.

**El barge-in y la máquina de turnos** siguen verdes tras los cambios, incluidos
los dos tests de regresión históricos y la medición del umbral de fin de turno.

**Dos clientes simultáneos en `/ws/voz` no se pisan**: el STT y el motor de TTS
se comparten a propósito (es lo que ahorra la carga por llamada) y el estado
conversacional no. Con test (`::test_dos_clientes_a_la_vez_no_comparten_turnos`).

**`dividir_en_frases()`** aguantó todo lo que se le tiró: abreviaturas (`Dr.`,
`etc.`), iniciales (`J. Villalba`), decimales (`38.5 grados`), unidades sin punto
(`Tome 500 mg. Después…` sí parte, correctamente), listas numeradas, elipsis,
cadena vacía y cadena de solo espacios. El único fallo estaba fuera, en cómo
`_hablar()` reconstruye el buffer (§1.6).

**La autenticación**: comparación en tiempo constante en las dos variantes
(cabecera y query del SSE), y el token del SSE se valida igual de estricto.

---

## 4. Cómo se probó la consola contra el backend real

El encargo pedía comprobar que la pantalla funciona sin el mock. Se hizo
ejecutando **el código real del frontend** (`src/api/real.ts`, `http.ts`,
`normalizar.ts`, `flujo.ts`) fuera del navegador: Vite compila los `.ts` y
resuelve el alias `@/`, y solo se rellenan los globales que ese código toca
(`localStorage`, `XMLHttpRequest` sobre `fetch`, y el `EventSource` que trae Node
con `--experimental-eventsource`). Backend propio en el 8021, base `postop_t2`,
`PRECARGAR_MODELOS=0`.

Vale más que abrir la pantalla a ojo: comprueba tipos y nombres de campo reales,
no que «se vea bien». Fue lo que cazó §1.3 y §1.4. El arnés está en el
scratchpad de la sesión (`consola_real.mjs`, `flujo_caidas.mjs`,
`vigilante.mjs`); si merece la pena conservarlo, su sitio sería
`frontend/scripts/`, pero eso ya es construir, no revisar.

Resultado: todo verde tras los arreglos, incluido `pages=7` llegando por el flujo
hasta el objeto que la tabla pinta.

---

## 5. Qué necesita decidir Samuel

**1. `RERANK_ENABLED=0` está roto (§1.12), y es el escape de latencia del
proyecto.** Hay que elegir una:

- **(a) Normalizar la rama sin reranker.** RRF llega como mucho a 0,0328; basta
  con dividir por ese máximo teórico para devolver un 0..1 comparable. Tres
  líneas en `reordenar()`. Rápido, y el umbral 0,35 vuelve a significar algo.
- **(b) Un umbral por rama.** `MIN_RELEVANCE_SCORE` para el cross-encoder y otro
  para RRF. Más honesto (son magnitudes distintas), dos constantes que calibrar.
- **(c) Declarar que el interruptor no existe** y quitarlo de `.env`, del README
  y del comentario de `app/api/rag.py`.

Lo urgente no es cuál, es que ahora mismo el interruptor **parece** que funciona.

**2. La versión reemplazada: `superseded` para siempre, o borrado físico.** El
contrato dice «pasa a `superseded` y luego se borra»; el worker hace lo primero y
no lo segundo, deliberadamente, para conservar el rastro de qué versión estuvo
activa. El mock del frontend sí la borra a los 2,5 s. Son tres comportamientos
distintos para la misma frase del enunciado y la consola enseña uno de ellos.
(Ya estaba anotado como pendiente en el contrato; sigue pendiente.)

**3. `MS_TURNO_MAXIMO = 60 s`** (§1.7) es un número que me he inventado con
criterio, no medido. Un minuto de monólogo sin una sola pausa de 640 ms es mucho
para un seguimiento telefónico, pero si en las pruebas con micrófono aparece un
paciente que lo supera, el efecto es que se pierde el principio de lo que dijo.

**4. `frontend/.env.local` no existe**, así que la consola ya apunta al backend
real por defecto. Bien para mañana; conviene saberlo antes de ensayar creyendo
que `VITE_MOCK=1` sigue activo.

**5. `npx tsc --noEmit` en la raíz del frontend no comprueba nada.**
`tsconfig.json` tiene `"files": []` y solo referencias, así que `--noEmit` no
construye los proyectos referenciados y **siempre sale verde**. El comando que sí
comprueba es `npx tsc -b` (lo que hace `npm run build`). Merece la pena corregir
el checklist de pre-vuelo, porque ahora mismo da un verde falso.

**6. Dos servidores míos siguen levantados** en 127.0.0.1:8020 y :8021 (base
`postop_t2`, 12 MB y 189 MB de RSS). No se han matado por la restricción de no
tocar procesos uvicorn con la prueba de micrófono en marcha; se pueden cerrar
cuando quieras. El del 8000 con `postop_wt` no se tocó, y `postop_wt` sigue con
su documento intacto.

---

## 6. Lo que no se pudo comprobar

- **La voz con audio real de micrófono.** Todo lo de la Fase 3 se ejercitó con
  los clips de `scripts/spikes/audio/` y dobles deterministas; no se cargaron
  Whisper, Kokoro ni el reranker por la restricción de memoria. El
  comportamiento del VAD con la voz de Samuel, su micrófono y su sala es
  exactamente lo que su prueba del 8000 sí cubre.
- **La calidad del reranker y del retrieval.** Fuera de ámbito (`app/rag/**`) y
  además es lo que miden los evals de la Fase 6. La única afirmación que se hace
  aquí sobre el reranker es de escalas (§1.12), y no requiere cargarlo.
- **Presión de memoria con ingesta grande y llamada de voz a la vez** — ya
  figuraba como pendiente en `docs/ROBUSTEZ.md` y sigue igual.
- **La consola en un navegador de verdad.** Se ejercitó su capa de red real
  (§4), no su render. `npm run build` compila, pero nadie ha hecho clic.
- **`npm run build` no pasa ahora mismo**, y no por esta revisión: falla en
  `src/api/llamadas/**`, de la Fase 5, que otro agente estaba escribiendo
  mientras esto se revisaba. Comprobado que el error no toca ningún fichero de
  las Fases 2 y 3.
