"""Entry point: python -m ai_ops_agent"""

import logging

from .telegram_bot import build_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Ops agent starting")
    app = build_application()
    app.run_polling()


if __name__ == "__main__":
    main()
