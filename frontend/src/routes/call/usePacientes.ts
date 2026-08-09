import { useCallback, useEffect, useState } from 'react'

import { mensajeDeError } from '@/api/errores'
import { apiLlamadas } from '@/api/llamadas'
import type { Paciente } from '@/types/llamadas'

/** Pacientes con seguimiento pendiente. Carga simple: la lista no cambia sola. */
export function usePacientes(activo: boolean) {
  const [pacientes, setPacientes] = useState<Paciente[]>([])
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const cargar = useCallback(async () => {
    setCargando(true)
    try {
      const lista = await apiLlamadas.pacientes()
      setPacientes(lista.pacientes)
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

  return { pacientes, cargando, error, cargar }
}
