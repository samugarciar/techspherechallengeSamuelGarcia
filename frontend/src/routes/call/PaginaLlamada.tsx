import { Loader2, RefreshCw, ShieldCheck } from 'lucide-react'
import { useState } from 'react'

import { MODO_SIMULADO } from '@/api'
import {
  elegirGuionSimulado,
  elegirVelocidadSimulada,
  guionSimulado,
  VELOCIDADES,
  velocidadSimulada,
  type ModoGuion,
} from '@/api/llamadas/mock'
import { Aviso } from '@/components/ui/aviso'
import { Boton } from '@/components/ui/boton'
import { CabeceraTarjeta, Tarjeta } from '@/components/ui/superficie'
import { useToken } from '@/hooks/useToken'
import { ConsolaLlamada } from '@/routes/call/ConsolaLlamada'
import { ListaPacientes } from '@/routes/call/ListaPacientes'
import { ResumenFinal } from '@/routes/call/ResumenFinal'
import { useLlamadaVoz } from '@/routes/call/useLlamadaVoz'
import { usePacientes } from '@/routes/call/usePacientes'

import '@/routes/call/llamada.css'

/**
 * `/call` — la pantalla principal de la demo.
 *
 * Tres momentos, uno detrás de otro y nunca dos a la vez: se elige a quién
 * llamar, se llama, y se ve qué quedó registrado. Están en la misma ruta porque
 * son el mismo acto: si se elige a alguien y la URL cambia, quien esté
 * enseñándolo tiene que explicar la navegación en vez de la llamada.
 */
export function PaginaLlamada() {
  const token = useToken()
  // Igual que en la consola de documentos: sin token no se pide nada, para no
  // llenar el log del backend de 401 justo antes de la demo.
  const habilitado = MODO_SIMULADO || token !== ''

  const llamada = useLlamadaVoz()
  const pacientes = usePacientes(habilitado && llamada.situacion === 'elegir')

  if (llamada.situacion === 'en_curso') return <ConsolaLlamada llamada={llamada} />

  if (llamada.situacion === 'terminada') {
    return (
      <ResumenFinal
        paciente={llamada.paciente}
        llamada={llamada.llamada}
        conversacion={llamada.conversacion}
        segundos={llamada.medidores.segundos}
        onVolver={llamada.volverALista}
      />
    )
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-tinta">Llamada de seguimiento</h1>
        <p className="mt-1 text-[0.8125rem] text-tinta-tenue">
          Elige a quién llamar. El agente se presenta como sistema automatizado, verifica la
          identidad y hace el seguimiento que corresponde a su cirugía.
        </p>
      </div>

      {!habilitado ? (
        <Aviso tono="atencion" titulo="Falta el token de administrador">
          Las rutas de pacientes y llamadas piden la cabecera `X-Admin-Token`. Pégalo en «Configurar
          token», arriba a la derecha, y la lista se cargará sola.
        </Aviso>
      ) : null}

      {llamada.errorCreacion ? (
        <Aviso tono="error" titulo="No se pudo iniciar la llamada">
          {llamada.errorCreacion}
        </Aviso>
      ) : null}

      {pacientes.error ? (
        <Aviso
          tono="error"
          titulo="No se pudo cargar la lista de pacientes"
          acciones={
            <Boton variante="contorno" tamano="sm" onClick={() => void pacientes.cargar()}>
              <RefreshCw />
              Reintentar
            </Boton>
          }
        >
          {pacientes.error}
        </Aviso>
      ) : null}

      {MODO_SIMULADO ? <MandosDeEnsayo /> : null}

      <Tarjeta>
        <CabeceraTarjeta
          titulo="Seguimientos pendientes"
          descripcion="El hospital llama al paciente, no al revés: se elige de esta lista igual que lo haría una enfermera."
          acciones={
            <Boton
              variante="contorno"
              tamano="sm"
              disabled={!habilitado}
              onClick={() => void pacientes.cargar()}
            >
              <RefreshCw />
              Recargar
            </Boton>
          }
        />
        <ListaPacientes
          pacientes={pacientes.pacientes}
          cargando={pacientes.cargando && habilitado}
          creando={llamada.situacion === 'creando' ? (llamada.paciente?.id ?? null) : null}
          onLlamar={llamada.iniciar}
        />
      </Tarjeta>

      {llamada.situacion === 'creando' ? (
        <p className="flex items-center gap-2 text-[0.8125rem] text-tinta-tenue">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          Creando la llamada y abriendo el canal de voz…
        </p>
      ) : null}

      <NotaLegal />
    </div>
  )
}

/**
 * Mandos de ensayo. Sólo existen en modo simulado y sólo sirven para eso:
 * poder repasar la pantalla entera —incluida la bandera roja— sin backend y sin
 * tener que fingir fiebre delante del micrófono.
 */
function MandosDeEnsayo() {
  const [modo, setModo] = useState<ModoGuion>(guionSimulado())
  const [velocidad, setVelocidad] = useState<number>(velocidadSimulada())

  return (
    <Tarjeta className="border-ambar/35 bg-ambar-suave/40">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3 px-5 py-3.5">
        <div className="min-w-[16rem] flex-1">
          <p className="text-[0.8125rem] font-medium text-tinta">Ensayo con datos simulados</p>
          <p className="text-xs leading-snug text-tinta-tenue">
            No hay backend ni micrófono: la conversación viene de un guion con tiempos realistas.
            El audio que emite son muestras a cero, así que no se oye nada — lo que sí es real es
            que llena el buffer, para poder probar la interrupción.
          </p>
        </div>

        <Selector
          etiqueta="Guion"
          opciones={[
            { valor: 'con_bandera', etiqueta: 'Con bandera roja' },
            { valor: 'sin_bandera', etiqueta: 'Sin incidencias' },
          ]}
          activo={modo}
          onElegir={(valor) => {
            setModo(valor as ModoGuion)
            elegirGuionSimulado(valor as ModoGuion)
          }}
        />

        <Selector
          etiqueta="Velocidad"
          opciones={VELOCIDADES.map((v) => ({ valor: String(v), etiqueta: `${v}×` }))}
          activo={String(velocidad)}
          onElegir={(valor) => {
            setVelocidad(Number(valor))
            elegirVelocidadSimulada(Number(valor))
          }}
        />
      </div>
    </Tarjeta>
  )
}

function Selector({
  etiqueta,
  opciones,
  activo,
  onElegir,
}: {
  etiqueta: string
  opciones: Array<{ valor: string; etiqueta: string }>
  activo: string
  onElegir: (valor: string) => void
}) {
  return (
    <div>
      <p className="mb-1 text-[0.6875rem] uppercase tracking-wide text-tinta-tenue">{etiqueta}</p>
      <div className="flex gap-1 rounded-consola border border-borde bg-superficie p-1">
        {opciones.map((opcion) => (
          <button
            key={opcion.valor}
            type="button"
            onClick={() => onElegir(opcion.valor)}
            aria-pressed={activo === opcion.valor}
            className={
              activo === opcion.valor
                ? 'rounded-[0.375rem] bg-primario-suave px-3 py-1 text-xs font-medium text-primario'
                : 'rounded-[0.375rem] px-3 py-1 text-xs text-tinta-tenue hover:text-tinta'
            }
          >
            {opcion.etiqueta}
          </button>
        ))}
      </div>
    </div>
  )
}

function NotaLegal() {
  return (
    <p className="flex items-start gap-2 pb-4 text-xs leading-relaxed text-tinta-tenue">
      <ShieldCheck className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <span>
        En su primera intervención el agente declara que es un sistema automatizado, como exige el
        AI Act para las interacciones con IA, y verifica la identidad con nombre y número de
        cédula contra la ficha antes de hablar de nada clínico.
      </span>
    </p>
  )
}
