import { MODO_SIMULADO } from '@/api'
import type { ClienteLlamadas } from '@/api/llamadas/cliente'
import { clienteLlamadasSimulado } from '@/api/llamadas/mock'
import { clienteLlamadasReal } from '@/api/llamadas/real'

/**
 * El único punto donde se decide contra qué backend habla la pantalla de
 * llamadas. Comparte el interruptor con la consola de documentos —`VITE_MOCK=1`—
 * a propósito: dos interruptores distintos acabarían en una demo con media
 * aplicación simulada y la otra media no, que es el peor sitio donde estar.
 */
export const apiLlamadas: ClienteLlamadas = MODO_SIMULADO
  ? clienteLlamadasSimulado
  : clienteLlamadasReal

export type { ClienteLlamadas, ManejadoresVoz, SesionVoz } from '@/api/llamadas/cliente'
