import json
import re

from donatix import cache
from donatix.app import cache_policy


def test_home_has_seo_tags(client):
    html = client.get("/").text
    assert 'rel="canonical"' in html and 'property="og:image"' in html
    assert 'content="index, follow' in html
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
    types = {g["@type"] for g in ld["@graph"]}
    assert {"Organization", "WebSite", "FAQPage"} <= types


def test_private_pages_are_noindex(client):
    assert 'content="noindex' in client.get("/login").text
    assert 'content="noindex' in client.get("/nope-404").text


def test_robots_and_sitemap(client):
    r = client.get("/robots.txt")
    assert "Disallow: /panel" in r.text and "Sitemap:" in r.text
    s = client.get("/sitemap.xml")
    assert s.headers["content-type"].startswith("application/xml")
    assert s.text.count("<url>") == 5 and "/docs</loc>" in s.text


def test_cache_headers_and_gzip(client):
    r = client.get("/static/donatix.css?v=1", headers={"Accept-Encoding": "gzip"})
    assert "immutable" in r.headers["cache-control"]
    assert r.headers.get("content-encoding") == "gzip"
    assert client.get("/login").headers["cache-control"] == "private, no-store"
    assert client.get("/").headers["cache-control"] == "private, no-cache"
    assert cache_policy("/media/a.webp", "", 200).startswith("public")
    assert cache_policy("/", "", 404) == "no-store"


def test_memory_cache_expires_and_clears():
    calls = []
    make = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert cache.get_or_set("k", 60, make) == 1
    assert cache.get_or_set("k", 60, make) == 1
    cache.clear()
    assert cache.get_or_set("k", 60, make) == 2
    assert cache.get_or_set("z", -1, make) == 3 and cache.get_or_set("z", -1, make) == 4


def test_search_console_verification_tag(client, app):
    import dataclasses
    app.state.config = dataclasses.replace(app.state.config, google_verify="abc123", yandex_verify="y777")
    html = client.get("/").text
    assert '<meta name="google-site-verification" content="abc123">' in html
    assert '<meta name="yandex-verification" content="y777">' in html


def test_legal_pages(client):
    assert "Политика конфиденциальности" in client.get("/privacy").text
    terms = client.get("/terms").text
    assert "Пользовательское соглашение" in terms and "Коротко о главном" in terms and "Возвраты" in terms
    assert 'href="/privacy"' in client.get("/").text
