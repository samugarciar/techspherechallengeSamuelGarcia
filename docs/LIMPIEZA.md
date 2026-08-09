# Limpieza — quitar lo que sobra sin romper nada

Pasada de limpieza sobre la rama `nocturno` (2026-08-09), después de que una decena
de agentes construyeran el sistema en paralelo durante dos días. El encargo era
acotado a propósito: **no refactorizar ni rediseñar**, solo borrar lo muerto,
unificar lo duplicado y corregir lo que miente.

**Antes:** 421 tests + 1 `xfail`, ruff limpio, el frontend compila.
**Después:** 421 tests + 1 `xfail`, ruff limpio, el frontend compila.

El grueso del valor no está en el código borrado (poco, y era poco) sino en el
apartado 2: este proyecto cambió de opinión cinco veces con datos delante, y los
comentarios de la versión anterior seguían ahí, afirmando en presente cosas que la
medición ya había tumbado. Un comentario que miente es peor que no tener comentario,
y dos de los corregidos daban consejos que rompen el sistema si se siguen.

---

## 1. Qué se borró

Cada candidato se comprobó con `grep` sobre **todo** el repositorio —código, tests,
scripts, spikes, documentación y HTML— antes de tocarlo, y la batería se pasó
después de cada tanda.

| Qué | Dónde | Por qué se fue | Cómo se comprobó |
|---|---|---|---|
| `README.md` de la plantilla de Vite (32 líneas) | `frontend/` | Andamio intacto del `npm create vite`: en inglés, habla de «this template» y de cómo activar el React Compiler. No dice ni una palabra de este proyecto | Ningún fichero lo referencia; no entra en el build |
| `favicon.svg` (9,5 kB) e `icons.svg` (5 kB) | `frontend/public/` | Restos de la misma plantilla — el segundo son iconos de Bluesky, Discord y demás. `index.html` embebe su propio favicon en un `data:` URI, y lo explica en un comentario | `grep -rn "favicon\|icons"` sobre `src/`, `index.html` y `vite.config.ts`: cero referencias. Se copiaban a `dist/` en cada build |
| `TTSEngine.stream_por_frases()` y el import `AsyncIterator` que lo sostenía | `backend/app/voice/tts.py` | Método por defecto de la clase base que no llama nadie y que ningún motor sobrescribe. Los tres sitios que emiten audio frase a frase (`agente.py`, `pipeline_ws.py` ×2) hacen `dividir_en_frases()` + `sintetizar()` a mano **porque tienen que intercalar la comprobación de barge-in entre frase y frase**, cosa que un generador asíncrono cerrado no permite; `servicios_pipecat.py` lo evita a propósito y lo tiene escrito. Nació como la abstracción bonita y ningún caso real la pudo usar | `grep -rn stream_por_frases` en todo el repo: solo su definición. Después, `ruff` cazó el import huérfano |
| `esRecuperable(error)` y `esFaltaDeToken(error)` | `frontend/src/api/errores.ts` | Las cinco pantallas con botón «Reintentar» lo ofrecen siempre, sin preguntar; el 401 se resuelve por otra vía (`ControlToken`) | `grep` sobre `src/`: solo sus definiciones |
| `esRecuperable(estado)` | `frontend/src/types/estados.ts` | Homónima de la anterior, distinto significado, y también sin consumidores. Dos funciones con el mismo nombre y ninguna usada | ídem |
| `AreaTexto` | `frontend/src/components/ui/campo.tsx` | No hay un solo `<textarea>` en la aplicación | ídem |
| Parámetro `worker` de `_procesar()` | `backend/app/workers/ingest_worker.py` | Se pasaba desde `bucle()` y no se usaba dentro. Quien identifica al worker es `queue.tomar_trabajo(conn, worker=…)`, que sí lo recibe y sí lo escribe en `jobs.locked_by` | `ruff --select ARG001`. Los dos tests que llamaban a `_procesar(job, "test")` se actualizaron a `_procesar(job)` — ningún test se perdió |

Nada más se borró. La lista de lo que **parecía** dead code y no lo es está en el
apartado 4, y es más larga que ésta.

