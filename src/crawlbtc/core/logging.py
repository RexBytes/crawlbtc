"""Structured JSON logging, identical output format to the legacy scripts.

LOG_LEVEL=progress keeps only "progress" events, exactly as before, so
anything already parsing the progress stream keeps working.
"""

import os

import structlog
from structlog.processors import JSONRenderer, TimeStamper
from structlog.stdlib import BoundLogger
from structlog.typing import EventDict


class ProgressOnlyFilter:
    def __init__(self, allowed_event: str = "progress"):
        self.allowed_event = allowed_event

    def __call__(self, logger: BoundLogger, method_name: str, event_dict: EventDict) -> EventDict:
        log_level = os.getenv("LOG_LEVEL", "").lower()
        if log_level == "progress":
            if event_dict.get("event") == self.allowed_event:
                return event_dict
            raise structlog.DropEvent
        return event_dict


_configured = False


def get_logger(name: str) -> BoundLogger:
    global _configured
    if not _configured:
        structlog.configure(
            processors=[
                TimeStamper(fmt="iso"),
                structlog.stdlib.add_log_level,
                ProgressOnlyFilter(),
                JSONRenderer(),
            ]
        )
        _configured = True
    return structlog.get_logger(name)
