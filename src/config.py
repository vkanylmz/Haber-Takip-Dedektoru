"""config.yaml ve .env dosyalarını yükleyen yardımcı modül."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """config.yaml'ı okur ve .env'deki ortam değişkenlerini yükler.

    API anahtarı gibi sırlar config.yaml'da değil, yalnızca ortam
    değişkenlerinde (.env) tutulur.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=env_path)  # proje kökündeki .env dosyasını (varsa) yükler

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Yapılandırma dosyası bulunamadı: {path}. "
            "Önce config.yaml dosyasını oluşturun."
        )

    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    config.setdefault("app", {})
    config.setdefault("scheduler", {})
    config.setdefault("summarizer", {})
    config.setdefault("sources", [])

    return config


def get_anthropic_api_key() -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY ortam değişkeni bulunamadı. "
            ".env.example dosyasını .env olarak kopyalayıp API anahtarınızı girin."
        )
    return api_key


def get_gemini_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY ortam değişkeni bulunamadı. Google AI Studio'dan "
            "(https://aistudio.google.com/apikey) bir anahtar alıp .env dosyasına ekleyin."
        )
    return api_key


def get_summarizer_api_key(summarizer_cfg: dict[str, Any]) -> tuple[str, str]:
    """`summarizer.llm_provider` ayarına göre doğru sağlayıcının API anahtarını
    döner. Dönen tuple: (provider, api_key). Anahtar tanımlı değilse
    RuntimeError fırlatır (çağıran taraf bunu yakalayıp özelliği devre dışı
    bırakır, bkz. src/main.py)."""
    provider = summarizer_cfg.get("llm_provider", "gemini")
    if provider == "anthropic":
        return provider, get_anthropic_api_key()
    if provider == "gemini":
        return provider, get_gemini_api_key()
    raise RuntimeError(
        f"Bilinmeyen summarizer.llm_provider: {provider!r} ('gemini' veya 'anthropic' olmalı)."
    )
