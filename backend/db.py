import os
from urllib.parse import quote_plus

from sqlalchemy import create_engine


def build_db_url() -> str:
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    jdbc_url = os.getenv("JDBC_URL")
    if jdbc_url:
        if jdbc_url.startswith("jdbc:postgresql://"):
            return jdbc_url.replace("jdbc:postgresql://", "postgresql+psycopg2://", 1)
        raise RuntimeError("JDBC_URL must start with jdbc:postgresql://")

    host = os.getenv("DB_HOST")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD", "")
    name = os.getenv("DB_NAME")
    port = os.getenv("DB_PORT", "5432")
    sslmode = os.getenv("DB_SSLMODE", "require")

    if not host or not user or not name:
        raise RuntimeError("DATABASE_URL or DB_HOST/DB_USER/DB_NAME must be set")

    password_part = f":{quote_plus(password)}" if password else ""
    return f"postgresql+psycopg2://{user}{password_part}@{host}:{port}/{name}?sslmode={sslmode}"


def get_engine():
    return create_engine(build_db_url(), pool_pre_ping=True)
