import { ChevronRight, PhoneMissed, Siren } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Esqueleto } from '@/components/ui/superficie'
import { Insignia } from '@/components/ui/insignia'
import { formatearDuracion } from '@/lib/duracion'
import { formatearFechaCorta, formatearNumero } from '@/lib/formato'
import { ETIQUETA_ESTADO_LLAMADA, type ResumenLlamada } from '@/types/llamadas'

/**
 * Historial de llamadas.
 *
 * La columna que manda es «Escalada»: es la que un clínico busca primero al
 * abrir esta pantalla, así que va marcada en rojo con icono y texto, no sólo con
 * color. Lo demás es contexto para encontrar la llamada que se busca.
 */
export function TablaLlamadas({
  llamadas,
  cargando,
}: {
  llamadas: ResumenLlamada[]
  cargando: boolean
}) {
  if (cargando) return <CargandoTabla />
  if (llamadas.length === 0) return <SinLlamadas />

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[48rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-borde text-left text-xs uppercase tracking-wide text-tinta-tenue">
            <th scope="col" className="px-5 py-2.5 font-medium">
              Paciente
            </th>
            <th scope="col" className="px-3 py-2.5 font-medium">
              Cuándo
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-medium">
              Duración
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-medium">
              Turnos
            </th>
            <th scope="col" className="px-3 py-2.5 font-medium">
              Estado
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Resultado
            </th>
          </tr>
        </thead>
        <tbody>
          {llamadas.map((llamada) => (
            <tr key={llamada.id} className="border-b border-borde transition-colors hover:bg-superficie-tenue/60">
              <td className="max-w-[20rem] px-5 py-3">
                <Link
                  to={`/calls/${llamada.id}`}
                  className="group flex items-center gap-1.5 text-left"
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-tinta group-hover:underline">
                      {llamada.paciente}
                    </span>
                    <span className="block truncate text-xs text-tinta-tenue">
                      {llamada.cirugia ?? 'sin cirugía registrada'}
                    </span>
                  </span>
                  <ChevronRight className="size-4 shrink-0 text-tinta-tenue" aria-hidden />
                </Link>
              </td>

              <td className="whitespace-nowrap px-3 py-3 text-tinta-tenue">
                {formatearFechaCorta(llamada.iniciada)}
              </td>

              <td className="numerico px-3 py-3 text-right text-tinta-tenue">
                {formatearDuracion(llamada.duracion_s)}
              </td>

              <td className="numerico px-3 py-3 text-right text-tinta-tenue">
                {formatearNumero(llamada.turnos)}
              </td>

              <td className="px-3 py-3">
                <Insignia
                  color={
                    llamada.estado === 'completada'
                      ? 'verde'
                      : llamada.estado === 'en_curso'
                        ? 'ambar'
                        : 'gris'
                  }
                >
                  {ETIQUETA_ESTADO_LLAMADA[llamada.estado]}
                </Insignia>
              </td>

              <td className="px-5 py-3">
                {llamada.escalada ? (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-rojo/35 bg-rojo-suave px-2.5 py-0.5 text-xs font-semibold text-rojo">
                    <Siren className="size-3.5 shrink-0" aria-hidden />
                    <span className="max-w-[16rem] truncate" title={llamada.motivo_escalada ?? ''}>
                      {llamada.motivo_escalada ?? 'Escalada'}
                    </span>
                  </span>
                ) : (
                  <span className="text-tinta-tenue">Sin signos de alarma</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CargandoTabla() {
  return (
    <div className="space-y-3 px-5 py-5">
      {[0, 1, 2].map((fila) => (
        <div key={fila} className="flex items-center gap-4">
          <Esqueleto className="h-9 flex-1" />
          <Esqueleto className="h-6 w-28" />
          <Esqueleto className="h-6 w-20" />
        </div>
      ))}
      <p className="sr-only">Cargando llamadas…</p>
    </div>
  )
}

function SinLlamadas() {
  return (
    <div className="px-6 py-14 text-center">
      <div className="mx-auto flex size-11 items-center justify-center rounded-full bg-superficie-tenue">
        <PhoneMissed className="size-5 text-tinta-tenue" aria-hidden />
      </div>
      <p className="mt-3 text-sm font-medium text-tinta">Todavía no se ha llamado a nadie</p>
      <p className="mx-auto mt-1 max-w-md text-[0.8125rem] leading-relaxed text-tinta-tenue">
        Cuando termine la primera llamada aparecerá aquí, con su transcripción completa y las citas
        de cada respuesta.
      </p>
    </div>
  )
}
