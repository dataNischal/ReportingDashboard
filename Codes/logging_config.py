"""
logging_config.py -- centralized `logging` setup shared by the ETL CLI
(postgres_pipeline.py) and the FastAPI app (api/main.py). Replaces this
project's previous convention of raw print() calls for pipeline
diagnostics -- print() has no severity levels, no timestamps, and can't be
filtered/redirected independently of stdout.
"""

import logging
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """
    Idempotent -- safe to call from multiple entrypoints (the ETL CLI's
    __main__ block AND the API's lifespan startup) without duplicating
    handlers, since a second call would otherwise double every log line.
    """
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
