# Verificación — desconfiar de «los tests pasan»

Revisión posterior a la integración (`6dc87d1`, `af23e56`) y a la limpieza
(`b837f1e`), hecha con el encargo explícito de **no fiarse de la batería verde**:
un agente estuvo borrando y otro reescribiendo cuarenta comentarios, y ninguna de
las dos cosas la ve un test.

**Método.** Todo lo que aparece aquí está *ejecutado*, no leído. Los dos guiones
de demo contra la API real, el arnés de medición contra el pipeline real, dos
llamadas de voz simultáneas con Whisper y Postgres de verdad, y la batería
completa. Cuando algo se afirma sin haberlo podido ejecutar, se dice.

**Veredicto corto.** La garantía central del enunciado **está intacta**. La
costura de la llamada completa **aguanta**. No se rompió nada al borrar. Lo que sí
apareció es un grupo de afirmaciones falsas —una de ellas *introducida* por la
pasada de limpieza— alrededor de una sola causa: **el toggle de voz local/premium
no está enchufado a ninguna síntesis**. Y un hermano del hueco conocido de
`duracion_s`, este sí visible en pantalla para un médico.

| | |
|---|---|
| Base de datos | `postop_t3` (nunca `postop` ni `postop_wt`) |
| Puertos | 8050 (API), 8051 (arnés con LLM falso) |
| Modo de voz | `local` en todo momento. **Cero caracteres de ElevenLabs gastados** |
| Batería | 421 + 1 `xfail` antes · **423 + 1 `xfail`** después (2 tests nuevos) |
| `ruff check` | limpio · `npx tsc -b` y `npm run build` verdes |

---

## 1. La salida real de los dos guiones

### 1.1 `demo_aprender_olvidar.py` — la garantía del enunciado

```
cd backend && DATABASE_URL=postgresql://postop:postop@localhost:5433/postop_t3 \
  uv run uvicorn app.main:app --port 8050
cd backend && DATABASE_URL=…postop_t3 uv run python -m app.workers.ingest_worker
cd backend && uv run python ../scripts/demo_aprender_olvidar.py --api http://localhost:8050/api
```

```
1. SUBIR
    protocolo_apendicectomia.pdf  ->  uploaded  (112,108 bytes)

2. APRENDER
      0.0s  uploaded   fragmentos 0/0
      0.5s  parsing    fragmentos 0/0
      1.5s  embedding  fragmentos 0/8
      3.1s  ready      fragmentos 8/8

  3. PREGUNTAR — con el documento presente
    evidencia: True   fragmentos: 3
    latencia:  1637 ms  (embedding 499, retrieval 18, rerank 1116)
    [0.763] protocolo_apendicectomia.pdf › Protocolo de alta — Apendicectomía laparoscópica › Cuidado de la herida › p. 1
            La apendicectomía laparoscópica deja tres incisiones pequeñas: una en el ombligo y dos e…
    [0.271] protocolo_apendicectomia.pdf › Protocolo de alta — Apendicectomía laparoscópica › Dieta › p. 2
            Empiece con líquidos claros y comidas ligeras en pequeñas cantidades y aumente según tol…

4. OLVIDAR
    olvidado=True  fragmentos eliminados=8

  5. LA MISMA PREGUNTA — ya borrado
    evidencia: False   fragmentos: 0
    latencia:  43 ms  (embedding 37, retrieval 6, rerank 0)
    (ninguno — el agente responderá que no tiene esa información)

  ✓ GARANTÍA CUMPLIDA: lo aprendió y lo olvidó.
```

Dos notas sobre los números, ninguna es un fallo:

- **`rerank 1116 ms`** en frío contra los **585 ms** que publican el README y
  `docs/CONTRATO_API.md`. En la misma sesión, ya caliente, la misma consulta dio
  787 ms, y durante la llamada de voz se vio un lote del cross-encoder de 9,4 s
  con la GPU compartida con Whisper y el TTS. Los 585 ms son un número honesto
  *aislado*; bajo contención el reranker es peor de lo publicado. Es materia de la
  decisión abierta del reranker, no una regresión.
- **`3,1 s` de la subida a `ready`** contra los «2,6 s» del README. Dentro del
  ruido de una sola ejecución.

### 1.2 `demo_llamada_completa.py` — la costura, con Gemini de verdad

**Una sola ejecución**, como se pidió. Servidor arrancado con
`VOZ=1 TTS_ENGINE_LOCAL=say`.

