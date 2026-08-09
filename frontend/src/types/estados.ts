import type { EstadoDocumento } from '@/types/api'

/**
 * Etiquetas y colores de estado. Vienen FIJADOS en `docs/CONTRATO_API.md`; este
 * fichero es su única transcripción para que ningún componente se invente otra.
 */
export type ColorEstado = 'gris' | 'ambar' | 'verde' | 'rojo'

interface DefinicionEstado {
  etiqueta: string
  color: ColorEstado
  /** ¿Está el documento en mitad de la ingesta? Decide si se pinta el progreso. */
  enCurso: boolean
  /** Frase corta para el paso a paso, en gerundio o participio según toque. */
  descripcion: string
}

export const ESTADOS: Record<EstadoDocumento, DefinicionEstado> = {
  uploaded: {
    etiqueta: 'Recibido',
    color: 'gris',
    enCurso: true,
    descripcion: 'El archivo está guardado y en cola.',
  },
  parsing: {
    etiqueta: 'Leyendo el archivo',
    color: 'ambar',
    enCurso: true,
    descripcion: 'Extrayendo el texto y su estructura de secciones.',
  },
  chunking: {
    etiqueta: 'Troceando',
    color: 'ambar',
    enCurso: true,
    descripcion: 'Partiendo el documento por secciones, sin romper reglas a la mitad.',
  },
  embedding: {
    etiqueta: 'Generando embeddings',
    color: 'ambar',
    enCurso: true,
    descripcion: 'Convirtiendo cada trozo en un vector consultable.',
  },
  ready: {
    etiqueta: 'Listo — el agente ya lo sabe',
    color: 'verde',
    enCurso: false,
    descripcion: 'Recuperable por el RAG. El agente puede citarlo en una llamada.',
  },
  failed: {
    etiqueta: 'Error',
    color: 'rojo',
    enCurso: false,
    descripcion: 'La ingesta no terminó. El documento no es recuperable.',
  },
  superseded: {
    etiqueta: 'Reemplazado',
    color: 'gris',
    enCurso: false,
    descripcion: 'Una versión más nueva ocupó su lugar. Ya no es recuperable.',
  },
}

/** Orden de la máquina de estados, para el paso a paso de la fila. */
export const SECUENCIA_INGESTA: EstadoDocumento[] = [
  'uploaded',
  'parsing',
  'chunking',
  'embedding',
  'ready',
]

export function definicionEstado(estado: EstadoDocumento): DefinicionEstado {
  return (
    ESTADOS[estado] ?? {
      etiqueta: estado,
      color: 'gris' as const,
      enCurso: false,
      descripcion: 'Estado desconocido para esta versión de la consola.',
    }
  )
}
