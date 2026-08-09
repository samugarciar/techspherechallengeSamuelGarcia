# Contrato de llamadas — Fases 4 y 5

**Normativo.** El agente clínico y la web app de llamada se construyen en
paralelo, por agentes distintos, cada uno contra este documento. Quien necesite
cambiarlo lo cambia **aquí primero** y lo anota al final.

Complementa a `CONTRATO_API.md`, que sigue vigente para todo lo de documentos.
Misma autenticación (`X-Admin-Token`) y misma forma de error.

---

## Decisiones de producto ya tomadas por Samuel

No son negociables sin preguntarle:

1. **Guion adaptativo según la cirugía.** Un bloque común (dolor, herida, fiebre,
   medicación, movilidad, dudas) más preguntas específicas del procedimiento: a un
   colecistectomizado se le pregunta por tolerancia a las grasas; a un
   herniorrafiado, por esfuerzos y peso.
2. **Ante bandera roja, el agente corta.** Abandona las preguntas pendientes, da la
   instrucción de urgencia del protocolo, **confirma que el paciente la ha
   entendido**, registra el escalamiento y cierra la llamada. Seguir preguntando por
   la dieta cuando alguien sangra es indefendible.
3. **Verificación de identidad con nombre y fecha de nacimiento**, contra los datos
   vivos de la base, antes de entrar en materia clínica.
4. **El agente se presenta como sistema automatizado** en su primera intervención.
   Lo exige el AI Act para interacciones con IA.
5. **La llamada se inicia eligiendo paciente** de una lista de pendientes.
6. **El historial de llamadas entra en esta tanda**, con transcripción y citas.
7. **LLM primario: Gemini 2.5 Flash.**

---

## Endpoints

### `GET /api/patients`
Pacientes con seguimiento pendiente, para la pantalla de inicio de llamada.
```json
{ "pacientes": [
  { "id": "uuid", "nombre": "María Fernández", "preferred_name": "María",
    "fecha_nacimiento": "1978-04-12",
    "cirugia": { "nombre": "Apendicectomía laparoscópica", "fecha": "2026-08-05",
                 "dias_desde": 3 },
    "medicacion_activa": 2, "proxima_cita": "2026-08-20",
    "ultima_llamada": null }
] }
```

### `POST /api/calls`
```json
{ "patient_id": "uuid" }
```
Crea la llamada en estado `en_curso` y devuelve `{ "call_id": "uuid", "ws": "/ws/voz?call_id=…" }`.

### `GET /api/calls`
```json
{ "llamadas": [
  { "id":"uuid", "paciente":"María Fernández", "cirugia":"Apendicectomía laparoscópica",
    "iniciada":"2026-08-09T10:12:00Z", "duracion_s": 184,
    "estado":"completada", "escalada": true, "motivo_escalada":"fiebre 39,2",
    "turnos": 14 }
] }
```
`estado`: `en_curso` · `completada` · `interrumpida`.

### `GET /api/calls/{id}`
La llamada más `turnos`, en orden:
```json
{ "turnos": [
  { "ordinal":1, "quien":"agente", "texto":"Buenos días…", "ms":{"llm":420,"tts":210} },
  { "ordinal":2, "quien":"paciente", "texto":"Sí, soy yo", "ms":{"stt":390} },
  { "ordinal":3, "quien":"agente", "texto":"Según el protocolo…",
    "citas":[{"filename":"protocolo.pdf","heading":"Cuidado de la herida","page":3}] }
] }
```
Las **citas por turno** son lo que hace auditable el sistema: se puede comprobar de
dónde salió cada afirmación clínica.

---

## Protocolo del WebSocket `/ws/voz`

Ya existe y funciona. Acepta ahora `?call_id=…`; sin él, sesión suelta sin
persistir. Mensajes del servidor hacia el cliente, además del audio binario:

| `tipo` | Cuándo | Campos |
|---|---|---|
| `listo` | al conectar | `sample_rate_entrada`, `sample_rate_salida` |
| `estado` | cambia la fase | `fase`: `escuchando`\|`pensando`\|`hablando` |
| `transcripcion` | STT resuelve | `quien`, `texto`, `parcial` |
| `citas` | el agente responde con evidencia | lista de citas |
| `bandera_roja` | salta el detector | `motivo`, `urgencia` |
| `metricas` | fin de turno | `ms` por etapa |
| `fin` | la llamada termina | `motivo`: `completada`\|`escalada`\|`cortada` |

---

## Pantalla `/call`

1. **Antes de llamar:** lista de pacientes con su cirugía y días transcurridos.
2. **Durante:** indicador de fase (escuchando / pensando / hablando), transcripción
   en vivo de ambos lados, citas que va usando, y **panel de latencias por etapa**.