---

## 2. Comentarios y documentación que mentían

Ordenado por lo que costaría creérselo.

### 2.1 Dos comentarios que dan un consejo que rompe el sistema

`app/api/rag.py` decía, sobre el reranker:

> «Si el reranker se dispara, se ve aquí y **se apaga con RERANK_ENABLED sin tocar
> nada más**.»

Y `app/rag/rerank.py`:

> «Si el presupuesto de latencia no le da cabida **se apaga sin tocar el resto del
> sistema**.»

Es falso desde el fallo 1.12 de `docs/REVISION_F2_F3.md`: las dos ramas de
`reordenar()` puntúan en escalas distintas —cross-encoder 0..1 contra RRF, cuyo
máximo teórico es 0,0328— y `hay_evidencia()` compara ambas contra el mismo umbral
de 0,35. Apagar el interruptor hoy deja `hay_evidencia` en `False` para todo: el
agente responde «no tengo esa información» con el protocolo delante, y en silencio.
El interruptor es además la palanca de latencia más grande que queda (585 ms), o
sea, el consejo estaba escrito justo donde alguien lo va a leer con prisa.

Reescritos los dos para decir lo contrario y apuntar al `xfail(strict=True)`. Se
corrigió también la misma promesa en `.env.example` (que además publicaba el coste
viejo, 114 ms) y en la lista de palancas del `README.md`, donde «apagar el reranker»
figuraba como la opción número 1 sin ninguna advertencia. **No se ha tocado ni el
interruptor ni `rerank.py` ni el `xfail`**: la decisión sigue abierta y es de Samuel.

*(El docstring del `xfail` citaba literalmente la frase de `rag.py` que ahora ya no
existe; se ajustó la cita para que no quede señalando a un texto fantasma. El
marcador, su `strict=True` y el test siguen intactos.)*

### 2.2 Números que describían el mundo anterior

| Dónde | Decía | Dice |
|---|---|---|
| `app/api/rag.py`, `app/rag/query.py`, `app/rag/retrieval.py` (×2) | «~120 ms del cross-encoder» — usado además para dimensionar la ventana de riesgo de `revalidar()` | ~585 ms |
| `.env.example`, `docs/PLAN.md` | «top-8 = 114 ms (cabe)» | 585 ms, con la explicación de por qué el spike de la Fase 0 se equivocó (midió pasajes de 250 caracteres; los reales tienen 500-1400) |
| `docs/CONTRATO_API.md` | ejemplo de respuesta con `"rerank": 114` | `"rerank": 585`, y el resto de etapas con los valores medidos |
| `app/voice/tts.py` (×2: `dividir_en_frases` y `KokoroTTS`) | «461 ms con Kokoro» | 196-303 ms, con la nota de que el número viejo era el que sostenía la conclusión contraria |
| `README.md` tabla de TTS y fila de ElevenLabs | Kokoro 461 ms; ElevenLabs «más rápido que el local» | 196-303 ms; y ElevenLabs pasa a «ganaba al local cuando Kokoro se creía en 461 ms» |
| `docs/PLAN.md` | «Premium no es mejor voz a cambio de latencia, sino mejor voz **y menos latencia**» | La conclusión duró un día: con Kokoro remedido, el local vuelve a ser el más rápido |
| `app/voice/pipeline_ws.py` | «que "el barge-in tarda 180 ms" sea una medición» | 96 ms, que es lo medido |
| `docs/VOZ_COMPARATIVA.md` | tres casillas dando por pendiente lo ya resuelto (Kokoro sin instalar, TTFT sin medir) | notas fechadas; el informe no se reescribe, se anota, como manda la bitácora |

### 2.3 Groq: la palanca de latencia que no lo era

`app/agent/llm_client.py` abría con «Groq sirve respuestas con un TTFT bastante
menor» y explicaba que existía «si en la demo el TTFT de Gemini estorba». Lo mismo
en `app/core/config.py` y en `.env.example` («escape de latencia si el TTFT
estorba»). Medido: Groq gana **91 ms** de TTFT —dentro del ruido— y en el turno
completo con *tool calling*, que es lo que este agente hace de verdad, su free tier
da timeouts: **22,5 s de mediana frente a 1,2 s** de Gemini. Los tres sitios dicen
ahora que Groq es el plan B de **disponibilidad**, no de velocidad.

