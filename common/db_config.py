"""Shared database settings and helpers for every script.

Two databases, one .env (see .env.example), and each setting's prefix names the database it
points to (14 Nov 2026; until then the shop database's settings were DB_*):

    CMMS_DB_*   MySQL: the shop's data (the CMMS, sensors, spares, scans, SOPs)
    CONV_DB_*   Postgres: the agent's saved conversations (conversations.py)

The login comes after the prefix: CMMS_DB_USERNAME is the owner the ingest scripts load
with, CMMS_DB_AGENT_* is agent_ro (what the agent reads with), CMMS_DB_ADMIN_* creates
users. Only these names are read: an old name (DB_NAME, AGENT_DB_PASSWORD, ...) is ignored,
so a .env that wasn't updated fails with "Missing setting(s) ..." naming the new ones.
Settings already exported in the shell win over .env.
"""
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

REQUIRED_KEYS = ("CMMS_DB_USERNAME", "CMMS_DB_HOST", "CMMS_DB_NAME")
# CMMS_DB_PASSWORD must be present but may be empty (local root with no password).


def getenv(key: str, default: str | None = None) -> str | None:
    """os.getenv without stray line endings.

    A .env saved on Windows has CRLF line endings. python-dotenv reads it correctly, but
    a shell that loads it (`source .env`, `export $(cat .env | xargs)`) keeps the '\\r':
    CMMS_DB_AGENT_USERNAME becomes 'agent_ro\\r', and shell values win over .env. Only CR/LF
    are removed, so a password that really starts or ends with a space is kept.
    """
    value = os.getenv(key, default)
    return value.strip("\r\n") if isinstance(value, str) else value


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


def cmms_config(user: str, password: str) -> DbConfig:
    """The shop database (host, port, name from CMMS_DB_*) as another login: agent_ro, the
    session user, the admin."""
    return DbConfig(user=user, password=password, host=getenv("CMMS_DB_HOST"),
                    port=int(getenv("CMMS_DB_PORT", "3306")), name=getenv("CMMS_DB_NAME"))


def load_db_config(env_file: str | None = None) -> DbConfig:
    """The shop database as its owner (CMMS_DB_*), from env_file or the nearest .env."""
    if env_file:
        if not Path(env_file).is_file():
            raise FileNotFoundError(f"Env file not found: {env_file}")
        load_dotenv(env_file)
    else:
        load_dotenv()

    missing = [k for k in REQUIRED_KEYS if not getenv(k)]
    if getenv("CMMS_DB_PASSWORD") is None:
        missing.append("CMMS_DB_PASSWORD")
    if missing:
        raise ValueError(
            f"Missing setting(s) {', '.join(missing)}. Add them to .env (see .env.example)."
        )
    return cmms_config(getenv("CMMS_DB_USERNAME"), getenv("CMMS_DB_PASSWORD"))


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