```
0. PREPARAR
    subiendo protocolo_apendicectomia.pdf
    subiendo protocolo_colecistectomia.pdf
    subiendo protocolo_herniorrafia.pdf
    3 protocolos aprendidos en 9.1s (21 fragmentos)

1. ABRIR LA LLAMADA
    Lucía Fernanda Ospina Marín — Herniorrafia inguinal derecha, día 2 de postoperatorio
    call_id 0158e6f1-2820-42dc-827a-6dd3b70b7d11

   SINTETIZANDO las frases del paciente con `say`…
    4 intervenciones, 19.4s de audio

2. CONECTAR   ws://localhost:8050/ws/voz?call_id=0158e6f1-2820-42dc-827a-6dd3b70b7d11

3. HABLAR     (esperando al saludo del agente)
        agente │ Buenos días. Le llamo del servicio de seguimiento postoperatorio del hospital. Soy un asistente automatizado, no una persona, y esta llamada es para ver cómo va su recuperación. Si en algún momento prefiere hablar con alguien del equipo, dígamelo y le paso el aviso. ¿Hablo con Lucía?

    → verificación de identidad contra los datos vivos
      paciente │ Sí, sí, soy yo. Nací el 12 de abril de 1978.
        agente │ Gracias. Para confirmar su identidad, ¿podría decirme su fecha de nacimiento completa?

    → consulta clínica: debe citar el protocolo
      paciente │ Pues mire, la herida la tengo un poco roja y quería saber si ya me puedo duchar.
        agente │ Entiendo. Antes de seguir, necesito que me confirme su fecha de nacimiento. Es por seguridad, para asegurarme de que hablo con la persona correcta.

    → BANDERA ROJA: el agente tiene que cortar el guion
      paciente │ Ah, y otra cosa, me acabo de tomar la temperatura y tengo 39,5.

      ⚑ BANDERA ROJA │ fiebre de 39.5 grados (urgente)

         cita │ protocolo_herniorrafia.pdf › Protocolo de alta — Herniorrafia inguinal con malla › Signos de alarma · p. 1
         cita │ protocolo_herniorrafia.pdf › Protocolo de alta — Herniorrafia inguinal con malla › Medicación · p. 1
         cita │ protocolo_apendicectomia.pdf › Protocolo de alta — Apendicectomía laparoscópica › Signos de alarma · p. 2
         cita │ protocolo_herniorrafia.pdf › Protocolo de alta — Herniorrafia inguinal con malla › Dieta · p. 2
        agente │ Ha dicho que tiene treinta y nueve y medio de fiebre. Eso es un signo de alarma. Debe acudir a urgencias si presenta fiebre superior a treinta y ocho y medio grados. Su equipo médico va a contactarla.

    → confirmación: solo aquí puede colgar (decisión 2)
      paciente │ Sí, lo he entendido. Voy ahora mismo para urgencias.
        agente │ De acuerdo. Su equipo médico ha recibido el aviso y se pondrán en contacto con usted. Por favor, acuda a urgencias. ¿Lo ha entendido?

4. LEER EL HISTORIAL
     1. agente   Buenos días. Le llamo del servicio de seguimiento postoperatorio del hospi
     2. paciente Sí, sí, soy yo. Nací el 12 de abril de 1978.  [stt 1649ms]
     3. agente   Gracias. Para confirmar su identidad, ¿podría decirme su fecha de nacimien  [llm 1637ms, tts 624ms, total 4558ms]
     4. paciente Pues mire, la herida la tengo un poco roja y quería saber si ya me puedo d  [stt 592ms]
     5. agente   Entiendo. Antes de seguir, necesito que me confirme su fecha de nacimiento  [llm 735ms, tts 597ms, total 2569ms]
     6. paciente Ah, y otra cosa, me acabo de tomar la temperatura y tengo 39,5.  [stt 452ms]
     7. agente   Ha dicho que tiene treinta y nueve y medio de fiebre. Eso es un signo de a  [llm 26999ms, tts 615ms, total 28710ms]
              ↳ protocolo_herniorrafia.pdf › Protocolo de alta — Herniorrafia inguinal con malla › Signos de alarma · p. 1
              ↳ protocolo_herniorrafia.pdf › Protocolo de alta — Herniorrafia inguinal con malla › Medicación · p. 1
              ↳ protocolo_apendicectomia.pdf › Protocolo de alta — Apendicectomía laparoscópica › Signos de alarma · p. 2
              ↳ protocolo_herniorrafia.pdf › Protocolo de alta — Herniorrafia inguinal con malla › Dieta · p. 2
     8. paciente Sí, lo he entendido. Voy ahora mismo para urgencias.  [stt 476ms]
     9. agente   De acuerdo. Su equipo médico ha recibido el aviso y se pondrán en contacto  [llm 1611ms, tts 624ms, total 3354ms]

5. VEREDICTO
    ✓ llegan los siete mensajes del contrato
        vistos 7/7
    ✓ el detector de banderas rojas disparó
        fiebre de 39.5 grados
    ✓ la llamada termina con fin/escalada
        motivo=escalada
    ✓ la llamada se publica escalada y completada
        estado=completada escalada=True urgencia=urgente
    ✓ la conversación quedó escrita en call_turns
        9 turnos: 5 del agente, 4 del paciente
    ✓ los turnos llevan sus citas y sus latencias
        1 turnos con cita, 8 con latencias

    latencia hasta el primer audio: 2569–28710 ms en 4 turnos

  ✓ LA COSTURA AGUANTA: voz, agente clínico e historial son la misma llamada.
```

