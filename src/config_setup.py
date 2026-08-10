"""Uygulamanın ihtiyaç duyduğu TÜM API anahtarları/token'lar için ortak
"eksikse sor, doğrula, .env'e kaydet" akışı.

Kapsanan kimlik bilgileri:
  - GEMINI_API_KEY         -> özetleme + önem skorlama (src/summarizer.py),
                              config.yaml > summarizer.llm_provider VARSAYILANI ("gemini")
  - ANTHROPIC_API_KEY      -> özetleme + önem skorlama (src/summarizer.py),
                              yalnızca summarizer.llm_provider: "anthropic" ise gerekir
  - EVENTREGISTRY_API_KEY  -> Reuters/Bloomberg lisanslı erişimi (src/fetchers/licensed_aggregator.py)
  - TELEGRAM_BOT_TOKEN     -> Telegram bildirimleri (src/notifier.py)
  - TELEGRAM_CHAT_ID       -> Telegram bildirimleri (src/notifier.py)

Davranış (gereksinim):
  1. Yalnızca .env'de (ortam değişkeninde) değer YOKSA bu akış devreye girer.
     Bir kere girilmiş/doğrulanmış bir değer bir daha sorulmaz.
  2. Etkileşimli bir terminalde değilsek (stdin/stdout bir TTY değilse — ör.
     bir servis olarak veya çıktısı bir dosyaya yönlendirilmiş şekilde
     çalıştırılıyorsa) hiçbir şey SORULMAZ; eksik anahtar sadece loglanır ve
     ilgili özellik devre dışı bırakılır. Böylece uygulama `input()` üzerinde
     asla sonsuza kadar takılı kalmaz.
  3. Kullanıcı bir değer girerse, mümkünse gerçek bir API çağrısıyla
     doğrulanır (Gemini/Anthropic: ilgili SDK'nın `models.list()` çağrısı,
     Telegram bot: `get_me()`, Telegram chat_id: gerçek bir onay mesajı
     gönderme denemesi, EventRegistry: tek makalelik minimal bir sorgu).
     Geçersizse en fazla 3 deneme hakkı verilir.
  4. Kullanıcı boş bırakırsa VEYA 3 denemede de doğrulama başarısız olursa,
     ilgili özellik devre dışı bırakılır (net bir uyarı loglanır), uygulama
     ÇÖKMEZ — diğer her şey normal çalışmaya devam eder.
  5. Doğrulanan/kabul edilen değer `.env` dosyasına yazılır ki bir sonraki
     çalıştırmada tekrar sorulmasın.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

Validator = Callable[[str], tuple[bool, str]]


@dataclass
class CredentialSpec:
    env_var: str
    description: str  # "... için gerekli" cümlesinin devamı
    signup_url: str
    feature_name: str  # devre dışı kalacak özelliğin adı (log/mesajlarda kullanılır)
    validator: Validator | None  # None ise doğrulama yapılmaz, girilen değer olduğu gibi kabul edilir


# --------------------------------------------------------------------------
# Doğrulayıcılar — her biri (değer) -> (gecerli_mi, hata_veya_bilgi_mesaji) döner.
# Hiçbiri exception fırlatmaz (hepsi kendi try/except'ini içerir).
# --------------------------------------------------------------------------


def _validate_gemini_key(value: str) -> tuple[bool, str]:
    try:
        from google import genai
        from google.genai import errors as genai_errors
    except ImportError:
        return False, "'google-genai' paketi kurulu değil"
    try:
        client = genai.Client(api_key=value)
        next(iter(client.models.list()), None)  # ucuz, üretim maliyeti olmayan bir doğrulama çağrısı
        return True, "anahtar geçerli"
    except genai_errors.ClientError as exc:
        message = str(exc)
        if "API_KEY_INVALID" in message or "API key not valid" in message or "401" in message or "403" in message:
            return False, "geçersiz API anahtarı"
        return False, f"doğrulama sırasında hata: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"doğrulama sırasında hata: {exc}"


def _validate_anthropic_key(value: str) -> tuple[bool, str]:
    try:
        import anthropic
    except ImportError:
        return False, "'anthropic' paketi kurulu değil"
    try:
        client = anthropic.Anthropic(api_key=value)
        client.models.list(limit=1)  # ucuz, üretim maliyeti olmayan bir doğrulama çağrısı
        return True, "anahtar geçerli"
    except anthropic.AuthenticationError:
        return False, "geçersiz API anahtarı"
    except Exception as exc:  # noqa: BLE001
        return False, f"doğrulama sırasında hata: {exc}"


def _validate_eventregistry_key(value: str) -> tuple[bool, str]:
    try:
        from eventregistry import ArticleInfoFlags, EventRegistry, QueryArticlesIter, ReturnInfo
    except ImportError:
        return False, "'eventregistry' paketi kurulu değil"
    try:
        er = EventRegistry(apiKey=value, allowUseOfArchive=False, verboseOutput=False)
        query = QueryArticlesIter(sourceUri="reuters.com")
        return_info = ReturnInfo(articleInfo=ArticleInfoFlags(bodyLen=0, concepts=False, categories=False))
        # maxItems=1 -> minimal, tek makalelik bir doğrulama sorgusu (gerçek ama en ucuz çağrı)
        list(query.execQuery(er, maxItems=1, returnInfo=return_info))
        return True, "anahtar geçerli"
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "not valid" in message.lower() or "invalid" in message.lower() or "auth" in message.lower():
            return False, "geçersiz API anahtarı"
        return False, f"doğrulama sırasında hata: {exc}"


def _validate_telegram_bot_token(value: str) -> tuple[bool, str]:
    try:
        from telegram import Bot
        from telegram.error import TelegramError
    except ImportError:
        return False, "'python-telegram-bot' paketi kurulu değil"

    async def _check() -> str:
        async with Bot(token=value) as bot:
            me = await bot.get_me()
            return me.username or me.first_name

    try:
        username = asyncio.run(_check())
        return True, f"bot doğrulandı: @{username}"
    except TelegramError as exc:
        return False, f"geçersiz bot token: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"doğrulama sırasında hata: {exc}"


def _make_telegram_chat_id_validator(bot_token: str) -> Validator:
    """chat_id doğrulaması bot_token'a ihtiyaç duyduğundan (henüz .env'e
    yazılmamış olabilir), token'ı kapatma (closure) ile bağlıyoruz."""

    def _validate(value: str) -> tuple[bool, str]:
        try:
            from telegram import Bot
            from telegram.error import TelegramError
        except ImportError:
            return False, "'python-telegram-bot' paketi kurulu değil"

        async def _check() -> None:
            async with Bot(token=bot_token) as bot:
                await bot.send_message(
                    chat_id=value,
                    text=(
                        "✅ Finansal Haber Toplayıcı botu başarıyla bağlandı.\n"
                        "Önem skoru eşiği geçen haberler bundan sonra buraya bildirilecek."
                    ),
                )

        try:
            asyncio.run(_check())
            return True, "test mesajı gönderildi"
        except TelegramError as exc:
            return False, f"mesaj gönderilemedi (chat_id yanlış olabilir): {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"doğrulama sırasında hata: {exc}"

    return _validate


