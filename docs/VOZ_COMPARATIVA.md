# Pipecat contra WebSocket propio — decidido midiendo

El README daba Pipecat por decidido («Lo difícil de la voz en tiempo real no es
STT ni TTS: es el barge-in, la detección de fin de turno, el resampleo y el
jitter. Escribir eso a mano cuesta días»). Samuel pidió construir **las dos** y
elegir con números. Están construidas, medidas y aquí está el veredicto.

> **Recomendación: Opción A, Pipecat.**
> El número que decide: **1.596 ms contra 1.975 ms hasta el primer audio** — 379
> ms menos con el mismo presupuesto de fin de turno, el mismo STT, el mismo LLM
> y el mismo TTS. Y con **la mitad del código** (171 líneas contra 312).
> Con una condición no negociable: **el STT se queda siendo el nuestro.** El
> servicio Whisper de Pipecat transcribe *«appendicitomía»*.
> El razonamiento completo está al final.

Todo lo de abajo es reproducible:

```bash
cd backend && uv run python ../scripts/spikes/spike_voz.py todo --tts say -n 3
```

---

## Cómo se ha medido, y por qué así

No hay micrófono ni humano despierto, así que todo se mide **inyectando WAV
conocidos a ritmo real** (trozos de 20 ms contra un reloj absoluto, como los
manda un `AudioWorklet`) y fechando lo que sale. Acelerar la inyección habría
falseado justo lo que se quiere medir: el fin de turno, el barge-in y el ritmo de
reproducción.

Cuatro decisiones de método que hacen comparables las dos columnas:

**1. Mismo presupuesto de fin de turno en las dos.** 640 ms. En la Opción B es un
umbral único; en Pipecat son dos sumandos (`stop_secs` del VAD = 200 ms más
`user_speech_timeout` del `UserTurnProcessor` = 440 ms). Sin igualarlo se estaría
comparando dos *ajustes*, no dos orquestadores.

**2. Mismos modelos y mismo LLM.** Whisper `small` con prompt clínico, `say`
Mónica como TTS, y `ClienteLLMFalso` con TTFT fijo de 400 ms. Lo único distinto
entre columnas es el orquestador.

**3. Precalentamiento.** Sin él la primera medición sale con 2.615 ms de STT y
943 ms de TTS: eso es cargar el modelo y arrancar el subproceso, un coste que en
producción se paga una vez al levantar el servicio, no en cada turno.

**4. El barge-in se mide donde se oye, no donde se corta.** Las dos opciones
adelantan audio al cliente (medido: ~5 s encolados). Cortar la síntesis en el
servidor no calla al agente si el navegador tiene 5 s en el buffer. Por eso la
métrica que manda es *«cuándo deja de sonar»*, modelada con el mismo
`ReproductorSimulado` en ambas columnas.

**El LLM es falso.** `GEMINI_API_KEY` sigue vacía en `.env` (comprobado: longitud
0, igual que `GROQ_API_KEY`). El TTFT de 400 ms es el centro del rango que el
README estima para Gemini 2.5 Flash. El intercambio por el real es una línea —
`crear_cliente()` en vez de `ClienteLLMFalso()`, misma interfaz — y **cuando
aparezca la clave hay que repetir esta medición**, porque 400 ms es el único
número de la tabla que no está medido.

---

## 1. Latencia — la tabla

Mediana de 3 ejecuciones, MacBook Air M4. Clip: *«Me hicieron una apendicectomía
hace tres días y tengo la herida un poco roja»* (3,5 s) + 1,5 s de silencio.

