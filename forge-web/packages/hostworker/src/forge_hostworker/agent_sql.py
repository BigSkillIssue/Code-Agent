"""Forge's agent's SQL on its app's own database (W54), which is hostile input (SEC-W6).

`app`, the role the app itself uses, is the database's superuser (the official image makes
POSTGRES_USER one), and a superuser can leave any read-only setting and run programs in the
database container (`COPY … TO PROGRAM`). So the agent's SQL never runs as `app`: reads run as
`forge_reader` (SELECT on everything, `pg_read_all_data`) in read-only transactions, writes as
`forge_writer` (rows only, `pg_write_all_data`; schema changes belong in the app's migrations).
Neither is a superuser or has a password; the worker sets both up again before every query, so
whatever changed them since is undone. psql runs a `-c` that starts with a backslash as one of
its own commands (`\\!` is a shell), so such queries are refused; and a read is one statement
that reads, so a mistake never gets as far as the database's own refusal."""

import re

from forge_hostworker.docker import Docker
from forge_hostworker.postgres import container

READER = "forge_reader"
WRITER = "forge_writer"
SQL_OUTPUT = 19_000  # characters of a query's answer kept (a result's log_tail holds 20 000)
READS = frozenset({"select", "with", "table", "values", "explain", "show"})
# Leading blanks, comments and parentheses before a statement's first word.
LEADING = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/|\()*", re.DOTALL)

# Idempotent; run as `app`. Each role gets exactly its attributes and its one or two grants.
SETUP = f"""DO $forge$
DECLARE
  who text;
  granted text;
  allowed text[];
BEGIN
  FOREACH who IN ARRAY ARRAY['{READER}', '{WRITER}'] LOOP
    BEGIN
      EXECUTE format('CREATE ROLE %I', who);
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
    EXECUTE format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
                   'NOBYPASSRLS INHERIT CONNECTION LIMIT 4 PASSWORD NULL', who);
    EXECUTE format('ALTER ROLE %I RESET ALL', who);
    allowed := CASE who WHEN '{READER}' THEN ARRAY['pg_read_all_data']
               ELSE ARRAY['pg_read_all_data', 'pg_write_all_data'] END;
    FOR granted IN
      SELECT g.rolname FROM pg_auth_members m
      JOIN pg_roles g ON g.oid = m.roleid JOIN pg_roles r ON r.oid = m.member
      WHERE r.rolname = who AND NOT g.rolname = ANY (allowed)
    LOOP
      EXECUTE format('REVOKE %I FROM %I', granted, who);
    END LOOP;
    FOREACH granted IN ARRAY allowed LOOP
      EXECUTE format('GRANT %I TO %I', granted, who);
    END LOOP;
  END LOOP;
END
$forge$"""


def refusal(query: str, write: bool) -> str:
    """Why the agent's query is not run ("" when it is)."""
    if query.lstrip().startswith("\\"):
        return "psql's own commands (a leading backslash) are not run; send SQL"
    if write:
        return ""
    body = query.strip().rstrip(";").rstrip()
    if ";" in body:
        return "a read is one statement (no ';' before its end); send one query at a time"
    words = LEADING.sub("", body, count=1).split(maxsplit=1)
    if not words or words[0].lower() not in READS:
        return "a read starts with SELECT, WITH, TABLE, VALUES, EXPLAIN or SHOW; writes need write"
    return ""


async def run_sql(
    docker: Docker, app: str, environment: str, query: str, write: bool, timeout_s: int
) -> tuple[bool, str]:
    """Run the agent's SQL on the app's own database (psql, CSV): whether it worked, and what
    it printed. Reads as `forge_reader`, writes as `forge_writer`; a statement timeout for both."""
    refused = refusal(query, write)
    if refused:
        return False, refused
    name = container(app, environment)
    state = await docker("container", "inspect", "-f", "{{.State.Running}}", name)
    if state.code != 0 or state.out.strip() != "true":
        return False, "the app has no running database in this environment"
    ready = await docker(
        "exec", "-e", "PGOPTIONS=-c client_min_messages=warning", name, "psql", "-U", "app",
        "-d", "app", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", SETUP, timeout=60,
    )  # fmt: skip
    if ready.code != 0:
        return False, f"the agent's database roles could not be set up: {ready.tail()[-500:]}"
    options = f"-c statement_timeout={timeout_s * 1000}"
    if not write:
        options += " -c default_transaction_read_only=on"
    done = await docker(
        "exec", "-e", f"PGOPTIONS={options}", name, "psql", "-U", WRITER if write else READER,
        "-d", "app", "-X", "-v", "ON_ERROR_STOP=1", "--csv", "-c", query, timeout=timeout_s + 15,
    )  # fmt: skip
    text = "\n".join(t for t in (done.out.rstrip(), done.err.rstrip()) if t)
    return done.code == 0, text[:SQL_OUTPUT]
