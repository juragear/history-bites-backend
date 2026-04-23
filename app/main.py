import json
import logging
import sys

from fastapi import FastAPI

from app.config import settings


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = record.__dict__.get("extra")
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.handlers = [handler]


configure_logging()

logger = logging.getLogger(__name__)

app = FastAPI(title="HistoryBites backend")


@app.on_event("startup")
def on_startup() -> None:
    logger.info("app startup", extra={"extra": {"environment": settings.ENVIRONMENT}})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
