# Agente de Voz para Seguimiento Postoperatorio

**Tech Sphere Challenge 2026 — Samuel García**

Sistema inteligente de voz para llamadas automatizadas de seguimiento clínico postoperatorio. El agente realiza llamadas telefónicas/WebSockets a pacientes operados, verifica su identidad sin revelar datos sensibles, evalúa su recuperación mediante un guion adaptativo, responde dudas clínicas consultando protocolos médicos mediante RAG vectorial y **escala inmediatamente al equipo médico ante cualquier signo de alarma determinista**.

La voz (STT/TTS), la base de datos vectorial y el motor RAG corren **100% en local**. El texto procesado por el agente utiliza Gemini 2.5 Flash en la nube con razonamiento optimizado.

Una consola de administración y panel de llamadas alimentan la base de conocimiento: **lo que se sube el agente lo aprende, lo que se borra lo olvida** de forma inmediata y transaccional (`ON DELETE CASCADE` en Postgres).

---

## 🏗️ Stack Tecnológico y Justificación de Arquitectura

| Capa / Componente | Elección Tecnológica | Justificación y Medición de Selección |
|---|---|---|
| **Modelo de Lenguaje (LLM)** | **Gemini 2.5 Flash** (Primario) <br> *Groq Llama 3.3 70B (Respaldo)* | **Gemini 2.5 Flash** ofrece el mejor español clínico y *tool calling* estructurado sin fallos. Con `thinking_budget=0` (razonamiento apagado), el TTFT se reduce de **956 ms a 462 ms (2.1× más rápido)**. Groq queda como respaldo de disponibilidad. |
| **Búsqueda Vectorial & Base de Datos** | **Postgres 16 + `pgvector`** (HNSW) | Convierte la garantía *"borrar = olvidar"* en una propiedad ACID de base de datos (`ON DELETE CASCADE` en `chunks`), evitando ventanas de desincronización entre Postgres y un Vector DB externo (e.g. Qdrant/Chroma). |
| **Generación de Embeddings** | **`BAAI/bge-m3`** (sobre Apple MPS / PyTorch) | 1024 dimensiones, soporte multilingüe avanzado. Procesa consultas RAG en solo **24-26 ms**. |
| **Re-ranker & Grounding** | **`BAAI/bge-reranker-v2-m3`** + RRF Normalizado | *Hallazgo empírico en Golden Set*: La búsqueda híbrida con RRF normalizado ($0..1$) alcanza **100% Groundedness Rate** sin reranker, reduciendo la latencia de **580 ms a 31 ms (-549 ms, -94.7%)**. |
| **Búsqueda Híbrida** | **Denso (pgvector) + Léxico (Postgres FTS) + RRF** | Fusiona búsqueda semántica con emparejamiento léxico exacto (e.g., dosis de medicamentos como *"cefalexina 500 mg"*). |
| **Reconocimiento de Voz (STT)** | **Whisper `small` (MLX)** + Prompt Clínico | 481 ms de latencia con la misma precisión médica que `medium` (1222 ms). El prompt `VOCABULARIO_CLINICO` sesga la decodificación para términos complejos (*"apendicectomía"*). |
| **Síntesis de Voz (TTS)** | **`VoiceRouter`**: Local (**Kokoro-82M**) / Premium (**ElevenLabs Flash**) | Conmutación dinámica en caliente con fallback a local y contabilidad en `tts_usage`. Kokoro local responde en **196-303 ms** a la primera frase. |
| **Detección de Silencio (VAD)** | **Silero VAD** (ONNX en CPU) | Detección de inicio y fin de turno con confirmación de 640 ms. Permite interrupción (**barge-in**) instantánea (< 96 ms). |
| **Seguridad y Banderas Rojas** | **Detector Determinista NegEx** (`redflags.py`) | Filtra 7 familias de alarmas médicas en **< 1 ms** sin pasar por el LLM. Fiebre `>= 38.5`, sangrado activo, dehiscencia, disnea, dolor torácico e infección interrumpen de inmediato. |
| **Verificación de Identidad** | **Herramienta `verificar_identidad`** (Postgres) | El número de Cédula de Ciudadanía (`documento_cc`) expresado por el paciente se valida en Postgres mediante normalización numérica estricta, sin arriesgar alucinaciones ni exponer datos sensibles en el prompt. |
| **Cola de Ingesta Asíncrona** | **Postgres `FOR UPDATE SKIP LOCKED`** | Garantiza procesamiento asíncrono sin dependencias externas (Redis/Celery). Las versiones sustituidas (`superseded`) eliminan físicamente sus vectores conservando la auditoría en `document_events`. |
| **Backend & Servidor** | **Python 3.12 + FastAPI + Uvicorn** | Asincronía completa, gestión de WebSockets en `/ws/voz` y eventos SSE en vivo (`/api/documents/events`). |
| **Frontend Web App** | **React + Vite + TypeScript + Tailwind + shadcn/ui** | Tres interfaces integradas: Consola de Administración (`/admin`), Llamada en Vivo (`/call`) y Historial (`/calls`). |

