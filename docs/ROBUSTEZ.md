# Robustez: qué pasa cuando las cosas van mal

El camino feliz de aprender/olvidar funciona y está verificado
(`scripts/demo_aprender_olvidar.py`). Este documento es lo otro: los ataques
deliberados al sistema, porque lo que hunde una demo nunca es el camino feliz.

Cada afirmación de aquí tiene un test detrás en `backend/tests/test_robustez_*.py`.
Ejecutarlos: `cd backend && DATABASE_URL=…postop_t4 uv run pytest tests/test_robustez_*`.

---

## Los dos fallos reales que aparecieron

### 1. El olvido dejaba el archivo en disco

**Gravedad: alta.** Era el requisito central del enunciado incumplido a medias, y
de forma silenciosa.

`STORAGE_DIR=./storage/documents` es relativo y se resolvía contra el directorio
de trabajo del proceso. La API arranca desde `backend/` y los scripts desde la
raíz del repo, así que resolvían a carpetas distintas. `olvidar_documento()`
comprueba que el archivo esté por debajo de `storage_dir` antes de borrarlo —una
defensa correcta contra travesías de ruta— y esa comparación fallaba. El
`except OSError: pass` se tragaba el fallo.

Resultado: la fila desaparecía de la base de datos, el agente olvidaba de verdad,
y **el PDF con datos clínicos del paciente se quedaba en el disco del hospital
para siempre**. Nadie se enteraba.

Arreglado en tres sitios:
- `app/core/config.py` — `storage_dir` se ancla a la raíz del repo si viene relativa.
- `app/rag/ingest.py` — un fallo al borrar ya no se traga: registra
  `file_delete_failed` en `document_events` y avisa por el log. El comentario que
  decía que un huérfano en disco «es inocuo porque el agente ya no puede
  recuperarlo» era cierto para el retrieval y falso para un hospital.
- `.gitignore` — patrón `**/storage/documents/*`. Un PDF de paciente commiteado
  por accidente no se arregla con un revert.

Efecto colateral que confirma el diagnóstico: los tests que fallaban de forma
intermitente según el orden de ejecución dejaron de hacerlo. Se pisaban entre
ellos a través de esa carpeta mal resuelta.

### 2. El modo de voz por defecto no arrancaba

`kokoro` no estaba en `pyproject.toml`. La Fase 0 lo midió con
`uv run --with kokoro` y nunca llegó a ser dependencia del proyecto, así que
`crear_motor("kokoro")` —el modo `local`, que es el de por defecto— lanzaba
`ModuleNotFoundError`. Una línea en `pyproject.toml`.

---

## El PDF escaneado: el fallo silencioso que se evitó

Era el riesgo más probable de cara a mañana, porque los documentos clínicos
reales suelen venir escaneados. Un PDF sin capa de texto extrae cadena vacía, y
sin protección el documento habría llegado a `ready` con 0 fragmentos: la consola
diría «Listo — el agente ya lo sabe» habiendo aprendido nada, y el fallo se
descubriría en mitad de la demo, con el agente diciendo que no sabe nada de un
documento que se ve verde en pantalla.

La solución es mejor que rechazarlo: **PyMuPDF de primario y Docling con OCR de
respaldo**, que se dispara solo cuando la extracción directa no saca texto. Así
un escaneado se *rescata* en vez de fallar. Si ni el OCR saca nada, entonces sí
acaba en `failed`, con un mensaje que le dice al administrador qué hacer.

| Situación | Qué ocurre |
|---|---|
| PDF con capa de texto | PyMuPDF, rápido, sin tocar el OCR |
| PDF escaneado | Docling + OCR lo rescata |
| Escaneado con solo un sello | No cuela como legible: mismo umbral que el primario |
| Ni el OCR saca texto | `failed`: «pásale un OCR o súbelo en .docx, .md o .txt» |
| PDF protegido | `failed`, y sin gastar el OCR en balde |

---

## Lo que se atacó y aguantó

**Borrado a mitad de consulta.** Era el peor fallo posible: que el agente le cite
a un paciente un protocolo que el hospital acaba de retirar. No ocurre, ni por
llamada directa ni por HTTP, ni tampoco al reemplazar una versión a mitad de
consulta.

**La vista `retrievable_chunks` como último cinturón.** Un fragmento sin vector no
es recuperable; uno de un documento que no está `ready`, tampoco; y no se puede
crear un fragmento huérfano. Hay además un test que verifica por inspección del
código que **ningún camino de retrieval consulta la tabla `chunks` directamente** —
es una invariante del diseño, y sin ese test se erosiona en cuanto alguien
escriba una consulta nueva con prisa.

**Concurrencia.** Dos workers que promuevan el mismo contenido a la vez no se
pisan. Dos subidas simultáneas del mismo archivo no crean dos documentos. Dos
borrados del mismo id olvidan una sola vez y dejan un solo evento en la
auditoría, que es lo que de verdad ocurrió.

**Worker muerto a media faena.** No deja el documento colgado: otro lo recupera
por antigüedad de `locked_at`. Y un documento largo no pierde su job por tardar,
porque el worker late durante el embedding.

**Documentos venenosos.** Ninguno deja el documento colgado. Los caracteres de
control no impiden aprender. Un markdown de solo encabezados se rechaza porque no
deja nada que aprender.

**Límites de la API.** Ninguna ruta responde sin token, ni con uno equivocado,
incluido el flujo SSE. Un id que no es UUID devuelve un error con la forma del
contrato, y una ruta inexistente también. El límite de tamaño se aplica al byte.
Doce flujos SSE abiertos no dejan sin conexiones a la base, y uno cortado a lo
bruto devuelve la suya.

---

## Lo que sigue sin cubrir

- **Volumen.** Todo se ha probado con documentos de 2 a 6 páginas. Un protocolo de
  200 páginas son miles de fragmentos y no se ha ejercitado.
- **Corpus real.** Los tres protocolos son sintéticos. La forma de los documentos
  de verdad —tablas complejas, columnas, sellos, anexos— es la variable que más
  puede sorprender.
- **Presión de memoria.** Con 16 GB compartidos entre Whisper, bge-m3, el reranker
  y Docling con OCR, hubo contención real durante el desarrollo. No se ha medido
  qué pasa si coinciden una ingesta grande y una llamada de voz.
