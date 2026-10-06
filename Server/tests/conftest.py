import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import db as db_module  # noqa: E402
from bot.config import Config  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    connection = db_module.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def config(tmp_path):
    from bot.config import _first_existing, FONT_CANDIDATES, FONT_BOLD_CANDIDATES

    return Config(
        telegram_token="x",
        allowed_user_ids=frozenset({1}),
        openai_api_key="x",
        anthropic_api_key="x",
        data_dir=tmp_path / "data",
        font_path=_first_existing(FONT_CANDIDATES),
        font_bold_path=_first_existing(FONT_BOLD_CANDIDATES),
    )


# ── общий стенд для сквозных тестов ────────────────────────────────────────

import importlib  # noqa: E402
import io  # noqa: E402
from datetime import datetime  # noqa: E402

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from aiogram.types import Chat, Message, Update, User  # noqa: E402

from bot import access, db, handlers  # noqa: E402
from bot import admin as admin_module  # noqa: E402
from bot.brain import Brain  # noqa: E402
from bot.stt import Transcriber  # noqa: E402

TOKEN = "42:TESTTESTTESTTESTTESTTESTTESTTESTTEST"
OWNER = 111
STRANGER = 222


class RecordingSession:
    """Перехватывает исходящие вызовы вместо похода в Telegram."""

    def __init__(self):
        self.sent = []

    async def __call__(self, bot, method, timeout=None):
        self.sent.append(method)
        return self._fake_result(method)

    async def close(self):
        pass

    @staticmethod
    def _fake_result(method):
        chat = Chat(id=OWNER, type="private")
        return Message(message_id=1, date=datetime.now(), chat=chat,
                       text=getattr(method, "text", None))

    @property
    def texts(self):
        return [m.text for m in self.sent if isinstance(m, SendMessage)]


class ScriptedBrain(Brain):
    """Вместо Claude — заранее заданный ответ."""

    def __init__(self, reply="Записал."):
        self.reply = reply
        self.seen = []

    async def handle(self, chat_id, user_text, *, source="text", editing_transaction_id=None):
        from bot.tools import TurnResult

        self.seen.append((user_text, source, editing_transaction_id))
        return TurnResult(reply=self.reply)


class ScriptedTranscriber(Transcriber):
    def __init__(self, text="отправил Абубакру три тысячи сомони"):
        self.text = text

    async def transcribe(self, audio, filename="voice.ogg"):
        return self.text


def make_bot():
    """Поддельный Bot: ничего не уходит в Телеграм, всё складывается в session."""
    session = RecordingSession()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=None))
    bot.session = session

    async def fake_download(file_id, destination=None, **kwargs):
        if destination is not None:  # фото: пишем заглушку на диск
            Path(destination).write_bytes(b"\xff\xd8\xff\xd9")
            return None
        return io.BytesIO(b"OggS-fake-audio")

    bot.download = fake_download
    return bot, session


@pytest.fixture
def env(conn, config):
    # Роутеры aiogram привязываются к одному Dispatcher, а состояние
    # «жду правку» и «жду id» живёт в модулях — пересоздаём их для каждого теста.
    importlib.reload(admin_module)
    importlib.reload(handlers)
    config.ensure_dirs()
    config = type(config)(**{**config.__dict__, "allowed_user_ids": frozenset({OWNER})})

    # Владелец — админ и уже зарегистрирован.
    access.bootstrap_admins(conn, frozenset({OWNER}))
    db.register_user(conn, OWNER, "Алиджон")

    bot, session = make_bot()

    brain = ScriptedBrain()
    dispatcher = Dispatcher()

    guard = access.AccessMiddleware(conn)
    dispatcher.message.outer_middleware(guard)
    dispatcher.callback_query.outer_middleware(guard)

    dispatcher.include_router(admin_module.router)
    dispatcher.include_router(handlers.router)
    dispatcher["config"] = config
    dispatcher["bot_username"] = "moneybot"
    dispatcher["conn"] = conn
    dispatcher["brain"] = brain
    dispatcher["stt"] = ScriptedTranscriber()

    return dispatcher, bot, session, brain, conn, guard


def text_update(text, user_id=OWNER, update_id=1):
    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="Алиджон")
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(), chat=chat, from_user=user, text=text,
    ))


def voice_update(user_id=OWNER, duration=5):
    from aiogram.types import Voice

    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="Алиджон")
    return Update(update_id=2, message=Message(
        message_id=2, date=datetime.now(), chat=chat, from_user=user,
        voice=Voice(file_id="v1", file_unique_id="u1", duration=duration),
    ))




def photo_update(caption=None, update_id=6, user_id=OWNER):
    from aiogram.types import PhotoSize

    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="Алиджон")
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(), chat=chat, from_user=user,
        caption=caption,
        photo=[
            PhotoSize(file_id="p_small", file_unique_id="us", width=90, height=90, file_size=100),
            PhotoSize(file_id="p_big", file_unique_id="ub", width=1280, height=960, file_size=9000),
        ],
    ))



@pytest.fixture
def setup(env):
    """Старое имя стенда — оставлено, чтобы не переписывать существующие тесты."""
    dispatcher, bot, session, brain, conn, _guard = env
    return dispatcher, bot, session, brain, conn
