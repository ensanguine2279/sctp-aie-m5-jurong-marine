#!/usr/bin/env python3
"""Create the read-only MySQL user the app (the LLM's tools) connects as. PDPA guardrail.

The app must never connect as DB_USERNAME: that user owns every table, including
cmms_employees.emp_name. Telling the model "use the views" is not protection; the
database has to refuse. This script makes a separate user that can only SELECT from
an allow-list of views, then proves it:

    allowed   the linking views (machine_overview, work_order_detail, ...), the
              table_relationships catalog, and the *_audit views needed for citations
              (including the scanned nameplates and incident forms)
    never     cmms_employees, cmms_employees_raw, cmms_employees_audit, any *_raw table
              (img_documents_raw keeps a form's "person involved" exactly as read),
              any write, any schema change

Views run with their creator's rights (SQL SECURITY DEFINER, MySQL's default), so the
app user can read work_order_detail without any access to the tables underneath.

Before granting, every object is checked for personal-data columns (emp_name); one that
has any is refused, so a view changed later can't leak names by accident. Objects that
don't exist yet (a source not loaded) are skipped: run this again after the loads.

Settings come from the same .env as the other scripts, plus:

    AGENT_DB_USERNAME   the app's user (default agent_ro)
    AGENT_DB_PASSWORD   its password (required)
    DB_ADMIN_USERNAME   a user allowed to create users and grant (default root)
    DB_ADMIN_PASSWORD   its password (falls back to MYSQL_ROOT_PASSWORD)

Usage
    python scripts/agent_user.py --dry-run   # show the plan; no database needed
    python scripts/agent_user.py             # create/update the user, then --check
    python scripts/agent_user.py --check     # prove what it can and can't read (exit 1 if not)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

from db_config import DbConfig, add_common_args, load_db_config
from relationships import CATALOG, VIEWS, Names

PII_COLUMNS = {"emp_name"}  # columns the app user must never be able to read
DENIED_CODES = {1044, 1142, 1143, 1227, 3879}  # MySQL "access denied" errors
PARTIAL_REVOKE_CODE = 3879  # access denied to a database (hosted MySQL: system databases)
MISSING_CODES = {1146}  # table doesn't exist


def allowed_objects(names: Names) -> list[str]:
    """Everything the app may SELECT from, in order. Never employees, never *_raw."""
    return [
        *VIEWS,                                       # the linking views
        CATALOG,                                      # join paths and orphan counts
        f"{names['machines']}_audit",                 # citations, one per source table
        f"{names['work_orders']}_audit",
        f"{names['safety_incidents']}_audit",
        f"{names['readings']}_audit",
        "critical_spares_audit",
        f"{names['nameplates']}_audit",               # scanned images: file + SHA-256 cited
        f"{names['incident_reports']}_audit",
    ]


def forbidden_probes(names: Names) -> list[tuple[str, str]]:
    """(what, SQL) that the app user must NOT be able to run. All are harmless if they ran."""
    emp = names["employees"]
    return [
        ("employee names", f"SELECT emp_name FROM `{emp}` LIMIT 1"),
        ("employees table", f"SELECT * FROM `{emp}` LIMIT 1"),
        ("employees audit view", f"SELECT * FROM `{emp}_audit` LIMIT 1"),
        ("raw employee rows", f"SELECT * FROM `{emp}_raw` LIMIT 1"),
        ("writing data", f"UPDATE `{names['machines']}` SET machine_name = machine_name "
                         f"WHERE 1 = 0"),
        ("changing the schema", "CREATE TABLE agent_user_probe (x INT)"),
    ]


# -------------------------------------------------------------------- plan ---

@dataclass
class Plan:
    grant: list
    missing: list
    refused: dict  # object -> PII columns found


def plan_grants(names: Names, existing: dict[str, set[str]]) -> Plan:
    """existing: object name -> its column names (from information_schema.COLUMNS)."""
    cols = {k.lower(): {c.lower() for c in v} for k, v in existing.items()}
    p = Plan([], [], {})
    for obj in allowed_objects(names):
        if obj.lower() not in cols:
            p.missing.append(obj)
        elif pii := sorted(cols[obj.lower()] & PII_COLUMNS):
            p.refused[obj] = pii
        else:
            p.grant.append(obj)
    return p


_USER = re.compile(r"[A-Za-z0-9_]{1,32}")
_HOST = re.compile(r"[A-Za-z0-9._%-]{1,255}")
_DB = re.compile(r"[A-Za-z0-9_$]{1,64}")


def account(user: str, host: str) -> str:
    if not _USER.fullmatch(user):
        raise ValueError(f"Invalid user name: {user!r}")
    if not _HOST.fullmatch(host):
        raise ValueError(f"Invalid host: {host!r}")
    return f"'{user}'@'{host}'"


def statements(db: str, user: str, host: str, grant: list[str]) -> list[str]:
    """SQL to (re)create the user with exactly these grants. :pw is bound, never inlined."""
    if not _DB.fullmatch(db):
        raise ValueError(f"Invalid database name: {db!r}")
    acct = account(user, host)
    # Drop and recreate rather than REVOKE ALL: same clean slate (no grant survives, the
    # password comes from .env), but needs no rights over every database. Hosted MySQL
    # (Aiven) keeps even its admin out of system databases, and REVOKE ALL fails there.
    out = [f"DROP USER IF EXISTS {acct}",
           f"CREATE USER {acct} IDENTIFIED BY :pw"]
    out += [f"GRANT SELECT ON `{db}`.`{obj}` TO {acct}" for obj in grant]
    return out


def classify(code: int | None) -> str:
    """MySQL error code -> 'denied' | 'missing' | 'error'."""
    if code in DENIED_CODES:
        return "denied"
    if code in MISSING_CODES:
        return "missing"
    return "error"


# ---------------------------------------------------------------- database ---

def make_engine(cfg: DbConfig):
    """Engine whose error messages never include query parameters (the password).
    DbConfig.engine() already hides them; this keeps the guarantee local and tested."""
    return cfg.engine()


class SetupError(Exception):
    """A setup statement failed: which step, MySQL's code and its own message.

    MySQL's messages name users and databases but never echo a password, and the
    statement is given without its parameters, so this is safe to print."""

    def __init__(self, step: str, code: int | None, message: str):
        super().__init__(message)
        self.step, self.code, self.message = step, code, message


def step_label(sql: str) -> str:
    """The statement as shown in errors: up to the password placeholder."""
    return sql.split(" IDENTIFIED BY")[0]


def admin_error(code: int | None, admin_user: str, step: str | None = None,
                message: str | None = None) -> str:
    """A readable message for a failed setup, without the SQL's parameters."""
    if code == 1045:
        hint = (f"Login refused for {admin_user}: check DB_ADMIN_USERNAME / DB_ADMIN_PASSWORD "
                f"in .env.")
    elif code == PARTIAL_REVOKE_CODE:
        hint = (f"{admin_user} is refused access to a database it would need for this step. "
                f"Hosted MySQL (e.g. Aiven) keeps even its admin out of some databases.")
    elif code in DENIED_CODES:
        hint = (f"{admin_user} is not allowed to create users or grant. Use an admin account: "
                f"root on Docker (docker compose exec mysql sh -c 'echo \"$MYSQL_ROOT_PASSWORD\"'), "
                f"avnadmin on Aiven.")
    else:
        hint = f"Setting up the app user failed (MySQL error {code})."
    if step:
        hint += f"\n  Failed at: {step}"
    if message:
        hint += f"\n  MySQL said: {message}"
    return hint


