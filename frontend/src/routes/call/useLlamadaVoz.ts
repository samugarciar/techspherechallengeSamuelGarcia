import { useCallback, useEffect, useReducer, useRef, useState } from 'react'

import type { EstadoConexion } from '@/api/cliente'
import { mensajeDeError } from '@/api/errores'
import { apiLlamadas, type SesionVoz } from '@/api/llamadas'
import { CapturaMicrofono, mensajeDeErrorDeMicrofono } from '@/lib/audio/captura'
import { ReproductorAgente } from '@/lib/audio/reproductor'
import {
  ESTADO_INICIAL,
  reductorLlamada,
  type EstadoConversacion,
} from '@/routes/call/estadoLlamada'
import type { LlamadaCreada, Paciente } from '@/types/llamadas'

/**
 * Orquesta una llamada entera: crearla, abrir el WebSocket, encender el
 * micrófono, mover el estado con lo que llega y apagarlo todo al terminar.
 *
 * Dos decisiones que conviene tener a la vista:
 *
 * 1. **No hay reconexión automática.** El SSE de documentos sí la tiene, porque
 *    reconectar allí es idempotente: se recarga la lista y ya. Aquí no: el
 *    servidor no define cómo se retoma una llamada a medias, y reconectar solo
 *    podría hacer que el agente vuelva a presentarse desde cero encima de una
 *    conversación clínica ya empezada. Se avisa en español y se ofrece el botón.
 *
 * 2. **`parar` vacía el buffer aquí, no en el reductor.** Es un efecto sobre el
 *    AudioContext y tiene que ocurrir en el mismo tick en que llega el mensaje;
 *    pasar por un render intermedio son decenas de milisegundos de agente
 *    hablando encima del paciente.
 */

export type SituacionLlamada = 'elegir' | 'creando' | 'en_curso' | 'terminada'

export type EstadoMicrofono = 'inactivo' | 'abriendo' | 'abierto' | 'error'

export interface Medidores {
  /** ms de voz del agente pendientes de sonar en el cliente. */
  msEnCola: number
  nivelSalida: number
  nivelEntrada: number
  segundos: number
}

const MEDIDORES_VACIOS: Medidores = { msEnCola: 0, nivelSalida: 0, nivelEntrada: 0, segundos: 0 }

/** 10 Hz: suficiente para que un vúmetro parezca vivo y no repinta de más. */
const REFRESCO_MEDIDORES_MS = 100

export interface LlamadaVoz {
  situacion: SituacionLlamada
  paciente: Paciente | null
  llamada: LlamadaCreada | null
  conversacion: EstadoConversacion
  conexion: EstadoConexion
  /** Fallo de la conexión de voz, ya en español y mostrable. */
  errorConexion: string | null
  /** Fallo al crear la llamada (`POST /api/calls`). */
  errorCreacion: string | null
  microfono: EstadoMicrofono
  errorMicrofono: string | null
  medidores: Medidores
  iniciar: (paciente: Paciente) => void
  colgar: () => void
  reconectar: () => void
  abrirMicrofono: () => void
  volverALista: () => void
}

