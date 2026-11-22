"""
Logging setup for Zoom Auto Recorder.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from src.config import LOG_DIR, LOG_FILE


def setup_logging(console_level=logging.INFO):
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler (rotating, keep up to 2 MB * 5)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # Console handler (visible when run from terminal)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(console_level)
    root.addHandler(console)

    return logging.getLogger("ZoomAutoRecorder")