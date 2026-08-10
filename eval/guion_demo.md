# Guion y Manual de Demostración En Vivo (Demo Day)

Este documento es el **guion paso a paso** para realizar la demostración del proyecto ante evaluadores o en un entorno de presentación.

---

## 🎯 Caso de Uso Demostrado

Seguimiento telefónico postoperatorio automatizado asistido por IA, con las siguientes garantías:
1. **Cumplimiento AI Act**: Notificación obligatoria al inicio de la llamada de que se trata de un sistema automatizado.
2. **Privacidad estricta**: Verificación de identidad con fecha de nacimiento (`verificar_identidad` en Postgres) antes de revelar información médica.
3. **Seguridad clínica determinista**: Detección inmediata (< 1 ms) de banderas rojas clínicas (fiebre `>= 38.5`, dehiscencia, sangrado activo, disnea, dolor torácico, pus/infección) sin pasar por el LLM.
4. **Garantía RAG "Subes = aprende / Borras = olvida"**: Olvido instantáneo en la misma transacción mediante `ON DELETE CASCADE` en la base de datos vectorial (`pgvector`).
5. **Transparencia y trazabilidad**: Desglose de latencias por etapa (`stt`, `retrieval`, `llm`, `tts`), citas explícitas de protocolo y conmutación de voz local/premium.

---

## 📋 Escenarios de la Demostración

### Escenario 1: Consola RAG — "Subes = Aprende / Borras = Olvida"

**Objetivo**: Demostrar la ingesta de documentos clínicos y la garantía matemática de borrado.

1. **Arrancar la aplicación**:
   ```bash
   cd backend && DATABASE_URL=postgresql://postop:postop@localhost:5433/postop uv run uvicorn app.main:app --port 8000
   ```
2. **Ejecutar el script automatizado de garantía de olvido**:
   ```bash
   cd backend && uv run python ../scripts/demo_aprender_olvidar.py
   ```
3. **Lo que se observa en pantalla**:
   - Subida del documento `protocolo_apendicectomia.pdf`.
   - Transición de estados: `uploaded` ➔ `parsing` ➔ `chunking` ➔ `embedding` ➔ `ready`.
   - Consulta RAG sobre ducha postoperatoria: devuelve **evidencia verdadera** con cita al protocolo.
   - Borrado del documento (`DELETE /api/documents/{id}`).
   - Repetición de la consulta: devuelve `hay_evidencia: false` y **0 fragmentos**, obligando al agente a responder que no tiene esa información en lugar de inventar (cero alucinación).

---

### Escenario 2: Seguimiento Normal y Verificación de Identidad

**Objetivo**: Mostrar una llamada completa de seguimiento postoperatorio sin incidencias.

1. **Abrir la web app**: Navegar a `http://localhost:5173/call` (o iniciar la demo con `scripts/demo_llamada_completa.py`).
2. **Selección de paciente**: Elegir a **María** (Apendicectomía laparoscópica).
3. **Paso a paso de la llamada**:
   - **Agente**: *"Buenos días. Le llamo del servicio de seguimiento postoperatorio... Soy un asistente automatizado... ¿Hablo con María?"*
   - **Paciente**: *"Sí, soy María."*
   - **Agente**: *"Para confirmar que hablo con la persona correcta, ¿me dice su fecha de nacimiento?"*
   - **Paciente**: *"Catorce de mayo de mil novecientos noventa."*
   - **Verificación**: El backend ejecuta `verificar_identidad("1990-05-14")` en Postgres. Coincide ➔ procede al encuadre clínico.
   - **Preguntas del guion**:
     - Pain scale (1-10) y control del dolor.
     - Estado de la herida.
     - Fiebre y temperatura.
     - Tránsito intestinal y tolerancia digestiva.
     - Medicación y dudas.
   - **Respuesta RAG a duda**: Si la paciente pregunta *"¿cuándo puedo volver a conducir?"*, el agente busca en RAG y responde con precisión fundamentada en el protocolo de apendicectomía.

---

### Escenario 3: Detección Determinista de Bandera Roja (Escalamiento Clínico)

**Objetivo**: Probar la interrupción instantánea del cuestionario ante un síntoma de alarma.

1. **Paciente menciona un signo de alarma**:
   - *"Mire, me tomé la temperatura y tengo 38.5 de fiebre y la herida me huele mal."*
2. **Comportamiento del sistema**:
   - `redflags.py` detecta la fiebre `>= 38.5` y el signo de infección de forma determinista en **< 1 ms**.
   - El agente interrumpe de inmediato el cuestionario.
   - Se guarda una entrada `system` en el historial de la llamada (`call_turns`).
   - El agente emite la recomendación del protocolo, verifica la comprensión del paciente y cierra la llamada marcándola como `escalated` con urgencia `urgente`.

---

### Escenario 4: Panel de Resumen, Citas y Trazas

**Objetivo**: Demostrar la transparencia y auditoría final.

1. Al terminar la llamada, la interfaz web muestra:
   - Resumen del seguimiento (respuestas al cuestionario estructurado).
   - Estado final (`escalated` o `completed`).
   - Transcripción completa incluyendo turnos del agente, paciente y avisos de sistema.
   - **Citas del RAG**: Documento, sección y página de donde el agente obtuvo la información.
   - **Latencias por etapa**: Tiempo consumido en STT, Búsqueda, Reranking, TTFT de Gemini y TTS.

---

## 🛠️ Comandos de Respaldo para la Demo

```bash
# 1. Verificación rápida de la suite de pruebas (289 tests en verde)
cd backend && uv run pytest

# 2. Evaluación completa del RAG con el Golden Set de 30 preguntas
cd backend && DATABASE_URL=postgresql://postop:postop@localhost:5433/postop uv run python ../eval/evaluar_rag.py

# 3. Ensayo de llamada completa interactiva
cd backend && uv run python ../scripts/demo_llamada_completa.py
```
