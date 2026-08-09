import { useSyncExternalStore } from 'react'

import { almacenToken } from '@/lib/token'

/** El token, siempre el mismo valor en toda la pantalla y en todas las pestañas. */
export function useToken(): string {
  return useSyncExternalStore(almacenToken.suscribir, almacenToken.leer, almacenToken.leer)
}
