import { Clock, Hand, MicOff, PhoneOff, ShieldCheck, Unplug, UserCheck } from 'lucide-react'

import { MODO_SIMULADO } from '@/api'
import { interrumpirAgenteSimulado, simularCaidaDeVoz } from '@/api/llamadas/mock'
import { IndicadorConexion } from '@/components/admin/IndicadorConexion'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import { CabeceraTarjeta, Tarjeta } from '@/components/ui/superficie'
import { diasDesde, formatearDuracion } from '@/lib/duracion'
import { formatearMs, pluralizar } from '@/lib/formato'
import { AvisoBanderaRoja } from '@/routes/call/AvisoBanderaRoja'
import { IndicadorFase } from '@/routes/call/IndicadorFase'
import { PanelCitas } from '@/routes/call/PanelCitas'
import { PanelLatencias } from '@/routes/call/PanelLatencias'
import { Transcripcion } from '@/routes/call/Transcripcion'
import type { LlamadaVoz } from '@/routes/call/useLlamadaVoz'

/**
 * La pantalla mientras se habla.
 *
 * Jerarquía deliberada, de arriba abajo: primero lo que hay que ver desde lejos
 * (bandera roja, fase), después lo que hay que leer (transcripción), y a un lado
 * lo que demuestra que por debajo hay ingeniería (latencias, citas, cola de
 * audio). Si el jurado sólo mira la mitad superior, ya ha entendido el producto.
 */
export function ConsolaLlamada({ llamada }: { llamada: LlamadaVoz }) {
  const { conversacion, conexion, errorConexion, errorMicrofono, medidores, microfono } = llamada
  const { bandera } = conversacion

  return (
    <div className="space-y-4">
      <Cabecera llamada={llamada} />

      {errorConexion ? (
        <Aviso
          tono="error"
          titulo="Se ha cortado la conexión de voz"
          acciones={
            <Boton variante="contorno" tamano="sm" onClick={llamada.reconectar}>
              Reconectar
            </Boton>
          }
        >
          {errorConexion} La transcripción de lo dicho hasta ahora se conserva. Al reconectar, el
          agente retoma desde donde el servidor tenga guardada la llamada.
        </Aviso>
      ) : null}

      {microfono === 'error' && errorMicrofono ? (
        <Aviso
          tono="atencion"
          titulo="El agente no puede oírte"
          acciones={
            <Boton variante="contorno" tamano="sm" onClick={llamada.abrirMicrofono}>
              Reintentar
            </Boton>
          }
        >
          {errorMicrofono}
        </Aviso>
      ) : null}

      {bandera ? <AvisoBanderaRoja bandera={bandera} /> : null}

      <IndicadorFase
        fase={conversacion.fase}
        escalada={bandera !== null}
        nivelEntrada={medidores.nivelEntrada}
        nivelSalida={medidores.nivelSalida}
      />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
        <Tarjeta className="self-start">
          <CabeceraTarjeta
            titulo="Transcripción en vivo"
            descripcion="Las frases en gris son parciales del reconocedor: todavía pueden cambiar."
            acciones={<IndicadorConexion estado={conexion} intento={0} />}
          />
          <Transcripcion
            intervenciones={conversacion.intervenciones}
            banderaDesde={bandera?.instante ?? null}
          />
        </Tarjeta>

        <div className="space-y-4">
          <Tarjeta>
            <CabeceraTarjeta
              titulo="Latencia por etapa"
              descripcion="Dónde se va el tiempo entre que el paciente calla y el agente suena."
            />
            <PanelLatencias metricas={conversacion.metricas} />
          </Tarjeta>

          <Tarjeta>
            <CabeceraTarjeta
              titulo={`Evidencia citada (${conversacion.citas.length})`}
              descripcion="Cada afirmación clínica, con su origen."
            />
            <PanelCitas citas={conversacion.citas} fase={conversacion.fase} />
          </Tarjeta>

          <EstadoDelCanal llamada={llamada} />
        </div>
      </div>
    </div>
  )
}

