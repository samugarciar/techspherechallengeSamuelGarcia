"""Configuración central. Todo se lee de .env — nada hardcodeado en el código."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Base de datos -------------------------------------------------------
    database_url: str = "postgresql://postop:postop@localhost:5433/postop"

    # --- LLM -----------------------------------------------------------------
    # Cambiar este campo conmuta el agente entero. Es el plan B para cuando Gemini
    # no esté disponible, NO una palanca de latencia: medido, Groq gana 91 ms de
    # TTFT y su free tier da timeouts con tool calling (22,5 s de mediana en el
    # turno completo frente a 1,2 s). Ver README §Presupuesto de latencia.
    llm_provider: Literal["gemini", "groq"] = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # --- Voz -----------------------------------------------------------------
    # STT siempre local. `small` + sesgo de vocabulario clínico: 481 ms con los
    # mismos aciertos que `medium` (1222 ms). Medido en M4.
    stt_model: str = "mlx-community/whisper-small-mlx"

    # Dos modos de voz conmutables desde la consola (tabla app_settings), no
    # desde aquí: el admin cambia local <-> premium en caliente, incluso a
    # mitad de llamada. Aquí solo se declara QUÉ motor implementa cada modo.
    #
    #   local   -> gratis, ilimitado, sin red. Es el modo de desarrollo.
    #   premium -> mejor voz, coste por carácter. Para pruebas finales y demo.
    tts_engine_local: Literal["kokoro", "piper", "say"] = "kokoro"
    tts_engine_premium: Literal["elevenlabs", "cartesia", "kokoro"] = "elevenlabs"
    tts_voice: str = "ef_dora"

    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"   # el de menor latencia

    cartesia_api_key: str = ""
    cartesia_voice_id: str = ""
    cartesia_model: str = "sonic-2"

    # --- RAG (local) ---------------------------------------------------------
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    # CUIDADO con este bloque: aquí hubo un error de medición que costó caro.
    # El spike de la Fase 0 dijo 114 ms para 8 candidatos, pero medía pasajes de
    # 250 caracteres. Los fragmentos reales tienen entre 500 y 1400, y el coste
    # de un cross-encoder escala con la longitud de la secuencia. Medido contra la
    # API real, en caliente: **585 ms**, el mayor bloque del pipeline de voz.
    # Ver README §«El reranker costaba cinco veces lo presupuestado» y
    # eval/medir_reranker.py, que está escrito para cerrar la decisión de
    # encenderlo o no con el corpus real en vez de con documentos sintéticos.
    retrieve_top_k: int = 8
    context_top_k: int = 4

    # Grounding obligatorio: por debajo de este score el agente dice "no tengo
    # esa información" y escala, en vez de improvisar. En dominio clínico
    # inventar es peor que no responder.
    min_relevance_score: float = 0.35

    # --- Almacenamiento ------------------------------------------------------
    storage_dir: Path = Field(default=REPO_ROOT / "storage" / "documents")

    @field_validator("storage_dir")
    @classmethod
    def _absoluta(cls, v: Path) -> Path:
        """Ancla la carpeta a la raíz del repo si viene relativa.

        `.env` trae `STORAGE_DIR=./storage/documents`, y una ruta relativa se
        resuelve contra el directorio de trabajo del proceso. Como la API se
        arranca desde `backend/` y los scripts desde la raíz, cada uno miraría
        una carpeta distinta. La consecuencia leve es que el worker no encuentra
        lo que escribió la API. La grave es que `olvidar_documento()` comprueba
        que el archivo esté por debajo de `storage_dir` antes de borrarlo —una
        defensa correcta contra travesías de ruta— y esa comparación falla si los
        dos procesos resuelven distinto: la fila desaparece de la base, el agente
        olvida de verdad, y el PDF con datos clínicos se queda en el disco del
        hospital para siempre. Un olvido a medias, y silencioso.
        """
        return v if v.is_absolute() else (REPO_ROOT / v).resolve()

    # --- Admin ---------------------------------------------------------------
    admin_token: str = "cambiar-esto-en-local"

    @property
    def llm_api_key(self) -> str:
        return self.gemini_api_key if self.llm_provider == "gemini" else self.groq_api_key

    @property
    def llm_model(self) -> str:
        return self.gemini_model if self.llm_provider == "gemini" else self.groq_model


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.storage_dir.mkdir(parents=True, exist_ok=True)
    return s