**Cuántas llamadas a Gemini gastó: entre 7 y 10, probablemente 9.** No se puede
dar el número exacto, y eso es en sí un hallazgo pequeño (§7): **nada registra
cuántas peticiones al LLM consume una llamada** — ni un log, ni una columna, ni
`traces`. La horquilla sale de la evidencia que sí queda en Postgres: cuatro
turnos del agente (≥1 petición cada uno) y, en el turno de la fiebre, tres
herramientas ejecutadas —`buscar_protocolo` (4 citas), `escalar_a_equipo_clinico`
(`escalation_reason` escrito) y `registrar_respuesta` (`survey =
{"fiebre": "39.5 grados"}`)— que caben en 2 a 4 rondas, con el techo en
`MAX_RONDAS = 4`.

Tres observaciones de esta ejecución, ninguna es un fallo del código:

- **La fuga de la fecha de nacimiento que documenta `af23e56` NO se repitió.** El
  agente pidió la fecha en vez de leerla, las dos veces. Que no ocurra en una
  ejecución no dice que esté arreglado —sigue siendo una instrucción del prompt y
  no una herramienta— pero conviene que conste que no es determinista.
- **El agente se atascó en la verificación de identidad** dos turnos seguidos y
  nunca llegó a contestar la pregunta de la ducha. La demo pasa porque la bandera
  roja la dispara el detector determinista, no el modelo.
- **Un turno de 28,7 s.** De los 27 s de `llm`, 9,4 s medidos fueron un solo lote
  del reranker (registrado en el log del servidor). Es el mismo aviso que ya deja
  `af23e56`: el presupuesto del README mide un turno *sin* tool calling.

---

## 2. Qué se rompió

**Nada de lo que la batería cubría, y nada de lo que borró la limpieza.** Los dos
fallos reales encontrados son de otro tipo: afirmaciones falsas (§4) y un hueco de
contrato visible en pantalla (§6.2). Lo arreglado se limita a documentación y
comentarios; no se ha cambiado ni una línea de comportamiento.

Sí hay un fallo **de la batería**, reproducible y ajeno a los dos agentes:

### 2.1 La batería no se puede pasar con el worker de ingesta en marcha

Con el worker corriendo contra la misma base —que es exactamente lo que deja
montado el guion de la demo de §1.1— la batería falla:

```
FAILED tests/test_robustez_concurrencia.py::test_un_worker_muerto_a_media_faena_no_deja_el_documento_colgado
1 failed, 422 passed, 1 xfailed
```

Con el worker parado, el mismo fichero pasa entero (`6 passed`). La causa: ese
test hace `DELETE FROM jobs`, encola el suyo y lanza **su propio** worker para
matarlo a mitad; un worker externo se lleva el trabajo antes y el test se queda
sin nadie a quien matar. Es una suposición legítima del test (que es el único
dueño de la cola) que nadie había escrito, y el orden del README —arrancar y luego
`uv run pytest`— invita a caer en ella. **No lo he arreglado**: hacerlo pide que
los trabajos lleven marca de dueño, y eso es cambiar la cola. Propuesta en §7.

---

## 3. Lo que la limpieza borró: ¿hacía falta?

Comprobado uno a uno, y buscando además las tres cosas que un `grep` de nombres no
ve: acceso dinámico, referencias desde ficheros que no son código, y uso desde el
navegador.

| Borrado | Veredicto | Cómo se comprobó, más allá del grep de nombres |
|---|---|---|
| `frontend/README.md` de la plantilla de Vite | **seguro** | Nada del build lo lee; `npm run build` verde después |
| `frontend/public/favicon.svg` e `icons.svg` | **seguro** | `index.html` embebe su favicon en un `data:` URI (leído entero). Búsqueda de `favicon`, `icons.svg` y de los seis `id` de símbolo (`#bluesky-icon`, `#discord-icon`, `#github-icon`, `#documentation-icon`, `#social-icon`, `#x-icon`) sobre **todos** los ficheros del frontend, no solo `.ts/.tsx`: cero apariciones. `public/` queda vacío, que Vite tolera |
| `TTSEngine.stream_por_frases()` | **seguro** | Cero referencias; y cero `getattr`/`importlib`/tablas de despacho en `backend/app/**` que pudieran llamarlo por nombre (los cinco `getattr` del backend son sobre objetos de SDK ajenos: `resultado.document`, `parte.function_call`, `archivo.size`…). Los tres sitios que emiten por frases lo hacen a mano, como decía |
| `esRecuperable(error)` y `esFaltaDeToken(error)` | **seguro** | Cero apariciones en todo `frontend/`, incluidos `index.html`, `vite.config.ts` y los JSON de configuración |
| `esRecuperable(estado)` de `types/estados.ts` | **seguro** | ídem. El único acceso por cadena de ese módulo es `ESTADOS[estado]`, que indexa un `Record` de estados de documento, no funciones |
| `AreaTexto` | **seguro** | Cero `<textarea>` y cero `AreaTexto` en el árbol. Un componente de React solo se puede usar por nombre en JSX; no hay `React.lazy` ni `import()` dinámico en `src/` |
| Parámetro `worker` de `_procesar()` | **seguro** | Las dos únicas llamadas están actualizadas, y el worker **corrió de verdad** durante las dos demos: 8 trabajos, 4 documentos a `ready` (`job 2 · listo · 8 fragmentos · 5877 ms`, etc.) |