# --------------------------------------------------------------------------
# .env dosyasına yazma
# --------------------------------------------------------------------------


def _persist_to_env(env_path: Path, key: str, value: str) -> None:
    """`.env` dosyasında KEY=value satırını ekler ya da varsa yerinde günceller."""
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            found = True
            break

    if not found:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Tek bir kimlik bilgisi için sor/doğrula/kaydet akışı
# --------------------------------------------------------------------------


def _ensure_credential(spec: CredentialSpec, env_path: Path, interactive: bool) -> bool:
    current = os.environ.get(spec.env_var, "").strip()
    if current:
        return True  # zaten var -> gereksinim #5: bir daha sorulmaz

    if not interactive:
        logger.warning(
            "%s tanımlı değil (%s). %s devre dışı kalacak. Etkileşimli bir "
            "terminalde `python main.py` çalıştırırsanız girmeniz istenecektir; "
            "ya da .env dosyasına elle ekleyebilirsiniz.",
            spec.env_var,
            spec.description,
            spec.feature_name,
        )
        return False

    print()
    print(f"⚠  {spec.env_var} bulunamadı ({spec.description}).")
    print(f"   Buradan alabilirsiniz: {spec.signup_url}")

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            value = input(
                f"   Şimdi girin (boş bırakırsanız {spec.feature_name} devre dışı kalır): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            logger.warning("Girdi alınamadı, %s devre dışı bırakılıyor.", spec.feature_name)
            return False

        if not value:
            logger.warning("%s boş bırakıldı, %s devre dışı.", spec.env_var, spec.feature_name)
            return False

        if spec.validator is None:
            _persist_to_env(env_path, spec.env_var, value)
            os.environ[spec.env_var] = value
            print("   ✅ .env dosyasına kaydedildi.")
            return True

        print("   Doğrulanıyor...")
        is_valid, message = spec.validator(value)

        if is_valid:
            _persist_to_env(env_path, spec.env_var, value)
            os.environ[spec.env_var] = value
            print(f"   ✅ Doğrulandı ({message}) ve .env dosyasına kaydedildi.")
            return True

        print(f"   ❌ Doğrulama başarısız: {message}")
        if attempt < _MAX_ATTEMPTS:
            print(f"   Tekrar deneyin ({attempt}/{_MAX_ATTEMPTS})...")

    logger.warning(
        "%s için %d deneme de başarısız oldu, %s devre dışı bırakıldı.",
        spec.env_var,
        _MAX_ATTEMPTS,
        spec.feature_name,
    )
    return False


# --------------------------------------------------------------------------
# Kendi kendine üretilen sırlar (üçüncü taraf bir servisten ALINMAZ, bu
# yüzden ensure_all_credentials'daki "sor/doğrula" akışına uymaz)
# --------------------------------------------------------------------------


def ensure_webhook_secret(env_path: str | Path | None = None) -> str:
    """`WEBHOOK_INGEST_SECRET` .env'de yoksa kriptografik olarak güvenli
    rastgele bir değer üretip kaydeder (bkz. src/web/app.py > POST
    /api/webhook/kap-bildirim, src/fetchers/telegram_listener.py). Diğer
    kimlik bilgilerinin (API anahtarları) AKSİNE bu üçüncü taraf bir
    servisten ALINAN bir değer DEĞİL - kullanıcıya SORULMAZ/doğrulanmaz,
    tıpkı `src/db.py > create_api_key` gibi kendi kendine üretilir. Zaten
    tanımlıysa dokunmadan mevcut değeri döner (idempotent)."""
    path = Path(env_path) if env_path else _DEFAULT_ENV_PATH
    current = os.environ.get("WEBHOOK_INGEST_SECRET", "").strip()
    if current:
        return current

    if not path.exists():
        path.touch()

    value = secrets.token_urlsafe(32)
    _persist_to_env(path, "WEBHOOK_INGEST_SECRET", value)
    os.environ["WEBHOOK_INGEST_SECRET"] = value
    logger.info(
        "WEBHOOK_INGEST_SECRET otomatik üretildi ve .env'e kaydedildi "
        "(bkz. README > 'Event-Driven Ingestion: KAP Bildirimleri')."
    )
    return value


# --------------------------------------------------------------------------
# Genel giriş noktası
# --------------------------------------------------------------------------


def ensure_all_credentials(
    env_path: str | Path | None = None, llm_provider: str = "gemini"
) -> dict[str, bool]:
    """Uygulamanın ihtiyaç duyduğu kimlik bilgilerini sırayla kontrol eder.

    Her biri için: .env'de zaten varsa dokunmaz; yoksa (ve etkileşimli bir
    terminaldeysek) sorar, doğrular, kaydeder; yoksa/başarısızsa ilgili
    özelliği devre dışı bırakıp devam eder.

    `llm_provider` (config.yaml > summarizer.llm_provider, varsayılan
    "gemini") hangi özetleme API anahtarının sorulacağını belirler — sadece
    fiilen kullanılan sağlayıcının anahtarı istenir, diğeri hiç sorulmaz.

    Dönen sözlük: {env_var_adi: kullanilabilir_mi (bool)}
    """
    path = Path(env_path) if env_path else _DEFAULT_ENV_PATH
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if not path.exists():
        # .env yoksa boş bir tane oluştur ki _persist_to_env yazabilsin.
        path.touch()

    results: dict[str, bool] = {}

    if llm_provider == "anthropic":
        results["ANTHROPIC_API_KEY"] = _ensure_credential(
            CredentialSpec(
                env_var="ANTHROPIC_API_KEY",
                description="haber özetleme ve önem skorlama için gerekli",
                signup_url="https://console.anthropic.com",
                feature_name="Özetleme ve önem skorlama (Anthropic)",
                validator=_validate_anthropic_key,
            ),
            path,
            interactive,
        )
    else:
        results["GEMINI_API_KEY"] = _ensure_credential(
            CredentialSpec(
                env_var="GEMINI_API_KEY",
                description=(
                    "haber özetleme ve önem skorlama için gerekli "
                    "(varsayılan sağlayıcı, bkz. config.yaml > summarizer.llm_provider)"
                ),
                signup_url="https://aistudio.google.com/apikey",
                feature_name="Özetleme ve önem skorlama (Gemini)",
                validator=_validate_gemini_key,
            ),
            path,
            interactive,
        )

    results["EVENTREGISTRY_API_KEY"] = _ensure_credential(
        CredentialSpec(
            env_var="EVENTREGISTRY_API_KEY",
            description="Reuters/Bloomberg lisanslı erişimi (NewsAPI.ai) için gerekli",
            signup_url="https://www.newsapi.ai/register",
            feature_name="Reuters/Bloomberg (NewsAPI.ai) kaynağı",
            validator=_validate_eventregistry_key,
        ),
        path,
        interactive,
    )

    bot_ok = _ensure_credential(
        CredentialSpec(
            env_var="TELEGRAM_BOT_TOKEN",
            description="önemli haberler için Telegram bildirimi göndermek üzere gerekli",
            signup_url="https://t.me/BotFather ile /newbot komutu",
            feature_name="Telegram bildirimleri",
            validator=_validate_telegram_bot_token,
        ),
        path,
        interactive,
    )
    results["TELEGRAM_BOT_TOKEN"] = bot_ok

    if bot_ok:
        bot_token_value = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        results["TELEGRAM_CHAT_ID"] = _ensure_credential(
            CredentialSpec(
                env_var="TELEGRAM_CHAT_ID",
                description="bildirimlerin gönderileceği sohbet/kanal kimliği",
                signup_url="https://api.telegram.org/bot<TOKEN>/getUpdates (bkz. README > Telegram Bildirimi)",
                feature_name="Telegram bildirimleri",
                validator=_make_telegram_chat_id_validator(bot_token_value),
            ),
            path,
            interactive,
        )
    else:
        # Bot token yoksa/geçersizse chat_id sormanın anlamı yok.
        logger.warning(
            "TELEGRAM_BOT_TOKEN olmadığı için TELEGRAM_CHAT_ID sorulmuyor. "
            "Telegram bildirimleri devre dışı."
        )
        results["TELEGRAM_CHAT_ID"] = False

    return results
