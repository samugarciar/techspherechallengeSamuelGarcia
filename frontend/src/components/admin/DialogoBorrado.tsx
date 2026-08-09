import { Loader2, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import {
  CabeceraDialogo,
  CerrarDialogo,
  ContenidoDialogo,
  Dialogo,
  PieDialogo,
} from '@/components/ui/dialogo'
import { formatearTamano, pluralizar } from '@/lib/formato'
import type { Documento } from '@/types/api'

/**
 * Confirmación de borrado.
 *
 * El diálogo dice el nombre del documento, no «¿estás seguro?». Un genérico se
 * acepta por reflejo; leer el nombre obliga a comprobar que es ese y no el de al
 * lado. Y explica la consecuencia en los términos del problema —el agente deja de
 * poder citarlo— en vez de en términos de base de datos.
 */
export function DialogoBorrado({
  documento,
  abierto,
  borrando,
  onCambiarApertura,
  onConfirmar,
}: {
  documento: Documento | null
  abierto: boolean
  borrando: boolean
  onCambiarApertura: (abierto: boolean) => void
  onConfirmar: (documento: Documento) => Promise<{ ok: boolean; mensaje?: string }>
}) {
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (abierto) setError(null)
  }, [abierto, documento?.id])

  if (!documento) return null

  const nombre = documento.title || documento.filename
  const trozos = documento.chunks_count

  return (
    <Dialogo open={abierto} onOpenChange={(valor) => !borrando && onCambiarApertura(valor)}>
      <ContenidoDialogo className="w-[min(32rem,calc(100vw-2rem))]">
        <CabeceraDialogo
          titulo="Borrar de la base de conocimiento"
          descripcion="El agente dejará de conocer este documento de forma inmediata: sus fragmentos y sus vectores se eliminan en la misma transacción, no en una limpieza posterior."
        />

        <div className="space-y-4 px-5 py-4">
          <div className="rounded-consola border border-borde bg-superficie-tenue px-4 py-3">
            <p className="text-sm font-semibold leading-snug text-tinta">{nombre}</p>
            <p className="mt-0.5 truncate font-mono text-xs text-tinta-tenue">
              {documento.filename}
            </p>
            <p className="mt-2 text-[0.8125rem] text-tinta-tenue">
              {trozos > 0
                ? `Se eliminarán ${pluralizar(trozos, 'fragmento')} con sus embeddings.`
                : 'No llegó a generar fragmentos: no hay nada que el agente sepa de él.'}{' '}
              {formatearTamano(documento.size_bytes)}
            </p>
          </div>

          <p className="text-[0.8125rem] leading-relaxed text-tinta-tenue">
            La operación no se puede deshacer. Para volver a tenerlo disponible habría que subir el
            archivo otra vez y esperar a que termine la ingesta.
          </p>

          {error ? <Aviso tono="error">{error}</Aviso> : null}
        </div>

        <PieDialogo>
          <CerrarDialogo asChild>
            <Boton variante="contorno" disabled={borrando}>
              Cancelar
            </Boton>
          </CerrarDialogo>
          <Boton
            variante="destructivo"
            disabled={borrando}
            onClick={async () => {
              setError(null)
              const resultado = await onConfirmar(documento)
              if (resultado.ok) onCambiarApertura(false)
              else setError(resultado.mensaje ?? 'No se pudo borrar el documento.')
            }}
          >
            {borrando ? <Loader2 className="animate-spin" /> : <Trash2 />}
            {borrando ? 'Borrando…' : 'Borrar y olvidar'}
          </Boton>
        </PieDialogo>
      </ContenidoDialogo>
    </Dialogo>
  )
}