De paso, la cabecera de `llm_client.py` fechaba el comportamiento de
`generate_content_stream` en `google-genai>=2` mientras el comentario en línea
—veinte líneas más abajo, sobre la misma llamada— decía `>=1`. Unificado a `>=1`,
que es lo que dice la bitácora y lo que se verificó (instalado: 2.17.0).

### 2.4 Parsing: la tabla del README tenía los motores al revés

> `| Parsing | Docling (PyMuPDF de respaldo) | Conserva la jerarquía de secciones… |`

Es exactamente lo contrario de lo que se midió y de lo que hace `app/rag/parsing.py`
(cuya cabecera sí lo cuenta bien): **PyMuPDF acierta 25/25 niveles de encabezado y
Docling 3/25**, así que PyMuPDF es el primario en `.pdf` y Docling el respaldo; en
`.docx` se invierte, porque Docling lee el nivel del estilo del párrafo. El README
era el único sitio donde quedaba la versión del plan original.

### 2.5 Pipecat: el argumento que la medición tumbó

La tabla de stack del README seguía diciendo que Pipecat «resuelve barge-in y fin de
turno, **que es el trabajo difícil**», que es literalmente el argumento que la propia
sección 3 del mismo README declara falso doscientas líneas más abajo. Corregido para
que la tabla diga lo que decide de verdad (solapar el STT con la espera de fin de
turno, −379 ms) y para que quede claro que **lo montado hoy es el WebSocket propio**,
que es el que tiene cliente de navegador. La fila del VAD decía «ya integrado en
Pipecat» cuando `app/voice/vad.py` carga Silero por ONNX a mano, precisamente para
poder medirlo sin transporte.

### 2.6 Cosas que el README daba por hechas y no lo están

- **Trazas.** «Las trazas van a una tabla de Postgres: ~0 RAM y **se muestran en la
  propia consola**». La tabla `traces` está en el schema y **no la escribe ni la lee
  nadie** (comprobado: 0 referencias en `backend/app/**`). Hoy las latencias por
  etapa viajan en `call_turns.latencies` y en el `ms` de `/api/rag/query`, que es lo
  que las pantallas pintan. Reescrito; la tabla **no se ha borrado** (ver §4).
- **El toggle de voz.** «se conmuta **desde la consola de administración**». No hay
  botón: el alcance de la Fase 2 se cerró en documentos, como dice el propio
  contrato. El endpoint existe y se demuestra con `curl`.
- **La contabilidad de TTS.** «con esto **la consola muestra** qué está costando
  premium». Tampoco. `tts_usage` se llena en cada síntesis y
  `voice_mode.resumen_consumo()` lo agrega, pero el panel está pendiente.
- **El estado de las fases.** Fase 3 marcada como pendiente («falta elegir por
  medición» — ya se eligió) y Fase 5 sin marcar estando construida, cosida y
  probada. Actualizadas, con la salvedad real: falta el micrófono.

### 2.7 Comandos de la documentación que no funcionan

`README.md` proponía `uv run python scripts/demo_aprender_olvidar.py` desde la raíz.
Lanzado así, `uv` monta un entorno efímero sin `httpx` y el script muere en el
import — **comprobado ejecutándolo**. La forma que funciona es desde `backend/`, que
es donde vive el `pyproject.toml`. Corregido en el README y en los docstrings de los
dos scripts de demo.

`scripts/spikes/cliente_voz/README.md` —que es lo que va a leer quien haga la prueba
con micrófono, el último paso pendiente del proyecto— tenía tres afirmaciones falsas
seguidas: que el router de voz «aún no está en `app/main.py`», que `app/main.py`
«todavía no existe», y que el paquete `kokoro` «no figura en `backend/pyproject.toml`».
Las tres se arreglaron hace días. Además le faltaba `VOZ=1`, sin lo cual el router no
se monta y nada de lo que describe ocurre. Reescrito el arranque entero. La misma
mentira sobre `kokoro` estaba en la cabecera de `scripts/spikes/spike_voz.py`.

