"""Tüm fetcher'ların ortak kullandığı yardımcılar: robots.txt kontrolü ve
alan adı (domain) bazlı rate-limit.

Gereksinim #1: RSS yoksa siteye saygılı şekilde (robots.txt'e uyarak,
rate-limit koyarak) scraping yap.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_request_time: dict[str, float] = {}
_robots_cache: dict[str, RobotFileParser | None] = {}


def rate_limit(url: str, min_interval_seconds: float) -> None:
    """Aynı alan adına ardışık isteklerin arasında en az `min_interval_seconds`
    kadar bekleme sağlar. Çoklu thread'lerden güvenle çağrılabilir."""
    domain = urlparse(url).netloc
    with _lock:
        now = time.monotonic()
        last = _last_request_time.get(domain, 0.0)
        wait = min_interval_seconds - (now - last)
        _last_request_time[domain] = now + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)


def _fetch_robots_parser(domain_root: str, user_agent: str, timeout: float) -> RobotFileParser | None:
    """robots.txt'i KENDİ User-Agent'ımızla çekip ayrıştırır.

    Önemli: `RobotFileParser.read()` kullanmıyoruz çünkü o, isteği Python'un
    varsayılan urllib User-Agent'ı ile yapar. Bazı siteler (ör. CloudFront
    arkasındaki foreks.com, cnbce.com) bu varsayılan bot imzasına 403
    döndürüyor; RobotFileParser bir 401/403'ü "her şeyi yasakla" olarak
    yorumluyor ve bu da robots.txt aslında hiçbir şeyi yasaklamasa bile
    kaynağın tamamen engellenmiş sanılmasına yol açıyor. Bunun yerine
    robots.txt'i gerçek isteklerimizde kullandığımız User-Agent ile kendimiz
    çekip ayrıştırıyoruz — hem daha doğru hem de tutarlı.
    """
    robots_url = f"{domain_root}/robots.txt"
    try:
        response = httpx.get(
            robots_url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "robots.txt alınamadı (%s): %s — bu kaynak için varsayılan olarak izinli kabul ediliyor",
            robots_url,
            exc,
        )
        return None

    rp = RobotFileParser()
    rp.set_url(robots_url)

    if response.status_code in (401, 403):
        # RFC/robotparser konvansiyonu: 401/403 -> siteye erişim tamamen yasak kabul edilir.
        rp.disallow_all = True
        return rp
    if response.status_code >= 400:
        # Diğer 4xx durumlarda robots.txt yok sayılır -> tamamen izinli.
        rp.allow_all = True
        return rp

    rp.parse(response.text.splitlines())
    return rp


def is_allowed(url: str, user_agent: str, timeout: float = 10.0) -> bool:
    """İlgili URL için robots.txt kurallarına göre erişime izin olup olmadığını
    döndürür. robots.txt hiç alınamazsa (ağ hatası vb.) temkinli davranmak
    yerine varsayılan olarak izinli kabul edilir, ancak durum loglanır."""
    parsed = urlparse(url)
    domain_root = f"{parsed.scheme}://{parsed.netloc}"
    cache_key = f"{domain_root}|{user_agent}"

    with _lock:
        rp = _robots_cache.get(cache_key, "MISSING")

    if rp == "MISSING":
        rp = _fetch_robots_parser(domain_root, user_agent, timeout)
        with _lock:
            _robots_cache[cache_key] = rp

    if rp is None:
        return True

    try:
        return rp.can_fetch(user_agent, url)
    except Exception:  # noqa: BLE001
        return True
