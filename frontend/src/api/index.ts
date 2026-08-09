import type { ClienteApi } from '@/api/cliente'
import { clienteSimulado } from '@/api/mock'
import { clienteReal } from '@/api/real'

/**
 * El único punto donde se decide contra qué backend habla la consola.
 *
 * `VITE_MOCK=1` -> simulador en memoria (`src/api/mock/`).
 * Sin esa variable -> FastAPI de verdad, vía el proxy `/api` de Vite.
 *
 * Ningún componente importa nunca del mock directamente; así apagarlo es cambiar
 * una variable de entorno y no tocar código. El bundle de producción se queda con
 * el mock dentro (unos pocos kB) a cambio de que el interruptor sea trivial; si
 * llegara a molestar, `import()` dinámico aquí y resuelto.
 */
export const MODO_SIMULADO = import.meta.env.VITE_MOCK === '1'

export const api: ClienteApi = MODO_SIMULADO ? clienteSimulado : clienteReal

export type {
  ClienteApi,
  EstadoConexion,
  ManejadoresFlujo,
  OpcionesSubida,
  ParametrosListado,
  Suscripcion,
} from '@/api/cliente'