3. **Bandera roja:** cambio visual inequívoco. Es el momento clínico de la demo.
4. **Al terminar:** resumen con lo registrado y si hubo escalamiento.

## Pantalla `/calls` — historial

Lista, y al abrir una: transcripción completa con las citas de cada turno y el
escalamiento destacado.

---

## Cambios sobre el contrato

### 1. Montar el router de llamadas en `app/main.py` (agente clínico)

`app/main.py` es de otro agente, así que no lo he tocado. El router está escrito
y probado en `app/api/llamadas.py`. **La línea exacta**, junto a las otras
`include_router`:

```python
from app.api import ajustes, documentos, errores, eventos, llamadas, rag, salud
...
app.include_router(llamadas.router, prefix="/api")
```

El prefijo es `/api` a secas y no `/api/calls` porque este router publica dos
raíces distintas del contrato: `/patients` y `/calls`. El orden respecto a los
demás `include_router` da igual: no hay solape de rutas.

No necesita `VOZ=1`. Es texto y SQL: no carga Whisper, ni Kokoro, ni el
reranker. `POST /api/calls/{id}/mensaje` sí llama al LLM y al RAG.

### 2. `GET /api/calls/{id}` publica dos campos más

`respuestas` (el `survey` de la llamada, clave → lo que contestó el paciente) y
`urgencia_escalada`. Los añade el historial también. Son aditivos: nada de lo
que el contrato ya definía cambia de forma.

### 3. `POST /api/calls` devuelve además el saludo

```json
{ "call_id": "uuid", "ws": "/ws/voz?call_id=…",
  "saludo": "Buenos días. Le llamo del servicio de seguimiento…",
  "paciente": "María Elena Restrepo Gómez",
  "cirugia": "Apendicectomía laparoscópica", "dias_postop": 3 }
```

La primera intervención del agente es **una constante**, no la genera el modelo:
contiene la declaración de sistema automatizado que exige el AI Act (decisión 4)
y una frase generada saldría distinta cada vez, sin forma de demostrar que se
dijo. Devolverla aquí permite que el TTS empiece a sonar sin esperar al LLM.

### 4. Dos endpoints nuevos

| Ruta | Para qué |
|---|---|
| `POST /api/calls/{id}/mensaje` | Hablar con el agente **escribiendo**: `{"texto": "…"}` → el turno completo (`texto`, `citas`, `banderas`, `escalada`, `terminar`, `ms`). Es a la Fase 4 lo que `python -m app.rag.query` fue a la Fase 1: prueba guion, herramientas, grounding y escalamiento sin micrófono. Cuando el agente responde mal, separa en dos segundos «el modelo se equivocó» de «Whisper oyó otra cosa» |
| `POST /api/calls/{id}/fin` | `{"motivo": "completada"\|"escalada"\|"cortada"}` → cierra la llamada y sella `ended_at`. Sin ella, `duracion_s` y `estado` del historial no significan nada |

### 5. El mapa de estados de `calls`

La tabla tiene cuatro estados y el contrato publica tres:

| `calls.status` | `estado` publicado | |
|---|---|---|
| `active` | `en_curso` | |
| `completed` | `completada` | |
| `escalated` | `completada` | con `escalada: true` |
| `failed` | `interrumpida` | |

Una llamada escalada se publica como **completada**: escalar no es terminar mal,
es exactamente lo que el sistema debe hacer. Mezclarla con `interrumpida` haría
que el historial contara los aciertos como fallos.

### 6. El agente vive en memoria del proceso

El historial de conversación y la fase de la llamada (¿ya hubo alarma?, ¿estoy
esperando la confirmación?) están en un diccionario del proceso, no en la base.
Reconstruirlos desde `call_turns` se descartó: la transcripción no contiene los
resultados de las herramientas, que es justo lo que el modelo necesita para no
volver a consultarlos. Consecuencia visible: si el backend se reinicia a mitad de
llamada, el turno siguiente da `404 llamada_no_encontrada`. Con llamadas de tres
minutos y un solo proceso, es preferible a cubrirlo a medias.

### 7bis. La integración: quién emite cada mensaje del WebSocket

La tabla de arriba dice *cuándo* se emite cada mensaje pero no *quién*, y eso
resultó ser la pregunta importante: las citas y las banderas rojas las conoce el
agente clínico, no el bucle de voz, y el bucle de voz no puede importarlo sin
atarse a la Fase 4.

| `tipo` | Lo emite | Dónde |
|---|---|---|
| `listo` | el endpoint | al aceptar el WebSocket |
| `estado` | `SesionVoz` | las transiciones de fase que ya tenía |
| `transcripcion` | `SesionVoz` | donde resuelve el STT |
| `citas` | `SesionDeVoz` | donde el agente las produce, antes del audio |
| `bandera_roja` | `SesionDeVoz` | ídem, en cuanto dispara el detector |
| `metricas` | `SesionVoz` | fin de turno, con las etapas ya medidas |
| `fin` | `SesionVoz` | preguntándole a la llamada si ha terminado |

