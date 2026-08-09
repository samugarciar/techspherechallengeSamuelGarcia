import { useCallback, useEffect, useState } from 'react'

import { mensajeDeError } from '@/api/errores'
import { apiLlamadas } from '@/api/llamadas'
import type { LlamadaDetalle, ResumenLlamada } from '@/types/llamadas'

/** Lista de llamadas ya registradas. */
export function useHistorial(activo: boolean) {
  const [llamadas, setLlamadas] = useState<ResumenLlamada[]>([])
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const cargar = useCallback(async () => {
    setCargando(true)
    try {
      const lista = await apiLlamadas.historial()
      setLlamadas(lista.llamadas)
      setError(null)
    } catch (causa) {
      setError(mensajeDeError(causa))
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    if (!activo) {
      setCargando(false)
      return
    }
    void cargar()
  }, [activo, cargar])

  return { llamadas, cargando, error, cargar }
}

/** Una llamada con sus turnos. */
export function useDetalleLlamada(id: string | undefined, activo: boolean) {
  const [llamada, setLlamada] = useState<LlamadaDetalle | null>(null)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const cargar = useCallback(async () => {
    if (!id) {
      setError('No se ha indicado qué llamada abrir.')
      setCargando(false)
      return
    }
    setCargando(true)
    try {
      setLlamada(await apiLlamadas.detalleLlamada(id))
      setError(null)
    } catch (causa) {
      setError(mensajeDeError(causa))
    } finally {
      setCargando(false)
    }
  }, [id])

  useEffect(() => {
    if (!activo) {
      setCargando(false)
      return
    }
    void cargar()
  }, [activo, cargar])

  return { llamada, cargando, error, cargar }
}