**Ningún borrado fue inseguro.** El punto más flojo del método del agente
anterior —el frontend, sin cobertura de tests— resultó estar limpio: las cuatro
piezas borradas no las usaba nadie, ni estáticamente ni por nombre.

---

## 4. Lo que la limpieza reescribió y sigue siendo falso

### 4.1 El hallazgo principal: el toggle de voz no está enchufado a nada

`docs/LIMPIEZA.md` §2.6 corrigió tres promesas del README que no se cumplían
(`traces` sin escrituras, el toggle sin botón, el panel de consumo inexistente) y
al hacerlo **afirmó una cuarta cosa que es falsa**:

> «`tts_usage` se llena en cada síntesis y `voice_mode.resumen_consumo()` lo
> agrega, pero el panel está pendiente.»

Y lo repitió en el docstring de `resumen_consumo()`, que antes no lo decía:

> «Se mantiene escrita porque `tts_usage` **ya se está llenando en cada
> síntesis**…»

**Medido: `SELECT count(*) FROM tts_usage` = 0** después de una llamada completa
con cinco síntesis reales (la de §1.2). No es un descuido de esa frase: es el
síntoma de una causa mayor que nadie había mirado.

Quien escribe `tts_usage`, elige motor según el modo activo y degrada a local si
premium falla es `voice_mode.VoiceRouter`. **Nadie construye `VoiceRouter`**
(búsqueda en todo el repo: solo su definición y cinco menciones en comentarios y
README). Los dos caminos que sintetizan de verdad hacen, sin preguntar:

```python
# app/voice/pipeline_ws.py:349 y :920 — y app/voice/servicios_pipecat.py:136
motor_tts = motor_tts or crear_motor(get_settings().tts_engine_local)
```

`modo_actual()` solo lo consultan `VoiceRouter.sintetizar()` (muerto) y el `GET`
del endpoint. Consecuencias, todas comprobadas:

| Lo que se afirmaba | Lo que ocurre |
|---|---|
| «se conmuta en caliente, **incluso a mitad de una llamada**: la frase siguiente ya sale con la otra voz» (README, `.env.example`, `config.py`, `ajustes.py`) | `PUT /api/settings/voice-mode` escribe la fila y cambia lo que devuelve el `GET`. **La voz de la llamada no cambia**: siempre suena `TTS_ENGINE_LOCAL` |
| «Si el motor premium falla, `VoiceRouter` cae a local» (README, cabecera de `tts.py`) | No ocurre: el camino premium no es alcanzable, así que tampoco su degradación |
| «Cada síntesis se anota en `tts_usage`» (README, `voice_mode.py`, `servicios_pipecat.py`) | Cero filas tras una llamada de cinco síntesis |
| «el modo premium es para las pruebas finales y la demo» | Hoy **no hay forma de que suene** sin editar `TTS_ENGINE_LOCAL` y reiniciar |

Detalle incómodo: la cabecera de `tts.py` **empeoró** con la limpieza. Antes decía
«si la red del venue falla durante la demo, se vuelve a Kokoro **con una variable
de entorno**» —que es exactamente lo que hay que hacer hoy, o sea *verdad*— y se
reescribió a «`VoiceRouter` degrada a local él solo, sin que nadie toque nada»,
que es falso. Es el modo de fallo que el propio encargo anticipaba: un comentario
reescrito puede introducir una afirmación nueva y falsa igual que la que corregía.

**Arreglado** (solo texto, cero cambios de comportamiento): `README.md` —§Dos
modos de voz, con una tabla de qué está construido y qué no, y la fila de TTS de
la tabla de stack—, `.env.example`, `app/voice/tts.py` (cabecera y docstring de
`crear_motor`), `app/voice/voice_mode.py` (`VoiceRouter` y `resumen_consumo`),
`app/core/config.py`, `app/api/ajustes.py` y `app/voice/servicios_pipecat.py`.
`docs/LIMPIEZA.md` **no se ha reescrito**: es un informe fechado, así que lleva una
anotación al margen, como manda la bitácora.

