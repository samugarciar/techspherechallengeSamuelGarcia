import { Bot, Siren, User } from 'lucide-react'

import { formatearMs } from '@/lib/formato'
import { cn } from '@/lib/utils'
import { EtiquetaCita } from '@/routes/call/EtiquetaCita'
import {
  ETAPAS_LATENCIA,
  NOMBRE_ETAPA,
  type QuienHabla,
  type TurnoLlamada,
} from '@/types/llamadas'

/**
 * La conversación completa, turno a turno.
 *
 * Cada respuesta del agente lleva pegadas sus citas y sus latencias. Ese par
 * —qué dijo y de dónde lo sacó— es lo que convierte una grabación en algo
 * revisable: se puede señalar una frase concreta y comprobar el documento, la
 * sección y la página de los que salió.
 */

const ASPECTO: Record<QuienHabla, { nombre: string; icono: typeof Bot; clases: string }> = {
  agente: { nombre: 'Agente', icono: Bot, clases: 'border-primario/25 bg-primario-suave/40' },
  paciente: { nombre: 'Paciente', icono: User, clases: 'border-borde bg-superficie-tenue' },
  sistema: { nombre: 'Sistema', icono: Siren, clases: 'border-rojo/35 bg-rojo-suave' },
}

export function TurnosLlamada({ turnos }: { turnos: TurnoLlamada[] }) {
  if (turnos.length === 0) {
    return (
      <p className="px-5 py-10 text-center text-[0.8125rem] text-tinta-tenue">
        Esta llamada no llegó a registrar ningún turno.
      </p>
    )
  }

  return (
    <ol className="space-y-2.5 px-5 py-4">
      {turnos.map((turno) => {
        const aspecto = ASPECTO[turno.quien]
        const Icono = aspecto.icono
        const etapas = ETAPAS_LATENCIA.filter((etapa) => typeof turno.ms[etapa] === 'number')

        return (
          <li key={turno.ordinal}>
            <article className={cn('rounded-consola border px-4 py-3', aspecto.clases)}>
              <header className="mb-1 flex items-center gap-2 text-xs">
                <Icono
                  className={cn(
                    'size-3.5 shrink-0',
                    turno.quien === 'sistema' ? 'text-rojo' : 'text-tinta-tenue',
                  )}
                  aria-hidden
                />
                <span
                  className={cn(
                    'font-semibold',
                    turno.quien === 'sistema' ? 'text-rojo' : 'text-tinta',
                  )}
                >
                  {aspecto.nombre}
                </span>
                <span className="numerico ml-auto text-tinta-tenue">turno {turno.ordinal}</span>
              </header>

              <p className="text-[0.9375rem] leading-relaxed text-tinta">{turno.texto}</p>

              {turno.citas.length > 0 ? (
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {turno.citas.map((cita, indice) => (
                    <EtiquetaCita key={`${cita.filename}-${indice}`} cita={cita} />
                  ))}
                </div>
              ) : null}

              {etapas.length > 0 ? (
                <p className="numerico mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[0.6875rem] text-tinta-tenue">
                  {etapas.map((etapa) => (
                    <span key={etapa}>
                      {NOMBRE_ETAPA[etapa]} {formatearMs(turno.ms[etapa])}
                    </span>
                  ))}
                </p>
              ) : null}
            </article>
          </li>
        )
      })}
    </ol>
  )
}