function Cabecera({ llamada }: { llamada: LlamadaVoz }) {
  const { paciente, medidores, microfono } = llamada

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-3 rounded-2xl border border-borde bg-superficie p-4 shadow-sm">
      <div className="flex items-center gap-3.5 min-w-0">
        <div className="flex size-11 shrink-0 items-center justify-center rounded-xl border border-primario/30 bg-primario-suave text-primario">
          <UserCheck className="size-5" />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-lg font-bold tracking-tight text-tinta">
              {paciente?.nombre ?? 'Llamada en curso'}
            </h1>
            {paciente?.documento_cc ? (
              <span className="inline-flex items-center gap-1 rounded-md border border-borde bg-superficie-tenue px-2 py-0.5 font-mono text-[0.6875rem] font-medium text-tinta-tenue">
                <ShieldCheck className="size-3 text-verde" />
                CC: {paciente.documento_cc}
              </span>
            ) : null}
          </div>
          <p className="mt-0.5 truncate text-[0.8125rem] text-tinta-tenue">
            {paciente?.cirugia?.nombre ?? 'Sin cirugía registrada'}
            {paciente?.cirugia ? ` · ${diasDesde(paciente.cirugia.dias_desde)}` : ''}
          </p>
        </div>
      </div>

      {/* Temporizador y Controles de Llamada */}
      <div className="flex items-center gap-3.5 ml-auto">
        {/* Indicador de tiempo en vivo */}
        <div className="flex items-center gap-2.5 rounded-xl border border-borde bg-superficie-tenue/80 px-3.5 py-1.5 shadow-2xs">
          <span className="relative flex size-2.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-rojo opacity-75" />
            <span className="relative inline-flex size-2.5 rounded-full bg-rojo" />
          </span>
          <div className="flex items-center gap-1.5">
            <Clock className="size-3.5 text-tinta-tenue" aria-hidden />
            <span className="numerico text-base font-bold tabular-nums tracking-wide text-tinta">
              {formatearDuracion(medidores.segundos)}
            </span>
          </div>
        </div>

        {microfono === 'inactivo' && !MODO_SIMULADO ? (
          <span className="hidden items-center gap-1.5 rounded-full border border-borde bg-superficie-tenue px-2.5 py-1 text-xs text-tinta-tenue sm:inline-flex">
            <MicOff className="size-3.5" aria-hidden />
            Micrófono cerrado
          </span>
        ) : null}

        {/* Botón de Colgar Llamada */}
        <Boton variante="destructivo" tamano="md" onClick={llamada.colgar} className="gap-2 font-semibold shadow-xs">
          <PhoneOff className="size-4" />
          Colgar llamada
        </Boton>
      </div>
    </div>
  )
}

/**
 * Estado del canal de audio.
 *
 * La cifra que importa es «audio en cola»: es la que explica por qué el barge-in
 * necesita vaciar el buffer del cliente. Cuando el agente habla, ahí hay varios
 * segundos de voz ya sintetizada esperando turno; si al interrumpir sólo se
 * callara el servidor, el altavoz seguiría soltando todo eso.
 */
function EstadoDelCanal({ llamada }: { llamada: LlamadaVoz }) {
  const { conversacion, medidores } = llamada

  return (
    <Tarjeta>
      <CabeceraTarjeta
        titulo="Canal de audio"
        descripcion="Lo que el navegador tiene entre manos ahora mismo."
      />
      <dl className="grid grid-cols-2 gap-x-5 gap-y-3 px-5 py-4 text-[0.8125rem]">
        <div>
          <dt className="text-xs uppercase tracking-wide text-tinta-tenue">Audio en cola</dt>
          <dd className="numerico mt-0.5 font-medium text-tinta">
            {formatearMs(medidores.msEnCola)}
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-tinta-tenue">Interrupciones</dt>
          <dd className="numerico mt-0.5 font-medium text-tinta">
            {conversacion.interrupciones === 0
              ? 'ninguna'
              : pluralizar(conversacion.interrupciones, 'vez', 'veces')}
          </dd>
        </div>
        {conversacion.msDescartados > 0 ? (
          <div className="col-span-2">
            <dt className="text-xs uppercase tracking-wide text-tinta-tenue">
              Voz descartada al interrumpir
            </dt>
            <dd className="numerico mt-0.5 font-medium text-tinta">
              {formatearMs(conversacion.msDescartados)}
            </dd>
          </div>
        ) : null}
      </dl>

      {MODO_SIMULADO ? (
        <div className="space-y-2.5 border-t border-borde px-5 py-3">
          <p className="text-xs leading-relaxed text-tinta-tenue">
            <span className="font-medium text-ambar">Llamada simulada.</span> El guion viene de{' '}
            <code className="font-mono">src/api/llamadas/mock</code>, no hay micrófono abierto y el
            audio son muestras a cero. La cola y el vaciado sí son reales.
          </p>
          <div className="flex flex-wrap gap-2">
          <Boton
            variante="fantasma"
            tamano="sm"
            onClick={interrumpirAgenteSimulado}
            title="En el simulador no hay micrófono: este botón hace el papel de la voz del paciente"
          >
            <Hand />
            Interrumpir al agente
          </Boton>
          <Boton
            variante="fantasma"
            tamano="sm"
            onClick={simularCaidaDeVoz}
            title="Corta el WebSocket simulado para ver el aviso de reconexión"
          >
            <Unplug />
            Simular caída
          </Boton>
          </div>
        </div>
      ) : null}
    </Tarjeta>
  )
}
