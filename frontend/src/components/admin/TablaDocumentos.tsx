import { Eye, Inbox, Trash2 } from 'lucide-react'

import { InsigniaEstado } from '@/components/admin/InsigniaEstado'
import { PasosIngesta } from '@/components/admin/PasosIngesta'
import { Boton } from '@/components/ui/boton'
import { Esqueleto } from '@/components/ui/superficie'
import { formatearFechaCorta, formatearNumero, formatearTamano } from '@/lib/formato'
import { cn } from '@/lib/utils'
import type { Documento } from '@/types/api'
import { definicionEstado } from '@/types/estados'

/**
 * Tabla de documentos.
 *
 * Cada documento ocupa su propio `tbody` para poder desplegar bajo la fila el
 * paso a paso de la ingesta sin romper la rejilla de columnas. Mientras el
 * documento se procesa, ese despliegue es lo que se mira; cuando termina,
 * desaparece y la fila vuelve a ser una fila.
 */
export function TablaDocumentos({
  documentos,
  cargando,
  recienAprendidos,
  borrando,
  onVerDetalle,
  onBorrar,
}: {
  documentos: Documento[]
  cargando: boolean
  recienAprendidos: Set<string>
  borrando: string | null
  onVerDetalle: (documento: Documento) => void
  onBorrar: (documento: Documento) => void
}) {
  if (cargando) return <CargandoTabla />
  if (documentos.length === 0) return <SinDocumentos />

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[54rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-borde text-left text-xs uppercase tracking-wide text-tinta-tenue">
            <th scope="col" className="px-5 py-2.5 font-medium">
              Documento
            </th>
            <th scope="col" className="px-3 py-2.5 font-medium">
              Estado
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-medium">
              Fragmentos
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-medium">
              Páginas
            </th>
            <th scope="col" className="px-3 py-2.5 text-right font-medium">
              Tamaño
            </th>
            <th scope="col" className="px-3 py-2.5 font-medium">
              Subido
            </th>
            <th scope="col" className="px-5 py-2.5 text-right font-medium">
              <span className="sr-only">Acciones</span>
            </th>
          </tr>
        </thead>

        {documentos.map((documento) => (
          <FilaDocumento
            key={documento.id}
            documento={documento}
            recienAprendido={recienAprendidos.has(documento.id)}
            borrando={borrando === documento.id}
            onVerDetalle={onVerDetalle}
            onBorrar={onBorrar}
          />
        ))}
      </table>
    </div>
  )
}

function FilaDocumento({
  documento,
  recienAprendido,
  borrando,
  onVerDetalle,
  onBorrar,
}: {
  documento: Documento
  recienAprendido: boolean
  borrando: boolean
  onVerDetalle: (documento: Documento) => void
  onBorrar: (documento: Documento) => void
}) {
  const definicion = definicionEstado(documento.status)
  const enCurso = definicion.enCurso
  const nombre = documento.title || documento.filename

  return (
    <tbody className={cn('border-b border-borde align-middle', recienAprendido && 'destello-aprendido')}>
      <tr className="transition-colors hover:bg-superficie-tenue/60">
        <td className="max-w-[22rem] px-5 py-3">
          <p className="truncate font-medium text-tinta" title={nombre}>
            {nombre}
          </p>
          <p className="truncate font-mono text-xs text-tinta-tenue" title={documento.filename}>
            {documento.filename}
          </p>
        </td>

        <td className="px-3 py-3">
          <InsigniaEstado estado={documento.status} />
        </td>

        <td className="numerico px-3 py-3 text-right">
          {documento.chunks_count === 0 ? (
            <span className="text-tinta-tenue">—</span>
          ) : documento.embedded_count < documento.chunks_count ? (
            <span className="text-tinta">
              {formatearNumero(documento.embedded_count)}
              <span className="text-tinta-tenue">/{formatearNumero(documento.chunks_count)}</span>
            </span>
          ) : (
            <span className="text-tinta">{formatearNumero(documento.chunks_count)}</span>
          )}
        </td>

        <td className="numerico px-3 py-3 text-right text-tinta-tenue">
          {documento.pages === null ? '—' : formatearNumero(documento.pages)}
        </td>

        <td className="numerico px-3 py-3 text-right text-tinta-tenue">
          {formatearTamano(documento.size_bytes)}
        </td>

        <td className="px-3 py-3 whitespace-nowrap text-tinta-tenue">
          {formatearFechaCorta(documento.created_at)}
        </td>

        <td className="px-5 py-3">
          <div className="flex justify-end gap-1">
            <Boton
              variante="fantasma"
              tamano="iconoSm"
              onClick={() => onVerDetalle(documento)}
              aria-label={`Ver qué aprendió de ${nombre}`}
              title="Ver qué aprendió"
            >
              <Eye />
            </Boton>
            <Boton
              variante="fantasma"
              tamano="iconoSm"
              disabled={borrando}
              onClick={() => onBorrar(documento)}
              aria-label={`Borrar ${nombre}`}
              title="Borrar y olvidar"
              className="hover:bg-rojo-suave hover:text-rojo"
            >
              <Trash2 />
            </Boton>
          </div>
        </td>
      </tr>

      {enCurso ? (
        <tr>
          <td colSpan={7} className="px-5 pb-3.5 pt-0">
            <PasosIngesta documento={documento} />
          </td>
        </tr>
      ) : null}

      {documento.status === 'failed' && documento.error ? (
        <tr>
          <td colSpan={7} className="px-5 pb-3.5 pt-0">
            <p className="rounded-consola border border-rojo/30 bg-rojo-suave px-4 py-2.5 text-[0.8125rem] text-tinta">
              {documento.error}
            </p>
          </td>
        </tr>
      ) : null}

      {documento.status === 'superseded' ? (
        <tr>
          <td colSpan={7} className="px-5 pb-3.5 pt-0">
            <p className="text-[0.8125rem] text-tinta-tenue">
              Una versión más reciente ocupó su lugar. Ya no es recuperable por el agente.
            </p>
          </td>
        </tr>
      ) : null}
    </tbody>
  )
}

function CargandoTabla() {
  return (
    <div className="space-y-3 px-5 py-5">
      {[0, 1, 2].map((fila) => (
        <div key={fila} className="flex items-center gap-4">
          <Esqueleto className="h-9 flex-1" />
          <Esqueleto className="h-6 w-40" />
          <Esqueleto className="h-6 w-16" />
        </div>
      ))}
      <p className="sr-only">Cargando documentos…</p>
    </div>
  )
}

function SinDocumentos() {
  return (
    <div className="px-6 py-14 text-center">
      <div className="mx-auto flex size-11 items-center justify-center rounded-full bg-superficie-tenue">
        <Inbox className="size-5 text-tinta-tenue" aria-hidden />
      </div>
      <p className="mt-3 text-sm font-medium text-tinta">La base de conocimiento está vacía</p>
      <p className="mx-auto mt-1 max-w-md text-[0.8125rem] leading-relaxed text-tinta-tenue">
        El agente todavía no conoce ningún protocolo. Sube el primer documento y verás cómo lo lee,
        lo trocea y lo aprende, paso a paso y en directo.
      </p>
    </div>
  )
}