El enganche es el protocolo `LlamadaEnCurso` (`app/voice/pipeline_ws.py`), que
implementa `SesionDeVoz` (`app/api/llamadas.py`) y conecta `app/main.py` con
`crear_router(fabrica_llamada=…)`. `app/voice/**` no importa `app/agent/**` en
ningún punto: es lo que permite que el arnés de medición siga montando el mismo
bucle con `ClienteLLMFalso`.

`metricas` publica `stt`, `llm`, `tts` y `total` (= hasta el primer audio). **No
publica `retrieval`**, que el panel de la pantalla sí contempla: el pipeline de
voz no mide el RAG —lo mide el agente por dentro— y mandar un cero diría «la
búsqueda tardó 0 ms» en vez de «esto no lo mido yo».

### 7ter. Cuatro decisiones que la integración obligó a tomar

1. **El saludo lo pronuncia el servidor al conectar el WebSocket.** `POST
   /api/calls` lo devuelve escrito, pero el navegador no tiene TTS: sin esto la
   llamada empezaba en silencio y el paciente no sabía que había alguien. Va como
   tarea cancelable, así que se le puede interrumpir como a cualquier turno.

2. **Un `call_id` sin agente vivo cierra la conexión** con `fin`/`cortada` en vez
   de caer al LLM falso. Es lo que pasa si el backend se reinicia a mitad de
   llamada (§Cambios 6). Continuar daría una llamada que suena bien y no guarda
   nada, que es el fallo silencioso de siempre.

3. **Colgar el WebSocket sella la llamada** como `cortada` si el agente no la
   había dado por terminada. Sin esto una llamada abandonada se queda `en_curso`
   para siempre y con la duración subiendo. *Consecuencia:* el botón «Reconectar»
   de la pantalla ya no puede retomar una llamada, porque al irse la conexión el
   agente se descarta. **Pendiente de Samuel:** si prefiere una ventana de gracia
   para reconectar, es un temporizador en `SesionDeVoz.cerrar()`.

4. **Solo un WebSocket por llamada.** El segundo se rechaza. Dos conexiones sobre
   el mismo `AgenteLlamada` entrelazarían los turnos en un único historial.

### 8. Un mensaje más del que la tabla publica: `parar`

Ya estaba en el bucle de voz y el frontend ya lo maneja; se anota aquí porque no
figuraba en la tabla y es **obligatorio**: es el único mensaje que vacía el
buffer del cliente en un barge-in. Cortar la síntesis en el servidor no calla al
agente si el navegador tiene dos segundos de audio encolados.

Siguen emitiéndose además `paciente_habla`, `fin_de_turno`, `agente_habla` y
`fin_audio`, que son los del bucle de voz de la Fase 3. `estado` no los sustituye:
son el mismo hecho contado a dos clientes distintos, y retirarlos rompería
`scripts/spikes/cliente_voz/` sin avisar.

### 9. Recomendación pendiente de Samuel: una séptima herramienta

`obtener_paciente` devuelve la fecha de nacimiento para poder compararla con la
que diga el paciente. Que el modelo la vea significa que *podría* leerla en voz
alta antes de que la persona se identifique; hoy se lo impide una regla del
prompt, que es una petición y no una garantía. La forma sólida es
`verificar_identidad(fecha_dicha) -> {coincide: bool}`, con la comparación en
Postgres y el dato nunca en el contexto del modelo. No la he añadido porque el
contrato fija seis herramientas. Anotado también en `eval/guion_llamada.md`.

**2026-08-09 — ya no es hipotético: ocurre.** En las dos ejecuciones de
`scripts/demo_llamada_completa.py` contra Gemini 2.5 Flash, con el prompt real y
su regla «Nunca le digas su fecha de nacimiento […] antes de que la persona se
identifique», el agente contestó:

> «Gracias. Según mi registro, su fecha de nacimiento es el tres de julio de mil
> novecientos noventa y dos. ¿Es correcto?»

El disparador es que el guion de la demo dice **una fecha que no coincide** con
la del sistema. Ante el desajuste el modelo «ayuda» leyendo la buena, que es
exactamente el caso en que no debe: quien está al teléfono acaba de demostrar que
no sabe la fecha del paciente. Un impostor obtiene el dato preguntando mal.

La regla del prompt está bien escrita y aun así se incumple, que es el argumento
entero de por qué esto tiene que ser una herramienta y no una instrucción.
**Decisión para Samuel:** añadir `verificar_identidad` (rompe la cifra de seis
herramientas del contrato) o dejarlo documentado como límite conocido.
