import pytest
from donatix.production import validate


def configure(config, monkeypatch):
    config.secret_key = 's' * 48
    monkeypatch.setenv('DONATIX_SECRET_KEY', config.secret_key)
    config.supplier, config.fazer_api_key = 'fazer', 'test-never-sent'
    config.base_url, config.cookie_secure, config.run_worker = 'https://example.com', True, True


def test_production_rejects_mock_supplier(config, monkeypatch):
    configure(config, monkeypatch)
    config.supplier = 'mock'
    with pytest.raises(RuntimeError, match='DONATIX_SUPPLIER'):
        validate(config)


def test_production_rejects_unsafe_session_and_http(config, monkeypatch):
    configure(config, monkeypatch)
    config.cookie_secure = False
    config.base_url = 'http://example.com'
    monkeypatch.delenv('DONATIX_SECRET_KEY')
    with pytest.raises(RuntimeError) as error:
        validate(config)
    assert 'DONATIX_COOKIE_SECURE' in str(error.value) and 'DONATIX_SECRET_KEY' in str(error.value)


def test_production_accepts_complete_configuration_without_supplier_requests(config, monkeypatch):
    configure(config, monkeypatch)
    validate(config)