---

## ⚡ Presupuesto de Latencia Medido

Mediciones reales obtenidas en **MacBook Air M4 (16 GB RAM)** con la suite completa en ejecución:

| Etapa del Pipeline de Voz | Latencia Mediana | Descripción / Notas |
|---|---:|---|
| **Confirmación Fin de Turno (Silero VAD)** | **640 ms** | Tiempo de espera deliberado para asegurar que el paciente concluyó su frase. |
| **Transcripción STT (Whisper `small` + prompt)** | **391 ms** | Solapado con el procesamiento de audio entrante. |
| **Embedding de Consulta (`bge-m3`)** | **25 ms** | Generación de vector semántico de 1024 dims. |
| **Búsqueda Híbrida (`pgvector` + FTS + RRF)** | **4 ms** | Consulta en vista `retrievable_chunks`. |
| **Evaluación Grounding (`hay_evidencia`)** | **< 1 ms** | Validación del umbral de relevancia $0..1$. |
| **Reranker Cross-Encoder (`bge-reranker`)** | *(549 ms)* | *Opcional*: Desactivado por defecto (`RERANK_ENABLED=false`) para ahorrar 549 ms. |
| **Respuesta LLM TTFT (Gemini 2.5 Flash)** | **462 ms** | Tiempo hasta el primer token con `thinking_budget=0`. |
| **Síntesis TTS 1ª Frase (Kokoro `ef_dora`)** | **196-303 ms** | Generación de audio streaming para la primera oración. |
| **TOTAL HASTA PRIMER AUDIO (Sin Reranker)** | **≈ 1.5 s** | **Experiencia de conversación fluida y natural.** |

---

## 📊 Métricas Obligatorias de Producción (Rúbrica §5)

Medidas de forma automatizada mediante script `eval/benchmark_metricas.py`:

### 1. Latencia de Respuesta
- **P50 (Mediana)**: **1.74 s (1,742 ms)** desde que el paciente termina de hablar hasta el primer audio.
- **P95 (95% Percentil)**: **1.83 s (1,833 ms)** en el 95% de los turnos de conversación.

### 2. Consumo de Tokens e Invocaciones
- **Tokens de Entrada por Turno**: ~840 tokens (Prompt de sistema de 3,354 caracteres + historial de turnos).
- **Tokens de Salida por Turno**: ~42 tokens (Respuestas concisas habladas de 1-2 frases).
- **Consumo Total por Llamada (15 turnos)**: **12,600 tokens de entrada** y **630 tokens de salida**.
- **Invocaciones al LLM por Turno**: **1.0 invocación**.
- **Consultas al RAG por Llamada**: **1 a 3 consultas** (solo cuando el paciente formula dudas clínicas).

### 3. Costo Estimado por Llamada
- **Modelo LLM (Gemini 2.5 Flash)**:
  - Input: $0.075 / 1M tokens ($0.000945 USD por llamada).
  - Output: $0.300 / 1M tokens ($0.000189 USD por llamada).
  - **Costo Total LLM por Llamada**: **$0.00113 USD (~$4.54 COP)**.
- **Servicios de Voz & RAG Local**: **$0.00 USD** (STT Whisper `small`, TTS Kokoro-82M, VAD Silero y Postgres `pgvector` corren 100% en infraestructura local).

---

## 🔄 Flujo de Funcionamiento de la Solución