### 2.8 Andamiaje de coordinación disfrazado de decisión de diseño

Media docena de comentarios justificaban una decisión con «ese fichero es de otro
agente y no se puede tocar» — una restricción de proceso, ya caducada, en el sitio
donde uno espera encontrar un motivo técnico. En todos los casos había un motivo
durable debajo, y es el que se ha escrito:

- `app/agent/agente.py` — por qué el agente implementa `LLMClient`: no porque
  `app/voice/**` fuera intocable, sino porque la inversión es lo que permite que
  `ClienteLLMFalso` siga pudiendo ocupar ese hueco y que el arnés de medición monte
  el mismo bucle. Decía además que `SesionVoz` «hoy recibe un `ClienteLLMFalso`»,
  que dejó de ser cierto con la integración.
- `app/agent/llm_client.py` — por qué `Mensaje.herramientas` va al final del
  dataclass: porque `app/voice/**` construye `Mensaje("user", texto)`
  posicionalmente, y moverlo cambiaría el significado de esas llamadas en silencio.
- `app/voice/pipeline_ws.py` — `crear_router()` decía que la anotación para montarlo
  estaba en el contrato. Lleva montado desde la integración, detrás de `VOZ=1`.
- `app/api/eventos.py` — el argumento contra `LISTEN/NOTIFY` se apoyaba en «fichero
  de otro agente»; el argumento bueno (haría falta una migración, y el sondeo no
  necesita que nadie coopere) ya estaba al lado.
- `app/api/llamadas.py` — «la pantalla `/call` la construye otro agente». Construida.
- Frontend: `types/api.ts`, `api/normalizar.ts`, `api/llamadas/cliente.ts` y
  `api/mock/index.ts` afirmaban en presente que el backend «se está escribiendo en
  paralelo» o que el mock serviría «mañana contra FastAPI». En los tres primeros el
  motivo original se conserva como historia y se explica por qué la pieza **sigue**
  valiendo hoy.
- `frontend/src/lib/duracion.ts` — «Fichero aparte de `formato.ts` **para no tocar el
  suyo**», que es un motivo de coordinación, no de diseño. Reescrito con el criterio
  real de reparto.

### 2.9 Un texto visible al usuario

`frontend/src/pages/NoEncontrada.tsx`, la página 404, decía: «La consola cubre por
ahora la gestión de documentos. El resto de secciones llegará en las siguientes
fases.» La barra de navegación tiene tres secciones desde la Fase 5. Es la única
mentira de esta lista que un jurado podría leer en pantalla.

---

## 3. Duplicación unificada

**Dos normalizadores, las mismas seis funciones.** `api/normalizar.ts` (documentos) y
`api/llamadas/normalizar.ts` (llamadas) se escribieron en paralelo por agentes
distintos y cada uno declaró sus propios `texto`, `textoOpcional`, `entero`/`numero`,
`enteroOpcional`/`numeroOpcional`, `objeto` y `lista`, con cuerpos idénticos y dos
nombres para lo mismo. Es literalmente el mismo problema —coerción defensiva de un
escalar que llega por la red— así que se han movido a `frontend/src/api/coercion.ts` y
los dos ficheros importan de ahí. Lo que **no** se ha tocado es la forma de cada
objeto del contrato: cada normalizador conoce su parte de la API y esa parte no se
comparte. De paso, `normalizar.ts` usaba `(bruto ?? {}) as Record<string, unknown>`
donde el otro usaba `objeto(bruto)`: mismo objetivo, y la segunda además aguanta que
llegue un escalar. −37 líneas de código repetido.

**Dos formatos de cita en el mismo script.** `scripts/demo_llamada_completa.py`
imprimía las citas de dos maneras distintas: con página en el paso 3 (lo que llega
por el WebSocket) y sin página en el paso 4 (lo que quedó en `call_turns`). Como una
de las seis comprobaciones del script es justamente que ambas coincidan, imprimirlas
distinto invita a leer mal la salida. Unificadas en `cita_en_una_linea()`.

---

