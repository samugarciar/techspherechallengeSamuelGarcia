import { AlertTriangle, Bot, Info, User } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { EtiquetaCita } from '@/routes/call/EtiquetaCita'
import type { Intervencion } from '@/routes/call/estadoLlamada'
import { reloj } from '@/lib/duracion'
import { cn } from '@/lib/utils'
import type { QuienHabla } from '@/types/llamadas'

/**
 * Transcripción en vivo de los dos lados.
 *
 * Dos cosas que no son evidentes:
 *
 * · Las parciales del STT se SUSTITUYEN, no se acumulan (eso lo garantiza el
 *   reductor). Aquí sólo se marcan: mientras una intervención es parcial lleva
 *   un cursor y el texto va en gris, porque puede cambiar. Al consolidar se
 *   asienta. Ver esa consolidación en directo es media demostración de que hay
 *   un STT de verdad detrás y no un guion.
 *
 * · El desplazamiento automático se desactiva solo si alguien sube a leer algo.
 *   Arrastrar al jurado de vuelta abajo cada vez que llega una frase es la forma
 *   más rápida de que deje de leer.
 */

const ASPECTO: Record<QuienHabla, { nombre: string; icono: typeof Bot; clases: string }> = {
  agente: {
    nombre: 'Agente',
    icono: Bot,
    clases: 'border-primario/25 bg-primario-suave/45',
  },
  paciente: {
    nombre: 'Paciente',
    icono: User,
    clases: 'border-borde bg-superficie-tenue',
  },
  sistema: {
    nombre: 'Sistema',
    icono: Info,
    clases: 'border-ambar/35 bg-ambar-suave/60',
  },
}

export function Transcripcion({
  intervenciones,
  banderaDesde,
}: {
  intervenciones: Intervencion[]
  /** Instante de la bandera roja: lo dicho a partir de ahí va marcado. */
  banderaDesde: number | null
}) {
  const contenedor = useRef<HTMLDivElement | null>(null)
  const [seguir, setSeguir] = useState(true)

  useEffect(() => {
    if (!seguir) return
    const nodo = contenedor.current
    if (nodo) nodo.scrollTop = nodo.scrollHeight
  }, [intervenciones, seguir])

  return (
    <div className="relative">
      <div
        ref={contenedor}
        onScroll={(evento) => {
          const nodo = evento.currentTarget
          const alFinal = nodo.scrollHeight - nodo.scrollTop - nodo.clientHeight < 48
          setSeguir(alFinal)
        }}
        className="max-h-[26rem] min-h-[16rem] space-y-2.5 overflow-y-auto px-5 py-4"
      >
        {intervenciones.length === 0 ? (
          <p className="py-12 text-center text-[0.8125rem] text-tinta-tenue">
            La conversación aparecerá aquí en cuanto el agente descuelgue.
          </p>
        ) : (
          intervenciones.map((intervencion) => (
            <Burbuja
              key={intervencion.clave}
              intervencion={intervencion}
              trasBandera={banderaDesde !== null && intervencion.instante >= banderaDesde}
            />
          ))
        )}
      </div>

      {!seguir ? (
        <button
          type="button"
          onClick={() => {
            setSeguir(true)
            const nodo = contenedor.current
            if (nodo) nodo.scrollTop = nodo.scrollHeight
          }}
          className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-borde-fuerte bg-superficie px-3 py-1 text-xs font-medium text-tinta shadow-sm"
        >
          Volver al final
        </button>
      ) : null}
    </div>
  )
}

function Burbuja({
  intervencion,
  trasBandera,
}: {
  intervencion: Intervencion
  trasBandera: boolean
}) {
  const aspecto = ASPECTO[intervencion.quien]
  const Icono = aspecto.icono

  return (
    <article
      className={cn(
        'rounded-consola border px-4 py-3',
        aspecto.clases,
        // Todo lo que se dice después de la bandera roja pertenece al
        // escalamiento. Marcarlo con un filo rojo hace que en la revisión
        // posterior se vea de un golpe dónde cambió la llamada de naturaleza.
        trasBandera && 'border-l-[3px] border-l-rojo',
      )}
    >
      <header className="mb-1 flex items-center gap-2 text-xs">
        <Icono className="size-3.5 shrink-0 text-tinta-tenue" aria-hidden />
        <span className="font-semibold text-tinta">{aspecto.nombre}</span>
        <span className="numerico ml-auto text-tinta-tenue">{reloj(intervencion.instante)}</span>
      </header>

      <p
        className={cn(
          'text-[0.9375rem] leading-relaxed',
          intervencion.parcial ? 'text-tinta-tenue' : 'text-tinta',
        )}
      >
        {intervencion.texto}
        {intervencion.parcial ? (
          <span
            className="cursor-parcial ml-0.5 inline-block h-[1.05em] w-[2px] translate-y-[0.15em] bg-tinta-tenue"
            aria-hidden
          />
        ) : null}
      </p>

      {intervencion.citas.length > 0 ? (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {intervencion.citas.map((cita, indice) => (
            <EtiquetaCita key={`${cita.filename}-${indice}`} cita={cita} />
          ))}
        </div>
      ) : null}

      {trasBandera && intervencion.quien === 'agente' ? (
        <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-rojo">
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
          Instrucción de urgencia
        </p>
      ) : null}
    </article>
  )
}
