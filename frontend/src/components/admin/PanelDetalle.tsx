import { Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api'
import { mensajeDeError } from '@/api/errores'
import { InsigniaEstado } from '@/components/admin/InsigniaEstado'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import { CabeceraDialogo, ContenidoDialogo, Dialogo } from '@/components/ui/dialogo'
import { Esqueleto } from '@/components/ui/superficie'
import { formatearFecha, formatearNumero, formatearTamano, pluralizar } from '@/lib/formato'
import type { Documento, DocumentoConTrozos } from '@/types/api'

/**
 * Detalle del documento con los primeros fragmentos.
 *
 * Que un documento esté «Listo» sólo dice que el proceso terminó. Esto enseña QUÉ
 * aprendió: el encabezado con el que quedó archivado cada trozo —que es lo que el
 * agente citará por voz— y el principio de su texto. Es la diferencia entre
 * afirmar que el sistema aprendió y poder señalarlo con el dedo en pantalla.
 */
export function PanelDetalle({
  documento,
  abierto,
  onCambiarApertura,
}: {
  documento: Documento | null
  abierto: boolean
  onCambiarApertura: (abierto: boolean) => void
}) {
  const [detalle, setDetalle] = useState<DocumentoConTrozos | null>(null)
  const [cargando, setCargando] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const id = documento?.id ?? null

  useEffect(() => {
    if (!abierto || !id) return
    let vigente = true
    setCargando(true)
    setError(null)
    api
      .detalle(id)
      .then((datos) => {
        if (vigente) setDetalle(datos)
      })
      .catch((causa: unknown) => {
        if (vigente) setError(mensajeDeError(causa))
      })
      .finally(() => {
        if (vigente) setCargando(false)
      })
    return () => {
      vigente = false
    }
  }, [abierto, id])

  useEffect(() => {
    if (!abierto) setDetalle(null)
  }, [abierto])

  if (!documento) return null

  const mostrado = detalle ?? documento
  const trozos = detalle?.chunks_preview ?? []

  return (
    <Dialogo open={abierto} onOpenChange={onCambiarApertura}>
      <ContenidoDialogo>
        <CabeceraDialogo
          titulo={mostrado.title || mostrado.filename}
          descripcion={<span className="font-mono text-xs">{mostrado.filename}</span>}
        />

        <div className="space-y-5 px-5 py-4">
          <div className="flex flex-wrap items-center gap-2">
            <InsigniaEstado estado={mostrado.status} />
            {mostrado.status === 'ready' ? (
              <span className="text-[0.8125rem] text-tinta-tenue">
                Recuperable por el RAG ahora mismo.
              </span>
            ) : null}
          </div>

          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
            <Dato etiqueta="Fragmentos" valor={formatearNumero(mostrado.chunks_count)} />
            <Dato
              etiqueta="Con embedding"
              valor={`${formatearNumero(mostrado.embedded_count)} / ${formatearNumero(mostrado.chunks_count)}`}
            />
            <Dato
              etiqueta="Páginas"
              valor={mostrado.pages === null ? 'No aplica' : formatearNumero(mostrado.pages)}
            />
            <Dato etiqueta="Tamaño" valor={formatearTamano(mostrado.size_bytes)} />
            <Dato etiqueta="Subido" valor={formatearFecha(mostrado.created_at)} />
            <Dato etiqueta="Actualizado" valor={formatearFecha(mostrado.updated_at)} />
          </dl>

          {mostrado.sha256 ? (
            <p className="truncate font-mono text-[0.6875rem] text-tinta-tenue">
              sha256 {mostrado.sha256.slice(0, 32)}…
            </p>
          ) : null}

          {mostrado.error ? (
            <Aviso tono="error" titulo="La ingesta no terminó">
              {mostrado.error}
            </Aviso>
          ) : null}

          <div>
            <h3 className="text-sm font-semibold text-tinta">Qué aprendió el agente</h3>
            <p className="mt-0.5 text-[0.8125rem] text-tinta-tenue">
              Primeros fragmentos tal y como quedaron archivados, con el encabezado que el agente
              usará para citarlos.
            </p>

            <div className="mt-3 space-y-2.5">
              {cargando ? (
                <>
                  <Esqueleto className="h-20 w-full" />
                  <Esqueleto className="h-20 w-full" />
                </>
              ) : null}

              {!cargando && error ? <Aviso tono="error">{error}</Aviso> : null}

              {!cargando && !error && trozos.length === 0 ? (
                <p className="rounded-consola border border-dashed border-borde px-4 py-6 text-center text-[0.8125rem] text-tinta-tenue">
                  {mostrado.status === 'ready'
                    ? 'El servidor no devolvió vista previa para este documento.'
                    : 'Todavía no hay fragmentos: el documento no ha terminado la ingesta.'}
                </p>
              ) : null}

              {trozos.map((trozo, indice) => (
                <article
                  key={`${trozo.ordinal ?? indice}-${trozo.heading ?? ''}`}
                  className="rounded-consola border border-borde bg-superficie-tenue/55 px-4 py-3"
                >
                  <header className="flex flex-wrap items-baseline justify-between gap-2">
                    <p className="text-[0.8125rem] font-semibold text-tinta">
                      {trozo.heading ?? 'Sin encabezado'}
                    </p>
                    <p className="text-xs text-tinta-tenue">
                      fragmento {formatearNumero((trozo.ordinal ?? indice) + 1)}
                      {trozo.page !== null ? ` · p. ${formatearNumero(trozo.page)}` : ''}
                    </p>
                  </header>
                  <p className="mt-1.5 text-[0.8125rem] leading-relaxed text-tinta-tenue">
                    {trozo.content}
                    {trozo.content.length >= 200 ? '…' : ''}
                  </p>
                </article>
              ))}

              {trozos.length > 0 && mostrado.chunks_count > trozos.length ? (
                <p className="text-xs text-tinta-tenue">
                  Se muestran {trozos.length} de {pluralizar(mostrado.chunks_count, 'fragmento')}.
                </p>
              ) : null}
            </div>
          </div>
        </div>

        <div className="flex justify-end border-t border-borde bg-superficie-tenue/60 px-5 py-3.5">
          <Boton variante="contorno" onClick={() => onCambiarApertura(false)}>
            {cargando ? <Loader2 className="animate-spin" /> : null}
            Cerrar
          </Boton>
        </div>
      </ContenidoDialogo>
    </Dialogo>
  )
}

function Dato({ etiqueta, valor }: { etiqueta: string; valor: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-tinta-tenue">{etiqueta}</dt>
      <dd className="numerico mt-0.5 font-medium text-tinta">{valor}</dd>
    </div>
  )
}