def agent_settings(require_password: bool = True) -> tuple[str, str]:
    user = os.getenv("AGENT_DB_USERNAME", "agent_ro")
    pw = os.getenv("AGENT_DB_PASSWORD")
    if require_password and not pw:
        raise ValueError("Set AGENT_DB_PASSWORD in .env (the app's read-only user).")
    return user, pw or ""


def admin_config(cfg: DbConfig) -> DbConfig:
    pw = os.getenv("DB_ADMIN_PASSWORD", os.getenv("MYSQL_ROOT_PASSWORD"))
    if pw is None:
        raise ValueError("Set DB_ADMIN_PASSWORD (or MYSQL_ROOT_PASSWORD) in .env: "
                         "creating users needs an admin account.")
    return DbConfig(user=os.getenv("DB_ADMIN_USERNAME", "root"), password=pw,
                    host=cfg.host, port=cfg.port, name=cfg.name)


def existing_objects(conn) -> dict[str, set[str]]:
    from sqlalchemy import text

    out: dict[str, set[str]] = {}
    for table, col in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()")):
        out.setdefault(table, set()).add(col)
    return out


def apply(admin_engine, cfg: DbConfig, names: Names, user: str, host: str, pw: str) -> Plan:
    from sqlalchemy import exc, text

    with admin_engine.connect() as conn:
        plan = plan_grants(names, existing_objects(conn))
    with admin_engine.begin() as conn:
        for sql in statements(cfg.name, user, host, plan.grant):
            try:
                conn.execute(text(sql), {"pw": pw} if ":pw" in sql else {})
            except exc.DBAPIError as e:
                raise SetupError(step_label(sql), _code(e), _message(e)) from None
    return plan


def _message(e) -> str:
    """MySQL's own error text (never contains bound parameters)."""
    args = getattr(getattr(e, "orig", None), "args", None)
    return str(args[1]) if args and len(args) > 1 else type(e).__name__


def _code(e) -> int | None:
    args = getattr(getattr(e, "orig", None), "args", None)
    return args[0] if args and isinstance(args[0], int) else None


