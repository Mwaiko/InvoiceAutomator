
import logging
import sys
from app.config import settings


def setup_logging() -> None:
    level = logging.DEBUG if settings.app_debug else logging.INFO

    handler = logging.StreamHandler(sys.stdout)

    if settings.is_production:
        try:
            import json_log_formatter
            formatter = json_log_formatter.JSONFormatter()
        except ImportError:
            formatter = logging.Formatter(
                "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
            )
    else:
        formatter = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )

    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.app_debug else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)