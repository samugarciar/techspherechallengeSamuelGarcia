import { Check } from 'lucide-react'

import { formatearNumero } from '@/lib/formato'
import { cn } from '@/lib/utils'
import type { Documento } from '@/types/api'
import { definicionEstado, SECUENCIA_INGESTA } from '@/types/estados'

/**
 * El paso a paso de la ingesta, desplegado bajo la fila mientras el documento se
 * está procesando.
 *
 * Es el elemento con más peso visual de la pantalla y es deliberado: aquí es
 * donde se ve al sistema APRENDER. Un badge que cambia de texto cada dos segundos
 * pasa desapercibido en una demo; un recorrido con la etapa actual iluminada y un
 * contador de embeddings subiendo, no. Cuando el documento llega a `ready` este
 * bloque desaparece: ya no hay nada que mirar, y dejarlo sería ruido.
 */
export function PasosIngesta({ documento }: { documento: Documento }) {
  const indiceActual = SECUENCIA_INGESTA.indexOf(documento.status)
  const definicion = definicionEstado(documento.status)
  const total = documento.chunks_count
  const hechos = documento.embedded_count
  const fraccion = total > 0 ? Math.min(1, hechos / total) : 0

  return (
    <div className="rounded-consola border border-ambar/30 bg-ambar-suave/45 px-4 py-3.5">
      <ol className="flex flex-wrap items-center gap-x-1.5 gap-y-2">
        {SECUENCIA_INGESTA.map((etapa, indice) => {
          const completada = indice < indiceActual
          const activa = indice === indiceActual
          const etiqueta = definicionEstado(etapa).etiqueta
          return (
            <li key={etapa} className="flex items-center gap-1.5">
              <span
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors',
                  completada && 'border-transparent bg-transparent text-tinta-tenue',
                  activa && 'border-ambar/45 bg-superficie font-semibold text-tinta shadow-sm',
                  !completada && !activa && 'border-transparent text-tinta-tenue/55',
                )}
              >
                {completada ? (
                  <Check className="size-3 text-verde" aria-hidden />
                ) : (
                  <span
                    className={cn(
                      'size-1.5 rounded-full',
                      activa ? 'bg-ambar latiendo' : 'bg-tinta-tenue/35',
                    )}
                    aria-hidden
                  />
                )}
                {/* La etiqueta larga de `ready` no cabe en un paso: aquí basta «Listo». */}
                {etapa === 'ready' ? 'Listo' : etiqueta}
              </span>
              {indice < SECUENCIA_INGESTA.length - 1 ? (
                <span className="text-tinta-tenue/30" aria-hidden>
                  ·
                </span>
              ) : null}
            </li>
          )
        })}
      </ol>

      <p className="mt-2 text-[0.8125rem] text-tinta-tenue">{definicion.descripcion}</p>

      {documento.status === 'embedding' && total > 0 ? (
        <div className="mt-2.5">
          <div className="flex items-baseline justify-between text-xs text-tinta-tenue">
            <span>Vectorizando fragmentos</span>
            <span className="numerico font-semibold text-tinta">
              {formatearNumero(hechos)} / {formatearNumero(total)}
            </span>
          </div>
          <div
            className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-superficie"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={hechos}
            aria-label="Fragmentos vectorizados"
          >
            <div
              className="h-full rounded-full bg-ambar transition-[width] duration-300 ease-out"
              style={{ width: `${Math.round(fraccion * 100)}%` }}
            />
          </div>
        </div>
      ) : null}
    </div>
  )
}