def access_probe(obj: str) -> str:
    """Tests that obj can be read without reading it. LIMIT 0 still makes MySQL check the
    grants and resolve the view, but returns at once. LIMIT 1 would compute a grouped view
    in full first (work_order_sensor_window: every work order against every reading)."""
    return f"SELECT * FROM `{obj}` LIMIT 0"


def check(agent_cfg: DbConfig, names: Names) -> bool:
    """Connect as the app user: allowed objects must work, forbidden probes must fail."""
    from sqlalchemy import exc, text

    ok = True
    engine = make_engine(agent_cfg)
    print(f"\nChecking as {agent_cfg.describe()}")
    print("Allowed (must work):")
    for obj in allowed_objects(names):
        try:
            with engine.connect() as conn:
                conn.execute(text(access_probe(obj))).all()
            print(f"  ok      {obj}")
        except exc.DBAPIError as e:
            kind = classify(_code(e))
            if kind == "missing":
                print(f"  skip    {obj}: not created yet")
            else:
                ok = False
                print(f"  FAIL    {obj}: {kind} ({e.orig})")
    print("Blocked (must fail):")
    for what, sql in forbidden_probes(names):
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
                if sql.startswith("CREATE TABLE"):
                    conn.execute(text("DROP TABLE agent_user_probe"))
            ok = False
            print(f"  FAIL    {what}: the app user CAN do this")
        except exc.DBAPIError as e:
            kind = classify(_code(e))
            if kind == "denied":
                print(f"  ok      {what}: denied")
            elif kind == "missing":
                print(f"  ok      {what}: table not created yet")
            else:
                ok = False
                print(f"  FAIL    {what}: unexpected error ({e.orig})")
    return ok


# --------------------------------------------------------------------- main ---

def print_plan(plan: Plan, db: str, user: str, host: str) -> None:
    print(f"App user {account(user, host)} on database `{db}`")
    print(f"Grant SELECT on ({len(plan.grant)}):")
    for obj in plan.grant:
        print(f"  {obj}")
    if plan.missing:
        print(f"Skipped, not created yet ({len(plan.missing)}): {', '.join(plan.missing)}")
        print("  run the ingest scripts and relationships.py, then this script again")
    for obj, cols in plan.refused.items():
        print(f"REFUSED {obj}: has personal-data column(s) {', '.join(cols)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="%",
                    help="host the app connects from (default %(default)s: any; the "
                         "Docker port mapping makes local connections look remote)")
    ap.add_argument("--cmms-prefix", default="cmms_")
    ap.add_argument("--sensor-table", default="vibration_temp")
    ap.add_argument("--image-prefix", default="img_")
    ap.add_argument("--check", action="store_true",
                    help="only test what the app user can and can't read")
    add_common_args(ap)
    a = ap.parse_args()

    try:
        names = Names(a.cmms_prefix, a.sensor_table, a.image_prefix)
        names.mapping()
        if a.dry_run:
            from dotenv import load_dotenv

            load_dotenv(a.env_file) if a.env_file else load_dotenv()
            user, _ = agent_settings(require_password=False)
            db = os.getenv("DB_NAME") or "<DB_NAME>"
            objs = allowed_objects(names)
            print_plan(Plan(objs, [], {}), db, user, a.host)
            print("\nSQL (password bound as a parameter, never written out):")
            for sql in statements(db if _DB.fullmatch(db) else "DB_NAME", user, a.host, objs):
                print(f"  {sql.replace(':pw', '****')};")
            print("\nWill then check these are blocked: "
                  + ", ".join(w for w, _ in forbidden_probes(names)))
            print("Dry run - nothing written to the database.")
            return
        cfg = load_db_config(a.env_file)
        user, pw = agent_settings()
        agent_cfg = DbConfig(user=user, password=pw, host=cfg.host, port=cfg.port, name=cfg.name)
        if not a.check:
            admin = admin_config(cfg)
            print(f"Setting up the app user as {admin.describe()}")
            from sqlalchemy import exc

            try:
                plan = apply(make_engine(admin), cfg, names, user, a.host, pw)
            except SetupError as e:
                sys.exit(admin_error(e.code, admin.user, e.step, e.message))
            except exc.DBAPIError as e:  # e.g. login refused: no statement to name
                sys.exit(admin_error(_code(e), admin.user, message=_message(e)))
            print_plan(plan, cfg.name, user, a.host)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))

    if check(agent_cfg, names):
        print("\nPassed: the app user reads only the allowed views; names, raw data, "
              "writes and schema changes are refused.")
        print("Point the app's database connection at AGENT_DB_USERNAME, never DB_USERNAME.")
    else:
        sys.exit("\nFAILED: fix the grants above before the app uses this user.")


if __name__ == "__main__":
    main()