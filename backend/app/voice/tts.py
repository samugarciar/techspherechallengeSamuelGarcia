"""Síntesis de voz: cinco motores, una interfaz.

Estrategia decidida en la Fase 0: se DESARROLLA con Kokoro (local, gratis,
ilimitado) y se DEMUESTRA con un motor de nube, donde la calidad de voz manda.
El free tier de ElevenLabs son ~10.000 caracteres al mes — una tarde de iterar
sobre el guion se lo come, así que atarse a él durante el desarrollo sería un
error de logística, no de arquitectura.

El cambio es `TTS_ENGINE` en .env. Nada más del sistema se entera.

Los motores locales siguen siendo la red de seguridad: si la red del venue falla
durante la demo, se vuelve a Kokoro con una variable de entorno. Y sostienen el
argumento de que el sistema puede correr 100% on-prem si un hospital lo exige.

TODAS las salidas se normalizan a PCM float32 mono a 24 kHz, para que el
pipeline de voz no tenga que saber qué motor está detrás.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.core.config import get_settings

SAMPLE_RATE = 24_000

# Candidato a fin de frase: puntuación final seguida de espacio y mayúscula o
# signo de apertura. El lookbehind descarta ya los decimales ("38.5 grados"),
# porque exige espacio después del punto.
_FIN_FRASE = re.compile(r"(?<=[.!?…])\s+(?=[¿¡A-ZÁÉÍÓÚÑ])")

# Pero eso no basta: "Dr. Peláez" también encaja. Python no admite lookbehind de
# ancho variable, así que cada candidato se valida mirando la palabra anterior.
# Solo abreviaturas que REALMENTE llevan punto. Los símbolos de unidad (mg, ml,
# kg, cm...) se escriben sin punto en español, así que un punto detrás de ellos
# sí cierra frase: "Tome 500 mg. Después descanse." son dos frases de verdad.
# Meterlos aquí impediría un corte legítimo.
_ABREVIATURAS = frozenset(
    """dr dra sr sra srta ud uds av avda núm nro pág págs
       aprox etc ej vs cap art""".split()
)

_ULTIMA_PALABRA = re.compile(r"(\S+)$")


def _es_corte_real(texto: str, pos: int) -> bool:
    """¿El punto en `pos` cierra una frase, o pertenece a una abreviatura?

    Un corte falso hace que el agente diga «su cirujano fue el doctor» / pausa /
    «Peláez y todo salió bien», que suena roto por teléfono.
    """
    m = _ULTIMA_PALABRA.search(texto[:pos])
    if not m:
        return True
    palabra = m.group(1).rstrip(".!?…").lower()
    if palabra in _ABREVIATURAS:
        return False
    # Iniciales: "J. Villalba" no es un fin de frase.
    return not (len(palabra) == 1 and palabra.isalpha())


def _trocear_por_frases(texto: str) -> list[str]:
    piezas: list[str] = []
    inicio = 0
    for m in _FIN_FRASE.finditer(texto):
        if not _es_corte_real(texto, m.start()):
            continue
        piezas.append(texto[inicio : m.start()].strip())
        inicio = m.end()
    piezas.append(texto[inicio:].strip())
    return piezas


def dividir_en_frases(texto: str, minimo: int = 12) -> list[str]:
    """Trocea la respuesta del LLM en frases sintetizables.

    Es la optimización de latencia más importante del pipeline: sintetizar la
    primera frase (461 ms con Kokoro) y empezar a reproducirla mientras el LLM
    todavía está escribiendo el resto, en vez de esperar al párrafo completo
    (987 ms). Duplica la sensación de rapidez sin tocar ningún modelo.
    """
    piezas = [p for p in _trocear_por_frases(texto) if p]
    if not piezas:
        return []

    # Solo se pegan las muletillas ("Sí.", "Entiendo."): cada llamada al motor
    # tiene un coste fijo que no compensa para tres palabras. El umbral es bajo
    # a propósito — una frase de ~20 caracteres ya es ~1.2 s de audio, y
    # emitirla cuanto antes arranca la voz antes. Subirlo retrasa el primer
    # audio para ahorrar una llamada que no dolía.
    salida: list[str] = []
    for pieza in piezas:
        if salida and len(salida[-1]) < minimo:
            salida[-1] = f"{salida[-1]} {pieza}"
        else:
            salida.append(pieza)
    return salida


@dataclass(slots=True)
class Audio:
    pcm: np.ndarray          # float32 mono, [-1, 1]
    sample_rate: int = SAMPLE_RATE

    @property
    def duracion_s(self) -> float:
        return len(self.pcm) / self.sample_rate


class TTSEngine(ABC):
    nombre: str

    @abstractmethod
    async def sintetizar(self, texto: str) -> Audio: ...

    async def stream_por_frases(self, texto: str) -> AsyncIterator[Audio]:
        """Emite audio frase a frase, en orden.

        Implementación por defecto: secuencial. Sintetizar en paralelo y
        reordenar daría peor resultado — el primer trozo es el único que el
        paciente está esperando, y adelantar el tercero no sirve de nada.
        """
        for frase in dividir_en_frases(texto):
            yield await self.sintetizar(frase)

    async def cerrar(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Locales
# ---------------------------------------------------------------------------
class KokoroTTS(TTSEngine):
    """Kokoro-82M, Apache-2.0. 461 ms a la primera frase en M4."""

    nombre = "kokoro"

    def __init__(self, voz: str) -> None:
        self.voz = voz
        self._pipe = None
        # `sintetizar` va a un hilo con `asyncio.to_thread`, así que dos llamadas
        # de voz simultáneas ejecutan `_cargar()` en paralelo de verdad. Sin
        # cerrojo las dos ven `_pipe is None` y construyen un KPipeline cada una:
        # dos copias de Kokoro-82M en una máquina de 16 GB que ya sostiene
        # Whisper, bge-m3 y el reranker. La segunda además se descarta al asignar.
        self._cerrojo = threading.Lock()

    def _cargar(self):
        if self._pipe is not None:
            return self._pipe
        with self._cerrojo:
            if self._pipe is None:
                from kokoro import KPipeline

                self._pipe = KPipeline(lang_code="e", repo_id="hexgrad/Kokoro-82M")
        return self._pipe

    def _sync(self, texto: str) -> Audio:
        trozos = [c for _, _, c in self._cargar()(texto, voice=self.voz, speed=1.0)]
        if not trozos:
            return Audio(np.zeros(0, dtype=np.float32))
        return Audio(np.concatenate(trozos).astype(np.float32))

    async def sintetizar(self, texto: str) -> Audio:
        return await asyncio.to_thread(self._sync, texto)


class PiperTTS(TTSEngine):
    """ONNX puro en CPU. El más predecible: no depende de GPU ni de red."""

    nombre = "piper"

    def __init__(self, modelo: Path) -> None:
        self.modelo = modelo

    def _sync(self, texto: str) -> Audio:
        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            destino = Path(tmp.name)
        try:
            subprocess.run(
                ["piper", "-m", str(self.modelo), "-f", str(destino)],
                input=texto.encode(), check=True, capture_output=True,
            )
            pcm, sr = sf.read(destino, dtype="float32")
            return Audio(_a_mono(pcm), sr)
        finally:
            destino.unlink(missing_ok=True)

    async def sintetizar(self, texto: str) -> Audio:
        return await asyncio.to_thread(self._sync, texto)


class SayTTS(TTSEngine):
    """Voz del sistema macOS. Sin dependencias: el último recurso que siempre funciona."""

    nombre = "say"

    def __init__(self, voz: str = "Monica") -> None:
        self.voz = voz

    def _sync(self, texto: str) -> Audio:
        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            destino = Path(tmp.name)
        try:
            subprocess.run(
                ["say", "-v", self.voz, "-o", str(destino),
                 f"--data-format=LEI16@{SAMPLE_RATE}", texto],
                check=True, capture_output=True,
            )
            pcm, sr = sf.read(destino, dtype="float32")
            return Audio(_a_mono(pcm), sr)
        finally:
            destino.unlink(missing_ok=True)

    async def sintetizar(self, texto: str) -> Audio:
        return await asyncio.to_thread(self._sync, texto)


# ---------------------------------------------------------------------------
# Nube
# ---------------------------------------------------------------------------
class ElevenLabsTTS(TTSEngine):
    """Calidad claramente superior a cualquier motor local de este tamaño.

    Se pide PCM crudo en lugar de MP3 para no pagar la decodificación en el
    camino de voz. `eleven_flash_v2_5` es el modelo de menor latencia.

    OJO: el free tier son ~10.000 caracteres al mes. Usar solo para las pruebas
    finales y la demo, nunca durante el desarrollo.
    """

    nombre = "elevenlabs"
    BASE = "https://api.elevenlabs.io/v1/text-to-speech"

    def __init__(self, api_key: str, voice_id: str, modelo: str) -> None:
        if not api_key or not voice_id:
            raise RuntimeError(
                "ElevenLabs necesita ELEVENLABS_API_KEY y ELEVENLABS_VOICE_ID en .env"
            )
        import httpx

        self.voice_id = voice_id
        self.modelo = modelo
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"xi-api-key": api_key},
        )

    async def sintetizar(self, texto: str) -> Audio:
        r = await self._http.post(
            f"{self.BASE}/{self.voice_id}/stream",
            params={"output_format": f"pcm_{SAMPLE_RATE}"},
            json={
                "text": texto,
                "model_id": self.modelo,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
        )
        r.raise_for_status()
        # PCM 16 bits con signo, little-endian -> float32 en [-1, 1]
        pcm16 = np.frombuffer(r.content, dtype="<i2")
        return Audio((pcm16.astype(np.float32) / 32768.0), SAMPLE_RATE)

    async def cerrar(self) -> None:
        await self._http.aclose()


class CartesiaTTS(TTSEngine):
    """Diseñado específicamente para agentes de voz; suele batir a ElevenLabs
    en latencia. Merece entrar en la comparación final."""

    nombre = "cartesia"
    URL = "https://api.cartesia.ai/tts/bytes"

    def __init__(self, api_key: str, voice_id: str, modelo: str) -> None:
        if not api_key or not voice_id:
            raise RuntimeError(
                "Cartesia necesita CARTESIA_API_KEY y CARTESIA_VOICE_ID en .env"
            )
        import httpx

        self.voice_id = voice_id
        self.modelo = modelo
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"X-API-Key": api_key, "Cartesia-Version": "2024-06-10"},
        )

    async def sintetizar(self, texto: str) -> Audio:
        r = await self._http.post(
            self.URL,
            json={
                "model_id": self.modelo,
                "transcript": texto,
                "voice": {"mode": "id", "id": self.voice_id},
                "language": "es",
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_f32le",
                    "sample_rate": SAMPLE_RATE,
                },
            },
        )
        r.raise_for_status()
        return Audio(np.frombuffer(r.content, dtype="<f4").copy(), SAMPLE_RATE)

    async def cerrar(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------
def _a_mono(pcm: np.ndarray) -> np.ndarray:
    return pcm.mean(axis=1).astype(np.float32) if pcm.ndim > 1 else pcm.astype(np.float32)


def _voz_piper() -> Path:
    candidatos = sorted(
        Path(__file__).resolve().parents[3].glob("scripts/spikes/piper_voices/*.onnx")
    )
    if not candidatos:
        raise RuntimeError("No hay ninguna voz .onnx de Piper descargada")
    return candidatos[0]


def crear_motor(nombre: str) -> TTSEngine:
    """Fábrica por nombre de motor.

    Quien decide qué motor toca es `voice_mode.VoiceRouter`, según el modo
    (local/premium) que el admin tenga activo en la consola. Esta función solo
    construye lo que le pidan.
    """
    s = get_settings()
    match nombre:
        case "kokoro":
            return KokoroTTS(s.tts_voice)
        case "piper":
            return PiperTTS(_voz_piper())
        case "say":
            return SayTTS(s.tts_voice if s.tts_voice.isalpha() else "Monica")
        case "elevenlabs":
            return ElevenLabsTTS(
                s.elevenlabs_api_key, s.elevenlabs_voice_id, s.elevenlabs_model
            )
        case "cartesia":
            return CartesiaTTS(
                s.cartesia_api_key, s.cartesia_voice_id, s.cartesia_model
            )
    raise ValueError(f"motor TTS desconocido: {nombre}")
