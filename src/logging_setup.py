"""Konsol + dosya loglamasını yapılandırır.

Gereksinim: bir kaynak erişilemezse diğerleri etkilenmemeli ve durum
loglanmalıdır. Bu modül tüm modüllerin ortak kullandığı logger yapılandırmasını
kurar; hata izolasyonunun kendisi fetcher/main modüllerindeki try/except
bloklarında sağlanır.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_configured = False


def setup_logging(output_dir: str | Path = "data", level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return

    log_dir = Path(output_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "finans_haber.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    _configured = True