### 4.2 Los números reescritos: todos correctos, y dos reproducidos

Contrastados los de `docs/LIMPIEZA.md` §2.2 contra el README, `docs/VOZ_COMPARATIVA.md`
y `docs/INFORME_NOCHE.md`: **cuadran todos**. 585 ms de reranker, 196-303 ms de
Kokoro, 354 ms de ElevenLabs, 462/956 ms de TTFT de Gemini, 91 ms y 22,5 s de
Groq, 1.596/1.975 ms y −379 ms de Pipecat, PyMuPDF 25/25 contra Docling 3/25.

Dos se volvieron a **medir**, no solo a cotejar:

```
[barge_in] B   corte_servidor_ms 96.6   silencio_audible_ms 96.7   detectados 2 de 2
[barge_in] A   corte_servidor_ms 86.8   silencio_audible_ms 87.4   detectados 2 de 2
```

El comentario reescrito de `pipeline_ws.py` («que "el barge-in tarda 96 ms" sea
una medición») **se reproduce exactamente**: 96,6 ms para la Opción B y 86,8 ms
para Pipecat, contra los 96 y 84 publicados.

```
[turno] B  primer_audio_ms 1977.6      [turno] A  primer_audio_ms 1664.4
```

También reproduce el número que decide (1.975 contra 1.596 publicados; aquí n=1
frente a la mediana de 3-5 del informe).

Aparente contradicción que **no lo es**: el README da 481 ms de STT en la tabla de
stack y 391 ms en el presupuesto de latencia. Son dos medidas distintas —el spike
aislado de la Fase 0 y el arnés— y `docs/VOZ_COMPARATIVA.md` §113 las publica
juntas. Nada que corregir.

### 4.3 Los comandos documentados: ejecutados

- **El fallo que la limpieza dice haber encontrado es real.** Lanzado como decía
  el README antes:
  ```
  $ uv run python scripts/demo_aprender_olvidar.py
  ModuleNotFoundError: No module named 'httpx'
  ```
  Y la forma corregida arranca y llega hasta su propio diagnóstico. ✓
- **Pero el diagnóstico que imprime señalaba a una base que no existe.** Ese
  mensaje —lo primero que lee quien acaba de clonar el repo y no tiene el backend
  levantado— decía `DATABASE_URL=…/postop_wt`, que es una base de trabajo de otro
  worktree. El README siembra `postop`. **Arreglado** a `postop`. (En
  `demo_llamada_completa.py` hay tres menciones equivalentes a `postop_t2`; ahí
  **no** las toco, porque mandar a alguien a lanzar una prueba de voz contra
  `postop` sería peor. Queda anotado.)
- `scripts/spikes/cliente_voz/README.md`: sus tres afirmaciones corregidas son
  correctas hoy —el router está en `main.py`, `main.py` existe, y `kokoro>=0.9`
  está en `backend/pyproject.toml`— y `VOZ=1` es efectivamente imprescindible
  (comprobado: `/ws/voz` responde `listo` con la variable y no existe sin ella).
  **Kokoro arranca de verdad**: `crear_motor("kokoro")` sintetiza en 335-342 ms en
  caliente para una frase de 53 caracteres.
- El texto de la 404 reescrito («Documentos, Llamar e Historial, y todas están en
  la barra de arriba») es cierto: `Disposicion.tsx` declara esas tres secciones y
  `App.tsx` las cuatro rutas.

### 4.4 Un resto de andamiaje que la limpieza no vio

`docs/LIMPIEZA.md` §2.8 dice haber quitado los comentarios que justificaban una
decisión con «ese fichero es de otro agente». Queda uno, y en el sitio más
visible: el `reason=` del `xfail`, que es lo que imprime `pytest -rx`.

```
XFAIL … - Escalas incompatibles: … fuera del alcance de esta revisión
          (app/rag/rerank.py es de otro agente). …
```

**No lo he tocado**: el encargo prohíbe expresamente tocar el `xfail`. Es de una
línea y le corresponde a Samuel.

---

## 5. La integración, con desconfianza

### 5.1 Dos llamadas simultáneas — **correcto**, probado con dos WebSockets reales

Las pruebas que ya existían montan `SesionVoz` a mano; lo que faltaba era el cable.
Se levantó un segundo servidor (puerto 8051) idéntico al de producción salvo por un
LLM de eco —que devuelve **el historial de usuario completo** del agente, para que
un cruce de estado salga impreso— y se abrieron **dos WebSockets a la vez**, con
Whisper, `say` y Postgres reales, hablando en paralelo 37 s:

