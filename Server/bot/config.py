"""Настройки бота. Читаются из окружения или из файла .env рядом с проектом."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Шрифты с кириллицей — берём первый существующий.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
    "C:/Windows/Fonts/DejaVuSans.ttf",
)
FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "/Library/Fonts/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
)


def _first_existing(paths: tuple[str, ...]) -> str | None:
    for p in paths:
        if Path(p).is_file():
            return p
    return None


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError as exc:
                raise ValueError(f"ALLOWED_USER_IDS: {part!r} — это не число") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    openai_api_key: str
    anthropic_api_key: str

    data_dir: Path
    tz_name: str = "Asia/Dushanbe"
    default_currency: str = "TJS"
    anthropic_model: str = "claude-opus-5"
    whisper_model: str = "whisper-1"
    max_voice_seconds: int = 300
    max_tool_iterations: int = 8
    log_level: str = "INFO"
    font_path: str | None = field(default=None)
    font_bold_path: str | None = field(default=None)

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.photos_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config(require_secrets: bool = True) -> Config:
    """Собрать конфиг. require_secrets=False — для тестов и офлайн-проверок."""
    load_dotenv(ROOT / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if require_secrets:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", token),
                ("ALLOWED_USER_IDS", allowed_raw),
                ("OPENAI_API_KEY", openai_key),
                ("ANTHROPIC_API_KEY", anthropic_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Не заданы обязательные настройки: "
                + ", ".join(missing)
                + ".\nСкопируй .env.example в .env и заполни его."
            )

    data_dir = Path(os.getenv("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    return Config(
        telegram_token=token,
        allowed_user_ids=_parse_ids(allowed_raw),
        openai_api_key=openai_key,
        anthropic_api_key=anthropic_key,
        data_dir=data_dir,
        tz_name=os.getenv("TZ", "Asia/Dushanbe").strip() or "Asia/Dushanbe",
        default_currency=os.getenv("DEFAULT_CURRENCY", "TJS").strip().upper() or "TJS",
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5").strip(),
        whisper_model=os.getenv("WHISPER_MODEL", "whisper-1").strip(),
        max_voice_seconds=int(os.getenv("MAX_VOICE_SECONDS", "300")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        font_path=os.getenv("FONT_PATH") or _first_existing(FONT_CANDIDATES),
        font_bold_path=os.getenv("FONT_BOLD_PATH") or _first_existing(FONT_BOLD_CANDIDATES),
    )
