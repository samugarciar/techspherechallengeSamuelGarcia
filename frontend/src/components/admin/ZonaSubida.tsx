import { CheckCircle2, UploadCloud, X } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'

import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import type { SubidaEnCurso } from '@/hooks/useDocumentos'
import { ACCEPT_INPUT, archivosDeDrop, triarArchivos, type ArchivoRechazado } from '@/lib/archivos'
import { formatearTamano, pluralizar } from '@/lib/formato'
import { cn } from '@/lib/utils'

/**
 * Zona de subida: arrastrar y soltar, o botón.
 *
 * El contador de `dragenter`/`dragleave` no es paranoia: esos eventos se disparan
 * también al pasar por encima de los hijos del recuadro, así que con un booleano
 * el borde parpadea mientras se arrastra por dentro. Contando entradas y salidas
 * el resaltado se mantiene estable.
 *
 * Los archivos inválidos no cancelan el lote: de cuatro documentos, los tres
 * buenos suben y el cuarto explica por qué no.
 */
export function ZonaSubida({
  onSubir,
  subidas,
  onDescartarSubida,
  deshabilitado,
  motivoDeshabilitado,
}: {
  onSubir: (archivos: File[]) => void
  subidas: SubidaEnCurso[]
  onDescartarSubida: (clave: string) => void
  deshabilitado?: boolean
  motivoDeshabilitado?: string
}) {
  const [arrastrando, setArrastrando] = useState(false)
  const [rechazados, setRechazados] = useState<ArchivoRechazado[]>([])
  const profundidad = useRef(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const procesar = useCallback(
    (archivos: File[]) => {
      if (archivos.length === 0) return
      const { aceptados, rechazados: fuera } = triarArchivos(archivos)
      setRechazados(fuera)
      if (aceptados.length > 0) onSubir(aceptados)
    },
    [onSubir],
  )

  const alSoltar = useCallback(
    (evento: React.DragEvent<HTMLDivElement>) => {
      evento.preventDefault()
      profundidad.current = 0
      setArrastrando(false)
      if (deshabilitado) return
      procesar(archivosDeDrop(evento.dataTransfer))
    },
    [deshabilitado, procesar],
  )

  return (
    <div className="space-y-3">
      <div
        onDragEnter={(evento) => {
          evento.preventDefault()
          profundidad.current += 1
          if (!deshabilitado) setArrastrando(true)
        }}
        onDragOver={(evento) => evento.preventDefault()}
        onDragLeave={(evento) => {
          evento.preventDefault()
          profundidad.current -= 1
          if (profundidad.current <= 0) {
            profundidad.current = 0
            setArrastrando(false)
          }
        }}
        onDrop={alSoltar}
        className={cn(
          'rounded-consola border-2 border-dashed px-6 py-8 text-center transition-colors',
          arrastrando
            ? 'border-primario bg-primario-suave'
            : 'border-borde-fuerte bg-superficie-tenue/45 hover:border-primario/45',
          deshabilitado && 'pointer-events-none opacity-55',
        )}
      >
        <UploadCloud
          className={cn('mx-auto size-7', arrastrando ? 'text-primario' : 'text-tinta-tenue')}
          aria-hidden
        />
        <p className="mt-2.5 text-sm font-medium text-tinta">
          {arrastrando ? 'Suelta aquí los documentos' : 'Arrastra los documentos hasta aquí'}
        </p>
        <p className="mt-1 text-[0.8125rem] text-tinta-tenue">
          PDF, DOCX, Markdown o texto · hasta 25 MB por archivo · varios a la vez
        </p>

        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPT_INPUT}
          className="sr-only"
          onChange={(evento) => {
            procesar(Array.from(evento.target.files ?? []))
            // Sin esto, volver a elegir el MISMO archivo no dispara `change`.
            evento.target.value = ''
          }}
        />
        <Boton
          type="button"
          variante="contorno"
          tamano="sm"
          className="mt-4"
          onClick={() => inputRef.current?.click()}
        >
          Seleccionar archivos
        </Boton>

        {deshabilitado && motivoDeshabilitado ? (
          <p className="mt-3 text-[0.8125rem] text-ambar">{motivoDeshabilitado}</p>
        ) : null}
      </div>

      {rechazados.length > 0 ? (
        <Aviso
          tono="atencion"
          titulo={`${pluralizar(rechazados.length, 'archivo')} sin subir`}
          acciones={
            <Boton
              variante="fantasma"
              tamano="iconoSm"
              onClick={() => setRechazados([])}
              aria-label="Descartar avisos"
            >
              <X />
            </Boton>
          }
        >
          <ul className="space-y-1">
            {rechazados.map(({ archivo, motivo }) => (
              <li key={`${archivo.name}-${archivo.size}`}>
                <span className="font-medium text-tinta">{archivo.name}</span> — {motivo}
              </li>
            ))}
          </ul>
        </Aviso>
      ) : null}

      {subidas.length > 0 ? (
        <ul className="space-y-2">
          {subidas.map((subida) => (
            <li
              key={subida.clave}
              className="entrando rounded-consola border border-borde bg-superficie px-4 py-3"
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-tinta">{subida.nombre}</p>
                  <p className="text-xs text-tinta-tenue">
                    {formatearTamano(subida.tamano)}
                    {subida.estado === 'subiendo' ? ' · enviando al servidor…' : null}
                    {subida.estado === 'aceptado' ? ' · recibido, empieza la ingesta' : null}
                  </p>
                </div>
                {subida.estado === 'aceptado' ? (
                  <CheckCircle2 className="size-4 shrink-0 text-verde" aria-hidden />
                ) : null}
                {subida.estado === 'error' ? (
                  <Boton
                    variante="fantasma"
                    tamano="iconoSm"
                    onClick={() => onDescartarSubida(subida.clave)}
                    aria-label={`Descartar el error de ${subida.nombre}`}
                  >
                    <X />
                  </Boton>
                ) : null}
              </div>

              {subida.estado === 'error' ? (
                <p className="mt-2 text-[0.8125rem] text-rojo">{subida.error}</p>
              ) : (
                <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-superficie-tenue">
                  <div
                    className={cn(
                      'h-full rounded-full transition-[width] duration-200 ease-out',
                      subida.estado === 'aceptado' ? 'bg-verde' : 'bg-primario',
                    )}
                    style={{ width: `${Math.round(subida.fraccion * 100)}%` }}
                  />
                </div>
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
