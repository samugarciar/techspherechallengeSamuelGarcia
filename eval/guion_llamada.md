# Guion de la llamada de seguimiento

**Para revisar, Samuel.** Esto es todo lo que el agente le puede decir a un
paciente, escrito para leerse en voz alta. Es contenido clínico: lo que aquí esté
mal, estará mal en la llamada. Táchalo y reescríbelo encima.

El reflejo ejecutable de este documento es `backend/app/agent/guion.py`
(preguntas y claves) y `backend/app/agent/prompts.py` (frases fijas y reglas). Si
los dos divergen, **manda éste** y hay que corregir el código.

Lo que **no** está aquí, a propósito: las respuestas a las dudas del paciente. Esas
salen de los protocolos que subas a la consola, nunca de un texto escrito por mí.

---

## Cómo lee esto el agente

Tres niveles distintos, y conviene no confundirlos al revisar:

| Nivel | Qué es | ¿Se dice literal? |
|---|---|---|
| **Frases fijas** | Saludo y frase de seguridad | **Sí**, palabra por palabra |
| **Preguntas del guion** | Las de abajo, con su clave entre corchetes | Casi: el agente las adapta al hilo, pero no cambia *qué* pregunta |
| **Todo lo demás** | Encadenar, repreguntar, resumir, despedirse | No: lo redacta el modelo con las reglas de estilo |

---

## 1. Apertura — literal, siempre igual

> «Buenos días. Le llamo del servicio de seguimiento postoperatorio del hospital.
> Soy un asistente automatizado, no una persona, y esta llamada es para ver cómo
> va su recuperación. Si en algún momento prefiere hablar con alguien del equipo,
> dígamelo y le paso el aviso. ¿Hablo con **María**?»

Esta frase no la genera el modelo: es una constante. El AI Act (art. 50) obliga a
que la persona sepa que habla con una IA, y una frase generada saldría distinta
cada vez y no habría forma de demostrar que se dijo. De paso sale por el altavoz
sin esperar al LLM, así que la llamada arranca en el tiempo del TTS.

**A decidir:**
- ¿«Buenos días» siempre, o que dependa de la hora? Ahora es fijo. Si son las
  cinco de la tarde suena raro, y arreglarlo son tres líneas.
- ¿Debe decir el nombre del hospital? Ahora dice «del hospital», genérico.

## 2. Verificación de identidad

Antes de nada clínico. El agente consulta la ficha del paciente y pide la fecha
de nacimiento; **no la lee, la pide**, y compara con lo que hay en el sistema.

> «Para confirmar que hablo con la persona correcta, ¿me dice su fecha de
> nacimiento?»

Si no coincide, o si quien contesta es otra persona, el agente no da ningún dato:
dice que volverá a llamar en otro momento y se despide.

**A decidir:**
- **¿Cuántos intentos?** Ahora, si falla, se despide a la primera. Una persona
  mayor puede decir el año mal o entender la pregunta a medias. ¿Repreguntar una
  vez antes de cortar?
- **¿Qué pasa si contesta un familiar?** Es el caso más frecuente en la vida real
  y ahora mismo el agente corta. ¿Debería poder hacer la llamada con un cuidador
  identificado? Eso tiene implicaciones de protección de datos y no me toca a mí
  decidirlas.
- Riesgo conocido: la ficha del paciente incluye la fecha de nacimiento, así que
  técnicamente el modelo *podría* leerla en voz alta. Se lo prohíbe una regla del
  prompt. La forma sólida de cerrarlo es que la comparación la haga la base de
  datos y el modelo nunca vea la fecha — una herramienta
  `verificar_identidad(fecha_dicha)` que devuelva solo sí o no. Son veinte
  líneas; no las he hecho porque el contrato fija seis herramientas y ésa sería
  la séptima. **Dime si la añado.**

## 3. Encuadre

Una vez verificado, el agente consulta la cirugía y dice de qué llama:

> «Gracias, María. Le llamo por la apendicectomía del día cinco, hace tres días.
> Son solo unas preguntas rápidas para ver cómo va todo.»

La fecha y los días salen de la base de datos en ese momento, no del prompt.

---

## 4. Bloque común — se pregunta en todos los seguimientos

En este orden. El dolor primero porque es lo que el paciente tiene en la cabeza y
contesta sin esfuerzo; las dudas al final porque abren conversación y no conviene
abrirla en medio del cuestionario.

**[dolor]**
> «¿Cómo va el dolor? Del uno al diez, ¿dónde lo pondría hoy?»

**[dolor_control]**
> «¿Con las pastillas que le mandaron se le calma, o se le queda igual?»

**[herida]**
> «Cuénteme cómo tiene la herida. ¿La ve limpia y seca, o le sale algo?»

**[fiebre]**
> «¿Ha tenido fiebre? Si se ha tomado la temperatura, dígame cuánto le marcó.»

*(aquí van las preguntas propias de la cirugía — apartado 5)*

