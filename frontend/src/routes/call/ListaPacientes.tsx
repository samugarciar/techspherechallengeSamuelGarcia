import { CalendarClock, Phone, PhoneOff, Pill, UserRound } from 'lucide-react'

import { Boton } from '@/components/ui/boton'
import { Esqueleto } from '@/components/ui/superficie'
import { diasDesde } from '@/lib/duracion'
import { formatearFechaCorta, formatearRelativo, pluralizar } from '@/lib/formato'
import { cn } from '@/lib/utils'
import type { Paciente } from '@/types/llamadas'

/**
 * Lista de pacientes con seguimiento pendiente.
 *
 * El flujo es «se elige a quién llamar y se llama», que es decisión de producto
 * (la 5): refleja el caso real —el hospital llama al paciente, no al revés— y en
 * la demo permite escoger el caso que se quiere enseñar. Por eso cada fila lleva
 * a la vista lo que decide el guion: qué cirugía, cuántos días han pasado y
 * cuánta medicación sigue activa.
 */
export function ListaPacientes({
  pacientes,
  cargando,
  creando,
  onLlamar,
}: {
  pacientes: Paciente[]
  cargando: boolean
  /** Id del paciente cuya llamada se está creando, si hay alguna. */
  creando: string | null
  onLlamar: (paciente: Paciente) => void
}) {
  if (cargando) return <CargandoLista />
  if (pacientes.length === 0) return <SinPacientes />

  return (
    <ul className="divide-y divide-borde">
      {pacientes.map((paciente) => (
        <FilaPaciente
          key={paciente.id}
          paciente={paciente}
          ocupado={creando !== null}
          llamando={creando === paciente.id}
          onLlamar={onLlamar}
        />
      ))}
    </ul>
  )
}

function FilaPaciente({
  paciente,
  ocupado,
  llamando,
  onLlamar,
}: {
  paciente: Paciente
  ocupado: boolean
  llamando: boolean
  onLlamar: (paciente: Paciente) => void
}) {
  const dias = paciente.cirugia?.dias_desde ?? null
  // Menos de 72 h es cuando aparecen las complicaciones que este seguimiento
  // busca: se marca para que el orden de las llamadas no sea sólo alfabético.
  const reciente = dias !== null && dias <= 3

  return (
    <li>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3 px-5 py-4 transition-colors hover:bg-superficie-tenue/60">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-superficie-tenue text-tinta-tenue">
          <UserRound className="size-5" aria-hidden />
        </span>

        <div className="min-w-[13rem] flex-1">
          <p className="font-medium leading-tight text-tinta">{paciente.nombre}</p>
          <p className="mt-0.5 text-[0.8125rem] leading-snug text-tinta-tenue">
            {paciente.cirugia?.nombre ?? 'Sin cirugía registrada'}
          </p>
        </div>

        <div className="min-w-[10rem]">
          <p
            className={cn(
              'text-[0.8125rem] font-medium',
              reciente ? 'text-ambar' : 'text-tinta-tenue',
            )}
          >
            {diasDesde(dias)}
          </p>
          {paciente.cirugia?.fecha ? (
            <p className="text-xs text-tinta-tenue">{formatearFechaCorta(paciente.cirugia.fecha)}</p>
          ) : null}
        </div>

        <div className="min-w-[9rem] text-[0.8125rem] text-tinta-tenue">
          <p className="flex items-center gap-1.5">
            <Pill className="size-3.5 shrink-0" aria-hidden />
            {paciente.medicacion_activa > 0
              ? pluralizar(paciente.medicacion_activa, 'medicamento')
              : 'sin medicación activa'}
          </p>
          {paciente.proxima_cita ? (
            <p className="mt-0.5 flex items-center gap-1.5">
              <CalendarClock className="size-3.5 shrink-0" aria-hidden />
              cita {formatearFechaCorta(paciente.proxima_cita)}
            </p>
          ) : null}
        </div>

        <div className="min-w-[8rem] text-[0.8125rem] text-tinta-tenue">
          {paciente.ultima_llamada ? (
            <>
              <p className="text-xs uppercase tracking-wide">Última llamada</p>
              <p>{formatearRelativo(paciente.ultima_llamada)}</p>
            </>
          ) : (
            <p className="flex items-center gap-1.5">
              <PhoneOff className="size-3.5 shrink-0" aria-hidden />
              nunca se le ha llamado
            </p>
          )}
        </div>

        <Boton
          className="ml-auto"
          disabled={ocupado}
          onClick={() => onLlamar(paciente)}
          aria-label={`Llamar a ${paciente.nombre}`}
        >
          <Phone />
          {llamando ? 'Conectando…' : 'Llamar'}
        </Boton>
      </div>
    </li>
  )
}

function CargandoLista() {
  return (
    <div className="space-y-4 px-5 py-5">
      {[0, 1, 2].map((fila) => (
        <div key={fila} className="flex items-center gap-4">
          <Esqueleto className="size-10 rounded-full" />
          <Esqueleto className="h-9 flex-1" />
          <Esqueleto className="h-8 w-24" />
        </div>
      ))}
      <p className="sr-only">Cargando pacientes…</p>
    </div>
  )
}

function SinPacientes() {
  return (
    <div className="px-6 py-14 text-center">
      <div className="mx-auto flex size-11 items-center justify-center rounded-full bg-superficie-tenue">
        <PhoneOff className="size-5 text-tinta-tenue" aria-hidden />
      </div>
      <p className="mt-3 text-sm font-medium text-tinta">No hay seguimientos pendientes</p>
      <p className="mx-auto mt-1 max-w-md text-[0.8125rem] leading-relaxed text-tinta-tenue">
        Nadie tiene una llamada de seguimiento programada ahora mismo. Cuando se registre una
        cirugía nueva, el paciente aparecerá en esta lista.
      </p>
    </div>
  )
}
