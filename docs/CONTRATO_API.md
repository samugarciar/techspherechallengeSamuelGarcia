# Contrato de API — Fases 1 y 2

Este documento es **normativo**. Varios agentes trabajan en paralelo sobre este
worktree sin comunicarse entre sí: el backend y el frontend se construyen a la
vez, cada uno contra este contrato. Si un agente necesita cambiarlo, lo cambia
**aquí primero** y lo deja anotado al final, en «Cambios sobre el contrato».

Todo el texto visible al usuario va en **español**. Base: `http://localhost:8000`.

---

## Autenticación

Cabecera `X-Admin-Token: <ADMIN_TOKEN>` en todo `/api/*` salvo `/api/health`.
Si falta o no coincide → `401`. Un solo administrador; no hay sesiones ni JWT:
con esta ventana de tiempo, más que eso es ceremonia sin valor demostrable.

## Forma de los errores

Siempre este objeto, nunca el `{"detail": ...}` por defecto de FastAPI:

```json
{ "error": { "codigo": "documento_no_encontrado", "mensaje": "…en español, mostrable al usuario" } }
```

Códigos: `no_autorizado` · `documento_no_encontrado` · `formato_no_soportado` ·
`archivo_vacio` · `archivo_demasiado_grande` · `documento_duplicado` · `error_interno`.

---

## Estados del documento

```
uploaded → parsing → chunking → embedding → ready
     ↘ failed                                  ↓
                                        superseded
```

`ready` es el **único** estado recuperable por el RAG. La vista
`retrievable_chunks` lo garantiza a nivel de schema.

Etiquetas para la UI (español, ya decididas — no inventar otras):

| estado | etiqueta | color |
|---|---|---|
| `uploaded` | Recibido | gris |
| `parsing` | Leyendo el archivo | ámbar |
| `chunking` | Troceando | ámbar |
| `embedding` | Generando embeddings | ámbar |
| `ready` | **Listo — el agente ya lo sabe** | verde |
| `failed` | Error | rojo |
| `superseded` | Reemplazado | gris |

---

## `Documento` (objeto compartido)

```json
{
  "id": "uuid",
  "filename": "protocolo_apendicectomia.pdf",
  "title": "Protocolo de alta — apendicectomía",
  "mime_type": "application/pdf",
  "size_bytes": 184320,
  "sha256": "…",
  "status": "ready",
  "error": null,
  "chunks_count": 24,
  "embedded_count": 24,
  "pages": 6,
  "supersedes_id": null,
  "created_at": "2026-08-08T01:12:04Z",
  "updated_at": "2026-08-08T01:12:31Z"
}
```

`chunks_count` y `embedded_count` son lo que hace **visible** el aprendizaje: la
UI los muestra avanzando. Cuando el documento se borra, la fila desaparece; ese
es el momento «lo olvidó».

---

## Endpoints

### `GET /api/health` — sin auth
```json
{ "ok": true, "db": true, "version": "0.1.0" }
```

### `GET /api/documents`
`?estado=ready&q=apendic&limit=50&offset=0` (todos opcionales)
```json
{ "documentos": [ Documento, … ], "total": 12 }
```
Orden por defecto: `created_at` descendente.

### `POST /api/documents` — multipart/form-data
Campos: `file` (obligatorio), `title` (opcional).
Acepta `.pdf .docx .md .txt`. Máximo 25 MB.

Responde **`202 Accepted`** con el `Documento` en estado `uploaded` — **no
espera al procesamiento**. La ingesta es asíncrona vía la cola de `jobs`; el
frontend sigue el progreso por SSE. Bloquear aquí haría que la subida de un PDF
de 40 páginas pareciera colgada.

Si el `sha256` coincide con un documento ya `ready`, se trata como **nueva
versión**: se acepta, y al llegar a `ready` el anterior pasa a `superseded`
(deja de ser recuperable al instante) y luego se borra. Nunca coexisten dos
versiones consultables del mismo contenido.

### `GET /api/documents/{id}`
`Documento`, más `"chunks_preview"`: los 3 primeros trozos con `heading` y los
200 primeros caracteres. Sirve para demostrar en pantalla *qué* aprendió.