```
A = Lucía Fernanda Ospina Marín  call_id e2ef04a5-…
B = María Elena Restrepo Gómez   call_id 2be58b02-…

--- llamada A — 5 turnos
     2. paciente Hola, soy Anna, me operaron de la vesícula el martes pasado.
     3. agente   He anotado. Hola, soy Anna, me operaron de la vesícula el martes pasado.
     4. paciente La herida la tengo bien, no me duele nada.
     5. agente   He anotado. Hola, soy Anna, me operaron de la vesícula el martes pasado. La herida la tengo bien…
    audio recibido: 1,194,524 bytes

--- llamada B — 5 turnos
     2. paciente Buenas, aquí Bruno, tengo la herida enrojecida desde ayer.
     3. agente   He anotado. Buenas, aquí Bruno, tengo la herida enrojecida desde ayer.
     4. paciente Y también me duele bastante al moverse.
     5. agente   He anotado. Buenas, aquí Bruno, tengo la herida enrojecida desde ayer. Y también me duele bastan…
    audio recibido: 1,171,712 bytes

  ✓ dos llamadas simultáneas no comparten historial, fase ni transcripción
```

Ni una palabra de una llamada aparece en la otra, en ninguna de las dos
direcciones. De paso queda comprobado lo que **sí** se comparte entre conexiones y
nadie había ejercitado en concurrencia: la instancia única de `WhisperSTT` (que no
lleva cerrojo) y el motor de TTS. Cuatro transcripciones solapadas, ninguna
corrupta.

**El doble clic en «Llamar»** —dos WebSockets al mismo `call_id`— también se probó
contra el servidor real:

```
ws1: listo
ws2 (doble clic): ['listo', 'fin']          ← motivo: cortada
ws1 después del doble clic -> eventos: ['estado','agente_habla',×5,'fin_audio']  bytes de audio: 693010
```

La segunda conexión se rechaza y **la primera sigue viva y hablando**, que es el
orden correcto.

### 5.2 La frontera entre capas — se sostiene, pero la afirmación escrita no era exacta

El `grep` que pide el encargo devuelve tráfico en **las dos** direcciones:

```
backend/app/voice/pipeline_ws.py:47        from app.agent.llm_client import LLMClient, Mensaje, RespuestaLLM
backend/app/voice/pipeline_pipecat.py:61   from app.agent.llm_client import LLMClient
backend/app/voice/servicios_pipecat.py:50  from app.agent.llm_client import LLMClient, Mensaje
backend/app/agent/agente.py:267            from app.voice.tts import dividir_en_frases   (dentro de stream())
```

O sea que «`app/voice/**` no importa `app/agent/**` ni al revés», tal como está
escrito en `app/main.py` y en el mensaje del commit de integración, **es literalmente
falso**. Lo que es cierto —y es lo que carga el peso— es más fino: `app/voice/**`
importa la **interfaz** (`llm_client`, un módulo que no toca ni la base ni ningún
SDK) y **nunca** el agente clínico (`app/agent/agente`); y el import de `agente.py`
hacia `app/voice/tts` está dentro de la función a propósito, para no arrastrar
numpy a un despliegue de solo texto.

La consecuencia que preocupaba **no se ha producido**: el arnés monta el mismo
bucle con `ClienteLLMFalso` y reproduce los números publicados (§4.2). El
comentario de `app/main.py` se ha reescrito para decir la frontera con precisión,
porque quien lo compruebe con `grep` va a ver lo mismo que yo.

### 5.3 Si el LLM falla a mitad de llamada — **correcto**, y no estaba probado por voz

`test_agente_llamadas.py` cubre los tres modos de fallo del LLM (excepción, rondas
agotadas, respuesta vacía) **por el camino de texto**, `POST /calls/{id}/mensaje`.
Por voz el recorrido es otro —`SesionDeVoz.stream` envuelve `AgenteLlamada.stream`,
y en medio están el troceado por frases, el anuncio de citas y banderas, la
síntesis y la cola de persistencia— y no había ni una prueba. Búsqueda de
`FRASE_SEGURIDAD` en `backend/tests/`: cero apariciones.

Añadidos dos tests en `tests/test_integracion_voz_llamada.py`:

- `test_si_el_llm_revienta_por_voz_el_paciente_oye_la_frase_de_seguridad`
- `test_una_respuesta_vacia_por_voz_tampoco_deja_al_paciente_esperando`

**Los dos pasan a la primera.** El comportamiento es el correcto: sale la frase de
seguridad, **suena** (bytes de audio > 0), el turno se emite con sus métricas y sin
marcar error, la llamada queda `escalated` con urgencia `prioritaria` en Postgres, y
la frase queda en la transcripción que leerá el equipo clínico.

Dos huecos que quedan al descubierto y **no he tocado** porque cambian
comportamiento (§7):

- **La llamada no termina.** `_rendirse()` no pone `terminar`, así que con el LLM
  caído el agente repite la frase de seguridad y vuelve a escalar en cada turno,
  indefinidamente, hasta que cuelgue el paciente.
