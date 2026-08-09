import { Ear, MessageSquareText, Sparkles } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'
import { ETIQUETA_FASE, type FaseLlamada } from '@/types/llamadas'

/**
 * En qué punto del turno está el agente.
 *
 * Es el elemento más grande de la pantalla y lo es a propósito: durante la demo
 * la pregunta que todo el mundo se hace mirando desde lejos es «¿me está
 * oyendo?». Se responde con tamaño, color y forma a la vez —no sólo color— para
 * que se lea de un vistazo desde el fondo de una sala y para que siga
 * funcionando con daltonismo o en una proyección con el contraste destrozado.
 */

interface Aspecto {
  etiqueta: string
  detalle: string
  icono: ReactNode
  caja: string
  punto: string
  animacionPunto: string
}

function aspectoDe(fase: FaseLlamada | null, escalada: boolean): Aspecto {
  if (escalada) {
    return {
      etiqueta: 'Escalando',
      detalle: 'El agente ha abandonado el cuestionario y da la instrucción de urgencia.',
      icono: <Sparkles className="size-7" aria-hidden />,
      caja: 'border-rojo/40 bg-rojo-suave text-rojo',
      punto: 'bg-rojo',
      animacionPunto: 'respirando',
    }
  }

  switch (fase) {
    case 'escuchando':
      return {
        etiqueta: ETIQUETA_FASE.escuchando,
        detalle: 'El micrófono está abierto y el detector de voz vigila el turno.',
        icono: <Ear className="size-7" aria-hidden />,
        caja: 'border-verde/35 bg-verde-suave text-verde',
        punto: 'bg-verde',
        animacionPunto: 'respirando',
      }
    case 'pensando':
      return {
        etiqueta: ETIQUETA_FASE.pensando,
        detalle: 'Buscando evidencia en los protocolos y redactando la respuesta.',
        icono: <Sparkles className="size-7" aria-hidden />,
        caja: 'border-ambar/40 bg-ambar-suave text-ambar',
        punto: 'bg-ambar',
        animacionPunto: '',
      }
    case 'hablando':
      return {
        etiqueta: ETIQUETA_FASE.hablando,
        detalle: 'Puedes interrumpirle: se callará y te escuchará.',
        icono: <MessageSquareText className="size-7" aria-hidden />,
        caja: 'border-primario/40 bg-primario-suave text-primario',
        punto: 'bg-primario',
        animacionPunto: '',
      }
    default:
      return {
        etiqueta: 'En espera',
        detalle: 'Todavía no ha empezado el primer turno.',
        icono: <Ear className="size-7" aria-hidden />,
        caja: 'border-borde bg-superficie-tenue text-tinta-tenue',
        punto: 'bg-gris',
        animacionPunto: '',
      }
  }
}

export function IndicadorFase({
  fase,
  escalada,
  nivelEntrada,
  nivelSalida,
}: {
  fase: FaseLlamada | null
  escalada: boolean
  nivelEntrada: number
  nivelSalida: number
}) {
  const aspecto = aspectoDe(fase, escalada)
  const nivel = fase === 'hablando' ? nivelSalida : nivelEntrada

  return (
    <div
      className={cn(
        'flex items-center gap-4 rounded-consola border px-5 py-4 transition-colors',
        aspecto.caja,
      )}
      // `aria-live` sin más leería cada cambio de fase en voz alta encima de la
      // llamada. `polite` + `atomic` deja que el lector espere a una pausa.
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <span className="flex size-12 shrink-0 items-center justify-center rounded-full bg-superficie/70">
        {aspecto.icono}
      </span>

      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2.5 text-2xl font-semibold leading-none tracking-tight">
          {aspecto.etiqueta}
          {fase === 'pensando' && !escalada ? (
            <span className="flex items-end gap-1 pb-0.5" aria-hidden>
              {[0, 1, 2].map((i) => (
                <span key={i} className={cn('puntito size-1.5 rounded-full', aspecto.punto)} />
              ))}
            </span>
          ) : (
            <span
              className={cn(
                'size-2.5 rounded-full',
                aspecto.punto,
                aspecto.animacionPunto,
              )}
              aria-hidden
            />
          )}
        </p>
        <p className="mt-1.5 text-[0.8125rem] leading-snug text-tinta-tenue">{aspecto.detalle}</p>
      </div>

      {/* Vúmetro. Cuando el agente habla mide su salida; cuando escucha, el
          micrófono. Sirve para descartar en un segundo el fallo más tonto de
          todos: el micrófono silenciado. */}
      <div className="hidden w-28 shrink-0 sm:block" aria-hidden>
        <div className="h-1.5 overflow-hidden rounded-full bg-superficie/70">
          <div
            className={cn('h-full rounded-full transition-[width] duration-100', aspecto.punto)}
            style={{ width: `${Math.round(Math.min(1, nivel) * 100)}%` }}
          />
        </div>
        <p className="mt-1.5 text-right text-[0.6875rem] uppercase tracking-wide text-tinta-tenue">
          {fase === 'hablando' ? 'Salida' : 'Micrófono'}
        </p>
      </div>
    </div>
  )
}
