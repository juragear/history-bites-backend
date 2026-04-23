from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


def _normalize_url(url: str) -> str:
    # Railway hands out postgresql:// URLs; SQLAlchemy + psycopg v3 wants
    # the explicit driver suffix. Rewrite so the app runs against psycopg 3.
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


engine = create_engine(_normalize_url(settings.DATABASE_URL), pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
