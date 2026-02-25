import json
import logging
import os
import sys
import traceback
from datetime import datetime
from typing import Any, Dict, Optional


class JsonFormatter(logging.Formatter):
    """Formatter that outputs JSON strings after parsing the log record."""

    def __init__(self, **kwargs):
        self.default_keys = {
            "timestamp": "asctime",
            "level": "levelname",
            "message": "message",
        }
        self.default_keys.update(kwargs)

    def format(self, record: logging.LogRecord) -> str:
        log_record = {}

        log_record["timestamp"] = datetime.utcnow().isoformat() + "Z"
        log_record["level"] = record.levelname
        log_record["logger"] = record.name

        if isinstance(record.msg, dict):
            log_record["message"] = record.msg.get("message", "")
            for key, value in record.msg.items():
                if key != "message":
                    log_record[key] = value
        else:
            log_record["message"] = record.getMessage()

        if record.exc_info:
            log_record["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }

        for key, value in record.__dict__.items():
            if key not in [
                "msg", "args", "exc_info", "exc_text", "stack_info", "lineno",
                "funcName", "created", "msecs", "relativeCreated", "levelname",
                "levelno", "pathname", "filename", "module", "name", "thread",
                "threadName", "processName", "process",
            ]:
                log_record[key] = value

        return json.dumps(log_record)


def setup_logger(
    name: str = "auth",
    level: int = logging.INFO,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """Set up a JSON logger with console and optional file handler."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JsonFormatter())
    logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    return logger


log_file_path = os.getenv("LOG_FILE_PATH", None)
logger = setup_logger(log_file=log_file_path)
