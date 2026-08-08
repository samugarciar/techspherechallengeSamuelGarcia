"""Lo que hay que saber de Pipecat 1.7 antes de montar el pipeline, como pruebas.

No prueban nuestro código: prueban **supuestos sobre la librería**. Existen
porque los dos hallazgos que decidieron la comparativa
(`docs/VOZ_COMPARATIVA.md`) son propiedades de Pipecat que pueden cambiar sin
avisar en cualquier versión menor, y si cambian queremos enterarnos por un test
en rojo y no por un paciente que oye «appendicitomía».

Son rápidos y no cargan ningún modelo.
"""

from pathlib import Path

import numpy as np
import pytest

from app.voice.tts import Audio, TTSEngine

pytest.importorskip("pipecat", reason="Pipecat es opcional: la Opción B no lo necesita")


class TTSFalso(TTSEngine):
    nombre = "falso"

    async def sintetizar(self, texto: str) -> Audio:
        return Audio(np.zeros(2400, dtype=np.float32), 24_000)


def test_el_whisper_de_pipecat_no_es_utilizable_en_esta_instalacion():
    """Primer hallazgo que decide el STT: el servicio no se puede ni importar.

    `pipecat.services.whisper.stt` hace `from faster_whisper import WhisperModel`
    a nivel de módulo, y `WhisperSTTServiceMLX` vive en ese mismo módulo. O sea
    que la variante de MLX —que no usa faster-whisper para nada— está detrás de
    una dependencia que no tenemos y que arrastra CTranslate2. Con 21 GB de disco
    libres y 7 GB ya en modelos, instalarla no es gratis.
    """
    import importlib

    with pytest.raises(ImportError, match="faster_whisper"):
        importlib.import_module("pipecat.services.whisper.stt")


def test_el_whisper_de_pipecat_no_admite_vocabulario_clinico():
    """Segundo hallazgo, el de fondo: aunque se instalara, no serviría.

    Su `run_stt()` llama a `mlx_whisper.transcribe(...)` con `model`,
    `temperature` y `language`, y **no** con `initial_prompt`, así que no hay
    forma de pasarle el sesgo de vocabulario clínico de la Fase 0. Medido con el
    mismo modelo: transcribe «appendicitomía» donde el nuestro escribe
    «apendicectomía». Por eso existe `WhisperClinicoSTT`.

    Se comprueba leyendo el fuente porque el módulo no es importable (ver la
    prueba anterior). Si algún día Pipecat añade el campo, esta prueba se pone
    roja y toca reconsiderar: su servicio traería métricas y trazas gratis.
    """
    import pipecat.services.whisper as paquete

    fuente = (Path(paquete.__file__).parent / "stt.py").read_text(encoding="utf-8")
    assert "initial_prompt" not in fuente, (
        "Pipecat ya admite initial_prompt: revisar si conviene adoptar su servicio"
    )
    assert "path_or_hf_repo=model_path" in fuente, "cambió la llamada a mlx_whisper"


def test_el_vad_ya_no_es_un_parametro_del_transporte():
    """El montaje del README («TransportParams(vad_analyzer=…)») no compila en 1.7.

    El VAD pasó a ser un procesador del pipeline. Se fija como prueba porque es
    el error que más tiempo costó del spike: los tutoriales y los ejemplos de la
    red siguen enseñando la forma vieja.
    """
    from pipecat.transports.base_transport import TransportParams

    assert "vad_analyzer" not in TransportParams.model_fields


def test_la_parada_de_turno_por_defecto_descarga_un_modelo():
    """Por defecto, Pipecat 1.7 decide el fin de turno con un modelo semántico
    (`LocalSmartTurnAnalyzerV3`), no con un umbral de silencio.

    Importa para el presupuesto de latencia y para el disco: hay que elegir
    explícitamente la estrategia de umbral, que es la que se midió.
    """
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    estrategias = UserTurnStrategies()
    assert type(estrategias.stop[0]).__name__ == "TurnAnalyzerUserTurnStopStrategy"


def test_el_pipeline_se_monta_en_el_orden_esperado():
    """El STT va ANTES del `UserTurnProcessor`.

    Al revés el turno no se cierra nunca: las estrategias de parada leen las
    transcripciones, y las transcripciones solo viajan hacia abajo. No está
    documentado; se descubre leyendo `pipecat/turns/user_stop/`.
    """
    from app.voice.pipeline_pipecat import construir

    piezas = construir(motor_tts=TTSFalso())
    nombres = [type(p).__name__ for p in piezas.pipeline._processors]
    assert nombres.index("WhisperClinicoSTT") < nombres.index("UserTurnProcessor")
    assert nombres.index("VADProcessor") < nombres.index("WhisperClinicoSTT")
    assert nombres.index("MotorLocalTTS") < nombres.index("SalidaMedida")


def test_los_umbrales_del_vad_son_los_medidos():
    """Los dos VAD (el de Pipecat y el propio) tienen que ir igual ajustados, o
    la comparativa mediría dos configuraciones en vez de dos orquestadores."""
    from app.voice.pipeline_pipecat import parametros_vad
    from app.voice.vad import ParametrosVAD

    p = parametros_vad()
    propio = ParametrosVAD()
    assert p.confidence == propio.umbral
    assert p.start_secs * 1000 == propio.ms_inicio
    assert p.min_volume == 0.0, "el umbral de volumen se come los finales susurrados"
