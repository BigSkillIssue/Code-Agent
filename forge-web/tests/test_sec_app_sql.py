"""SEC-W6: the agent's SQL on its hosted app's database (W54) is hostile input.

`app` is the database's superuser (the official image makes POSTGRES_USER one), so the agent's
queries must not run as it: "read-only" as a session default can be switched off inside the
query (`SET TRANSACTION READ WRITE`, `BEGIN READ WRITE`), `COPY … TO PROGRAM` runs commands in
the database container even read-only, and psql runs a `-c` that starts with a backslash as a
meta-command (`\\!` is a shell in the container). Reads run as `forge_reader` (SELECT only),
writes as `forge_writer` (rows only, no schema), neither a superuser, both set up by the worker."""

from pathlib import Path

import pytest

from docker_standin import JOB_ID, FakeDocker, runner_for
from forge_hostworker.docker import DockerResult
from forge_hostworker.wire import HostJob, SqlQuery

DATABASE = "forge-shop-staging-db"


class SqlDocker(FakeDocker):
    """Docker whose database container runs and answers every psql."""

    def __init__(self) -> None:
        super().__init__()
        self.containers[DATABASE] = {"labels": {}, "running": True}

    def do_exec(self, args: list[str]) -> DockerResult:
        return DockerResult(0, "id\n1", "")

    def psql_runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "exec" and "psql" in c]


def sql_job(query: str, write: bool = False) -> HostJob:
    return HostJob(id=JOB_ID, kind="sql", app="shop", environment="staging",
                   sql=SqlQuery(query=query, write=write))  # fmt: skip


def role_of(call: list[str]) -> str:
    return call[call.index("-U") + 1]


def options_of(call: list[str]) -> str:
    return next(a for a in call if a.startswith("PGOPTIONS=")).removeprefix("PGOPTIONS=")


async def test_reads_and_writes_never_run_as_the_superuser(tmp_path: Path) -> None:
    docker = SqlDocker()
    runner = runner_for(tmp_path, docker)
    read = await runner.run(sql_job("select * from orders"), None, {})
    assert read.ok, read
    setup, query = docker.psql_runs()[-2:]
    assert role_of(setup) == "app"  # the worker sets the roles up, the agent's SQL is not in it
    script = setup[-1]
    assert "select * from orders" not in script
    for role in ("forge_reader", "forge_writer"):
        assert role in script
    assert "NOSUPERUSER" in script and "NOCREATEROLE" in script and "PASSWORD NULL" in script
    assert "pg_read_all_data" in script and "pg_write_all_data" in script
    assert "pg_execute_server_program" not in script
    assert role_of(query) == "forge_reader" and query[-1] == "select * from orders"
    assert "default_transaction_read_only=on" in options_of(query)
    write = await runner.run(sql_job("update orders set paid = true", write=True), None, {})
    assert write.ok, write
    query = docker.psql_runs()[-1]
    assert role_of(query) == "forge_writer" and "statement_timeout=" in options_of(query)


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("query", ["\\! id", "\\copy t to program 'id'", "  \\o |sh"])
async def test_psql_meta_commands_are_refused(tmp_path: Path, query: str, write: bool) -> None:
    docker = SqlDocker()
    result = await runner_for(tmp_path, docker).run(sql_job(query, write), None, {})
    assert not result.ok and "psql" in result.log_tail
    assert docker.psql_runs() == []


@pytest.mark.parametrize(
    "query",
    [
        "SET TRANSACTION READ WRITE; insert into orders values (1)",
        "BEGIN READ WRITE; delete from orders; COMMIT",
        "select 1; set default_transaction_read_only = off",
        "COPY (select 1) TO PROGRAM 'id'",
        "DO $$ BEGIN PERFORM 1; END $$",
        "set role app",
        "commit",
    ],
)
async def test_a_read_is_one_reading_statement(tmp_path: Path, query: str) -> None:
    docker = SqlDocker()
    result = await runner_for(tmp_path, docker).run(sql_job(query), None, {})
    assert not result.ok and "read" in result.log_tail
    assert docker.psql_runs() == []


@pytest.mark.parametrize(
    "query",
    [
        "select count(*) from orders;",
        "WITH paid AS (select * from orders where paid) select count(*) from paid",
        "-- what sold\nselect sku from orders",
        "/* newest */ (select * from orders order by id desc limit 5)",
        "explain select * from orders",
        "table orders",
        "show server_version",
    ],
)
async def test_ordinary_reads_still_run(tmp_path: Path, query: str) -> None:
    docker = SqlDocker()
    result = await runner_for(tmp_path, docker).run(sql_job(query), None, {})
    assert result.ok, result
    assert role_of(docker.psql_runs()[-1]) == "forge_reader"