**[medicacion]**
> «¿Está tomando la medicación como se la mandaron, sin saltarse tomas?»

**[movilidad]**
> «¿Se está levantando y caminando un poco por la casa?»

**[dudas]**
> «¿Hay algo que le preocupe o alguna duda que quiera que le resuelva?»

**A decidir:**
- **La escala del uno al diez.** Por teléfono mucha gente no la usa bien y
  contesta «regular». ¿Prefieres «¿le duele poco, bastante o mucho?» y que el
  número sea opcional?
- **[movilidad] es la misma pregunta al día 1 que al día 7**, y no debería
  serlo: al día 1 de una herniorrafia lo que se pregunta es si se ha levantado;
  al día 7, si ha vuelto a su vida normal. El código sabe los días de
  postoperatorio y podría cambiar la pregunta. ¿Merece la pena?
- **Falta el sueño y falta el ánimo.** En seguimiento postoperatorio real se
  preguntan. Los dejé fuera para no alargar la llamada. ¿Entran?

---

## 5. Preguntas propias de cada cirugía

Es la decisión 1 del contrato: lo que hace que la llamada suene a seguimiento de
verdad y no a cuestionario genérico.

### Apendicectomía laparoscópica — `apendicectomia`

**[transito]**
> «¿Ha podido ir al baño? ¿Ha expulsado gases con normalidad?»

**[tolerancia_dieta]**
> «¿Está tolerando bien la comida, o le dan náuseas o vómitos?»

**[dolor_hombro]**
> «¿Ha notado dolor en el hombro? Es frecuente después de una laparoscopia.»

### Colecistectomía laparoscópica — `colecistectomia`

**[tolerancia_grasas]**
> «¿Cómo le están sentando las comidas con grasa? ¿Le dan diarrea o le caen mal?»

**[color_piel_orina]**
> «¿Ha notado la piel o los ojos amarillos, o la orina más oscura de lo normal?»

**[dolor_hombro]**
> «¿Le ha dolido el hombro derecho? Después de esta cirugía es habitual.»

### Herniorrafia inguinal — `herniorrafia`

**[esfuerzos]**
> «¿Ha cargado algo de peso o ha hecho algún esfuerzo estos días?»

**[hinchazon_ingle]**
> «¿Ha notado la ingle hinchada o algún moretón grande por la zona?»

**[orinar]**
> «¿Está orinando con normalidad, sin molestias ni dificultad?»

**A decidir — esto es lo que más necesito que revises:**
- **[dolor_hombro] la hace dos veces**, una en apendicectomía y otra en
  colecistectomía, con redacción distinta. ¿Está bien así o sobra en una?
- **[color_piel_orina]** pregunta dos cosas en una frase, y el guion prohíbe
  hacer dos preguntas seguidas. ¿La parto en dos, o la ictericia y la coluria van
  siempre juntas y esto es una sola pregunta clínica?
- En herniorrafia falta preguntar por **hinchazón del escroto**, que en hernia
  inguinal es un hallazgo esperado y a la vez el que más asusta al paciente. No
  la puse por pudor telefónico. Dime si entra y con qué palabras.
- **¿Falta alguna cirugía?** Con estas tres cubro los tres pacientes del seed. Si
  la demo va a enseñar otra, hay que añadir su bloque y su protocolo.

---

## 6. Signos de alarma — cuando el agente corta

Decisión 2 del contrato. El detector es determinista, por palabras y patrones, y
**no pasa por el modelo**: la misma frase da siempre el mismo veredicto.

En cuanto salta, el agente abandona todo lo que le quede del guion, busca la
instrucción en el protocolo, la da, **comprueba que el paciente la ha entendido**
y cierra la llamada dejando el caso escalado.

### Lo que dispara

| Qué | Urgencia | Ejemplo de cómo lo diría un paciente |
|---|---|---|
| Fiebre por encima de 38,5 | urgente | «me tomé la temperatura y tengo treinta y nueve» |
| Fiebre alta sin número | prioritaria | «estoy ardiendo de fiebre» |
| Sangrado activo | urgente | «la herida no para de sangrar» |
| Dehiscencia | urgente | «se me abrió la herida», «se me soltaron los puntos» |
| Dolor torácico | urgente | «me duele el pecho» |
| Dificultad respiratoria | urgente | «me falta el aire» |
| Signos de infección | prioritaria | «le sale un líquido amarillo», «huele mal» |

### Lo que NO dispara, a propósito

Esto es tan importante como lo anterior: un agente que corta el guion cada dos
por tres es inservible, y si en la demo los tres pacientes acaban escalados, el
escalamiento deja de significar nada.

- **«Una manchita de sangre en la gasa», «un poquito de sangre».** Al día 1-3 de
  una laparoscopia manchar el apósito es lo esperado. Con cualquier
  intensificador —«mucha», «no para», «empapada»— sí escala.
- **«La herida está un poco roja».** Roja al tercer día es normal. Roja **y**
  caliente, o roja e hinchada, escala.
