import type { ClienteLlamadas } from '@/api/llamadas/cliente'
import { servidorLlamadasSimulado } from '@/api/llamadas/mock/servidor'

/**
 * Adaptador del backend de llamadas simulado a `ClienteLlamadas`.
 *
 * Igual que en `src/api/mock/index.ts`: la UI nunca importa de aquí salvo los
 * tres mandos de ensayo de abajo, y esos van siempre detrás de `MODO_SIMULADO`.
 */

export {
  elegirGuionSimulado,
  elegirVelocidadSimulada,
  guionSimulado,
  VELOCIDADES,
  velocidadSimulada,
} from '@/api/llamadas/mock/servidor'
export type { ModoGuion } from '@/api/llamadas/mock/guion'

/** Tira la conexión de voz para poder ver el estado de error y el reintento. */
export function simularCaidaDeVoz(): void {
  servidorLlamadasSimulado.simularCaidaDeVoz()
}

/**
 * Barge-in a mano. En el simulador no hay micrófono, así que sin este botón el
 * camino de interrupción —vaciar el buffer del cliente— no se puede probar.
 */
export function interrumpirAgenteSimulado(): void {
  servidorLlamadasSimulado.interrumpirAgente()
}

export const clienteLlamadasSimulado: ClienteLlamadas = {
  esSimulado: true,

  pacientes() {
    return servidorLlamadasSimulado.pacientes()
  },

  crearLlamada(pacienteId: string) {
    return servidorLlamadasSimulado.crearLlamada(pacienteId)
  },

  historial() {
    return servidorLlamadasSimulado.historial()
  },

  detalleLlamada(id: string) {
    return servidorLlamadasSimulado.detalleLlamada(id)
  },

  abrirVoz(llamada, manejadores) {
    return servidorLlamadasSimulado.abrirVoz(llamada, manejadores)
  },
}