| Etapa | A · Pipecat | B · WebSocket propio | Nota |
|---|---:|---:|---|
| Detección de fin de turno | **626 ms** | **640 ms** | mismo presupuesto configurado (640 ms) |
| Escritura del WAV temporal | 1,2 ms | 1,1 ms | peaje de que `stt.py` solo acepte rutas |
| STT — Whisper `small` + prompt | 391 ms | 392 ms | **en A no cuenta: va en paralelo** |
| LLM TTFT | 401 ms | 402 ms | simulado, no medido |
| TTS 1ª frase (`say` Mónica) | 530 ms | 550 ms | |
| **Hasta el primer audio** | **1.596 ms** | **1.975 ms** | desde que el paciente calla |
| ídem, sin la espera de turno | **970 ms** | **1.335 ms** | comparable con el presupuesto del README |
| Audio total emitido | 6.760 ms | 6.724 ms | misma respuesta, mismo troceado |

**De dónde salen los 379 ms de diferencia.** No de que Pipecat sea más rápido
ejecutando: las etapas cuestan lo mismo (391 contra 392 ms de STT, 530 contra 550
de TTS — el TTS incluso sale mejor en la columna B). Salen de **solapar el STT con
la espera de fin de turno**:

```
A · Pipecat      voz │ 200 ms │······· STT 391 ms ·······│
                     │ VAD    │ 440 ms espera de turno │  │ LLM │ TTS │
                     └───────────── 626 ms ───────────────┘
                     (el STT termina a los ~591 ms, ANTES de cerrarse el turno)

B · propio       voz │──────── 640 ms de silencio ────────│ STT 392 │ LLM │ TTS │
                     (el STT no empieza hasta que el turno está cerrado)
```

Pipecat parte la decisión en dos: `stop_secs` (200 ms) segmenta el audio y lanza
el STT, y `user_speech_timeout` (440 ms) decide si el turno se ha acabado de
verdad. Cuando decide, la transcripción ya está hecha. La Opción B usa un umbral
único y paga el STT entero después.

**El truco es copiable.** Partir el umbral de la Opción B en dos (segmentar a los
200 ms, decidir a los 640) recuperaría esos ~380 ms y son unas 15 líneas en
`SesionVoz`. No lo he hecho: la medición honesta es la del código que existe. Lo
que dice esto de Pipecat no es que sea más rápido, es que **trae la decisión
correcta puesta por defecto** — y esa clase de detalle es exactamente lo que se
paga en una ventana de tiempo corta.

### Contra el presupuesto del README: ¿cabe?

El README publica un presupuesto que **está mal en una etapa**, y con los números
corregidos por Samuel el reparto es otro:

| Etapa | README | Real medido | Fuente |
|---|---:|---:|---|
| Fin de turno | *no estaba* | **626 ms** | esta comparativa |
| STT Whisper `small` + prompt | 481 ms | 381-392 ms | aquí y en el escenario de STT |
| Embedding de la consulta | 24 ms | **25 ms** | medición de Samuel contra la API real |
| Retrieval híbrido | *pendiente* | **3 ms** | ídem |
| Reranker top-8 | 114 ms | **585 ms** | ídem — el README mide con pasajes de 250 caracteres; con fragmentos reales de 1.400 el cross-encoder cuesta 5× |
| LLM TTFT | ~300-600 ms | 400 ms (simulado) | sin API key entonces; medido después, 462 ms |
| TTS 1ª frase | 461 ms (Kokoro) | 530 ms (`say`) | medido con `say`; Kokoro remedido en 196-303 ms, ver §4 |
| **Total hasta el primer audio** | ≈1,4-1,7 s | **≈2,2 s** | con el rerank arreglado |

Cálculo del total, Opción A y suponiendo el reranker corregido:

```
626 (turno)  +  ~0 (STT, solapado)  +  25 (embedding)  +  3 (retrieval)
    +  225 (rerank, supuesto)  +  400 (LLM)  +  461 (TTS Kokoro)   ≈  1.740 ms
```

**Supuesto explícito**: se toman **225 ms** de reranker, el centro del rango
150-300 ms que Samuel estima tras el ajuste que está haciendo. Con los 585 ms
actuales el total sube a **≈2.100 ms** y sigue siendo usable, pero se nota. Si el
reranker acabara por encima de 400 ms, la palanca es la que ya dice el README:
`RERANK_ENABLED=false`.

