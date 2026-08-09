import { numero, numeroOpcional, objeto, texto, textoOpcional } from '@/api/coercion'
import type { Documento, DocumentoConTrozos, EstadoDocumento, TrozoPrevio } from '@/types/api'

/**
 * Normalización defensiva de lo que llega por la red.
 *
 * Nació porque backend y frontend se escribían en paralelo contra el mismo
 * contrato y algún campo iba a llegar con otro nombre o sin llegar. Se queda
 * porque la regla que impone sigue valiendo con el backend ya escrito: una
 * discrepancia degrada la celda («—») y nunca tumba la pantalla. En una demo,
 * una tabla con un hueco se explica; una pantalla en blanco, no.
 */

const ESTADOS_VALIDOS: readonly string[] = [
  'uploaded',
  'parsing',
  'chunking',
  'embedding',
  'ready',
  'failed',
  'superseded',
]

function estado(valor: unknown): EstadoDocumento {
  return ESTADOS_VALIDOS.includes(valor as string) ? (valor as EstadoDocumento) : 'uploaded'
}

export function normalizarDocumento(bruto: unknown): Documento {
  const d = objeto(bruto)
  return {
    id: texto(d.id),
    filename: texto(d.filename, 'documento'),
    title: textoOpcional(d.title),
    mime_type: texto(d.mime_type),
    size_bytes: numero(d.size_bytes),
    sha256: texto(d.sha256),
    status: estado(d.status),
    error: textoOpcional(d.error),
    chunks_count: numero(d.chunks_count),
    embedded_count: numero(d.embedded_count),
    pages: numeroOpcional(d.pages),
    supersedes_id: textoOpcional(d.supersedes_id),
    created_at: texto(d.created_at),
    updated_at: texto(d.updated_at),
  }
}

/**
 * El contrato promete «los 3 primeros trozos con `heading` y los 200 primeros
 * caracteres» pero no fija el nombre del campo del texto. Se aceptan los tres
 * candidatos razonables hasta que el backend lo cierre (anotado en
 * §Cambios sobre el contrato).
 */
export function normalizarTrozo(bruto: unknown): TrozoPrevio {
  const t = objeto(bruto)
  return {
    ordinal: numeroOpcional(t.ordinal),
    heading: textoOpcional(t.heading),
    content: texto(t.content ?? t.contenido ?? t.texto ?? t.text),
    page: numeroOpcional(t.page),
  }
}

export function normalizarDetalle(bruto: unknown): DocumentoConTrozos {
  const d = objeto(bruto)
  const previa = Array.isArray(d.chunks_preview) ? d.chunks_preview : []
  return {
    ...normalizarDocumento(bruto),
    chunks_preview: previa.map(normalizarTrozo).filter((t) => t.content !== ''),
  }
}
