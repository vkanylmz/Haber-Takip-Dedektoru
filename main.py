"""Tek komutla, worker'ı (periyodik tarama + özetleme + önem skorlama +
veritabanı kaydı + Telegram bildirimi) VE web dashboard'unu BİRLİKTE başlatır.

Çalıştırma (proje kök dizininden):
    python main.py

Ctrl+C ile her ikisi de düzgünce durdurulur.

Sadece worker'ı (web olmadan) çalıştırmak isterseniz:
    python worker.py

Sadece web dashboard'unu (worker olmadan, ör. sadece daha önce toplanmış
verileri incelemek için) çalıştırmak isterseniz:
    .\\.venv\\Scripts\\python.exe -m uvicorn src.web.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import sys

import uvicorn

# stdout yönlendirilmiş/dosyaya redirect edilmişse Python varsayılan olarak
# tam (blok) tamponlamaya geçer -> aşağıdaki dashboard adresi banner'ı,
# uzun süre çalışan uvicorn.run() bloke ettiği sürece hiç görünmeyebilir.
# Satır tamponlamaya zorlayarak print()'lerin hemen çıkması sağlanır.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

from src.config import load_config
from src.config_setup import ensure_all_credentials
from src.logging_setup import setup_logging
from worker import start_background_scheduler

logger = logging.getLogger(__name__)


def main() -> None:
    config = load_config()
    setup_logging(config["app"].get("output_dir", "data"))

    # Eksik API anahtarı/token'ları (varsa, etkileşimli bir terminaldeysek)
    # sorar, doğrular, .env'e kaydeder; eksik/geçersiz kalanlar için ilgili
    # özelliği devre dışı bırakıp devam eder (bkz. src/config_setup.py).
    # llm_provider: sadece config.yaml'da seçili sağlayıcının API anahtarı sorulur.
    llm_provider = config["summarizer"].get("llm_provider", "gemini")
    ensure_all_credentials(llm_provider=llm_provider)

    scheduler = start_background_scheduler(config)

    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 8000)
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    dashboard_url = f"http://{display_host}:{port}"

    logger.info("Web dashboard başlatılıyor: %s", dashboard_url)
    print(flush=True)
    print("=" * 60, flush=True)
    print(f"  📊 Dashboard: {dashboard_url}", flush=True)
    print("     (Ctrl+C ile durdurun)", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)

    try:
        uvicorn.run("src.web.app:app", host=host, port=port, log_level="warning")
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Uygulama kapatıldı.")


if __name__ == "__main__":
    main()