```mermaid
sequenceDiagram
    autonumber
    actor P as Paciente / Usuario
    participant WS as WebSocket (/ws/voz)
    participant VAD as Detector VAD / STT (Whisper)
    participant RF as Red Flags (Detector NegEx)
    participant AG as Agente Clínico (Gemini 2.5)
    participant DB as Postgres (pgvector + FTS)
    participant TTS as Motor TTS (VoiceRouter)

    P->>WS: Conexión WebSocket (call_id)
    WS->>P: Evento "listo" + Saludo AI Act (Audio Streaming)
    Note over P,WS: "Buenos días. Hablo con el paciente..."

    P->>WS: Audio de voz ("Sí, habla con ella, mi cédula es 1012345678")
    WS->>VAD: Procesamiento PCM16 (Silero VAD + Whisper small)
    VAD->>RF: Transcripción de usuario
    
    RF->>RF: Evaluación de Banderas Rojas (< 1 ms)

    alt ¿Bandera Roja Detectada? (e.g. "Tengo 38.5 de fiebre y pus")
        RF->>DB: Inserción de turno "system" en call_turns
        RF->>WS: Interrupción inmediata + Protocolo de Alarma
        WS->>TTS: Sintetizar instrucciones de urgencia
        TTS-->>P: Reproducción de Audio de Alarma & Cierre de llamada
    else Proceso Normal de Conversación
        RF->>AG: Enviar transcripción al Agente
        AG->>DB: Tool Call: verificar_identidad(documento_dicho="1012345678")
        DB-->>AG: Resultado: {coincide: true, paciente: "..."}

        alt ¿Identidad NO Verificada? (Cédula Incorrecta)
            AG->>TTS: "Los datos no coinciden. Volveré a llamar luego."
            TTS-->>P: Reproducción de aviso de seguridad & Colgado
        else Identidad Verificada (Seguimiento Postoperatorio)
            AG->>DB: Tool Call: buscar_protocolo("¿cuándo puedo bañarme?")
            Note over DB: Búsqueda Híbrida Vectorial (bge-m3) + FTS en retrievable_chunks
            DB-->>AG: Fragmentos de protocolo con citas clínicas
            AG->>TTS: Streaming de respuesta por oraciones
            TTS-->>P: Reproducción de Audio en vivo
        end
    end
```

---

## 🛠️ Guía de Arranque Rápido y Pruebas Integrales

### 1. Requisitos Previos

- **Docker & Docker Compose** (para la base de datos Postgres con pgvector).
- **Python 3.12** y paquete `uv` (`pip install uv` o `brew install uv`).
- **Node.js 18+** y `npm`.
- **Librerías de sistema**: `ffmpeg` (para Whisper) y `espeak-ng` (para Kokoro TTS en español):
  ```bash
  brew install ffmpeg espeak-ng
  ```

### 2. Configuración del Entorno y Base de Datos

```bash
# 1. Iniciar el contenedor de Postgres 16 con pgvector (puerto 5433)
docker compose up -d

# 2. Configurar variables de entorno
cp .env.example .env
# Editar .env y definir GEMINI_API_KEY=tu_api_key_de_gemini

# 3. Instalar dependencias del backend (incluyendo el grupo voice)
cd backend
uv sync --all-groups

# 4. Sembrar datos del dataset oficial del concurso (o app/db/seed.sql)
uv run python -m app.db.seed_official
```

### 3. Ejecución de la Batería de Pruebas Automáticas (100% Verde)

```bash
# Ejecutar la suite completa de 424 pruebas (289 pasadas, 135 ignoradas por mocks)
cd backend
uv run --all-groups pytest
```

### 4. Pruebas Integrales de Evaluación RAG y Olvido

```bash
# A. Prueba de Garantía de Olvido (Subes = aprende / Borras = olvida)
cd backend
uv run python ../scripts/demo_aprender_olvidar.py

# B. Evaluación del RAG sobre el Golden Set de 30 Preguntas Clínicas
cd backend
DATABASE_URL="postgresql://postop:postop@localhost:5433/postop" uv run python ../eval/evaluar_rag.py
```

### 5. Arranque del Servidor Backend y Aplicación Frontend

```bash
# Terminal 1: Iniciar Servidor Backend con Soporte de Voz
cd backend
VOZ=1 DATABASE_URL="postgresql://postop:postop@localhost:5433/postop" uv run uvicorn app.main:app --port 8000

# Terminal 2: Iniciar Worker de Ingesta RAG en Segundo Plano
cd backend
DATABASE_URL="postgresql://postop:postop@localhost:5433/postop" uv run python -m app.workers.ingest_worker

# Terminal 3: Iniciar Servidor Web de Desarrollo Frontend
cd frontend
npm install
npm run dev
```

Navega a **`http://localhost:5173/call`** en el navegador para iniciar la prueba integral de llamada postoperatoria con micrófono real.

---

## 📄 Documentación de Referencia

- **[Informe Final de la Solución (Google Docs)](https://docs.google.com/document/d/16hzJBmPZSF56jsglDQzQfcD03emEPNz3KgYkTRxIWho/edit?usp=sharing)**: Documento oficial de informe final con justificaciones de arquitectura, evidencias, prompts y modelo seleccionado.
- **[BITACORA.md](BITACORA.md)**: Historial completo de desarrollo, justificación de las 10 decisiones clínicas y evolución del proyecto.
- **[eval/guion_demo.md](eval/guion_demo.md)**: Guía paso a paso para la demostración en vivo.
- **[eval/eval_rag_results.md](eval/eval_rag_results.md)**: Reporte cuantitativo de Recall@k y latencias del motor RAG.