### `DELETE /api/documents/{id}`
```json
{ "olvidado": true, "chunks_eliminados": 24 }
```
Borrado real e inmediato: `DELETE FROM documents` arrastra los chunks por
`ON DELETE CASCADE` en la misma transacción. El rastro queda en
`document_events`, que **no tiene FK** a propósito para sobrevivir al borrado.
404 si no existe.

### `GET /api/documents/stream` — SSE
`text/event-stream`. Un evento por cambio de estado:
```
event: documento
data: {"id":"…","status":"embedding","chunks_count":24,"embedded_count":11}

event: eliminado
data: {"id":"…"}

event: latido
data: {}
```
`latido` cada 15 s para que proxies y navegadores no cierren la conexión.
El token va por query string (`?token=…`): `EventSource` no admite cabeceras.

### `POST /api/rag/query`
```json
{ "consulta": "¿cuándo puedo ducharme?", "top_k": 4 }
```
```json
{
  "fragmentos": [
    { "documento_id":"…", "filename":"…", "heading":"Cuidado de la herida",
      "page":3, "contenido":"…", "score":0.87, "cita":"protocolo.pdf › Cuidado de la herida › p. 3" }
  ],
  "hay_evidencia": true,
  "ms": { "embedding": 24, "retrieval": 18, "rerank": 114, "total": 156 }
}
```
Es la prueba de aprender/olvidar **sin micrófono**: misma consulta antes y
después de borrar. `hay_evidencia: false` es la señal de que el agente debe
decir «no tengo esa información» en vez de improvisar.

### `GET /api/settings/voice-mode` · `PUT /api/settings/voice-mode`
```json
{ "modo": "local", "motor": "kokoro" }
```
`PUT` acepta `{"modo": "local" | "premium"}`. El backend existe ya en
`app/voice/voice_mode.py`. **Sin UI todavía** — Samuel pidió que la consola
cubra por ahora solo la gestión de documentos.

---

## Interfaz de la cola (para quien construya la API sin haber escrito el worker)

En `app/db/queue.py`:

```python
async def encolar(document_id: UUID, tipo: str = "ingest") -> UUID: ...
async def tomar_trabajo(conn) -> Job | None: ...   # FOR UPDATE SKIP LOCKED
async def completar(job_id: UUID) -> None: ...
async def fallar(job_id: UUID, error: str, reintentar: bool = True) -> None: ...
```

La API **solo** llama a `encolar()` tras guardar el archivo y crear la fila del
documento, dentro de la misma transacción. Si el commit falla, no queda ni
documento ni job.

---

## Cambios sobre el contrato

Cualquier agente que se desvíe lo anota aquí: qué cambió, por qué, y a quién
afecta.

<!-- añadir entradas debajo -->

### Fase 1 · cola de ingesta y parser — afecta a quien construya `/api/documents`

**1. `schema.sql` cambió. Hay que RECREAR las bases de datos.** Tres cambios,
todos aditivos:

- `jobs.run_after timestamptz NOT NULL DEFAULT now()` — sin esta columna el
  backoff no existe: un job que falla vuelve a `queued` y el worker lo reclama en
  el mismo milisegundo, quemando los tres intentos contra el mismo error.
- `jobs_queued_idx` pasa a llamarse `jobs_reclamables_idx` y cubre
  `(run_after, id) WHERE status IN ('queued','running')`, que son las dos ramas
  de la reclamación: los que esperan turno y los huérfanos de un worker muerto.
- `documents.pages integer` — **el contrato ya publicaba `pages` en el objeto
  `Documento` pero la tabla no tenía dónde guardarlo.** Solo el parser conoce ese
  dato, así que lo escribe el worker al promover a `ready`. Es NULL en `.docx`,
  `.md` y `.txt`, que no tienen paginación.

```bash
docker exec postop_db psql -U postop -d postgres -c "DROP DATABASE IF EXISTS postop;" -c "CREATE DATABASE postop OWNER postop;"
docker exec -i postop_db psql -U postop -d postop < backend/app/db/schema.sql
docker exec -i postop_db psql -U postop -d postop < backend/app/db/seed.sql
```

**2. Las firmas de `app/db/queue.py` cambian en dos puntos.** La interfaz que
publicaba este documento no era implementable tal cual:

