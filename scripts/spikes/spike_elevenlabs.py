"""Smoke test del camino premium: ¿funciona ElevenLabs de verdad?

El código de ElevenLabsTTS estaba escrito contra la API pero NUNCA ejecutado.
Este spike existe para que el cambio del último día sea una variable de entorno
sobre un camino probado, y no una integración a estrenar bajo presión de demo.

Gasta a propósito MUY pocos caracteres: el free tier son ~10.000 al mes.

HALLAZGO (08-ago): el free tier NO permite voces de la biblioteca por API —
devuelve 402 `paid_plan_required`. Solo las voces *premade* funcionan. Por eso
ELEVENLABS_VOICE_ID apunta a una de ellas (Laura). Con plan de pago se puede
cambiar por una voz nativa española o un clon sin tocar código.

    uv run python ../scripts/spikes/spike_elevenlabs.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.voice.tts import SAMPLE_RATE, crear_motor  # noqa: E402

# Frase clínica real del guion: lleva número, unidad y término médico, que es
# donde un TTS se rompe. Corta a propósito — cada carácter cuesta.
FRASE = "Buenos días. ¿Ha tenido fiebre de más de 38 grados desde la cirugía?"


async def main() -> int:
    s = get_settings()
    if not s.elevenlabs_api_key or not s.elevenlabs_voice_id:
        print("✗ Falta ELEVENLABS_API_KEY o ELEVENLABS_VOICE_ID en .env")
        return 1

    print(f"modelo   : {s.elevenlabs_model}")
    print(f"voice_id : {s.elevenlabs_voice_id}")
    print(f"texto    : {FRASE!r}  ({len(FRASE)} caracteres)\n")

    motor = crear_motor("elevenlabs")
    try:
        t0 = time.perf_counter()
        audio = await motor.sintetizar(FRASE)
        ms = (time.perf_counter() - t0) * 1000

        # Lo que de verdad hay que verificar: que el PCM es audio y no basura.
        # Un output_format mal negociado devuelve MP3, y np.frombuffer lo lee
        # como ruido -- pasaría el "no falló" y sonaría a estática en la demo.
        pico = float(np.abs(audio.pcm).max()) if audio.pcm.size else 0.0
        rms = float(np.sqrt((audio.pcm.astype(np.float64) ** 2).mean())) if audio.pcm.size else 0.0
        esperado_s = len(FRASE) / 15.0  # ~15 caracteres/s de habla natural

        print(f"latencia      : {ms:.0f} ms")
        print(f"sample rate   : {audio.sample_rate} Hz")
        print(f"muestras      : {len(audio.pcm)}")
        print(f"duración      : {audio.duracion_s:.2f} s  (esperado ≈{esperado_s:.1f} s)")
        print(f"pico | rms    : {pico:.3f} | {rms:.3f}")

        problemas = []
        if audio.pcm.size == 0:
            problemas.append("audio vacío")
        if pico > 1.001:
            problemas.append(f"pico {pico:.2f} fuera de [-1,1]: el formato NO es pcm_s16le")
        if rms < 0.005:
            problemas.append("prácticamente silencio")
        if rms > 0.45:
            problemas.append(f"rms {rms:.2f} sospechoso de ruido (¿MP3 leído como PCM?)")
        if not (0.4 * esperado_s < audio.duracion_s < 2.5 * esperado_s):
            problemas.append(f"duración {audio.duracion_s:.1f}s incoherente con el texto")

        destino = Path(__file__).parent / "out" / "elevenlabs_frase.wav"
        destino.parent.mkdir(parents=True, exist_ok=True)
        import soundfile as sf

        sf.write(destino, audio.pcm, audio.sample_rate)
        print(f"\naudio         : {destino}")

        # Cuota restante: viene en las cabeceras de la última respuesta.
        try:
            r = await motor._http.get("https://api.elevenlabs.io/v1/user/subscription")
            if r.status_code == 200:
                d = r.json()
                usados, limite = d.get("character_count"), d.get("character_limit")
                print(f"cuota         : {usados}/{limite} caracteres usados este mes")
        except Exception as e:  # noqa: BLE001
            print(f"cuota         : no se pudo leer ({e})")

        if problemas:
            print("\n✗ PROBLEMAS:")
            for p in problemas:
                print(f"  - {p}")
            return 1

        print(f"\n✓ Camino premium verificado. Reproducir:  afplay {destino}")
        print(f"  Comparar con local:  afplay {destino.parent}/kokoro_completa.wav")
        return 0

    except Exception as e:  # noqa: BLE001
        import httpx

        if isinstance(e, httpx.HTTPStatusError):
            print(f"✗ HTTP {e.response.status_code}: {e.response.text[:400]}")
        else:
            print(f"✗ {type(e).__name__}: {e}")
        return 1
    finally:
        await motor.cerrar()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
