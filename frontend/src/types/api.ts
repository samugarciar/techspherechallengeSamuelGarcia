/**
 * Tipos derivados de `docs/CONTRATO_API.md`.
 *
 * El contrato es normativo y el backend se está escribiendo en paralelo, así que
 * este fichero es la traducción literal del documento, no una interpretación:
 * los nombres de campo se dejan en inglés donde el contrato los define en inglés
 * (`status`, `chunks_count`) y en español donde el contrato los define en español
 * (`olvidado`, `fragmentos`, `hay_evidencia`). Mezclar idiomas dentro de un mismo
 * objeto es feo, pero inventarse un alias es peor: el día que el backend cambie
 * un nombre, el error tiene que salir aquí y no tres capas más adentro.
 */

/** Máquina de estados de la ingesta. `ready` es el único recuperable por el RAG. */
export type EstadoDocumento =
  | 'uploaded'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'ready'
  | 'failed'
  | 'superseded'

export interface Documento {
  id: string
  filename: string
  title: string | null
  mime_type: string
  size_bytes: number
  sha256: string
  status: EstadoDocumento
  error: string | null
  chunks_count: number
  embedded_count: number
  pages: number | null
  supersedes_id: string | null
  created_at: string
  updated_at: string
}

export interface ListaDocumentos {
  documentos: Documento[]
  total: number
}

/**
 * Trozo de la vista previa de `GET /api/documents/{id}`.
 *
 * El contrato dice «los 3 primeros trozos con `heading` y los 200 primeros
 * caracteres» pero no fija el nombre del campo del texto. Se acepta cualquiera de
 * los tres candidatos razonables y se normaliza en `normalizarTrozo`; anotado en
 * §Cambios sobre el contrato para que el backend lo cierre.
 */
export interface TrozoPrevio {
  ordinal: number | null
  heading: string | null
  content: string
  page: number | null
}

export interface DocumentoConTrozos extends Documento {
  chunks_preview: TrozoPrevio[]
}

export interface ResultadoBorrado {
  olvidado: boolean
  chunks_eliminados: number
}

export interface FragmentoRag {
  documento_id: string
  filename: string
  heading: string | null
  page: number | null
  contenido: string
  score: number
  cita: string
}

export interface EtapasMs {
  embedding?: number
  retrieval?: number
  rerank?: number
  total?: number
}

export interface RespuestaRag {
  fragmentos: FragmentoRag[]
  hay_evidencia: boolean
  ms: EtapasMs
}

export interface Salud {
  ok: boolean
  db: boolean
  version: string
  /**
   * ¿Están bge-m3 y el reranker ya en memoria?
   *
   * No está en el contrato: lo añadió el backend (`app/api/salud.py`) y estaba
   * sin consumir. Merece la pena leerlo porque distingue los dos «no va» que
   * más se parecen en pantalla: el backend caído y el backend arrancando. La
   * primera consulta al RAG en frío tarda 13 s medidos, y sin este dato eso se
   * ve como un servidor roto. Opcional porque un backend anterior no lo manda.
   */
  modelos_listos?: boolean
}

/** Códigos de error del contrato. Se admite `string` porque un backend en obras
 *  puede devolver uno nuevo, y eso no debe romper la pantalla. */
export type CodigoError =
  | 'no_autorizado'
  | 'documento_no_encontrado'
  | 'formato_no_soportado'
  | 'archivo_vacio'
  | 'archivo_demasiado_grande'
  | 'documento_duplicado'
  | 'error_interno'
  | (string & {})

export interface CuerpoError {
  error: { codigo: CodigoError; mensaje: string }
}

/** Evento SSE de cambio de estado. Llega parcial: el contrato solo garantiza
 *  `id` y `status`, así que todo lo demás es opcional. */
export interface EventoDocumento {
  id: string
  status: EstadoDocumento
  chunks_count?: number
  embedded_count?: number
  error?: string | null
  filename?: string
  title?: string | null
  pages?: number | null
  updated_at?: string
}

export interface EventoEliminado {
  id: string
}
