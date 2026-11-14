from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.settings import get_settings


class Base(DeclarativeBase):
    pass


class AppSession(Session):
    """A session whose get() never returns another user's row when the session belongs to a user."""

    def get(self, entity, ident, **kwargs):
        obj = super().get(entity, ident, **kwargs)
        user_id = self.info.get("user_id")
        if obj is not None and user_id is not None:
            from app.tenancy import owned_by

            if not owned_by(obj, user_id):
                return None
        return obj


def engine_options(settings) -> dict:
    """How the engine is made. A server database gets a pool sized by DB_POOL_*; SQLite keeps SQLAlchemy's own."""
    if settings.database_url.startswith("sqlite"):
        return {"pool_pre_ping": True, "connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True, "connect_args": {},
            "pool_size": max(1, settings.db_pool_size), "max_overflow": max(0, settings.db_pool_max_overflow),
            "pool_timeout": max(1, settings.db_pool_timeout_seconds)}


settings = get_settings()
engine = create_engine(settings.database_url, **engine_options(settings))
SessionLocal = sessionmaker(bind=engine, class_=AppSession, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
