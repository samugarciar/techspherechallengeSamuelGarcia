import { Gauge } from 'lucide-react'

import { formatearMs } from '@/lib/formato'
import { medianaEtapa, ultimaConEtapa } from '@/routes/call/estadoLlamada'
import {
  DETALLE_ETAPA,
  ETAPAS_LATENCIA,
  NOMBRE_ETAPA,
  type EtapasLlamadaMs,
} from '@/types/llamadas'

/**
 * Latencias por etapa.
 *
 * Cuatro magnitudes de la MISMA medida (milisegundos), así que van todas del
 * mismo color: cuatro colores distintos sugerirían cuatro cosas distintas y
 * malgastarían la paleta, que en esta aplicación está reservada para el estado.
 * El número va escrito al lado de cada barra —con cuatro filas, etiquetar todas
 * es legible— para que nada dependa sólo de la longitud.
 *
 * La escala no se recalcula turno a turno: crece con el máximo visto en toda la
 * llamada y ya no baja. Si se reescalara en cada turno, un turno rápido pintaría
 * las mismas barras que uno lento y la comparación entre turnos sería mentira.
 */

/** Suelo de la escala. Por debajo, las barras de un turno rápido serían pelos. */
const ESCALA_MINIMA_MS = 400

export function PanelLatencias({ metricas }: { metricas: EtapasLlamadaMs[] }) {
  const observado = Math.max(
    ESCALA_MINIMA_MS,
    ...metricas.flatMap((m) =>
      ETAPAS_LATENCIA.map((etapa) => m[etapa]).filter(
        (valor): valor is number => typeof valor === 'number',
      ),
    ),
  )

  const totalUltimo = ETAPAS_LATENCIA.reduce(
    (suma, etapa) => suma + (ultimaConEtapa(metricas, etapa) ?? 0),
    0,
  )

  if (metricas.length === 0) {
    return (
      <p className="px-5 py-6 text-[0.8125rem] leading-relaxed text-tinta-tenue">
        Las latencias aparecen al cerrar el primer turno. Se miden por etapa —transcripción,
        búsqueda, modelo y voz— para poder decir dónde se va el tiempo y no sólo cuánto tarda.
      </p>
    )
  }

  return (
    <div className="px-5 py-4">
      <div className="mb-3.5 flex items-baseline justify-between gap-3">
        <p className="text-[0.8125rem] text-tinta-tenue">
          Último turno · mediana de {metricas.length}
        </p>
        <p className="numerico text-2xl font-semibold leading-none tracking-tight text-tinta">
          {formatearMs(totalUltimo)}
        </p>
      </div>

      <ul className="space-y-2.5">
        {ETAPAS_LATENCIA.map((etapa) => {
          const ultimo = ultimaConEtapa(metricas, etapa)
          const mediana = medianaEtapa(metricas, etapa)
          const fraccion = ultimo === null ? 0 : Math.min(1, ultimo / observado)

          return (
            <li key={etapa} title={DETALLE_ETAPA[etapa]}>
              <div className="flex items-baseline justify-between gap-3 text-[0.8125rem]">
                <span className="text-tinta">{NOMBRE_ETAPA[etapa]}</span>
                <span className="numerico shrink-0 text-tinta-tenue">
                  <span className="font-medium text-tinta">{formatearMs(ultimo ?? undefined)}</span>
                  {mediana !== null && metricas.length > 1 ? (
                    <span className="ml-1.5">· p50 {formatearMs(mediana)}</span>
                  ) : null}
                </span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-superficie-tenue" aria-hidden>
                <div
                  className="h-full rounded-full bg-primario transition-[width] duration-300"
                  style={{ width: `${Math.max(fraccion * 100, ultimo === null ? 0 : 2)}%` }}
                />
              </div>
            </li>
          )
        })}
      </ul>

      <p className="mt-3.5 flex items-start gap-1.5 text-xs leading-relaxed text-tinta-tenue">
        <Gauge className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        <span>
          Escala común a toda la llamada, hasta {formatearMs(observado)}. «Voz» es el tiempo hasta
          la primera frase sintetizada, no hasta la última: el agente empieza a sonar mientras
          todavía se está generando el resto.
        </span>
      </p>
    </div>
  )
}