## 4. Candidatos que NO se borraron, y por qué

Esta lista importa tanto como la primera. En todos los casos el `grep` decía «no lo
llama nadie» y la respuesta a «¿por qué se escribió esto?» decía que se queda.

| Candidato | Por qué se queda |
|---|---|
| `ClienteLLMFalso` (`pipeline_ws.py`) | Parece un doble de test abandonado y es tres cosas vivas: lo usan los tests de voz, es el LLM del arnés de `docs/VOZ_COMPARATIVA.md` —con TTFT fijo a propósito, para que la red de Google no decida qué framework gana— y es la «sesión suelta sin persistir» del contrato cuando se conecta a `/ws/voz` sin `call_id`, que es como funciona el cliente de micrófono |
| `ttft_ms = 400` dentro de `ClienteLLMFalso` | El TTFT real medido son 462 ms y la tentación es actualizarlo. **No se ha tocado**: los números de la comparativa se tomaron con 400 y cambiar la constante los volvería irreproducibles. Ahora lo explica el docstring |
| `pipeline_pipecat.py` y `servicios_pipecat.py` | Pipecat es la opción **recomendada** para producción; lo montado hoy es el WebSocket propio solo porque tiene cliente de navegador. Ninguna de las dos sobra |
| `app/rag/rerank.py` y `RERANK_ENABLED` | Decisión abierta de Samuel, pendiente del corpus real. Solo se han corregido los comentarios que decían que apagarlo sale gratis |
| El `xfail(strict=True)` de `test_hay_evidencia_sigue_significando_algo_sin_reranker` | Aviso deliberado. Sigue en su sitio, `strict` incluido |
| `scripts/spikes/**` | Nadie los llama y son la evidencia que sostiene las decisiones del README. Solo se corrigió una cabecera que mentía |
| `eval/corpus_prueba/`, `eval/medir_reranker.py`, `scripts/demo_*.py` | Guion de demo y prueba de regresión a la vez; `medir_reranker.py` es lo que cerrará la decisión del reranker |
| `retrieval.solo_denso()` | Búsqueda puramente vectorial, sin un solo consumidor. Se queda porque su motivo sigue vigente: el argumento de que la búsqueda híbrida hace falta es hoy una afirmación sin medir, y ésta es la mitad que falta para convertirla en un número en la Fase 6. El docstring ahora avisa de que no la llama nadie |
| `voice_mode.resumen_consumo()` | Sin consumidores porque el panel de consumo no se ha construido (la Fase 2 se cerró en documentos). `tts_usage` sí se está llenando en cada síntesis, así que sin esta agregación el dato solo se lee con `psql`. Docstring corregido para que no prometa un panel que no existe |
| Tabla `traces` del schema | Creada y sin escribir. **No se toca**: borrarla obliga a recrear las bases de datos (y el propio `App.tsx` deja la ruta `/trazas` anotada como pendiente). Lo que se ha arreglado es el README, que la daba por funcionando |
| Los mocks del frontend (`api/mock/`, `api/llamadas/mock/`) | Deliberados, documentados, y la pantalla avisa con la insignia «Datos simulados» |
| `PiperTTS`, `SayTTS`, `CartesiaTTS` | Solo los construye `crear_motor()` por nombre, así que ningún `grep` los ve usados. Son las opciones del interruptor: `say` es el que usan la demo de llamada completa y el arnés de voz |
| Los 40 `# noqa` que `ruff --select RUF100` marca como innecesarios (8 `BLE001`, el resto `F401`/`F811` en los tests) | Innecesarios solo porque esas reglas no están en el `select` del proyecto; el día que se activen vuelven a hacer falta. Los `BLE001` llevan además pegada la explicación de por qué ese `except Exception` concreto es correcto, y este proyecto ya se comió dos fallos silenciosos por tragarse excepciones. Se quedan |
| `docs/REVISION_F2_F3.md`, `docs/INFORME_NOCHE.md`, `docs/ROBUSTEZ.md` | Informes fechados, no documentación viva. Alguna de sus frases ya no es cierta (`npm run build` sí pasa ahora), pero reescribir un informe de una revisión adversarial es borrar la evidencia. Misma regla que la bitácora |
| `VITE_WS_BASE` | La lee `api/llamadas/real.ts` y **no está en `frontend/.env.example`**. No es una variable muerta sino lo contrario: una variable viva sin documentar. Anotado aquí porque documentarla es añadir, no limpiar |
| `duracion_s` en `GET /api/calls/{id}` | `PaginaDetalleLlamada.tsx` lo pinta y el endpoint de detalle no lo devuelve (sí el de listado), así que en el detalle sale siempre «—». El contrato no lo promete, así que **no es una desviación**: es un hueco. Se anota porque arreglarlo es cambiar comportamiento, que estaba fuera de este encargo |

