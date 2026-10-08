"""Run original regression tests through the optional wrapper, with mock suppliers."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("site", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.site.resolve()))
    from donatix_android_extension.factory import create_app
    import donatix.app
    import pytest

    def wrapped(config=None, supplier=None):
        return create_app(config, supplier, store_path=Path(config.db_path).parent / "android.db", start_push=False)

    donatix.app.create_app = wrapped
    names = ["google", "web", "security", "api_orders", "finance", "payments", "webpush", "notify_push"]
    return pytest.main(["-q", "--disable-warnings"] + [str(args.site / "donatix/tests" / f"test_{n}.py") for n in names])


if __name__ == "__main__":
    raise SystemExit(main())
