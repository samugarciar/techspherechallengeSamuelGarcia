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

<!-- añadir entradas debajo -->