Conclusión: **cabe**, pero el margen que el README creía tener (1,4 s) no existe.
El presupuesto real está en 1,7-2,2 s, y el fin de turno —que el README no
contabilizaba— es la segunda etapa más cara de todo el camino, por delante del
STT y del TTS.

---

## 2. Barge-in — ¿se calla el agente?

**Sí, en las dos, y en menos de 100 ms.**

El escenario: el paciente responde, el agente arranca su respuesta, y a los
5.500 ms el paciente le pisa con *«Perdone, una pregunta»* — mezclado sobre el
audio del micrófono, no sustituyéndolo, porque en una llamada real el micro capta
las dos cosas. 3 repeticiones.

| Medida | A · Pipecat | B · WebSocket propio |
|---|---:|---:|
| Interrupciones detectadas | **3 de 3** | **3 de 3** |
| Corte en el servidor | **83,7 ms** | **96,5 ms** |
| **Silencio audible** | **84,2 ms** | **96,5 ms** |
| Audio descartado del buffer | 5.053 ms | 5.371 ms |

Los dos números están dominados por el **umbral de confirmación de voz del VAD
(96 ms = 3 ventanas de Silero)**, que es deliberado: disparar con una sola
ventana haría que una tos o un golpe en la mesa callaran al agente. Lo que ocurre
*después* de decidir cuesta menos de 1 ms en las dos opciones. O sea: el barge-in
no lo resuelve el orquestador, lo resuelve el ajuste del VAD, y **eso empata**.

Lo que no empata es lo que costó llegar aquí. Los 5 segundos de audio descartados
son el hallazgo: **las dos opciones adelantan al cliente muchísimo más audio del
que ha sonado**, así que cortar en el servidor no basta. Hay que vaciar el buffer
del cliente:

- Pipecat lo hace solo. Su `BaseOutputTransport` limpia sus colas al recibir un
  `InterruptionFrame`; solo hay que replicar ese vaciado en el navegador.
- En la Opción B hay que mandar un mensaje de control `parar` **y** que el cliente
  cancele lo que tenga programado en el `AudioContext`. Está implementado en
  `scripts/spikes/cliente_voz/index.html` (`pararAudio()`), y es obligatorio: sin
  él el corte del servidor es una ficción y el paciente oye al agente 5 segundos
  más.

### El error que costó la mitad del tiempo de este spike

La primera versión de la Opción B midió **0 barge-ins de 3**, y no porque el
corte fallara: porque el agente ya no se consideraba «hablando». El audio se
manda tan rápido como se sintetiza, así que el bucle de emisión termina en
milisegundos mientras el paciente sigue oyendo al agente durante segundos. Un
booleano `agente_hablando = True` mientras se emite volvía a `False` casi al
instante, y a los 5.500 ms no había nada que interrumpir.

La solución es que **el servidor lleve el reloj de reproducción del cliente**
(`SesionVoz._suena_hasta`). Es una decena de líneas cuando ya sabes que hace
falta. Pipecat lo trae resuelto: su transporte de salida emite
`BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` a partir de ese mismo reloj.
Queda fijado en `tests/test_voz_barge_in.py::test_el_agente_sigue_hablando_despues_de_emitir`.

---

## 3. Fin de turno — cuánto esperar antes de contestar

El parámetro más delicado del sistema. Corto, y el agente corta al paciente a
media frase; largo, y el agente parece dormido. Barrido con seis clips, incluidos
dos con pausas internas metidas a propósito con `[[slnc]]` de `say` para que su
duración sea exacta y no una estimación:

| Umbral | Cortes falsos | Clips que rompe | Retraso |
|---:|---:|---|---:|
| 240 ms | 3 | `turno_dudoso`, `turno_pausa_larga` | 256 ms |
| 320 ms | 3 | `turno_dudoso`, `turno_pausa_larga` | 320 ms |
| 400 ms | 2 | `turno_dudoso`, `turno_pausa_larga` | 416 ms |
| 480 ms | 1 | `turno_pausa_larga` | 480 ms |
| 560 ms | 1 | `turno_pausa_larga` | 576 ms |
| **640 ms** | **1** | `turno_pausa_larga` | **640 ms** |
| 800 ms | 0 | — | 800 ms |
| 1.000 ms | 0 | — | 1.024 ms |

**Elegido: 640 ms.** No es el que da cero fallos, y es a propósito.

El único clip que 640 ms no salva es `turno_pausa_larga` — *«A ver… déjeme mirar
la caja de las pastillas»*, con una pausa deliberada de 700 ms. Para salvarlo hay
que subir a 800 ms, y eso son **160 ms de más en absolutamente todos los turnos
de la llamada**. El intercambio no está equilibrado, por una razón concreta:

> **Con el barge-in funcionando, un corte falso es recuperable; un umbral largo
> es un impuesto permanente.** Si el agente arranca antes de tiempo, el paciente
> sigue hablando, el barge-in lo calla en 97 ms y el turno se rehace. Si el
> umbral es de 800 ms, los 160 ms extra se pagan en cada una de las ~15
> respuestas de un seguimiento, y no hay forma de recuperarlos.

Esto también explica por qué merece la pena tener el barge-in fino antes de tocar
el umbral: **el barge-in es lo que permite ser agresivo con el fin de turno.**

Ajustes del VAD que se apartan de los valores por defecto de Pipecat, con motivo:

| Parámetro | Pipecat | Aquí | Por qué |
|---|---:|---:|---|
| `confidence` | 0,7 | **0,5** | 0,7 se come los finales de frase susurrados («…sí, un poco»), que en un seguimiento son respuestas legítimas |
| `min_volume` | 0,6 | **0,0** | el navegador ya normaliza el volumen; el umbral duplicado descartaba voz real |
| `start_secs` | 0,2 | **0,096** | 96 ms es lo que tarda el barge-in en dispararse: bajarlo de 0,2 a 0,096 son 104 ms menos de agente hablando encima del paciente |
| `stop_secs` | 0,2 | **0,2** + 0,44 de espera de turno | ver arriba |

El VAD cuesta **0,08 ms por ventana de 32 ms de audio** (p95 < 0,3 ms): corre en
tiempo real con ~400× de margen y no compite por CPU con Whisper ni con el TTS.

---

## 4. Código, dependencias y qué se rompe si Pipecat cambia

### Líneas de código

Contadas sin blancos, comentarios ni docstrings, y separando lo que va en
producción de los andamios de medición (recuento por símbolo, no a ojo):

| | A · Pipecat | B · WebSocket propio |
|---|---:|---:|
| Producción | **171** | **312** |
| — orquestación | 52 (`pipeline_pipecat.py`) | 190 (`SesionVoz` + endpoint) |
| — adaptadores de STT/TTS/LLM | 114 (`servicios_pipecat.py`) | — (usa los módulos tal cual) |
| — VAD y turnos | 0 (los trae Pipecat) | 112 (`vad.py`) |
| Andamios de medición | 76 | 69 |
| **Total del fichero, con documentación** | 550 | 734 |

La Opción B es **1,8× más código**, y todo ese extra está en la parte de la que no
te enteras si se rompe: el VAD, los umbrales, el reloj de reproducción del
cliente. Los adaptadores de Pipecat (114 líneas) son el peaje al revés — el precio
de que `stt.py` y `tts.py` sean módulos autónomos en vez de servicios.

De esas 114, unas 42 son `LLMFalsoProcessor` y desaparecen cuando entre el LLM de
verdad: Pipecat trae `GoogleLLMService` y sus agregadores de contexto. Con la API
key, la Opción A baja a ~130 líneas de producción.

