-- ============================================================================
-- Agente de voz para seguimiento postoperatorio — schema
--
-- Principio rector: "subir = aprender / borrar = olvidar" NO es lógica de
-- aplicación, es una propiedad del schema. Dos mecanismos lo garantizan:
--
--   1. chunks.document_id -> documents(id) ON DELETE CASCADE
--      El vector vive en la misma fila que el chunk. Borrar el documento
--      borra sus vectores en la MISMA transacción. No hay ventana.
--
--   2. La vista `retrievable_chunks` (abajo) es el ÚNICO punto de lectura
--      del RAG. Filtra por status='ready'. Aunque un bug dejara chunks
--      huérfanos, son irrecuperables por construcción.
--
-- El código de retrieval NUNCA debe consultar la tabla `chunks` directamente.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ============================================================================
-- CONOCIMIENTO NO ESTRUCTURADO  ->  RAG vectorial
-- ============================================================================

CREATE TABLE documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        text        NOT NULL,
    mime_type       text        NOT NULL,
    sha256          text        NOT NULL,
    size_bytes      bigint      NOT NULL,
    storage_path    text        NOT NULL,

    -- Máquina de estados:
    --   uploaded -> parsing -> chunking -> embedding -> ready
    --                                                     |
    --            failed <-/                    superseded <-
    status          text        NOT NULL DEFAULT 'uploaded'
                    CHECK (status IN ('uploaded','parsing','chunking',
                                      'embedding','ready','failed','superseded')),
    error_message   text,

    -- Contadores que la consola muestra. Es la evidencia visible de que el
    -- agente aprendió (>0) o de que olvidó (documento desaparecido).
    chunks_count    integer     NOT NULL DEFAULT 0,
    embedded_count  integer     NOT NULL DEFAULT 0,

    -- Versionado: re-subir el mismo archivo crea una fila nueva y marca la
    -- anterior como 'superseded' (deja de ser retrievable de inmediato).
    supersedes_id   uuid        REFERENCES documents(id) ON DELETE SET NULL,

    title           text,
    -- Páginas del original. Solo el parser las conoce, y el contrato de API las
    -- publica en el objeto `Documento`. NULL en los formatos sin paginación
    -- (.docx, .md, .txt): ahí la cita se queda en "archivo › sección".
    pages           integer,
    uploaded_by     text        NOT NULL DEFAULT 'admin',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX documents_status_idx  ON documents (status);
CREATE INDEX documents_sha256_idx  ON documents (sha256);
-- Un solo documento 'ready' por contenido: evita duplicados silenciosos.
CREATE UNIQUE INDEX documents_active_sha_idx
    ON documents (sha256) WHERE status = 'ready';


CREATE TABLE chunks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- ON DELETE CASCADE: ésta es la garantía de olvido.
    document_id   uuid        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

    ordinal       integer     NOT NULL,          -- posición dentro del documento
    content       text        NOT NULL,

    -- Procedencia, para que el agente pueda citar en voz:
    -- "según el protocolo de alta, sección cuidado de herida"
    heading       text,
    page          integer,

    -- bge-m3 = 1024 dimensiones
    embedding     vector(1024),

    -- Léxico español para la mitad BM25 de la búsqueda híbrida.
    -- Generado por Postgres: imposible que se desincronice del content.
    content_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('spanish', content)) STORED,

    token_count   integer,
    created_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (document_id, ordinal)
);

CREATE INDEX chunks_document_id_idx ON chunks (document_id);
CREATE INDEX chunks_tsv_idx         ON chunks USING gin (content_tsv);
-- HNSW sobre distancia coseno. m/ef_construction conservadores: el corpus es
-- de decenas de documentos, no de millones.
CREATE INDEX chunks_embedding_idx   ON chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);


