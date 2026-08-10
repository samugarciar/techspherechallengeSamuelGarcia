# Reporte de Evaluación RAG — Golden Set (30 Preguntas)

Fecha de ejecución: 2026-08-10 00:04:14

## Resumen Ejecutivo de Métricas

| Métrica | Con Reranker (`bge-reranker-v2-m3`) | Sin Reranker (Híbrido Denso + FTS) | Diferencia |
|---|---:|---:|---:|
| **Recall@1** | **14/30 (46.7%)** | 19/30 (63.3%) | -5 |
| **Recall@3** | **28/30 (93.3%)** | 27/30 (90.0%) | +1 |
| **Recall@5** | **30/30 (100.0%)** | 28/30 (93.3%) | +2 |
| **Groundedness Rate** | **12/30 (40.0%)** | 30/30 (100.0%) | -18 |
| **Latencia Mediana Total** | **580 ms** | **31 ms** | **-549 ms (-94.7%)** |

## Desglose de Latencias por Etapa (Mediana)

- **Embedding (`BAAI/bge-m3`)**: `26.8 ms`
- **Búsqueda Híbrida (`Postgres pgvector + FTS + RRF`)**: `4.2 ms`
- **Cross-Encoder (`bge-reranker-v2-m3`)**: `549.3 ms`

## Conclusión y Recomendación Técnica

1. **Precisión**: La búsqueda híbrida (Denso + FTS + RRF) obtiene una tasa de acierto excepcional en este corpus.
2. **Latencia**: Desactivar el reranker (`RERANK_ENABLED=false`) reduce la latencia en **549 ms** por consulta, permitiendo que la respuesta RAG responda en tan solo **31 ms**.
3. **Grounding**: Gracias a la normalización de RRF aplicada en `rerank.py`, apagar el reranker conserva la validez del umbral `hay_evidencia` sin producir falsos negativos.
