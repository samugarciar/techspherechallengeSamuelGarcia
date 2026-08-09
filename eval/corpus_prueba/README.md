# Corpus PROVISIONAL — se sustituye por los documentos reales

> **Estos protocolos NO son material clínico.** Están escritos para tener con qué
> ejercitar el camino de ingesta mientras llega el corpus real. En cuanto Samuel
> traiga los documentos definitivos, **este directorio se vacía y se sustituye**:
> nada de aquí debe acabar en la demo ni en la evaluación del RAG.

## Qué hay

Tres protocolos posoperatorios en español, uno por cada cirugía de
`backend/app/db/seed.sql`, para que las preguntas de prueba tengan respuesta:

| Archivo | Cirugía | Paciente de la semilla |
|---|---|---|
| `protocolo_apendicectomia.md` | Apendicectomía laparoscópica | María, 3 días de posoperatorio |
| `protocolo_colecistectomia.md` | Colecistectomía laparoscópica | Jorge, 7 días |
| `protocolo_herniorrafia.md` | Herniorrafia inguinal con malla | Lucía, 1 día |

Las dosis coinciden con las de `medications` en la semilla, para que el agente no
tenga que elegir entre lo que dice el protocolo y lo que dice la base de datos.

Cada uno lleva las cinco secciones del guion de seguimiento — cuidado de la
herida, medicación, signos de alarma, actividad y dieta — porque el agente
pregunta por ellas en ese orden.

## Los tres casos difíciles, puestos a propósito

El corpus no es una muestra amable: incluye las tres formas que rompen el
troceado o el parseo, para que se rompan aquí y no en la demo.

1. **Tabla de dosis.** Una tabla markdown tiene muy poca puntuación de fin de
   frase, así que `chunking._partir_largo()` no encuentra por dónde cortarla. Si
   una tabla crece por encima de `MAX_CHARS` sale como un trozo único
   sobredimensionado. Además obliga al parser de PDF a reconstruir la rejilla:
   una tabla aplanada a texto convierte «Cefalexina 500 mg cada 8 horas» en tres
   columnas sueltas y el agente pierde la asociación fármaco-dosis.

2. **Lista numerada** (`### Cómo curar la herida en casa`, en apendicectomía). Es
   una secuencia ordenada: si el parser pierde los números o el troceado la parte
   por la mitad, el agente puede dictar el paso 4 sin el paso 2.

3. **Sección muy corta** (`### Cuándo llamar al equipo`, `### Sangrado`,
   `### Retención urinaria`). Está por debajo de `MIN_CHARS`, así que se funde con
   un vecino. Es el fallo clínico que documenta el docstring de
   `chunking.trocear()`: si se fundiera con el **hermano** siguiente en vez de con
   su **padre**, una regla de alarma acabaría archivada bajo «Medicación» y el
   agente la citaría con la sección equivocada por voz.

## Generar los .pdf y los .docx

Los `.md` son la fuente; los demás formatos se derivan de ellos para ejercitar
los tres caminos de `app/rag/parsing.py` (PDF, DOCX y texto):

```bash
cd backend && uv run python ../eval/corpus_prueba/generar.py
```

Los `.pdf` y `.docx` se regeneran; no se editan a mano.