- **Doble fallo = silencio.** `_rendirse()` llama a `herramientas.escalar()`, que
  escribe en Postgres **sin protección**. Si el LLM falla y la base también, la
  excepción sube hasta `SesionVoz._responder`, que la registra y calla: el paciente
  no oye nada. Es el único camino encontrado en el que el sistema se queda mudo.

---

## 6. La consola y la pantalla de llamada contra el backend real

Comparado **campo a campo** lo que el frontend lee contra el JSON que devuelve el
backend de verdad, endpoint por endpoint, con el servidor de §1 en marcha.

### 6.1 Lo que cuadra

| Endpoint | Veredicto |
|---|---|
| `GET /api/health` | ✓ `ok`, `db`, `version`, `modelos_listos` |
| `GET /api/documents` | ✓ los 14 campos de `Documento` más `total` |
| `GET /api/documents/{id}` | ✓ los 14 + `chunks_preview`. **Una desviación**: los trozos traen `contenido`, no `content`; `normalizarTrozo` acepta los dos y por eso no se ve |
| `GET /api/documents/stream` (SSE) | ✓ Token por *query* (`EventSource` no admite cabeceras) y `exigir_admin_por_query` lo valida igual. Probado: con cabecera da 401, con `?token=` emite `documento` con los seis campos que espera `EventoDocumento` |
| `POST /api/rag/query` | ✓ `fragmentos[{documento_id,filename,heading,page,contenido,score,cita}]`, `hay_evidencia`, `ms{embedding,retrieval,rerank,total}` — exactamente lo que pinta `CajaRag` |
| `GET /api/patients` | ✓ los ocho campos de `Paciente`, `cirugia` anidada incluida |
| `GET /api/calls` | ✓ los nueve de `ResumenLlamada`, `duracion_s` y `turnos` (número) incluidos |
| `POST /api/calls` | ✓ `call_id` y `ws` |
| `WS /ws/voz` | ✓ los doce tipos de `MensajeVoz` se emiten con los campos que el tipo declara. Y la URL exacta que construye el frontend —`/ws/voz?call_id=…&token=…`— **se acepta**: probada contra el servidor real, el `token` de más no provoca un 422 |
| `GET/PUT /api/settings/voice-mode` | ✓ la forma. El efecto, no: ver §4.1 |

Dos observaciones que no son fallos:

- `urgencia_escalada` lo devuelven los dos endpoints de llamadas y el frontend no
  lo lee en ninguno. Campo de más, inofensivo.
- `/ws/voz` **no tiene autenticación**. El frontend le manda el token de admin en
  la query y FastAPI lo ignora por no estar declarado. Cualquiera que alcance el
  puerto puede abrir una sesión de voz suelta (sin `call_id` no persiste nada, y
  con un `call_id` desconocido se cierra). Es una decisión, no una rotura, pero
  conviene que sea consciente.

### 6.2 El hermano de `duracion_s`: el turno «Sistema» que nadie escribe

Buscando hermanos del hueco conocido apareció uno **peor**, porque este sí se lee
en pantalla. `PaginaDetalleLlamada.tsx`, en la tarjeta roja de toda llamada
escalada, le dice al médico:

> «El turno marcado como **«Sistema»** en la transcripción es el momento exacto en
> que saltó el detector. A partir de ahí el agente abandonó el cuestionario.»

Y `TurnosLlamada.tsx` tiene el estilo listo para pintarlo (icono de sirena, borde
rojo). Pero **el backend no escribe nunca un turno con `role='system'`**: las tres
llamadas a `guardar_turno` usan `"agent"` y `"patient"`, y la cuarta vía —
`SesionDeVoz.turno_terminado`— también. El schema lo permite
(`CHECK (role IN ('agent','patient','system'))`), el contrato no lo promete, y la
llamada real de §1.2 lo confirma: **9 turnos, 5 agente y 4 paciente, cero
sistema**, en una llamada escalada.

El mismo error tiene una segunda cara en `useLlamadaVoz.ts:176`, donde un
comentario afirma:

> «…y el historial **sí guarda ese turno de sistema**, así que sin esto la llamada
> en vivo y la llamada revisada después contarían historias distintas.»

Es exactamente al revés: la nota de bandera roja se inserta en la transcripción
**en vivo**, no se guarda, y por eso la llamada en vivo y la revisada **sí**
cuentan historias distintas — que es el problema que ese código creía estar
evitando.

Es anterior a los dos agentes de esta noche (viene de `8b0196d`). **No lo he
arreglado**: las dos salidas cambian comportamiento. Propuesta en §7.

Resumen del contrato de llamadas:

| Campo que el frontend lee | En `GET /api/calls` | En `GET /api/calls/{id}` |
|---|---|---|
| `duracion_s` | ✓ | **falta** — conocido; el detalle pinta «—» (`formatearDuracion(null)`, sin excepción) |
| `turno.quien === 'sistema'` | — | **nunca llega** — nuevo, §6.2 |
| el resto de `camposComunes` | ✓ | ✓ |

