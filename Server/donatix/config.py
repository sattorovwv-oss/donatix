"""Настройки Donatix. Читаются из окружения или из файла .env в папке donatix/."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TIERS = ("bronze", "silver", "gold")

# Способы пополнения баланса: код → (название, валюта перевода).
PAY_METHODS = {
    "alif": ("Алиф (Alif Mobi)", "TJS"),
    "dc": ("Душанбе Сити (DC)", "TJS"),
    "eskhata": ("Эсхата", "TJS"),
    "korti_milli": ("Корти Милли", "TJS"),
    "usdt_trc20": ("USDT TRC20 (Tron)", "USDT"),
    "usdt_bep20": ("USDT BEP20 (BNB Chain)", "USDT"),
    "binance": ("Binance Pay", "USDT"),
}


def _load_dotenv(path: Path) -> None:
    """Мини-загрузчик .env: KEY=VALUE, комментарии через #. Окружение важнее файла."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


@dataclass
class Config:
    secret_key: str
    db_path: Path
    site_name: str = "Donatix"
    base_url: str = "http://localhost:8000"

    # Поставщик: "fazer" — настоящий FazerCards, "mock" — игрушечный для разработки.
    supplier: str = "mock"
    fazer_api_key: str = ""
    fazer_base_url: str = "https://api.fzr.cards/api/v2"
    # Скидка вашего тарифа FazerCards на пополнение Steam, %.
    fazer_steam_discount: Decimal = Decimal("2.5")
    # Адрес, к которому дописываются пути картинок поставщика (пусто — домен из FAZER_BASE_URL)
    fazer_image_base: str = ""
    # Пауза между запросами каталога, сек. Больше — медленнее загрузка, но другим ботам на том же ключе
    # остаётся больше лимита FazerCards.
    fazer_catalog_pause: float = 2.0
    # CoinDrop (coindrop.uz) — второй поставщик, игры по ID (Standoff 2 и др.). Пусто — выключен.
    coindrop_api_key: str = ""
    coindrop_base_url: str = "https://coindrop.uz/api/v1"
    coindrop_games: str = ""          # список game_key через запятую; пусто — все игры-пополнения
    # Vendoria (vendoria.amadeustech.dev) — ещё один поставщик игр; берём только перечисленные игры
    vendoria_token: str = ""
    vendoria_base_url: str = "https://vendoria.amadeustech.dev"
    vendoria_games: str = "Standoff 2,Clash of Clans"

    # Наценка в процентах поверх закупочной цены, по уровням клиентов.
    markups: dict[str, Decimal] = field(
        default_factory=lambda: {"bronze": Decimal("8"), "silver": Decimal("8"), "gold": Decimal("8")}
    )

    # Своя наценка для вида товара (вместо наценки уровня). У Steam маржа тонкая.
    kind_markups: dict[str, Decimal] = field(default_factory=lambda: {"steam_topup": Decimal("1.5")})

    # Первый админ создаётся при запуске, если таких пользователей ещё нет.
    admin_email: str = ""
    admin_password: str = ""

    # Фоновый обработчик (проверка заказов, обновление каталога) внутри веб-процесса.
    run_worker: bool = True
    catalog_sync_minutes: int = 60
    order_poll_seconds: int = 20

    # Предупреждение, когда баланс у поставщика меньше этой суммы (USD).
    supplier_low_balance: Decimal = Decimal("50")
    alert_telegram_token: str = ""
    alert_telegram_chat_id: str = ""

    # Новые клиенты ждут одобрения админа.
    require_approval: bool = False   # новые клиенты активны сразу; админ проверяет только чеки
    cookie_secure: bool = False

    # Способы пополнения: код → реквизиты (показываются клиенту). Пустые не показываются.
    pay_methods: dict[str, str] = field(default_factory=dict)
    # Курс сомони за 1 USD — для способов оплаты в TJS.
    tjs_rate: Decimal = Decimal("10.9")
    # Курс берётся сам из открытых источников (каждые 5 минут, на странице оплаты — каждые 30 с)
    rate_auto: bool = True
    # Не больше стольких запросов к поставщику в минуту на весь проект (очередь, не отказ)
    supplier_rate_per_min: int = 50
    rate_margin_pct: Decimal = Decimal("1")
    pay_min_usd: Decimal = Decimal("5")
    # Минимальное пополнение в сомони и порог «мало денег» на балансе клиента, $
    pay_min_tjs: Decimal = Decimal("0")
    low_balance_usd: Decimal = Decimal("10")

    # Почта для уведомлений клиентам (необязательно).
    # Бот поддержки с AI (OpenAI): токен бота, ключ и модель
    support_bot_token: str = ""
    flashtopup_api_id: str = ""     # FlashTopup — только поиск ника по ID игрока
    flashtopup_api_key: str = ""
    shop_admin_ids: str = ""        # кому в боте-магазине видна админ-панель (через запятую)
    android_package: str = ""       # приложение для Android (TWA): имя пакета, например tj.donatix.app
    android_sha256: str = ""        # отпечатки ключа подписи APK через запятую — для /.well-known/assetlinks.json
    shop_bot_token: str = ""        # бот-магазин проекта (покупки кнопками в Telegram)
    openai_api_key: str = ""
    support_model: str = "gpt-4o-mini"
    receipt_model: str = "gpt-4o"      # читает чеки пополнений (модель с «зрением»)
    support_admin_id: str = ""   # Telegram ID, куда бот поддержки шлёт обращения (по умолчанию — чат админ-бота)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Куда писать клиентам для пополнения баланса и поддержки (например, @donatix_support).
    support_contact: str = ""
    # Официальный Telegram-канал: баннер на главной, плавающая кнопка, шапка и кабинет
    tg_channel: str = "https://t.me/+NdkoYArkuCw4NzBi"
    # Код подтверждения сайта из Google Search Console и Яндекс Вебмастера (метатег)
    google_verify: str = ""
    # Вход через Google (OAuth). Ключи — в Google Cloud Console → Credentials
    google_client_id: str = ""
    google_client_secret: str = ""
    # Автоплатёж криптой: Binance Pay (ключи мерчанта) и TronGrid (необязательный ключ — выше лимиты)
    binance_pay_key: str = ""
    binance_pay_secret: str = ""
    trongrid_key: str = ""
    # Bybit: ключ API вашего аккаунта «только чтение» (Wallet/Asset) — видеть входящие USDT
    bybit_key: str = ""
    bybit_secret: str = ""
    yandex_verify: str = ""
    # Часовой пояс для аналитики, часы от UTC (Душанбе — 5)
    tz_offset: int = 5
    # Конструктор ботов: запускать ботов партнёров и по какому адресу они ходят в Donatix
    run_bots: bool = True
    internal_url: str = "http://127.0.0.1:8000"

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv(ROOT / ".env")
        secret = _env("DONATIX_SECRET_KEY")
        if not secret:
            # Без ключа сессии слетают при каждом перезапуске — годится только для разработки.
            secret = secrets.token_urlsafe(32)
        markups = {}
        for tier, default in (("bronze", "8"), ("silver", "8"), ("gold", "8")):
            markups[tier] = Decimal(_env(f"DONATIX_MARKUP_{tier.upper()}", default))
        pay_methods = {code: _env(f"DONATIX_PAY_{code.upper()}") for code in PAY_METHODS}
        pay_methods = {k: v.replace("\\n", "\n") for k, v in pay_methods.items() if v}
        kind_markups = {"steam_topup": Decimal(_env("DONATIX_MARKUP_STEAM", "1.5"))}
        if _env("DONATIX_MARKUP_STEAM_GIFT"):
            kind_markups["steam_gift"] = Decimal(_env("DONATIX_MARKUP_STEAM_GIFT"))
        return cls(
            secret_key=secret,
            db_path=Path(_env("DONATIX_DB", str(ROOT / "data" / "donatix.db"))),
            site_name=_env("DONATIX_SITE_NAME", "Donatix"),
            base_url=_env("DONATIX_BASE_URL", "http://localhost:8000").rstrip("/"),
            supplier=_env("DONATIX_SUPPLIER", "mock").lower(),
            fazer_api_key=_env("FAZER_API_KEY"),
            fazer_base_url=_env("FAZER_BASE_URL", "https://api.fzr.cards/api/v2").rstrip("/"),
            fazer_steam_discount=Decimal(_env("FAZER_STEAM_DISCOUNT", "2.5")),
            fazer_image_base=_env("FAZER_IMAGE_BASE"),
            fazer_catalog_pause=float(_env("FAZER_CATALOG_PAUSE") or 2.0),
            coindrop_api_key=_env("DONATIX_COINDROP_API_KEY"),
            coindrop_base_url=_env("DONATIX_COINDROP_BASE_URL", "https://coindrop.uz/api/v1").rstrip("/"),
            coindrop_games=_env("DONATIX_COINDROP_GAMES"),
            vendoria_token=_env("DONATIX_VENDORIA_TOKEN"),
            vendoria_base_url=_env("DONATIX_VENDORIA_BASE_URL", "https://vendoria.amadeustech.dev").rstrip("/"),
            vendoria_games=_env("DONATIX_VENDORIA_GAMES", "Standoff 2,Clash of Clans"),
            markups=markups,
            kind_markups=kind_markups,
            admin_email=_env("DONATIX_ADMIN_EMAIL").lower(),
            admin_password=_env("DONATIX_ADMIN_PASSWORD"),
            run_worker=_flag("DONATIX_RUN_WORKER", True),
            # не чаще раза в 30 минут: полная загрузка каталога съедает лимит ключа FazerCards
            catalog_sync_minutes=max(30, int(_env("DONATIX_CATALOG_SYNC_MINUTES", "60"))),
            order_poll_seconds=int(_env("DONATIX_ORDER_POLL_SECONDS", "20")),
            supplier_low_balance=Decimal(_env("DONATIX_SUPPLIER_LOW_BALANCE", "50")),
            alert_telegram_token=_env("DONATIX_ALERT_TELEGRAM_TOKEN"),
            alert_telegram_chat_id=_env("DONATIX_ALERT_TELEGRAM_CHAT_ID"),
            require_approval=_flag("DONATIX_REQUIRE_APPROVAL", False),
            cookie_secure=_flag("DONATIX_COOKIE_SECURE", False),
            support_contact=_env("DONATIX_SUPPORT_CONTACT"),
            tg_channel=_env("DONATIX_TG_CHANNEL") or "https://t.me/+NdkoYArkuCw4NzBi",
            google_verify=_env("DONATIX_GOOGLE_VERIFY"),
            google_client_id=_env("DONATIX_GOOGLE_CLIENT_ID"),
            google_client_secret=_env("DONATIX_GOOGLE_CLIENT_SECRET"),
            binance_pay_key=_env("DONATIX_BINANCE_PAY_KEY"),
            binance_pay_secret=_env("DONATIX_BINANCE_PAY_SECRET"),
            trongrid_key=_env("DONATIX_TRONGRID_KEY"),
            bybit_key=_env("DONATIX_BYBIT_KEY"),
            bybit_secret=_env("DONATIX_BYBIT_SECRET"),
            yandex_verify=_env("DONATIX_YANDEX_VERIFY"),
            tz_offset=int(_env("DONATIX_TZ_OFFSET") or 5),
            run_bots=_flag("DONATIX_RUN_BOTS", True),
            internal_url=_env("DONATIX_INTERNAL_URL") or "http://127.0.0.1:8000",
            pay_methods=pay_methods,
            tjs_rate=Decimal(_env("DONATIX_TJS_RATE", "10.9")),
            rate_auto=_env("DONATIX_RATE_AUTO", "1") not in ("0", "false", "no"),
            supplier_rate_per_min=int(_env("DONATIX_SUPPLIER_RATE", "50") or 50),
            rate_margin_pct=Decimal(_env("DONATIX_RATE_MARGIN", "1")),
            pay_min_usd=Decimal(_env("DONATIX_PAY_MIN_USD", "5")),
            pay_min_tjs=Decimal(_env("DONATIX_PAY_MIN_TJS", "0")),
            low_balance_usd=Decimal(_env("DONATIX_LOW_BALANCE_USD", "10")),
            support_bot_token=_env("DONATIX_SUPPORT_BOT_TOKEN"),
            shop_bot_token=_env("DONATIX_SHOP_BOT_TOKEN"),
            shop_admin_ids=_env("DONATIX_SHOP_ADMIN_IDS"),
            android_package=_env("DONATIX_ANDROID_PACKAGE"),
            android_sha256=_env("DONATIX_ANDROID_SHA256"),
            flashtopup_api_id=_env("DONATIX_FLASHTOPUP_API_ID"),
            flashtopup_api_key=_env("DONATIX_FLASHTOPUP_API_KEY"),
            openai_api_key=_env("DONATIX_OPENAI_API_KEY") or _env("OPENAI_API_KEY"),
            support_model=_env("DONATIX_SUPPORT_MODEL") or "gpt-4o-mini",
            receipt_model=_env("DONATIX_RECEIPT_MODEL") or "gpt-4o",
            support_admin_id=_env("DONATIX_SUPPORT_ADMIN_ID"),
            smtp_host=_env("DONATIX_SMTP_HOST"),
            smtp_port=int(_env("DONATIX_SMTP_PORT", "587")),
            smtp_user=_env("DONATIX_SMTP_USER"),
            smtp_password=_env("DONATIX_SMTP_PASSWORD"),
            smtp_from=_env("DONATIX_SMTP_FROM"),
        )
