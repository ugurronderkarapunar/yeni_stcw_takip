"""Neon PostgreSQL baglanti yonetimi (serverless / scale-to-zero uyumlu)."""
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config import get_secret
from models import Base

_engine = None
_SessionLocal = None


def _normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_secret("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL tanimli degil (.env veya st.secrets).")
        _engine = create_engine(
            _normalize_url(url),
            pool_pre_ping=True,      # Neon uyku modundan donuste olu baglantilari eler
            pool_recycle=300,
            pool_size=3,
            max_overflow=2,
            connect_args={"connect_timeout": 20},
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # eski surumlerden gecis icin guvenli kolon ekleme
        conn.execute(text("ALTER TABLE certificates ADD COLUMN IF NOT EXISTS is_notified BOOLEAN DEFAULT FALSE"))


@contextmanager
def session_scope():
    get_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
