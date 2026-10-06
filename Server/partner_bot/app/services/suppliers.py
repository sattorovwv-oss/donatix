"""Какой ключ поставщика использовать для какого товара.

У партнёров разные счета: звёзды и Premium идут с одного, игры — с другого.
Ключ решает, чей баланс тратится, поэтому выбирать его надо до заказа,
а не разбираться потом, кто кому должен.

Клиенты кэшируются по ключу: каждый держит своё HTTP-соединение, и плодить
их на каждый заказ незачем.
"""
from __future__ import annotations

import logging

from app import runtime
from app.config import settings
from app.services.fragment import DeliveryProvider

log = logging.getLogger(__name__)

_clients: dict[str, DeliveryProvider] = {}


def games_key() -> str:
    """Ключ для игр. Пусто — игры идут с основного счёта.

    Панель важнее .env: ключ можно поменять на ходу, не трогая сервер.
    """
    return (runtime.get("fazer_games_key")
            or settings.fazer_games_key or "").strip()


def has_own_games_key() -> bool:
    key = games_key()
    return bool(key) and key != settings.fazer_api_key.strip()


def for_games(default: DeliveryProvider) -> DeliveryProvider:
    """Провайдер для игровых заказов."""
    if not has_own_games_key():
        return default

    key = games_key()
    client = _clients.get(key)
    if client is None:
        from app.services.fazer import FazerProvider

        client = FazerProvider(api_key=key)
        _clients[key] = client
        log.info("Игры: используется отдельный ключ поставщика")
    return client


def for_api(default: DeliveryProvider) -> DeliveryProvider:
    """Провайдер для заказов через API — со счёта игр, что бы ни заказали.

    Разработчики покупают у нас всё подряд: игры, звёзды, Premium. Пусть
    это будет один счёт — так владельцу видно, сколько API съело, одной
    цифрой, а не разностью двух.

    Подменяем, только если основная выдача сама на FazerCards. При
    mystars звёзды уходят другим сервисом, и отдать их фазеровскому
    ключу значило бы сменить поставщика молча — такое не делается
    мимоходом.
    """
    from app.services.fragment import FAZER_MODES, mode_now

    if mode_now() not in FAZER_MODES:
        return default
    return for_games(default)


async def close_all() -> None:
    for client in _clients.values():
        await client.close()
    _clients.clear()


def forget() -> None:
    """Забыть клиентов — нужно после смены ключа в панели."""
    _clients.clear()
