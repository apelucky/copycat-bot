import logging
import os

from .config import AppConfig


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .bot.telegram_app import run
    run(AppConfig.from_env())


if __name__ == "__main__":
    main()
