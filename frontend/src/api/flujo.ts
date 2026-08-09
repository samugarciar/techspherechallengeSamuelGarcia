import type { ManejadoresFlujo, Suscripcion } from '@/api/cliente'
import { urlApi } from '@/api/http'
import { obtenerToken } from '@/lib/token'
import type { EventoDocumento, EventoEliminado } from '@/types/api'

/**
 * Conexión SSE con `GET /api/documents/stream`.
 *
 * Tres decisiones que no son obvias:
 *
 * 1. El token va por query string. No es un capricho: `EventSource` no admite
 *    cabeceras, y el contrato ya lo contempla. Queda en los logs del servidor,
 *    que con un único administrador y una demo local es un coste asumible.
 *
 * 2. Se cierra la conexión en cuanto hay error y se reconecta a mano. `EventSource`
 *    reintenta solo, pero a ritmo fijo y para siempre: con el backend caído eso es
 *    una petición por segundo hasta que alguien cierre la pestaña. Y como el 401
 *    tampoco es distinguible desde el cliente, sin freno propio un token erróneo
 *    martillearía el servidor.
 *
 * 3. Hay un vigilante de silencio. El contrato manda un `latido` cada 15 s; si
 *    pasan 45 s sin recibir NADA, la conexión está muerta aunque el navegador no
 *    lo haya notado (típico al despertar el portátil o al caerse un proxy por
 *    medio). Se fuerza la reconexión en vez de esperar a un error que no llega.
 */

const RETARDOS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000]
const SILENCIO_MAXIMO_MS = 45_000
const VIGILANCIA_CADA_MS = 5_000

function retardo(intento: number): number {
  const base = RETARDOS_MS[Math.min(intento, RETARDOS_MS.length - 1)] ?? 30_000
  // Jitter para que varias pestañas abiertas no reintenten a la vez.
  return base + Math.random() * 400
}

function parsear<T>(datos: string): T | null {
  try {
    return JSON.parse(datos) as T
  } catch {
    return null
  }
}

export function abrirFlujoReal(manejadores: ManejadoresFlujo): Suscripcion {
  let fuente: EventSource | null = null
  let intento = 0
  let cerrado = false
  let ultimoMensaje = Date.now()
  let reconexion: ReturnType<typeof setTimeout> | undefined
  let vigilante: ReturnType<typeof setInterval> | undefined

  const marcarActividad = () => {
    ultimoMensaje = Date.now()
  }

  const programarReconexion = () => {
    if (cerrado || reconexion !== undefined) return
    const espera = retardo(intento)
    intento += 1
    manejadores.onEstado('reconectando', intento)
    reconexion = setTimeout(() => {
      reconexion = undefined
      conectar()
    }, espera)
  }

  const desconectar = () => {
    if (!fuente) return
    // Quitar los manejadores antes de cerrar evita un `onerror` fantasma del
    // propio close, que dispararía otra reconexión encima de la programada.
    fuente.onopen = null
    fuente.onerror = null
    fuente.close()
    fuente = null
  }

  const conectar = () => {
    if (cerrado) return
    desconectar()
    manejadores.onEstado(intento === 0 ? 'conectando' : 'reconectando', intento)
    marcarActividad()

    const token = obtenerToken()
    const url = urlApi('/api/documents/stream', token ? { token } : undefined)
    const es = new EventSource(url)
    fuente = es

    es.onopen = () => {
      intento = 0
      marcarActividad()
      manejadores.onEstado('conectado', 0)
      // Todo lo ocurrido mientras el flujo estaba caído se perdió: el contrato no
      // numera eventos. Una recarga completa es el único estado de verdad.
      manejadores.onResincronizar?.()
    }

    es.onerror = () => {
      if (cerrado) return
      desconectar()
      programarReconexion()
    }

    es.addEventListener('documento', (evento) => {
      marcarActividad()
      const datos = parsear<EventoDocumento>((evento as MessageEvent<string>).data)
      if (datos?.id) manejadores.onDocumento(datos)
    })

    es.addEventListener('eliminado', (evento) => {
      marcarActividad()
      const datos = parsear<EventoEliminado>((evento as MessageEvent<string>).data)
      if (datos?.id) manejadores.onEliminado(datos)
    })

    es.addEventListener('latido', marcarActividad)
    // Un backend que emita sin `event:` cae en `message`; también cuenta como vida.
    es.onmessage = marcarActividad
  }

  vigilante = setInterval(() => {
    if (cerrado || fuente === null) return
    if (Date.now() - ultimoMensaje > SILENCIO_MAXIMO_MS) {
      desconectar()
      programarReconexion()
    }
  }, VIGILANCIA_CADA_MS)

  conectar()

  return {
    cerrar() {
      cerrado = true
      if (reconexion !== undefined) clearTimeout(reconexion)
      if (vigilante !== undefined) clearInterval(vigilante)
      desconectar()
      manejadores.onEstado('cerrado', 0)
    },
  }
}
