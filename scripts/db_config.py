"""Shared database settings and helpers for every ingest script.

All scripts read the same DB_* keys from .env (see .env.example), so the team
keeps one .env file. Settings already exported in the shell win over .env.
"""
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

REQUIRED_KEYS = ("DB_USERNAME", "DB_HOST", "DB_NAME")
# DB_PASSWORD must be present but may be empty (local root with no password).


@dataclass(frozen=True)
class DbConfig:
    user: str
    password: str = field(repr=False)  # never shows up in print()/logs
    host: str = "localhost"
    port: int = 3306
    name: str = ""

    def url(self):
        """SQLAlchemy URL. URL.create escapes passwords containing @, :, / etc."""
        from sqlalchemy.engine import URL

        return URL.create(
            "mysql+pymysql",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
            query={"charset": "utf8mb4"},
        )

    def engine(self):
        """Engine for every script. hide_parameters=True keeps the values of a failed
        query (a password, or a row with an employee's name) out of error messages,
        tracebacks and ingest_runs.error. The SQL itself is still shown."""
        from sqlalchemy import create_engine

        return create_engine(self.url(), pool_pre_ping=True, hide_parameters=True)

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.name} (password hidden)"


def load_db_config(env_file: str | None = None) -> DbConfig:
    """Load DB settings from env_file, or the nearest .env if none is given."""
    if env_file:
        if not Path(env_file).is_file():
            raise FileNotFoundError(f"Env file not found: {env_file}")
        load_dotenv(env_file)
    else:
        load_dotenv()

    missing = [k for k in REQUIRED_KEYS if not os.getenv(k)]
    if os.getenv("DB_PASSWORD") is None:
        missing.append("DB_PASSWORD")
    if missing:
        raise ValueError(
            f"Missing setting(s) {', '.join(missing)}. Add them to .env (see .env.example)."
        )
    return DbConfig(
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.getenv("DB_PORT", "3306")),
        name=os.environ["DB_NAME"],
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", help="path to .env (default: nearest .env)")
    parser.add_argument(
        "--dry-run", action="store_true", help="read and clean only; don't touch the database"
    )


def safe_identifier(name: str) -> str:
    """Table names can't be bound as SQL parameters, so whitelist them."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
        raise ValueError(f"Invalid table name: {name!r}")
    return name


def to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of dicts of plain Python values for the DB driver.

    NaN/NaT/pd.NA become None; pandas Timestamps become datetime; numpy scalars
    become int/float (PyMySQL only knows the built-in types).
    """
    def plain(v):
        if isinstance(v, pd.Timestamp):
            return v.to_pydatetime()
        return v.item() if hasattr(v, "item") and not isinstance(v, (str, bytes)) else v

    rows = df.astype(object).where(df.notna(), None).to_dict("records")
    return [{k: plain(v) for k, v in r.items()} for r in rows]