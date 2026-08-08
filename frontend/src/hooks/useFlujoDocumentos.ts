import { useEffect, useRef, useState } from 'react'

import { api } from '@/api'
import type { EstadoConexion } from '@/api/cliente'
import type { EventoDocumento, EventoEliminado } from '@/types/api'

interface Manejadores {
  onDocumento: (evento: EventoDocumento) => void
  onEliminado: (evento: EventoEliminado) => void
  onResincronizar: () => void
}

/**
 * Mantiene abierto el flujo SSE mientras el componente esté montado.
 *
 * Los manejadores viven en una ref y no en las dependencias del efecto a
 * propósito: si entraran como dependencias, cada render de la tabla —y hay uno
 * por cada evento— cerraría y reabriría la conexión. El resultado sería una
 * reconexión por trozo embebido, que es exactamente lo contrario de lo que se
 * quiere enseñar.
 */
export function useFlujoDocumentos(manejadores: Manejadores, activo: boolean) {
  const ref = useRef(manejadores)
  const [estado, setEstado] = useState<EstadoConexion>('inactivo')
  const [intento, setIntento] = useState(0)

  useEffect(() => {
    ref.current = manejadores
  })

  useEffect(() => {
    if (!activo) {
      setEstado('inactivo')
      return
    }
    const suscripcion = api.abrirFlujo({
      onDocumento: (evento) => ref.current.onDocumento(evento),
      onEliminado: (evento) => ref.current.onEliminado(evento),
      onResincronizar: () => ref.current.onResincronizar(),
      onEstado: (nuevo, numeroIntento) => {
        setEstado(nuevo)
        setIntento(numeroIntento)
      },
    })
    return () => suscripcion.cerrar()
  }, [activo])

  return { estado, intento }
}
