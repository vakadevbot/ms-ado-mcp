"""Local diagnostics: only explicitly safe application messages enter the file."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("ms_ado_mcp.diagnostics")
logger.addHandler(logging.NullHandler())
logger.propagate = False


def configure_error_log(env_file: Path) -> Path:
    path = env_file.parent / "logs" / "ms-ado-mcp.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for old in logger.handlers[:]:
        logger.removeHandler(old)
        old.close()
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    return path
