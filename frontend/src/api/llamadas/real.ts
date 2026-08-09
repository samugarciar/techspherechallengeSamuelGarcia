import type { ClienteLlamadas, ManejadoresVoz, SesionVoz } from '@/api/llamadas/cliente'
import {
  normalizarDetalleLlamada,
  normalizarListaLlamadas,
  normalizarListaPacientes,
  normalizarLlamadaCreada,
} from '@/api/llamadas/normalizar'
import { BASE_API, pedirJson, urlApi } from '@/api/http'
import { obtenerToken } from '@/lib/token'
import type { LlamadaCreada, MensajeVoz } from '@/types/llamadas'

/**
 * Implementación contra el backend de verdad.
 *
 * Lo único que tiene miga es el WebSocket. Un WebSocket de navegador no puede
 * llevar cabeceras, así que el token va por query string —igual que en el SSE de
 * documentos, y el contrato ya lo contempla— y tampoco expone al JavaScript el
 * motivo del rechazo: `onclose` llega con 1006 tanto si no hay servidor como si
 * lo hay y la ruta no existe. Los dos casos se ven idénticos en pantalla y se
 * arreglan de forma distinta, así que se distinguen preguntando por HTTP, que sí
 * responde con detalle. Ese diagnóstico está copiado del cliente de pruebas de
 * `scripts/spikes/cliente_voz/`, donde ya demostró que ahorra media hora.
 */

/** Si la conexión se atasca, mejor tirar muestras que acumular segundos de retraso. */
const MAXIMO_EN_COLA_BYTES = 512 * 1024

/**
 * Base del WebSocket. Vacío = mismo origen, que en desarrollo es el proxy de
 * Vite (`/ws` -> localhost:8000, con `ws: true`). `VITE_WS_BASE` la fuerza si el
 * backend acaba viviendo en otro sitio; un WebSocket no pasa por CORS, así que
 * apuntar directo a `ws://localhost:8000` también funciona.
 */
function baseWebSocket(): string {
  const explicita = (import.meta.env.VITE_WS_BASE ?? '').replace(/\/$/, '')
  if (explicita) return explicita
  const base = BASE_API || window.location.origin
  return base.replace(/^http/, 'ws')
}

function urlDeVoz(ruta: string): string {
  const url = new URL(ruta, `${baseWebSocket()}/`)
  const token = obtenerToken()
  if (token) url.searchParams.set('token', token)
  return url.toString()
}

async function diagnosticar(): Promise<string> {
  try {
    const respuesta = await fetch(urlApi('/api/health'))
    if (respuesta.ok) {
      return (
        'El backend responde, pero no acepta /ws/voz. Lo más probable es que esté ' +
        'arrancado sin el bucle de voz: párralo y vuelve a levantarlo con VOZ=1.'
      )
    }
    return 'El backend responde con error y rechaza la conexión de voz.'
  } catch {
    return (
      'No hay ningún backend escuchando. Levántalo en el puerto 8000 con VOZ=1 ' +
      'y vuelve a intentarlo.'
    )
  }
}

function parsear(datos: string): MensajeVoz | null {
  try {
    const objeto = JSON.parse(datos) as unknown
    if (objeto === null || typeof objeto !== 'object') return null
    if (typeof (objeto as { tipo?: unknown }).tipo !== 'string') return null
    return objeto as MensajeVoz
  } catch {
    return null
  }
}

export function abrirVozReal(llamada: LlamadaCreada, manejadores: ManejadoresVoz): SesionVoz {
  let cerradaPorNosotros = false
  let llegoAAbrir = false

  manejadores.onEstado('conectando')

  const ws = new WebSocket(urlDeVoz(llamada.ws))
  ws.binaryType = 'arraybuffer'

  ws.onopen = () => {
    llegoAAbrir = true
    manejadores.onEstado('conectado')
  }

  ws.onmessage = (evento: MessageEvent<string | ArrayBuffer>) => {
    if (typeof evento.data !== 'string') {
      manejadores.onAudio(new Int16Array(evento.data))
      return
    }
    const mensaje = parsear(evento.data)
    if (mensaje) manejadores.onMensaje(mensaje)
  }

  // `onerror` no trae motivo; sirve sólo para no dejar el handler vacío. El
  // trabajo de verdad lo hace `onclose`, que siempre llega después.
  ws.onerror = () => {}

  ws.onclose = (evento) => {
    if (cerradaPorNosotros) return
    manejadores.onEstado('cerrado')
    if (!llegoAAbrir) {
      void diagnosticar().then(manejadores.onFallo)
      return
    }
    manejadores.onFallo(
      evento.wasClean
        ? 'El servidor cerró la conexión de voz.'
        : 'Se perdió la conexión de voz con el servidor.',
    )
  }

  return {
    enviarAudio(pcm) {
      if (ws.readyState !== WebSocket.OPEN) return
      if (ws.bufferedAmount > MAXIMO_EN_COLA_BYTES) return
      // Se manda el ArrayBuffer subyacente y no la vista. TypeScript tipa
      // `Int16Array.buffer` como `ArrayBufferLike`, que incluye
      // `SharedArrayBuffer`, y `send()` no lo acepta. La aserción es segura
      // porque este PCM sale del AudioWorklet, que nunca produce memoria
      // compartida.
      //
      // El `slice` sólo entra si la vista NO cubre su buffer entero. Hoy nunca
      // pasa —`captura.ts` crea un Int16Array nuevo por bloque— pero mandar
      // `.buffer` a secas de una subvista enviaría audio de más sin avisar, y
      // eso se depura fatal: llega ruido al VAD y todo lo demás parece roto.
      const cubreElBuffer = pcm.byteOffset === 0 && pcm.byteLength === pcm.buffer.byteLength
      ws.send(
        cubreElBuffer
          ? (pcm.buffer as ArrayBuffer)
          : (pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength) as ArrayBuffer),
      )
    },
    abierta() {
      return ws.readyState === WebSocket.OPEN
    },
    cerrar() {
      cerradaPorNosotros = true
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close()
      manejadores.onEstado('cerrado')
    },
  }
}

export const clienteLlamadasReal: ClienteLlamadas = {
  esSimulado: false,

  async pacientes() {
    return normalizarListaPacientes(await pedirJson<unknown>('/api/patients'))
  },

  async crearLlamada(pacienteId: string) {
    return normalizarLlamadaCreada(
      await pedirJson<unknown>('/api/calls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patient_id: pacienteId }),
      }),
    )
  },

  async historial() {
    return normalizarListaLlamadas(await pedirJson<unknown>('/api/calls'))
  },

  async detalleLlamada(id: string) {
    return normalizarDetalleLlamada(
      await pedirJson<unknown>(`/api/calls/${encodeURIComponent(id)}`),
    )
  },

  abrirVoz: abrirVozReal,
}