---

## 5. Dependencias que parecen no usarse

**No se ha tocado `pyproject.toml`, `uv.lock` ni `package.json`.** Anotado para
quien decida:

- **`pytest-timeout`** (grupo `voice` de `backend/pyproject.toml`) — **sin usar**. No
  hay un solo `@pytest.mark.timeout` ni `--timeout` en la configuración de pytest; los
  tests que necesitan un techo usan `asyncio.wait_for`. Candidata clara a caer.
- **`websockets`** — el caso contrario: `scripts/demo_llamada_completa.py` la importa
  y **no está declarada**. Funciona porque entra de rebote con `uvicorn[standard]`. Si
  ese extra cambia, la prueba de integración deja de arrancar.
- Todo lo demás se usa. `python-multipart` no aparece en ningún `import` porque la
  consume FastAPI para el `multipart/form-data` de la subida, y `pgvector` solo se
  nombra en `app/db/pool.py` (`register_vector_async`): las dos son necesarias.
- En el frontend, las once dependencias de producción se usan todas.

---

## 6. Cuentas

| | |
|---|---|
| Ficheros tocados | 39 — 35 modificados, 3 borrados, 1 nuevo (`api/coercion.ts`), más este informe |
| Líneas borradas | **298** |
| Líneas añadidas | 307 (269 en ficheros existentes + 38 de `api/coercion.ts`) |
| — borradas: andamiaje entero | 57 (`frontend/README.md`, `favicon.svg`, `icons.svg`) |
| — borradas: código muerto | ~35 (`stream_por_frases` y su import, tres funciones y un componente del frontend, un parámetro) |
| — borradas: duplicación | ~40 líneas de coercers repetidos, sustituidas por 24 compartidas |
| — el resto | comentarios y documentación reemplazados por su versión verdadera |
| Tests antes | **421 pasando + 1 `xfail`** |
| Tests después | **421 pasando + 1 `xfail`** |
| `ruff check` | limpio antes y después |
| `npx tsc -b` · `npm run build` · `npm run lint` | verde antes y después |

Ningún test se ha borrado ni se ha desactivado. Los dos únicos ficheros de test
tocados lo fueron sin cambiar qué comprueban: `test_ingest_e2e.py` ajusta dos
llamadas a la nueva firma de `_procesar()`, y `test_api_rag.py` corrige una cita
literal dentro del docstring del `xfail`.

El balance de líneas es casi neutro, y es lo esperable: se van 132 líneas de código
muerto, andamio y duplicación, y las que entran son casi todas comentarios que
explican por qué algo es como es —o por qué una medición anterior era falsa— en vez
de repetir lo que ya se lee en el código.

### Nota sobre `npx tsc --noEmit`

El encargo pedía sustituirlo por `npx tsc -b` «donde aparezca». Aparece **en un solo
sitio de todo el repositorio**: `docs/REVISION_F2_F3.md` §5, y allí no es un comando de
verificación sino la descripción del propio fallo («`tsconfig.json` tiene `"files": []`
… siempre sale verde; el comando que sí comprueba es `npx tsc -b`»). No hay nada que
sustituir: el aviso ya está bien escrito. `frontend/package.json` ya usa `tsc -b` en su
script de `build`, y el verde falso se confirmó de nuevo en esta pasada. Lo que sí
falta es que ese aviso llegue a algún checklist de pre-vuelo, pero eso es escribir el
checklist, no limpiarlo.