- **38,5 clavados.** El umbral es *mayor que* 38,5. 38,5 exactos no escalan.
- **Cualquier cosa negada.** «No he tenido fiebre», «la herida no sangra», «no me
  falta el aire».

**A decidir, y son las tres preguntas clínicas que no puedo contestar yo:**

1. **¿38,5 es el umbral correcto, y debería ser `>` o `>=`?** Muchos protocolos
   escalan ya en 38,5 clavados. Ahora mismo no. Está en una constante con nombre
   (`UMBRAL_FIEBRE`), se cambia en un sitio.
2. **¿«Tengo fiebre» sin número debe escalar?** Ahora no: solo escala si dice
   «mucha», «alta» o «estoy ardiendo». Si dice «tengo fiebre» a secas, el agente
   sigue y le pide el número. Me parece lo correcto, pero es una decisión clínica.
3. **¿La atenuación del sangrado es defendible?** Es la regla con la que menos
   cómodo estoy: distinguir «una manchita» de «sangrado» por el diminutivo
   funciona en las pruebas, pero un paciente que quita importancia a lo suyo
   diría exactamente eso. La alternativa es escalar siempre que se mencione
   sangre y aceptar que la demo escale mucho.

### Cómo suena un corte por alarma

Con «tengo treinta y nueve de fiebre», más o menos así (el texto exacto lo redacta
el modelo a partir del protocolo, esto es la forma):

> «María, treinta y nueve de fiebre a los tres días de la cirugía es algo que hay
> que mirar hoy mismo. Según el protocolo del hospital, **[lo que diga el
> protocolo]**. Voy a avisar ahora mismo a su equipo médico. ¿Me ha entendido lo
> que tiene que hacer?»

*(espera la respuesta)*

> «Perfecto. Ya está avisado su equipo, la van a llamar. Cuídese, María.»

**A decidir:** ¿debe decir un teléfono de urgencias concreto? Ahora no dice
ninguno porque no está en la base de datos. Si me das el número, lo digo.

---

## 7. Cuando el agente no sabe

Grounding obligatorio. Si el paciente pregunta algo que los protocolos subidos no
cubren, el agente **no improvisa**:

> «Eso no lo tengo en los protocolos que manejo, así que no quiero decirle nada
> que no esté seguro. Se lo paso a su equipo médico para que se lo confirmen
> ellos. ¿Le parece?»

No es solo una regla del prompt: cuando la búsqueda no encuentra evidencia
suficiente, la herramienta **no le devuelve ningún fragmento al modelo**. No tiene
con qué inventar aunque quiera.

## 8. Frase de seguridad — literal

Cuando el modelo falla, tarda de más o devuelve vacío:

> «Perdone, no le he entendido bien. Voy a avisar a su equipo médico para que le
> llamen y lo revisen con usted.»

Y escala. Callar no vale: por teléfono un silencio se interpreta como que se ha
cortado la llamada.

## 9. Cierre normal

> «Pues eso es todo, María. Le he anotado que el dolor va a menos y que la herida
> tiene buen aspecto. Recuerde su cita el dieciséis de agosto a las diez, en el
> consultorio trescientos dos. Que siga mejorando.»

El resumen y la cita salen de lo registrado y de la base de datos.

---

## Reglas de estilo que se aplican a todo

- Frases cortas, español de conversación, sin listas ni viñetas: esto se convierte
  en voz.
- **Una sola pregunta por intervención.** Dos seguidas por teléfono hacen que el
  paciente conteste solo a la última.
- Dos o tres frases como mucho.
- Los números, en palabras: «treinta y ocho y medio», no «38,5».
- De usted, siempre.

## Reglas duras

- Nunca diagnosticar. No decir qué le pasa ni qué puede ser.
- Nunca ajustar medicación: ni dosis, ni horarios, ni suspender, ni añadir. Solo
  recordar la pauta tal como está en el sistema.
- Toda indicación clínica sale de un protocolo. Sin evidencia, se dice que no se
  sabe y se escala.
- Si el paciente pide hablar con una persona: escalar y despedirse. Sin insistir.

---

## Resumen de lo que necesito que decidas

Por orden de cuánto cambia lo que oye el paciente:

1. **El umbral de fiebre** (`>38,5` o `>=38,5`) y si «tengo fiebre» sin número
   escala.
2. **La atenuación del sangrado** — la regla con la que menos cómodo estoy.
3. **Los intentos de verificación de identidad** y qué hacer si contesta un
   familiar.
4. **Si añado la herramienta `verificar_identidad`** para que el modelo nunca vea
   la fecha de nacimiento.
5. **La escala del dolor** del uno al diez, o cualitativa.
6. **[color_piel_orina]**: una pregunta o dos.
7. **Hinchazón del escroto** en herniorrafia: entra o no.
8. **Sueño y ánimo** en el bloque común: entran o no.
9. **El teléfono de urgencias** que debe dar al escalar.
