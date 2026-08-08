import type { ClienteApi, ManejadoresFlujo, OpcionesSubida, ParametrosListado } from '@/api/cliente'
import { servidorSimulado } from '@/api/mock/servidor'
import type { DocumentoConTrozos } from '@/types/api'

/**
 * Adaptador del backend simulado a la interfaz `ClienteApi`.
 *
 * Lo único que esta capa añade sobre `servidor.ts` es la ilusión de red: el
 * progreso de subida byte a byte y una reconexión SSE con la misma forma que la
 * real, para que el camino que se prueba esta noche sea el mismo que correrá
 * mañana contra FastAPI.
 */

/** Corta el flujo simulado. La consola expone un botón con esto en modo simulado
 *  para poder enseñar la reconexión con backoff sin apagar nada. */
export function simularCaidaDeConexion() {
  servidorSimulado.simularCaida()
}

function progresoFalso(onProgreso: ((f: number) => void) | undefined, duracionMs: number) {
  if (!onProgreso) return () => {}
  const arranque = Date.now()
  onProgreso(0)
  const id = setInterval(() => {
    // Se queda en 0,95: el 1 lo pone la respuesta, no el reloj. Una barra que
    // llega al 100 % y luego espera es peor que una que no llega.
    const fraccion = Math.min(0.95, (Date.now() - arranque) / duracionMs)
    onProgreso(fraccion)
  }, 60)
  return () => clearInterval(id)
}

export const clienteSimulado: ClienteApi = {
  esSimulado: true,

  salud() {
    return servidorSimulado.salud()
  },

  listar(parametros: ParametrosListado = {}) {
    return servidorSimulado.listar(parametros)
  },

  async subir(archivo, opciones: OpcionesSubida = {}) {
    const parar = progresoFalso(opciones.onProgreso, 700)
    try {
      const documento = await servidorSimulado.subir(archivo, opciones.title)
      opciones.onProgreso?.(1)
      return documento
    } finally {
      parar()
    }
  },

  detalle(id): Promise<DocumentoConTrozos> {
    return servidorSimulado.detalle(id)
  },

  eliminar(id) {
    return servidorSimulado.eliminar(id)
  },

  consultarRag(consulta, topK = 4) {
    return servidorSimulado.consultarRag(consulta, topK)
  },

  abrirFlujo(manejadores: ManejadoresFlujo) {
    let cerrado = false
    let intento = 0
    let reconexion: ReturnType<typeof setTimeout> | undefined

    const conectar = () => {
      if (cerrado) return
      manejadores.onEstado(intento === 0 ? 'conectando' : 'reconectando', intento)
      // Un tick de retardo para que la UI llegue a pintar «conectando».
      reconexion = setTimeout(
        () => {
          if (cerrado) return
          intento = 0
          manejadores.onEstado('conectado', 0)
          manejadores.onResincronizar?.()
        },
        intento === 0 ? 260 : 900,
      )
    }

    const desuscribir = servidorSimulado.suscribir((evento) => {
      if (cerrado) return
      switch (evento.tipo) {
        case 'documento':
          manejadores.onDocumento({
            id: evento.documento.id,
            status: evento.documento.status,
            chunks_count: evento.documento.chunks_count,
            embedded_count: evento.documento.embedded_count,
            error: evento.documento.error,
            filename: evento.documento.filename,
            title: evento.documento.title,
            pages: evento.documento.pages,
            updated_at: evento.documento.updated_at,
          })
          break
        case 'eliminado':
          manejadores.onEliminado({ id: evento.id })
          break
        case 'caida': {
          intento += 1
          manejadores.onEstado('reconectando', intento)
          if (reconexion !== undefined) clearTimeout(reconexion)
          reconexion = setTimeout(() => {
            if (cerrado) return
            intento = 0
            manejadores.onEstado('conectado', 0)
            manejadores.onResincronizar?.()
          }, 1_800)
          break
        }
        case 'latido':
          break
      }
    })

    conectar()

    return {
      cerrar() {
        cerrado = true
        if (reconexion !== undefined) clearTimeout(reconexion)
        desuscribir()
        manejadores.onEstado('cerrado', 0)
      },
    }
  },
}
