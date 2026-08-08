import type { ClienteApi, OpcionesSubida, ParametrosListado } from '@/api/cliente'
import { abrirFlujoReal } from '@/api/flujo'
import { pedirJson, subirConProgreso } from '@/api/http'
import { normalizarDetalle, normalizarDocumento } from '@/api/normalizar'
import type { ListaDocumentos, RespuestaRag, ResultadoBorrado, Salud } from '@/types/api'

/** Implementación contra el backend de verdad, tal como lo describe el contrato. */
export const clienteReal: ClienteApi = {
  esSimulado: false,

  salud() {
    return pedirJson<Salud>('/api/health')
  },

  async listar(parametros: ParametrosListado = {}) {
    const datos = await pedirJson<ListaDocumentos>('/api/documents', {
      parametros: {
        estado: parametros.estado,
        q: parametros.q,
        limit: parametros.limit ?? 100,
        offset: parametros.offset,
      },
    })
    const documentos = Array.isArray(datos?.documentos) ? datos.documentos : []
    return {
      documentos: documentos.map(normalizarDocumento),
      total: typeof datos?.total === 'number' ? datos.total : documentos.length,
    }
  },

  async subir(archivo: File, opciones: OpcionesSubida = {}) {
    const formulario = new FormData()
    formulario.append('file', archivo)
    if (opciones.title?.trim()) formulario.append('title', opciones.title.trim())
    const bruto = await subirConProgreso<unknown>('/api/documents', formulario, {
      onProgreso: opciones.onProgreso,
      signal: opciones.signal,
    })
    return normalizarDocumento(bruto)
  },

  async detalle(id: string) {
    return normalizarDetalle(await pedirJson<unknown>(`/api/documents/${encodeURIComponent(id)}`))
  },

  eliminar(id: string) {
    return pedirJson<ResultadoBorrado>(`/api/documents/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    })
  },

  async consultarRag(consulta: string, topK = 4) {
    const datos = await pedirJson<RespuestaRag>('/api/rag/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ consulta, top_k: topK }),
    })
    return {
      fragmentos: Array.isArray(datos?.fragmentos) ? datos.fragmentos : [],
      hay_evidencia: Boolean(datos?.hay_evidencia),
      ms: datos?.ms ?? {},
    }
  },

  abrirFlujo: abrirFlujoReal,
}
