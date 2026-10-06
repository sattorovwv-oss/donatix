"""Production launcher: a real-money server cannot silently use MockSupplier."""
from __future__ import annotations
import os
import sys
from urllib.parse import urlparse
from .config import Config


def validate(config: Config) -> None:
    failures = []
    if config.supplier != 'fazer' or not config.fazer_api_key:
        failures.append('DONATIX_SUPPLIER=fazer и настоящий FAZER_API_KEY')
    if len(config.secret_key) < 32 or not os.environ.get('DONATIX_SECRET_KEY'):
        failures.append('постоянный DONATIX_SECRET_KEY длиной минимум 32 символа')
    origin = urlparse(config.base_url)
    if origin.scheme != 'https' or not origin.hostname or origin.username or origin.query or origin.fragment or origin.path not in ('', '/'):
        failures.append('DONATIX_BASE_URL с HTTPS без логина и query-параметров')
    if not config.cookie_secure:
        failures.append('DONATIX_COOKIE_SECURE=1')
    if not config.run_worker:
        failures.append('DONATIX_RUN_WORKER=1 для выдачи и проверки заказов')
    if not config.db_path.is_absolute():
        failures.append('абсолютный путь DONATIX_DB')
    if not config.db_path.exists() and (not config.admin_email or len(config.admin_password) < 12):
        failures.append('DONATIX_ADMIN_EMAIL и пароль минимум 12 символов для первого запуска')
    if failures:
        raise RuntimeError('Сервер не запущен. Требуется: ' + '; '.join(failures) + '.')


def main() -> int:
    os.environ['DONATIX_PRODUCTION'] = '1'
    try:
        validate(Config.from_env())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if sys.argv[1:] == ['check-config']:
        print('Производственные настройки прошли проверку. Доступность поставщика проверяется отдельно.')
        return 0
    from .__main__ import main as run
    return run(sys.argv[1:] or ['serve'])


if __name__ == '__main__':
    raise SystemExit(main())
