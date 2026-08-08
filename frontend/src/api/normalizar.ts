import type { Documento, DocumentoConTrozos, EstadoDocumento, TrozoPrevio } from '@/types/api'

/**
 * Normalización defensiva de lo que llega por la red.
 *
 * El backend se está escribiendo en paralelo contra el mismo contrato, así que
 * habrá un rato en que un campo llegue con otro nombre o sin llegar. La regla es
 * que una discrepancia degrade la celda («—») y nunca tumbe la pantalla: en una
 * demo, una tabla con un hueco se explica; una pantalla en blanco, no.
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

function texto(valor: unknown, porDefecto = ''): string {
  return typeof valor === 'string' ? valor : porDefecto
}

function textoOpcional(valor: unknown): string | null {
  return typeof valor === 'string' && valor.trim() !== '' ? valor : null
}

function entero(valor: unknown, porDefecto = 0): number {
  return typeof valor === 'number' && Number.isFinite(valor) ? valor : porDefecto
}

function enteroOpcional(valor: unknown): number | null {
  return typeof valor === 'number' && Number.isFinite(valor) ? valor : null
}

function estado(valor: unknown): EstadoDocumento {
  return ESTADOS_VALIDOS.includes(valor as string) ? (valor as EstadoDocumento) : 'uploaded'
}

export function normalizarDocumento(bruto: unknown): Documento {
  const d = (bruto ?? {}) as Record<string, unknown>
  return {
    id: texto(d.id),
    filename: texto(d.filename, 'documento'),
    title: textoOpcional(d.title),
    mime_type: texto(d.mime_type),
    size_bytes: entero(d.size_bytes),
    sha256: texto(d.sha256),
    status: estado(d.status),
    error: textoOpcional(d.error),
    chunks_count: entero(d.chunks_count),
    embedded_count: entero(d.embedded_count),
    pages: enteroOpcional(d.pages),
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
  const t = (bruto ?? {}) as Record<string, unknown>
  return {
    ordinal: enteroOpcional(t.ordinal),
    heading: textoOpcional(t.heading),
    content: texto(t.content ?? t.contenido ?? t.texto ?? t.text),
    page: enteroOpcional(t.page),
  }
}

export function normalizarDetalle(bruto: unknown): DocumentoConTrozos {
  const d = (bruto ?? {}) as Record<string, unknown>
  const previa = Array.isArray(d.chunks_preview) ? d.chunks_preview : []
  return {
    ...normalizarDocumento(bruto),
    chunks_preview: previa.map(normalizarTrozo).filter((t) => t.content !== ''),
  }
}