### Dependencias arrastradas

Medido sobre el entorno instalado, cerrando el grafo de dependencias:

| | A · Pipecat | B · WebSocket propio |
|---|---:|---:|
| Distribuciones instaladas | **52** | **7** (`onnxruntime` y su cierre) |
| Peso en disco | **395 MB** | 117 MB |
| Exclusivo de esta opción | 45 paquetes, ~278 MB | 0 — todo lo suyo lo necesita también A |
| Modelo del VAD | el de Pipecat | `silero_vad.onnx`, 2,3 MB vendorizado |

Pipecat arrastra, sin que nadie los use en este proyecto: `openai` (el SDK
entero), `numba`, `nltk`, `resampy`, `soxr`, `pyloudnorm`, `Pillow`, `protobuf`,
`aiohttp`, `Markdown`. En una máquina con 16 GB compartidos con Whisper, bge-m3 y
el reranker, y con 21 GB de disco libres, no es gratis — pero tampoco es lo que
decide: son 278 MB, no 3 GB.

**Aviso de disco:** el servicio Whisper de Pipecat exige `faster-whisper`
(CTranslate2, cientos de MB) y **no está instalado**. No lo he instalado a
propósito. No hace falta: no vamos a usar ese servicio (§5).

### El TTS local por defecto no está instalado (afecta a las dos opciones)

> **Corregido el 2026-08-08, después de escribir esto:** `kokoro` ya está en
> `backend/pyproject.toml` y el modo `local` arranca. Lo de abajo se conserva
> porque es de donde salió el hallazgo y porque explica por qué todas las cifras
> de esta comparativa están tomadas con `say`.

`TTS_ENGINE_LOCAL=kokoro` es el valor por defecto en `config.py` y en `.env`, y
**`kokoro` no figura en `backend/pyproject.toml`**. Consecuencia práctica:
`crear_motor("kokoro")` lanza `ModuleNotFoundError`, así que hoy el modo `local`
—el que el proyecto usa por defecto y para desarrollo— no arranca. `piper`
tampoco: el binario no está en el PATH. El único motor local que funciona en este
entorno es `say`.

No he tocado `pyproject.toml`. Verificado que Kokoro sí funciona en un entorno
superpuesto, sin modificar el lock:

```bash
cd backend && uv run --with kokoro python ../scripts/spikes/spike_voz.py turno --tts kokoro
```

Medido así, Kokoro sintetiza la primera frase en **196-303 ms** (más rápido que
los 461 ms del README, y bastante más que los 530 ms de `say`), con una carga
inicial de ~36 s la primera vez. Todas las mediciones de esta comparativa usan
`say` para que sean reproducibles con el entorno tal como está declarado. Añadir
`kokoro` a las dependencias es decisión de Samuel, y es de una línea.

### Qué se rompe si Pipecat cambia su API

Ya ha cambiado, y esto no es especulación: **el montaje que describe el README no
compila en la versión instalada.** La 1.7.0 movió el VAD fuera del transporte:

| Lo que enseñan los tutoriales (y el README) | Lo que hay en 1.7.0 |
|---|---|
| `TransportParams(vad_analyzer=SileroVADAnalyzer())` | `VADProcessor(vad_analyzer=…)`, un procesador más del pipeline |
| El transporte decide el fin de turno | `UserTurnProcessor` con estrategias enchufables |
| — | La estrategia de parada **por defecto** es `LocalSmartTurnAnalyzerV3`, un modelo semántico que se descarga aparte |
| `PipelineTask` / `PipelineRunner` | Deprecados desde 1.3.0, se van en 2.0.0 → `PipelineWorker` / `WorkerRunner` |

Y hay dos cosas que no están documentadas y costaron el rato del spike:

1. **El STT va antes del `UserTurnProcessor`.** Al revés el turno nunca se cierra:
   la estrategia de parada lee transcripciones, y las transcripciones solo viajan
   hacia abajo. Se descubre leyendo `pipecat/turns/user_stop/`.
2. **Un transporte propio tiene que llamar a `set_transport_ready()`.** Si no, no
   existe `_audio_in_queue` y el primer `push_audio_frame()` revienta con un
   `AttributeError` **dentro de una tarea del runner**: el proceso se queda
   colgado sin traza, sin log y sin código de salida. Fue el atasco más caro del
   spike (~40 minutos), y el síntoma no apuntaba a nada.

La superficie expuesta al cambio son las **114 líneas de adaptadores** más el
montaje: si Pipecat 2.0 renombra `run_tts`, `wants_wav_segments` o los frames,
hay que reescribirlas. Es acotado y es un fichero. La mitigación está puesta:
`tests/test_voz_pipecat.py` fija los cuatro supuestos sobre la librería, así que
un cambio se manifiesta como un test en rojo y no como un paciente que oye
*«appendicitomía»*.

Riesgo simétrico y honesto: la Opción B no tiene ese riesgo, pero tiene el otro
—que el fallo esté en nuestro código y no lo cubra ningún test de nadie—, y ya se
ha materializado dos veces en una noche (el reloj de reproducción, y los espacios
comidos en el troceado por frases: *«graciaspor contármelo»*).

---

## 5. El STT no se toca: Whisper de Pipecat suspende

Pipecat 1.7 trae `WhisperSTTServiceMLX`, con binding MLX y el mismo `mlx_whisper`
que usamos nosotros. Parecía la ocasión de tirar código propio. No lo es.

Medido con **el mismo modelo** (`whisper-small-mlx`) sobre el mismo clip,
mediana de 3:

| | Latencia | «apendicectomía» |
|---|---:|---|
| `WhisperSTTServiceMLX` de Pipecat | **296 ms** | ✗ *«appendicitomía»* |
| **Nuestro `WhisperSTT`** (`small` + prompt clínico) | **381 ms** | **✓ «apendicectomía»** |

Es exactamente el resultado de la Fase 0 reproducido: **su `run_stt()` no expone
`initial_prompt`**, así que no hay forma de pasarle el vocabulario clínico.
Adoptar su servicio sería revertir la decisión que justifica el modelo actual, y
ahorrar 85 ms a cambio de que el agente escriba mal el nombre de la operación del
paciente en la historia clínica.

Y ni siquiera se puede probar sin más: `pipecat/services/whisper/stt.py` importa
`faster_whisper` a nivel de módulo, así que la variante de MLX —que no lo usa
para nada— está detrás de una dependencia de cientos de MB. No instalada, a
propósito.

Los dos hechos quedan fijados como pruebas en `tests/test_voz_pipecat.py`.

---

## 6. La recomendación

**Opción A, Pipecat, con el STT propio.**

El número que decide es **1.596 ms contra 1.975 ms hasta el primer audio**: 379
ms, un 19% del presupuesto, con etapas idénticas y el mismo umbral de turno. En
una conversación por teléfono 380 ms es la diferencia entre «me contesta» y «se
ha quedado colgado». Y no es un número que se compre con dinero ni con hardware:
sale de solapar el STT con la espera de fin de turno, que Pipecat trae puesto y
la Opción B no.

Detrás van dos argumentos de coste, no de rendimiento:

- **171 líneas contra 312**, y las 141 de diferencia están en el VAD, los
  umbrales y el reloj de reproducción, que es el código que falla en silencio.
  Esta noche ha fallado dos veces.
- **El barge-in empata (84 contra 97 ms)**, así que el argumento del README —«el
  barge-in es el trabajo difícil que Pipecat te ahorra»— resulta ser **falso**:
  el barge-in lo resuelve Silero con un umbral de 96 ms, y se escribe en un rato.
  Lo que Pipecat ahorra de verdad es otra cosa: el solapado del STT, el reloj de
  reproducción del bot y el vaciado de buffers en la interrupción. Tres detalles
  que nadie pone en un plan porque nadie sabe que existen hasta que los mide.

