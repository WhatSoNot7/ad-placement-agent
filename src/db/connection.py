"""Подключение к базе данных."""

import os

import psycopg2


def get_db_connection():
    """Возвращает соединение с PostgreSQL."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "ad_placement"),
        user=os.getenv("DB_USER", "agent"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )