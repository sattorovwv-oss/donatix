import argparse
import logging
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Match the original CLI: load the existing .env before choosing worker count.
    from donatix.config import Config
    Config.from_env()
    raw = os.environ.get("DONATIX_WEB_WORKERS", "").strip()
    workers = int(raw) if raw.isdigit() and int(raw) > 0 else max(1, min(4, (os.cpu_count() or 1) - 1))
    import uvicorn
    uvicorn.run("donatix_android_extension.factory:create_app", factory=True,
                host=args.host, port=args.port, workers=workers,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