En contra de Pipecat, y es real: 45 paquetes y 278 MB que no usamos; una API que
ya rompió lo que el README daba por escrito; dos atascos no documentados que
costaron ~1 h de las 4 de este spike; y su servicio de Whisper es inservible para
nuestro caso. Nada de eso llega a 400 ms de latencia por turno.

**Lo que NO se recomienda tirar.** La Opción B se queda en el repositorio y no es
código muerto:

1. `app/voice/vad.py` es el **plan B de un solo fichero** si Pipecat da problemas
   en la demo. No depende de Pipecat (el modelo Silero está vendorizado) y está
   probado.
2. `SesionVoz` es lo que **hace medibles las dos opciones**: `ReproductorSimulado`
   es el sumidero común que da los números de barge-in de esta comparativa.
3. `/ws/voz` y `cliente_voz/index.html` son el **banco de pruebas con micrófono**
   más barato que hay: sin negociación SDP, sin STUN, sin ICE. Para probar «¿se
   entiende la voz? ¿corta cuando le hablo encima?» sigue siendo la vía rápida.

**Camino de migración a A, si Samuel acepta**: `app/voice/pipeline_pipecat.py`
está montado y medido; falta cambiar `EntradaInyectada`/`SalidaMedida` por
`crear_transporte_webrtc()` y añadir el endpoint `POST /api/voz/offer` con el
manejador de ofertas SDP de Pipecat (anotado en `docs/CONTRATO_API.md` §Cambios
sobre el contrato, punto 5). Eso es lo único que no está probado con un cliente
real, porque exige un navegador.

---

## Lo que no se ha podido verificar sin un humano con micrófono

| Sin verificar | Por qué hace falta una persona | Cómo se cierra |
|---|---|---|
| **Cancelación de eco** | La voz del agente sale por el altavoz y vuelve por el micro. Si el cancelador del navegador no la mata, el VAD la toma por voz del paciente y **el agente se interrumpe a sí mismo en bucle**. Depende del equipo, no del código. | `cliente_voz/index.html`, subir el volumen del altavoz y hablar. Si se autointerrumpe: auriculares para la demo |
| **Transporte WebRTC de verdad** | `SmallWebRTCTransport` necesita una oferta SDP de un navegador. La medición usa un transporte inyectado, que es el mismo `BaseInputTransport` pero sin negociación ni jitter de red | Montar `POST /api/voz/offer` y abrir la página |
| **Ritmo de salida de WebRTC** | El transporte medido adelantó ~5 s de audio; el de WebRTC, con `auto_silence` activo, puede ir a ritmo real y cambiar cuánto buffer hay que vaciar (no el instante del corte, que lo fija el VAD) | Misma prueba con navegador |
| **Umbral de 640 ms con habla real** | Las pausas están simuladas con `[[slnc]]` de `say`. Una persona dudando de verdad hace pausas más largas y más irregulares | Grabar 5 turnos hablando con normalidad y volver a pasar el barrido del escenario 3 |
| **Calidad percibida de la voz** | Todo se midió con `say` Mónica. *(Kokoro ya es dependencia desde el 2026-08-08; queda solo escucharlo)* | `--tts kokoro` y escuchar |
| **TTFT real del LLM** | La comparativa usa `ClienteLLMFalso` a 400 ms fijos, a propósito, para que la red de Google no decida cuál de las dos opciones gana. *(El TTFT real ya está medido aparte: 462 ms con Gemini 2.5 Flash y el razonamiento apagado — README §El LLM)* | Repetir el escenario `turno` con `crear_cliente()` |

Los pasos concretos están en `scripts/spikes/cliente_voz/README.md`.
