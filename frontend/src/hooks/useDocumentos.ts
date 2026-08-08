import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { api } from '@/api'
import { mensajeDeError } from '@/api/errores'
import type { Documento, EventoDocumento } from '@/types/api'

/**
 * Estado de la lista de documentos: carga inicial, subidas en curso, fusión de
 * los eventos SSE y borrado.
 *
 * Regla de oro: la lista SOLO se sustituye entera en una recarga explícita. Todo
 * lo demás son fusiones sobre la fila que ya está en pantalla, porque el evento
 * del contrato es parcial (`id`, `status`, contadores) y sobrescribir con él
 * borraría el nombre del archivo justo mientras el jurado lo está leyendo.
 */

export interface SubidaEnCurso {
  clave: string
  nombre: string
  tamano: number
  /** 0–1 de bytes enviados. */
  fraccion: number
  estado: 'subiendo' | 'aceptado' | 'error'
  error?: string
}

export interface AvisoOlvido {
  id: string
  nombre: string
  chunks: number
  momento: number
}

/** Cuánto dura el destello verde de «acaba de aprender» en la fila. */
const DESTELLO_MS = 2_800

export function useDocumentos(activo: boolean) {
  const [documentos, setDocumentos] = useState<Documento[]>([])
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [subidas, setSubidas] = useState<SubidaEnCurso[]>([])
  const [recienAprendidos, setRecienAprendidos] = useState<Set<string>>(new Set())
  const [olvidoReciente, setOlvidoReciente] = useState<AvisoOlvido | null>(null)
  const [borrando, setBorrando] = useState<string | null>(null)

  const relojes = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())
  const recargaPendiente = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const programar = useCallback((fn: () => void, retardo: number) => {
    const id = setTimeout(() => {
      relojes.current.delete(id)
      fn()
    }, retardo)
    relojes.current.add(id)
  }, [])

  useEffect(
    () => () => {
      for (const id of relojes.current) clearTimeout(id)
      relojes.current.clear()
      if (recargaPendiente.current !== undefined) clearTimeout(recargaPendiente.current)
    },
    [],
  )

  const cargar = useCallback(
    async (silenciosa = false) => {
      if (!silenciosa) setCargando(true)
      try {
        const lista = await api.listar({ limit: 100 })
        setDocumentos(lista.documentos)
        setError(null)
      } catch (causa) {
        setError(mensajeDeError(causa))
      } finally {
        setCargando(false)
      }
    },
    [],
  )

  useEffect(() => {
    if (!activo) {
      setCargando(false)
      return
    }
    void cargar()
  }, [activo, cargar])

  const marcarAprendido = useCallback(
    (id: string) => {
      setRecienAprendidos((previo) => new Set(previo).add(id))
      programar(() => {
        setRecienAprendidos((previo) => {
          const copia = new Set(previo)
          copia.delete(id)
          return copia
        })
      }, DESTELLO_MS)
    },
    [programar],
  )

  /** Fusiona un evento SSE sobre la fila existente. */
  const aplicarEvento = useCallback(
    (evento: EventoDocumento) => {
      let conocido = false
      setDocumentos((previos) =>
        previos.map((documento) => {
          if (documento.id !== evento.id) return documento
          conocido = true
          if (documento.status !== 'ready' && evento.status === 'ready') marcarAprendido(evento.id)
          return {
            ...documento,
            status: evento.status,
            chunks_count: evento.chunks_count ?? documento.chunks_count,
            embedded_count: evento.embedded_count ?? documento.embedded_count,
            pages: evento.pages ?? documento.pages,
            error: evento.error ?? (evento.status === 'failed' ? documento.error : null),
            updated_at: evento.updated_at ?? new Date().toISOString(),
          }
        }),
      )

      // Documento que no estaba en la lista: lo ha subido otra pestaña, o llegó
      // antes que la respuesta del POST. El evento del contrato no trae nombre ni
      // tamaño, así que no se puede pintar una fila con él; se recarga la lista.
      if (!conocido) {
        if (recargaPendiente.current !== undefined) clearTimeout(recargaPendiente.current)
        recargaPendiente.current = setTimeout(() => {
          recargaPendiente.current = undefined
          void cargar(true)
        }, 400)
      }
    },
    [cargar, marcarAprendido],
  )

  const aplicarEliminado = useCallback((id: string) => {
    setDocumentos((previos) => previos.filter((documento) => documento.id !== id))
  }, [])

  const resincronizar = useCallback(() => {
    void cargar(true)
  }, [cargar])

  // -- subida ----------------------------------------------------------------

  const subir = useCallback(
    async (archivos: File[]) => {
      await Promise.all(
        archivos.map(async (archivo) => {
          const clave = `${archivo.name}-${archivo.size}-${Date.now()}-${Math.random()}`
          setSubidas((previas) => [
            ...previas,
            { clave, nombre: archivo.name, tamano: archivo.size, fraccion: 0, estado: 'subiendo' },
          ])

          const actualizar = (cambios: Partial<SubidaEnCurso>) =>
            setSubidas((previas) =>
              previas.map((subida) => (subida.clave === clave ? { ...subida, ...cambios } : subida)),
            )

          try {
            const documento = await api.subir(archivo, {
              onProgreso: (fraccion) => actualizar({ fraccion }),
            })
            // La fila entra ya en la tabla; la barra de subida sobra a partir de
            // aquí, así que se retira sola y deja el foco en la ingesta.
            setDocumentos((previos) =>
              previos.some((d) => d.id === documento.id) ? previos : [documento, ...previos],
            )
            actualizar({ estado: 'aceptado', fraccion: 1 })
            programar(
              () => setSubidas((previas) => previas.filter((s) => s.clave !== clave)),
              1_400,
            )
          } catch (causa) {
            actualizar({ estado: 'error', error: mensajeDeError(causa) })
          }
        }),
      )
    },
    [programar],
  )

  const descartarSubida = useCallback((clave: string) => {
    setSubidas((previas) => previas.filter((subida) => subida.clave !== clave))
  }, [])

  // -- borrado ---------------------------------------------------------------

  const eliminar = useCallback(async (documento: Documento) => {
    setBorrando(documento.id)
    try {
      const resultado = await api.eliminar(documento.id)
      setDocumentos((previos) => previos.filter((d) => d.id !== documento.id))
      setOlvidoReciente({
        id: documento.id,
        nombre: documento.title || documento.filename,
        // Si el backend no dijera cuántos, se cae al contador que la fila ya
        // mostraba: peor dato, pero nunca un hueco en la frase.
        chunks: resultado?.chunks_eliminados ?? documento.chunks_count,
        momento: Date.now(),
      })
      return { ok: true as const }
    } catch (causa) {
      return { ok: false as const, mensaje: mensajeDeError(causa) }
    } finally {
      setBorrando(null)
    }
  }, [])

  const resumen = useMemo(() => {
    let listos = 0
    let enCurso = 0
    let fallidos = 0
    let trozos = 0
    for (const documento of documentos) {
      if (documento.status === 'ready') {
        listos += 1
        trozos += documento.chunks_count
      } else if (documento.status === 'failed') fallidos += 1
      else if (documento.status !== 'superseded') enCurso += 1
    }
    return { listos, enCurso, fallidos, trozos, total: documentos.length }
  }, [documentos])

  return {
    documentos,
    cargando,
    error,
    subidas,
    recienAprendidos,
    olvidoReciente,
    borrando,
    resumen,
    cargar,
    aplicarEvento,
    aplicarEliminado,
    resincronizar,
    subir,
    descartarSubida,
    eliminar,
    descartarOlvido: () => setOlvidoReciente(null),
  }
}