-- ----------------------------------------------------------------------------
-- EL ÚNICO PUNTO DE LECTURA DEL RAG.
-- Todo retrieval consulta esta vista. Nunca `chunks` directamente.
-- ----------------------------------------------------------------------------
CREATE VIEW retrievable_chunks AS
SELECT
    c.id,
    c.document_id,
    c.ordinal,
    c.content,
    c.heading,
    c.page,
    c.embedding,
    c.content_tsv,
    d.filename,
    d.title
FROM chunks c
JOIN documents d ON d.id = c.document_id
WHERE d.status = 'ready'
  AND c.embedding IS NOT NULL;


-- Auditoría. En contexto clínico esto es lo que separa un prototipo de algo
-- defendible: quién subió qué, cuándo, y cuándo dejó de estar disponible.
CREATE TABLE document_events (
    id           bigserial PRIMARY KEY,
    document_id  uuid        NOT NULL,   -- SIN FK: el rastro sobrevive al borrado
    filename     text        NOT NULL,
    event        text        NOT NULL
                 CHECK (event IN ('uploaded','parsing','chunking','embedding',
                                  'ready','failed','superseded','deleted')),
    detail       text,
    actor        text        NOT NULL DEFAULT 'admin',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX document_events_doc_idx ON document_events (document_id, created_at DESC);


-- ============================================================================
-- DATOS VIVOS ESTRUCTURADOS  ->  function calling, NUNCA RAG
--
-- Estas tablas se leen con tools (obtener_paciente, obtener_cirugia...), no
-- por búsqueda vectorial. Un vector es una foto del momento de la ingesta;
-- la fecha de una cita o una medicación activa tienen que leerse frescas.
-- ============================================================================

CREATE TABLE patients (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name       text        NOT NULL,
    birth_date      date,
    phone           text,
    preferred_name  text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE surgeries (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id        uuid        NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    procedure_name    text        NOT NULL,
    performed_at      date        NOT NULL,
    surgeon           text,
    discharge_notes   text,
    -- Protocolo de seguimiento que aplica a esta cirugía; el agente lo usa
    -- para acotar la búsqueda en el RAG.
    protocol_tag      text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX surgeries_patient_idx ON surgeries (patient_id);

CREATE TABLE medications (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    surgery_id    uuid        NOT NULL REFERENCES surgeries(id) ON DELETE CASCADE,
    name          text        NOT NULL,
    dose          text        NOT NULL,
    schedule      text        NOT NULL,
    starts_on     date,
    ends_on       date,
    active        boolean     NOT NULL DEFAULT true
);

CREATE INDEX medications_surgery_idx ON medications (surgery_id);

CREATE TABLE appointments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id    uuid        NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    scheduled_at  timestamptz NOT NULL,
    location      text,
    purpose       text,
    status        text        NOT NULL DEFAULT 'scheduled'
                  CHECK (status IN ('scheduled','completed','cancelled'))
);

CREATE INDEX appointments_patient_idx ON appointments (patient_id, scheduled_at);


-- ============================================================================
-- LLAMADAS
-- ============================================================================

CREATE TABLE calls (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id    uuid        REFERENCES patients(id) ON DELETE SET NULL,
    status        text        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','completed','escalated','failed')),
    started_at    timestamptz NOT NULL DEFAULT now(),
    ended_at      timestamptz,
    -- Respuestas del guion de seguimiento (dolor, herida, fiebre, ...)
    survey        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    escalated     boolean     NOT NULL DEFAULT false,
    escalation_reason text,
    escalation_urgency text
                  CHECK (escalation_urgency IN ('rutina','prioritaria','urgente'))
);

CREATE INDEX calls_patient_idx ON calls (patient_id, started_at DESC);

CREATE TABLE call_turns (
    id          bigserial PRIMARY KEY,
    call_id     uuid        NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    role        text        NOT NULL CHECK (role IN ('agent','patient','system')),
    content     text        NOT NULL,
    -- Chunks usados para fundamentar esta respuesta. Permite auditar
    -- "¿de dónde sacó eso el agente?" y detectar respuestas sin evidencia.
    citations   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    -- Latencias por etapa, en ms: {"stt":180,"retrieval":95,"llm_ttft":420,...}
    latencies   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX call_turns_call_idx ON call_turns (call_id, created_at);


-- ============================================================================
-- COLA DE INGESTA — Postgres con SKIP LOCKED, sin Redis ni Celery.
-- El estado del job vive en la misma transacción que el documento, que es
-- justo lo que la consola necesita leer.
-- ============================================================================

CREATE TABLE jobs (
    id            bigserial PRIMARY KEY,
    kind          text        NOT NULL,          -- 'ingest_document'
    payload       jsonb       NOT NULL,
    status        text        NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','done','failed')),
    attempts      integer     NOT NULL DEFAULT 0,
    max_attempts  integer     NOT NULL DEFAULT 3,
    last_error    text,

    -- No antes de este instante. Es lo que convierte un reintento en backoff:
    -- sin esta columna un job que falla vuelve a 'queued' y el worker lo
    -- reclama en el mismo milisegundo, quemando los 3 intentos en un bucle
    -- cerrado contra el mismo error. Ver app/db/queue.py:fallar().
    run_after     timestamptz NOT NULL DEFAULT now(),

    -- Cuándo y quién lo reclamó. `locked_at` hace de claimed_at: un worker que
    -- muera a media faena deja el job en 'running' para siempre, así que otro
    -- worker lo recupera por antigüedad. El worker vivo refresca `locked_at` en
    -- cada cambio de estado (queue.latido) para no ser confundido con un muerto.
    locked_at     timestamptz,
    locked_by     text,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Cubre las dos ramas de la reclamación: los 'queued' que ya cumplieron su
-- backoff y los 'running' abandonados por un worker muerto.
CREATE INDEX jobs_reclamables_idx ON jobs (run_after, id)
    WHERE status IN ('queued', 'running');


-- ============================================================================
-- TRAZAS — sustituye a Langfuse self-hosted.
-- Motivo: Langfuse v3 arrastra ClickHouse + Redis + MinIO, y esta máquina
-- tiene 16 GB compartidos con Whisper, bge-m3 y el reranker. Guardar las
-- trazas aquí cuesta ~0 RAM y además se pueden mostrar en la propia consola.
-- ============================================================================

CREATE TABLE traces (
    id           bigserial PRIMARY KEY,
    call_id      uuid        REFERENCES calls(id) ON DELETE CASCADE,
    span         text        NOT NULL,           -- 'stt' | 'retrieval' | 'rerank' | 'llm' | 'tts'
    duration_ms  integer     NOT NULL,
    metadata     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX traces_call_idx ON traces (call_id, created_at);
CREATE INDEX traces_span_idx ON traces (span, created_at DESC);


-- ============================================================================
-- AJUSTES EN CALIENTE — conmutables desde la consola sin reiniciar nada.
--
-- El caso principal es el modo de voz: `local` usa Kokoro (gratis, ilimitado,
-- sin red) y `premium` usa un motor de nube (mejor calidad, coste por
-- carácter). Vive en la base de datos y no en .env justo para que el admin
-- pueda cambiarlo en mitad de una llamada.
-- ============================================================================

CREATE TABLE app_settings (
    key        text        PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text        NOT NULL DEFAULT 'admin'
);

INSERT INTO app_settings (key, value) VALUES ('voice_mode', 'local');


-- Consumo de TTS por modo. Sin esto el toggle es un interruptor a ciegas:
-- con esto la consola puede mostrar cuánto está costando el modo premium y
-- decidir con datos cuándo merece la pena.
CREATE TABLE tts_usage (
    id          bigserial PRIMARY KEY,
    call_id     uuid        REFERENCES calls(id) ON DELETE SET NULL,
    mode        text        NOT NULL CHECK (mode IN ('local','premium')),
    engine      text        NOT NULL,
    characters  integer     NOT NULL,
    duration_ms integer     NOT NULL,
    audio_s     real        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tts_usage_mode_idx ON tts_usage (mode, created_at DESC);


-- updated_at automático
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER documents_touch BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER jobs_touch BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
