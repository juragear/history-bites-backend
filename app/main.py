import json
import logging
import sys

from fastapi import FastAPI, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import engine


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
def health(response: Response) -> dict[str, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("health db probe failed", extra={"extra": {"error": str(exc)}})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "db": "down"}
    return {"status": "ok", "db": "ok"}