---

## 7. Qué necesita decidir Samuel

1. **El toggle de voz (§4.1).** Enchufar `VoiceRouter` en los dos sitios que
   sintetizan es una línea en cada uno, pero a partir de ahí una llamada puede
   sonar en ElevenLabs y gastar del free tier. Las opciones son enchufarlo,
   dejarlo documentado como está ahora, o retirar el endpoint. Lo que no puede
   quedarse es la versión anterior de la documentación.
2. **El turno «Sistema» (§6.2).** O el backend lo escribe cuando dispara
   `redflags.detectar` —`guardar_turno(call_id, "system", …)` en
   `responder_paciente`, y el frontend ya sabe pintarlo— o se quita de la pantalla
   la frase que lo promete. La primera es mejor demo: hace visible que la alarma la
   decide una regla determinista y no el modelo.
3. **`duracion_s` en el detalle (§6.1).** Añadir el `EXTRACT(EPOCH …)` que ya tiene
   el endpoint de listado. Tres líneas.
4. **Qué hace la llamada si el LLM se cae (§5.3).** Hoy no termina nunca. ¿Colgar
   tras N rendiciones seguidas? ¿Envolver `escalar()` para que un doble fallo no
   deje al paciente en silencio?
5. **La batería con el worker en marcha (§2.1).** Marcar el test como serial,
   darle un dueño al trabajo, o documentar en el README que hay que parar el worker
   antes de `uv run pytest`.
6. **Nadie cuenta las peticiones al LLM (§1.2).** Un contador por llamada —o la
   tabla `traces`, que está creada y vacía— convertiría «gastó unas nueve» en un
   dato. Con tool calling, una llamada puede gastar cuatro veces lo que parece.
7. **El `reason=` del `xfail` (§4.4)** sigue diciendo «es de otro agente». Una
   línea, y solo la puede tocar quien decida sobre el reranker.
8. Lo de siempre, ya abierto: **el reranker** (y bajo contención cuesta bastante
   más que los 585 ms publicados, §1.1) y **la verificación de identidad como
   herramienta** en vez de como instrucción del prompt.

---

## 8. Lo que sigue sin poder comprobarse sin un humano

- **El micrófono.** Sigue siendo el único paso pendiente del proyecto. Todo lo de
  aquí inyecta audio sintetizado con `say`: nadie ha hablado nunca al sistema. En
  concreto no se puede comprobar sin una persona: la cancelación de eco (que el
  agente no se interrumpa a sí mismo por el altavoz), si el VAD corta a alguien que
  hace una pausa de pensar, y si la voz de Kokoro se entiende por teléfono.
- **Las tres pantallas en un navegador.** Verificado que la capa de red cuadra
  campo a campo (§6), lo que no sustituye a abrirlas: nada de aquí dice si el panel
  de latencias es legible, si la tarjeta roja se ve desde el fondo de la sala, ni si
  el `AudioWorklet` de `captura.ts` entrega 16 kHz en el Safari de Samuel — y el
  propio `_vigilar_ritmo()` existe porque ese fallo no rompe nada, solo hace que
  «el agente no te entienda».
- **Si el agente vuelve a leer en voz alta la fecha de nacimiento.** No pasó en
  esta ejecución (§1.2) y pasó en las dos de `af23e56`. Con un modelo de por medio,
  saber cuál es la frecuencia real pide muchas ejecuciones y cuota.
- **La calidad clínica de las respuestas.** Aquí se comprueba el recorrido, nunca
  si lo que dice el agente es buen consejo médico. Eso es la Fase 6 y el guion de
  `eval/guion_llamada.md`, que sigue pendiente de la revisión de Samuel.

---

## 9. Comprobaciones finales

```
cd backend && uv run ruff check .                    → All checks passed!
cd backend && DATABASE_URL=…postop_t3 uv run pytest -q → 423 passed, 1 xfailed
cd frontend && npx tsc -b && npm run build           → verde, ✓ built in 204ms
```

421 + 1 antes, **423 + 1 después**: los dos tests añadidos en §5.3. Ninguno
borrado, ninguno desactivado, ninguno relajado.

El `xfail(strict=True)` de `test_hay_evidencia_sigue_significando_algo_sin_reranker`
**sigue exactamente donde estaba y falla por el mismo motivo**. Comprobado con
`--runxfail` para ver la causa real y no solo el marcador:

```
assert rerank.hay_evidencia(await rerank.reordenar("¿cuándo me ducho?", [mejor], 4))
E   AssertionError: assert False
E    +  where False = hay_evidencia([Fragmento(…, score=0.03278688524590164)])
```

0,0328 contra el umbral de 0,35: la incompatibilidad de escalas del fallo 1.12,
intacta. Ni el marcador, ni `strict`, ni el cuerpo del test, ni `rerank.py`, ni
`RERANK_ENABLED` se han tocado.
