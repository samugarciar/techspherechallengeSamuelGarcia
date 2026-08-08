/**
 * Validación de archivos en cliente, ANTES de enviar.
 *
 * El backend valida igual —esta comprobación no es seguridad, es cortesía—, pero
 * subir 25 MB por la red para que te digan que el formato no vale es una espera
 * inútil y, en una demo, un silencio incómodo. El rechazo se explica en español y
 * dice qué se aceptaba, no solo que no.
 */

export const TAMANO_MAXIMO_BYTES = 25 * 1024 * 1024

/** Extensiones del contrato. El MIME no se usa como criterio principal: macOS
 *  manda `.md` como `application/octet-stream` según qué app lo haya tocado. */
export const EXTENSIONES_ACEPTADAS = ['.pdf', '.docx', '.md', '.txt'] as const

/** Para el atributo `accept` del input: extensiones + los MIME más habituales,
 *  que es lo que hace que el diálogo del sistema no muestre todo en gris. */
export const ACCEPT_INPUT = [
  ...EXTENSIONES_ACEPTADAS,
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'text/markdown',
  'text/plain',
].join(',')

export interface ArchivoRechazado {
  archivo: File
  motivo: string
}

export interface Triaje {
  aceptados: File[]
  rechazados: ArchivoRechazado[]
}

function extension(nombre: string): string {
  const punto = nombre.lastIndexOf('.')
  return punto === -1 ? '' : nombre.slice(punto).toLowerCase()
}

function esExtensionAceptada(nombre: string): boolean {
  return (EXTENSIONES_ACEPTADAS as readonly string[]).includes(extension(nombre))
}

const FORMATOS_LEGIBLES = 'PDF, DOCX, Markdown (.md) o texto (.txt)'

export function validarArchivo(archivo: File): string | null {
  if (!esExtensionAceptada(archivo.name)) {
    const ext = extension(archivo.name)
    return ext
      ? `No se admiten archivos ${ext.slice(1).toUpperCase()}. Acepta ${FORMATOS_LEGIBLES}.`
      : `El archivo no tiene extensión. Acepta ${FORMATOS_LEGIBLES}.`
  }
  if (archivo.size === 0) {
    return 'El archivo está vacío (0 bytes).'
  }
  if (archivo.size > TAMANO_MAXIMO_BYTES) {
    const mb = (archivo.size / (1024 * 1024)).toFixed(1).replace('.', ',')
    return `Pesa ${mb} MB y el máximo son 25 MB. Divídelo o comprime las imágenes.`
  }
  return null
}

/** Separa el lote en aceptados y rechazados sin descartar el lote entero: si de
 *  cuatro archivos uno falla, los otros tres se suben igual. */
export function triarArchivos(archivos: readonly File[]): Triaje {
  const aceptados: File[] = []
  const rechazados: ArchivoRechazado[] = []
  for (const archivo of archivos) {
    const motivo = validarArchivo(archivo)
    if (motivo) rechazados.push({ archivo, motivo })
    else aceptados.push(archivo)
  }
  return { aceptados, rechazados }
}

/** Los eventos de arrastre pueden traer carpetas; se ignoran en silencio salvo
 *  que sean lo único que se soltó, en cuyo caso quien llama avisa. */
export function archivosDeDrop(transferencia: DataTransfer): File[] {
  if (transferencia.items && transferencia.items.length > 0) {
    return Array.from(transferencia.items)
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter((archivo): archivo is File => archivo !== null)
  }
  return Array.from(transferencia.files)
}
