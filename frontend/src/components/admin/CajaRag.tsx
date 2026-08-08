import { Loader2, RefreshCw, Search } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'

import { api } from '@/api'
import { mensajeDeError } from '@/api/errores'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import { Campo } from '@/components/ui/campo'
import { CabeceraTarjeta, Tarjeta } from '@/components/ui/superficie'
import { formatearMs, formatearScore } from '@/lib/formato'
import type { RespuestaRag } from '@/types/api'

/**
 * Consulta al RAG desde la propia consola.
 *
 * No es una funcionalidad aparte: es la VERIFICACIÓN del panel de documentos. Sin
 * ella, «el agente lo aprendió» y «el agente lo olvidó» son afirmaciones sobre una
 * tabla; con ella se comprueban haciendo la misma pregunta antes y después de
 * borrar, sin micrófono y sin levantar el pipeline de voz.
 *
 * Es autónoma a propósito: se quita borrando su única línea en `AdminPage`.
 */

const SUGERENCIAS = [
  '¿Cuándo puedo ducharme?',
  '¿Cuánta fiebre es motivo de urgencias?',
  '¿Cada cuánto tomo el paracetamol?',
  '¿Cuándo es la revisión?',
]

export function CajaRag() {
  const [consulta, setConsulta] = useState('')
  const [topK, setTopK] = useState(4)
  const [respuesta, setRespuesta] = useState<RespuestaRag | null>(null)
  const [ultimaConsulta, setUltimaConsulta] = useState('')
  const [cargando, setCargando] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const entradaRef = useRef<HTMLInputElement>(null)

  const preguntar = useCallback(
    async (texto: string, k: number) => {
      const limpia = texto.trim()
      if (!limpia || cargando) return
      setCargando(true)
      setError(null)
      try {
        const datos = await api.consultarRag(limpia, k)
        setRespuesta(datos)
        setUltimaConsulta(limpia)
      } catch (causa) {
        setError(mensajeDeError(causa))
        setRespuesta(null)
      } finally {
        setCargando(false)
      }
    },
    [cargando],
  )

  return (
    <Tarjeta>
      <CabeceraTarjeta
        titulo="Comprobar lo que sabe el agente"
        descripcion="Haz la misma pregunta antes y después de borrar un documento: es la forma de ver el aprender y el olvidar sin levantar la llamada."
      />

      <div className="space-y-4 px-5 py-4">
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(evento) => {
            evento.preventDefault()
            void preguntar(consulta, topK)
          }}
        >
          <div className="relative min-w-[16rem] flex-1">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-tinta-tenue"
              aria-hidden
            />
            <Campo
              ref={entradaRef}
              value={consulta}
              onChange={(evento) => setConsulta(evento.target.value)}
              placeholder="Escribe una pregunta de paciente…"
              aria-label="Consulta al RAG"
              className="pl-9"
            />
          </div>

          <label className="flex items-center gap-2 text-xs text-tinta-tenue">
            Fragmentos
            <select
              value={topK}
              onChange={(evento) => setTopK(Number(evento.target.value))}
              className="h-9 rounded-consola border border-borde bg-superficie px-2 text-sm text-tinta"
              aria-label="Número de fragmentos a recuperar"
            >
              {[2, 4, 6, 8].map((valor) => (
                <option key={valor} value={valor}>
                  {valor}
                </option>
              ))}
            </select>
          </label>

          <Boton type="submit" disabled={cargando || consulta.trim() === ''}>
            {cargando ? <Loader2 className="animate-spin" /> : <Search />}
            Consultar
          </Boton>
        </form>

        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-tinta-tenue">Ejemplos:</span>
          {SUGERENCIAS.map((sugerencia) => (
            <button
              key={sugerencia}
              type="button"
              className="rounded-full border border-borde px-2.5 py-1 text-xs text-tinta-tenue transition-colors hover:border-primario/45 hover:text-tinta"
              onClick={() => {
                setConsulta(sugerencia)
                void preguntar(sugerencia, topK)
              }}
            >
              {sugerencia}
            </button>
          ))}
        </div>

        {error ? <Aviso tono="error">{error}</Aviso> : null}

        {respuesta ? (
          <Resultados
            respuesta={respuesta}
            consulta={ultimaConsulta}
            recargando={cargando}
            onRepetir={() => void preguntar(ultimaConsulta, topK)}
          />
        ) : null}
      </div>
    </Tarjeta>
  )
}

function Resultados({
  respuesta,
  consulta,
  recargando,
  onRepetir,
}: {
  respuesta: RespuestaRag
  consulta: string
  recargando: boolean
  onRepetir: () => void
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[0.8125rem] text-tinta-tenue">
          Respuesta a <span className="font-medium text-tinta">«{consulta}»</span>
        </p>
        <div className="flex items-center gap-2">
          <EtapasLatencia respuesta={respuesta} />
          <Boton variante="fantasma" tamano="sm" onClick={onRepetir} disabled={recargando}>
            {recargando ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            Repetir
          </Boton>
        </div>
      </div>

      {respuesta.hay_evidencia ? (
        <Aviso tono="exito" titulo="Hay evidencia en la base de conocimiento">
          El agente respondería citando estos fragmentos.
        </Aviso>
      ) : (
        <Aviso tono="atencion" titulo="Sin evidencia suficiente">
          El agente respondería que no tiene esa información, en vez de improvisar una respuesta.
        </Aviso>
      )}

      {respuesta.fragmentos.length === 0 ? (
        <p className="rounded-consola border border-dashed border-borde px-4 py-6 text-center text-[0.8125rem] text-tinta-tenue">
          No se recuperó ningún fragmento para esta consulta.
        </p>
      ) : (
        <ol className="space-y-2.5">
          {respuesta.fragmentos.map((fragmento, indice) => (
            <li
              key={`${fragmento.documento_id}-${indice}`}
              className="rounded-consola border border-borde bg-superficie-tenue/50 px-4 py-3"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <p className="min-w-0 flex-1 truncate text-[0.8125rem] font-medium text-tinta" title={fragmento.cita}>
                  {fragmento.cita}
                </p>
                <BarraScore score={fragmento.score} />
              </div>
              <p className="mt-1.5 text-[0.8125rem] leading-relaxed text-tinta-tenue">
                {fragmento.contenido}
              </p>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function EtapasLatencia({ respuesta }: { respuesta: RespuestaRag }) {
  const etapas: Array<[string, number | undefined]> = [
    ['embedding', respuesta.ms.embedding],
    ['retrieval', respuesta.ms.retrieval],
    ['rerank', respuesta.ms.rerank],
    ['total', respuesta.ms.total],
  ]
  const visibles = etapas.filter(([, valor]) => valor !== undefined)
  if (visibles.length === 0) return null

  return (
    <ul className="flex flex-wrap items-center gap-1.5">
      {visibles.map(([nombre, valor]) => (
        <li
          key={nombre}
          className="numerico rounded-full border border-borde px-2 py-0.5 text-[0.6875rem] text-tinta-tenue"
        >
          {nombre} <span className="font-medium text-tinta">{formatearMs(valor)}</span>
        </li>
      ))}
    </ul>
  )
}

function BarraScore({ score }: { score: number }) {
  const porcentaje = Math.max(0, Math.min(1, score)) * 100
  return (
    <span className="flex shrink-0 items-center gap-2" title={`Score de relevancia ${score}`}>
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-borde">
        <span
          className="block h-full rounded-full bg-primario"
          style={{ width: `${porcentaje}%` }}
        />
      </span>
      <span className="numerico text-xs text-tinta-tenue">{formatearScore(score)}</span>
    </span>
  )
}
