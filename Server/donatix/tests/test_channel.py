import pytest
from conftest import make_client, web_login

from donatix import sitecfg

CH = "https://t.me/+NdkoYArkuCw4NzBi"


def test_channel_everywhere_on_site(client, conn):
    home = client.get("/").text
    assert 'id="channel"' in home and "Уроки, акции и новости" in home
    assert home.count(f'href="{CH}"') >= 4          # шапка, баннер, подвал, плавающая кнопка
    assert 'class="tg-fab"' in client.get("/terms").text
    make_client(conn)
    web_login(client, "shop1@example.com", "password123")
    panel = client.get("/panel").text
    assert 'class="side-tg"' in panel and 'class="tg-fab"' not in panel   # в кабинете — карточка, без кнопки


def test_clean_channel_and_hide(client, conn, config):
    assert sitecfg.clean_channel("@donatix_tj") == "https://t.me/donatix_tj"
    assert sitecfg.clean_channel("t.me/+AbCdEf123") == "https://t.me/+AbCdEf123"
    assert sitecfg.clean_channel("") == ""
    with pytest.raises(sitecfg.SettingsError):
        sitecfg.clean_channel("https://evil.example.com/x")
    sitecfg.save(conn, config, {"tg_channel": ""})
    sitecfg.load(conn, config)
    assert 'id="channel"' not in client.get("/").text and "tg-fab" not in client.get("/").text