| Antes | Ahora | Por qué |
|---|---|---|
| `encolar(document_id, tipo) -> UUID` | `encolar(document_id, tipo="ingest", conn=None) -> int` | `jobs.id` es `bigserial`, no uuid — y el orden de llegada **es** el id, así que un uuid aleatorio obligaría a ordenar por `created_at` para conservar el FIFO. El `conn` opcional es lo que hace posible lo que el propio contrato exige: **pásalo siempre** desde la transacción que crea el documento, o queda una ventana con el documento en `uploaded` y sin job. |
| `tomar_trabajo(conn)` | `tomar_trabajo(conn, worker="worker")` | Identifica quién reclamó, para diagnosticar workers colgados. |
| `completar(job_id: UUID)` · `fallar(job_id: UUID, ...)` | `job_id: int` | Mismo motivo que arriba. |

Añadidas: `latido(job_id)` y `cancelar_de_documento(document_id, conn=None)`.
**`DELETE /api/documents/{id}` no tiene que llamar a `cancelar_de_documento`**:
`ingest.olvidar_documento()` ya lo hace dentro de su transacción.

**3. `POST /api/documents` con un `sha256` repetido.** El contrato dice que la
versión anterior «pasa a `superseded` y luego se borra». El worker hace lo
primero y **no** lo segundo: la deja en `superseded` (irrecuperable al instante,
que es lo que importa) y le pone `supersedes_id` a la nueva. Borrarla destruiría
el rastro de qué versión estuvo activa y cuándo, que en contexto clínico es
justo lo que hay que poder enseñar. Si se quiere el borrado físico, es una
llamada explícita a `olvidar_documento()` sobre la versión vieja — decisión
pendiente para Samuel.

**4. Formatos aceptados: la extensión manda sobre el `mime_type`.** Los
navegadores mandan `application/octet-stream` para `.md` casi siempre; con el
mime como fuente primaria un markdown bien estructurado acabaría en el camino del
PDF y fallaría al abrirlo. La validación de `formato_no_soportado` debería usar
`app.rag.parsing.formato_de(path, mime)`, que ya implementa esa precedencia.

**5. El bucle de voz necesita dos rutas nuevas en `app/main.py` (agente de voz).**
No las he añadido yo: `app/main.py` es de otro agente. Están construidas y
probadas en `app/voice/pipeline_ws.py`; falta montarlas.

| Ruta | Qué es | Cómo se monta |
|---|---|---|
| `WS /ws/voz` | Bucle de voz de la Opción B (WebSocket propio). Audio PCM int16 LE mono **16 kHz** del navegador al servidor; PCM int16 LE mono **24 kHz** del servidor al navegador; control en JSON. | `from app.voice.pipeline_ws import crear_router` y `app.include_router(crear_router())` |
| `POST /api/voz/offer` | Solo si se elige la Opción A (Pipecat + WebRTC). Recibe la oferta SDP y devuelve la respuesta. | `pipecat.transports.smallwebrtc.request_handler` + `app.voice.pipeline_pipecat.crear_transporte_webrtc()` |

`/ws/voz` **no lleva `X-Admin-Token`**: un WebSocket de navegador no puede
enviar cabeceras. Si hace falta autenticarlo, va por query string igual que el
SSE de documentos (`?token=…`).

Mensajes de control que manda el servidor por `/ws/voz` (el cliente de prueba en
`scripts/spikes/cliente_voz/index.html` los implementa todos):

| `tipo` | Cuándo | Qué debe hacer la UI |
|---|---|---|
| `listo` | al conectar | guardar `sample_rate_salida` |
| `paciente_habla` | el VAD detecta voz | indicador «escuchando» |
| `fin_de_turno` | el paciente calló | indicador «pensando» |
| `transcripcion` | Whisper terminó | pintar lo que dijo el paciente |
| `agente_habla` | empieza a salir audio | indicador «hablando» |
| `parar` | **barge-in** | **vaciar el buffer de audio ya recibido** |
| `fin_audio` | terminó la respuesta | pintar el texto del agente |

`parar` es obligatorio de implementar: sin vaciar el buffer del cliente, el
agente sigue sonando lo que le quede encolado (medido: 5,4 s) y el corte del
servidor no se oye.
