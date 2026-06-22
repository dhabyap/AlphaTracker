"""Database config — supports SQLite (default) or MySQL (Laragon)."""

import os


def get_cfg():
    """Get database config. Uses SQLite by default, MySQL if DB_DRIVER=mysql."""
    driver = os.environ.get("DB_DRIVER", "sqlite").strip().lower()

    if driver == "mysql":
        return {
            "driver": "mysql",
            "host": os.environ.get("DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("DB_PORT", "3306")),
            "user": os.environ.get("DB_USER", "root"),
            "password": os.environ.get("DB_PASSWORD", ""),
            "database": os.environ.get("DB_NAME", "alpha_tracker"),
        }

    # SQLite
    return {
        "driver": "sqlite",
        "database": os.environ.get("DB_NAME", "alpha_tracker.db"),
    }
