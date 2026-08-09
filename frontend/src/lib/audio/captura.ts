/**
 * Captura del micrófono y conversión a PCM int16 LE mono a 16 kHz.
 *
 * Adaptado de `scripts/spikes/cliente_voz/index.html`. Las decisiones vienen de
 * allí y se mantienen:
 *
 *  · PCM crudo por AudioWorklet en vez de `MediaRecorder`. MediaRecorder entrega
 *    WebM/Opus, que obliga a decodificar en el servidor y añade el retardo de
 *    contenedor del códec. El worklet entrega Float32 sin contenedor y pasarlo a
 *    int16 aquí cuesta microsegundos.
 *  · `echoCancellation` es imprescindible, no un adorno: sin él la voz del
 *    agente sale por el altavoz, vuelve por el micrófono, el VAD del servidor la
 *    toma por voz del paciente y el agente se interrumpe a sí mismo en bucle.
 *
 * Lo único que se añade sobre el spike es agrupar en bloques de 20 ms antes de
 * enviar. El worklet dispara cada 128 muestras —unas 375 veces por segundo— y
 * mandar un WebSocket de 84 bytes a ese ritmo es puro peaje. El VAD del servidor
 * acumula en su propio buffer, así que el tamaño del bloque le da igual.
 */

const SR_OBJETIVO = 16_000
const MS_POR_BLOQUE = 20
const MUESTRAS_POR_BLOQUE = (SR_OBJETIVO * MS_POR_BLOQUE) / 1000

const CODIGO_WORKLET = `
class Captura extends AudioWorkletProcessor {
  process(entradas) {
    const canal = entradas[0] && entradas[0][0]
    if (canal) this.port.postMessage(new Float32Array(canal))
    return true
  }
}
registerProcessor('captura-postop', Captura)
`

export interface OpcionesCaptura {
  /** Bloques de PCM int16 LE mono a 16 kHz, listos para el WebSocket. */
  onPcm: (pcm: Int16Array) => void
  /** Pico de la señal, 0–1. Para el vúmetro del micrófono. */
  onNivel?: (nivel: number) => void
}

export class CapturaMicrofono {
  private flujo: MediaStream | null = null
  private ctx: AudioContext | null = null
  private nodo: AudioWorkletNode | null = null
  private origen: MediaStreamAudioSourceNode | null = null
  private acumulador: number[] = []
  private _sampleRate = 0

  get sampleRateReal(): number {
    return this._sampleRate
  }

  get activa(): boolean {
    return this.nodo !== null
  }

  async arrancar(opciones: OpcionesCaptura): Promise<void> {
    if (this.nodo) return

    this.flujo = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })

    // Chrome concede los 16 kHz pedidos; Safari suele imponer 48 kHz. Si no los
    // da, se remuestrea por diezmado más abajo.
    let ctx: AudioContext
    try {
      ctx = new AudioContext({ sampleRate: SR_OBJETIVO })
    } catch {
      ctx = new AudioContext()
    }
    this.ctx = ctx
    this._sampleRate = ctx.sampleRate

    const url = URL.createObjectURL(new Blob([CODIGO_WORKLET], { type: 'application/javascript' }))
    try {
      await ctx.audioWorklet.addModule(url)
    } finally {
      URL.revokeObjectURL(url)
    }

    const factor = Math.max(1, Math.round(ctx.sampleRate / SR_OBJETIVO))
    const nodo = new AudioWorkletNode(ctx, 'captura-postop')

    nodo.port.onmessage = (evento: MessageEvent<Float32Array>) => {
      const entrada = evento.data
      let pico = 0

      for (let i = 0; i + factor <= entrada.length; i += factor) {
        // Promediar la ventana en vez de quedarse con una muestra suelta: es un
        // filtro paso-bajo pobre pero real, y evita que el aliasing del diezmado
        // le llegue al STT como siseo. Con factor 1 (el caso normal) no hace nada.
        let suma = 0
        for (let j = 0; j < factor; j += 1) suma += entrada[i + j] ?? 0
        const valor = Math.max(-1, Math.min(1, suma / factor))
        if (Math.abs(valor) > pico) pico = Math.abs(valor)
        this.acumulador.push(valor)
      }

      opciones.onNivel?.(pico)

      while (this.acumulador.length >= MUESTRAS_POR_BLOQUE) {
        const bloque = this.acumulador.splice(0, MUESTRAS_POR_BLOQUE)
        const pcm = new Int16Array(MUESTRAS_POR_BLOQUE)
        for (let i = 0; i < MUESTRAS_POR_BLOQUE; i += 1) pcm[i] = (bloque[i] ?? 0) * 32767
        opciones.onPcm(pcm)
      }
    }

    this.origen = ctx.createMediaStreamSource(this.flujo)
    this.origen.connect(nodo)
    this.nodo = nodo
  }

  parar(): void {
    this.nodo?.port.close()
    this.nodo?.disconnect()
    this.origen?.disconnect()
    for (const pista of this.flujo?.getTracks() ?? []) pista.stop()
    void this.ctx?.close().catch(() => undefined)
    this.nodo = null
    this.origen = null
    this.flujo = null
    this.ctx = null
    this.acumulador = []
  }
}

/**
 * Traducción de los fallos de `getUserMedia` a algo accionable.
 *
 * «NotAllowedError» a secas en pantalla, en mitad de una demo, no le dice a
 * nadie que tiene que ir al candado de la barra de direcciones.
 */
export function mensajeDeErrorDeMicrofono(causa: unknown): string {
  const nombre = causa instanceof DOMException ? causa.name : ''
  switch (nombre) {
    case 'NotAllowedError':
    case 'SecurityError':
      return (
        'El navegador ha denegado el acceso al micrófono. Ábrelo desde el candado de la barra ' +
        'de direcciones y vuelve a permitirlo. Ojo: fuera de localhost hace falta HTTPS.'
      )
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No se ha encontrado ningún micrófono conectado.'
    case 'NotReadableError':
      return 'El micrófono está ocupado por otra aplicación y el navegador no puede usarlo.'
    default:
      return 'No se pudo abrir el micrófono en este navegador.'
  }
}