export function useLlamadaVoz(): LlamadaVoz {
  const [conversacion, despachar] = useReducer(reductorLlamada, ESTADO_INICIAL)
  const [situacion, setSituacion] = useState<SituacionLlamada>('elegir')
  const [paciente, setPaciente] = useState<Paciente | null>(null)
  const [llamada, setLlamada] = useState<LlamadaCreada | null>(null)
  const [conexion, setConexion] = useState<EstadoConexion>('inactivo')
  const [errorConexion, setErrorConexion] = useState<string | null>(null)
  const [errorCreacion, setErrorCreacion] = useState<string | null>(null)
  const [microfono, setMicrofono] = useState<EstadoMicrofono>('inactivo')
  const [errorMicrofono, setErrorMicrofono] = useState<string | null>(null)
  const [medidores, setMedidores] = useState<Medidores>(MEDIDORES_VACIOS)

  const sesion = useRef<SesionVoz | null>(null)
  const reproductor = useRef<ReproductorAgente | null>(null)
  const captura = useRef<CapturaMicrofono | null>(null)
  const arrancada = useRef<number>(0)
  const nivelEntrada = useRef(0)
  /** Evita que el `onclose` del cierre que provocamos nosotros pinte un error. */
  const colgadaPorNosotros = useRef(false)

  const desde = useCallback(() => Math.max(0, Date.now() - arrancada.current), [])

  const pararMicrofono = useCallback(() => {
    captura.current?.parar()
    captura.current = null
    nivelEntrada.current = 0
    setMicrofono('inactivo')
  }, [])

  const cerrarTodo = useCallback(() => {
    colgadaPorNosotros.current = true
    sesion.current?.cerrar()
    sesion.current = null
    pararMicrofono()
    void reproductor.current?.cerrar()
    reproductor.current = null
    setMedidores(MEDIDORES_VACIOS)
  }, [pararMicrofono])

  // Salir de la pantalla a media llamada tiene que colgar de verdad: si no, el
  // micrófono se queda abierto y el navegador deja el punto rojo encendido.
  useEffect(() => () => cerrarTodo(), [cerrarTodo])

  const abrirMicrofono = useCallback(async () => {
    if (apiLlamadas.esSimulado) return
    if (captura.current) return
    setMicrofono('abriendo')
    setErrorMicrofono(null)
    const micro = new CapturaMicrofono()
    try {
      await micro.arrancar({
        onPcm: (pcm) => sesion.current?.enviarAudio(pcm),
        onNivel: (nivel) => {
          nivelEntrada.current = nivel
        },
      })
      captura.current = micro
      setMicrofono('abierto')
    } catch (causa) {
      micro.parar()
      setMicrofono('error')
      setErrorMicrofono(mensajeDeErrorDeMicrofono(causa))
    }
  }, [])

  const conectarVoz = useCallback(
    (creada: LlamadaCreada) => {
      colgadaPorNosotros.current = false
      setErrorConexion(null)

      const repro = reproductor.current ?? new ReproductorAgente()
      reproductor.current = repro

      sesion.current = apiLlamadas.abrirVoz(creada, {
        onMensaje: (mensaje) => {
          if (mensaje.tipo === 'listo') repro.configurar(mensaje.sample_rate_salida)

          // Barge-in. El servidor ya nos lleva segundos de ventaja: si no se
          // vacía aquí, el agente sigue sonando aunque el servidor haya callado.
          if (mensaje.tipo === 'parar') {
            const descartados = repro.vaciar()
            despachar({ tipo: 'interrupcion', msDescartados: descartados })
          }

          if (mensaje.tipo === 'fin') {
            colgadaPorNosotros.current = true
            pararMicrofono()
            setSituacion('terminada')
          }

          despachar({ tipo: 'mensaje', mensaje, instante: desde() })

          // La bandera roja deja además una marca en la propia transcripción.
          // El cartel de arriba se ve desde lejos, pero desaparece del relato en
          // cuanto alguien lee la conversación de corrido; y el historial sí
          // guarda ese turno de sistema, así que sin esto la llamada en vivo y
          // la llamada revisada después contarían historias distintas.
          if (mensaje.tipo === 'bandera_roja') {
            despachar({
              tipo: 'nota',
              texto:
                `Bandera roja: ${mensaje.motivo}. Urgencia: ${mensaje.urgencia ?? 'urgente'}. ` +
                'El agente abandona el resto del cuestionario.',
              instante: desde(),
            })
          }
        },
        onAudio: (pcm) => repro.encolar(pcm),
        onEstado: (estado) => setConexion(estado),
        onFallo: (mensaje) => {
          if (colgadaPorNosotros.current) return
          setErrorConexion(mensaje)
          pararMicrofono()
        },
      })
    },
    [desde, pararMicrofono],
  )

  const iniciar = useCallback(
    async (elegido: Paciente) => {
      cerrarTodo()
      colgadaPorNosotros.current = false
      despachar({ tipo: 'reiniciar' })
      setPaciente(elegido)
      setLlamada(null)
      setErrorCreacion(null)
      setErrorConexion(null)
      setErrorMicrofono(null)
      setMicrofono('inactivo')
      setSituacion('creando')
      arrancada.current = Date.now()

      let creada: LlamadaCreada
      try {
        creada = await apiLlamadas.crearLlamada(elegido.id)
      } catch (causa) {
        setErrorCreacion(mensajeDeError(causa))
        setSituacion('elegir')
        return
      }

      setLlamada(creada)
      setSituacion('en_curso')
      arrancada.current = Date.now()
      conectarVoz(creada)
      // El micrófono se pide después de tener el canal abierto: si se pide antes
      // y el backend no responde, al usuario le sale el permiso del micro para
      // una llamada que no va a existir.
      void abrirMicrofono()
    },
    [abrirMicrofono, cerrarTodo, conectarVoz],
  )

  const colgar = useCallback(() => {
    cerrarTodo()
    setConexion('cerrado')
    despachar({ tipo: 'terminar', motivo: 'cortada' })
    setSituacion('terminada')
  }, [cerrarTodo])

  const reconectar = useCallback(() => {
    if (!llamada) return
    despachar({
      tipo: 'nota',
      texto: 'Se reanudó la conexión de voz tras una caída.',
      instante: desde(),
    })
    conectarVoz(llamada)
    void abrirMicrofono()
  }, [abrirMicrofono, conectarVoz, desde, llamada])

  const volverALista = useCallback(() => {
    cerrarTodo()
    despachar({ tipo: 'reiniciar' })
    setSituacion('elegir')
    setPaciente(null)
    setLlamada(null)
    setConexion('inactivo')
    setErrorConexion(null)
    setErrorMicrofono(null)
  }, [cerrarTodo])

  // Medidores. Van por intervalo y no por evento porque son continuos: la cola
  // de audio se vacía sola con el tiempo, sin que llegue ningún mensaje.
  useEffect(() => {
    if (situacion !== 'en_curso') return
    const id = setInterval(() => {
      const repro = reproductor.current
      setMedidores({
        msEnCola: repro?.msEnCola() ?? 0,
        nivelSalida: repro?.nivel() ?? 0,
        nivelEntrada: nivelEntrada.current,
        segundos: Math.floor((Date.now() - arrancada.current) / 1000),
      })
    }, REFRESCO_MEDIDORES_MS)
    return () => clearInterval(id)
  }, [situacion])

  return {
    situacion,
    paciente,
    llamada,
    conversacion,
    conexion,
    errorConexion,
    errorCreacion,
    microfono,
    errorMicrofono,
    medidores,
    iniciar: (elegido) => void iniciar(elegido),
    colgar,
    reconectar,
    abrirMicrofono: () => void abrirMicrofono(),
    volverALista,
  }
}
