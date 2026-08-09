"""El detector de turnos: lo que separa una conversación de un walkie-talkie.

Estas pruebas fijan las dos decisiones que el spike midió (ver
`docs/VOZ_COMPARATIVA.md`) para que nadie las cambie sin darse cuenta:

  - el umbral de fin de turno son 640 ms de silencio, y el detector tarda
    exactamente eso en decidir;
  - el VAD cuesta ~0,08 ms por ventana de 32 ms, o sea que corre en tiempo real
    con tres órdenes de magnitud de margen.

Todo se prueba con audio de fichero. No hay micrófono en CI y tampoco hace
falta: el reloj del detector cuenta muestras, así que el resultado es idéntico
en una máquina cargada y en una vacía.
"""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.voice.vad import (
    MS_POR_VENTANA,
    SAMPLE_RATE,
    VENTANA,
    DetectorTurnos,
    Evento,
    ParametrosVAD,
    limites_de_voz,
)

AUDIO = Path(__file__).resolve().parents[2] / "scripts" / "spikes" / "audio"


def cargar(nombre: str) -> np.ndarray:
    pcm, sr = sf.read(AUDIO / f"{nombre}.wav", dtype="float32")
    assert sr == SAMPLE_RATE
    return pcm if pcm.ndim == 1 else pcm.mean(axis=1)


def con_silencio(pcm: np.ndarray, ms: int) -> np.ndarray:
    return np.concatenate((pcm, np.zeros(int(SAMPLE_RATE * ms / 1000), dtype=np.float32)))


@pytest.fixture(scope="module")
def turno() -> np.ndarray:
    return cargar("turno_corto")


def test_detecta_inicio_y_fin_de_turno(turno):
    det = DetectorTurnos()
    eventos = det.procesar(con_silencio(turno, 1500))
    tipos = [e.tipo for e in eventos]
    assert tipos == [Evento.EMPEZO_A_HABLAR, Evento.DEJO_DE_HABLAR]


def test_el_fin_de_turno_tarda_el_umbral(turno):
    """`ms` fecha el fin de la voz; `ms_decision`, cuándo se supo.

    La diferencia entre los dos ES la latencia de fin de turno, y tiene que ser
    el umbral configurado con la precisión de una ventana (32 ms). Si esto se
    despega, el presupuesto de latencia de la comparativa deja de valer.
    """
    for umbral in (400, 640, 800):
        det = DetectorTurnos(ParametrosVAD(ms_silencio_fin_turno=umbral))
        fines = [
            e for e in det.procesar(con_silencio(turno, 1500)) if e.tipo is Evento.DEJO_DE_HABLAR
        ]
        assert len(fines) == 1
        assert fines[0].ms_decision - fines[0].ms == pytest.approx(umbral, abs=MS_POR_VENTANA)


def test_no_corta_dentro_de_una_frase_con_dudas():
    """`turno_dudoso` lleva pausas de 350, 500 y 250 ms metidas a propósito.

    Con 640 ms de umbral el detector tiene que aguantarlas sin cerrar el turno:
    cerrarlo ahí significa cortarle la frase al paciente por la mitad.
    """
    det = DetectorTurnos(ParametrosVAD(ms_silencio_fin_turno=640))
    eventos = det.procesar(con_silencio(cargar("turno_dudoso"), 1500))
    fines = [e for e in eventos if e.tipo is Evento.DEJO_DE_HABLAR]
    assert len(fines) == 1, "el turno se partió en una pausa interna"


def test_un_umbral_corto_si_parte_la_frase():
    """La cara b de la prueba anterior: con 320 ms el corte falso ocurre.

    Está aquí para demostrar que la prueba de arriba mide algo. Sin esto, subir
    el umbral a 3 s también «pasaría» y no se sabría que era por trampa.
    """
    det = DetectorTurnos(ParametrosVAD(ms_silencio_fin_turno=320))
    eventos = det.procesar(con_silencio(cargar("turno_dudoso"), 1500))
    fines = [e for e in eventos if e.tipo is Evento.DEJO_DE_HABLAR]
    assert len(fines) > 1


def test_detecta_una_respuesta_de_una_palabra():
    """«Sí.» dura 200 ms. Es la respuesta más frecuente de un seguimiento y el
    caso donde un umbral de arranque generoso se la come entera."""
    eventos = DetectorTurnos().procesar(con_silencio(cargar("si_corto"), 1000))
    assert [e.tipo for e in eventos] == [Evento.EMPEZO_A_HABLAR, Evento.DEJO_DE_HABLAR]


def test_el_silencio_no_dispara_nada():
    silencio = np.zeros(SAMPLE_RATE * 2, dtype=np.float32)
    assert DetectorTurnos().procesar(silencio) == []


def test_trocear_la_entrada_no_cambia_el_resultado(turno):
    """El navegador manda trozos de tamaño arbitrario, no múltiplos de 512.

    Si el detector no acumulara el resto entre llamadas, los eventos se moverían
    según el tamaño del trozo y ninguna medición sería reproducible.
    """
    pcm = con_silencio(turno, 1500)
    de_golpe = DetectorTurnos().procesar(pcm)

    det = DetectorTurnos()
    a_trozos = []
    tamano = 317  # deliberadamente primo y menor que una ventana
    for i in range(0, len(pcm), tamano):
        a_trozos += det.procesar(pcm[i : i + tamano])

    assert [(e.tipo, e.ms) for e in de_golpe] == [(e.tipo, e.ms) for e in a_trozos]


def test_el_vad_corre_en_tiempo_real(turno):
    """Cada ventana son 32 ms de audio: si costara más, el VAD iría por detrás.

    El margen medido es de ~400×, pero la prueba sirve de red por si alguien
    cambia el modelo o el proveedor de ONNX.
    """
    det = DetectorTurnos()
    det.procesar(turno)
    assert det.metricas.ventanas > 20
    assert det.metricas.ms_por_ventana < MS_POR_VENTANA / 4
    assert det.metricas.p95_ms < MS_POR_VENTANA


def test_limites_de_voz_encuadra_el_habla(turno):
    inicio, fin = limites_de_voz(turno)
    duracion_ms = len(turno) * 1000 / SAMPLE_RATE
    assert 0 <= inicio < 300
    assert duracion_ms - 200 < fin <= duracion_ms


def test_el_umbral_de_barge_in_es_mas_agresivo(turno):
    """Mientras el agente habla, arrancar tarde es peor que arrancar de más.

    Se comprueba que el detector usa el umbral de barge-in y no el normal, que
    es la razón de que existan dos.
    """
    params = ParametrosVAD(ms_inicio=500, ms_inicio_barge_in=96)
    det = DetectorTurnos(params)
    det.agente_empieza_a_hablar()
    eventos = det.procesar(turno[: VENTANA * 8])
    assert [e.tipo for e in eventos] == [Evento.EMPEZO_A_HABLAR]
