"""Start/stop only the task-owned local PostgreSQL cluster; retain its data on stop."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path.home() / ".local/share/condere/postgres"


def run(command: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    # Database processes do not inherit model, research, or Doppler credentials.
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ
    }
    return subprocess.run(command, input=stdin, text=True, capture_output=True, env=environment)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    for name in ("initdb", "pg_ctl"):
        if not shutil.which(name):
            raise SystemExit(f"Install a supported PostgreSQL distribution with {name} first.")
    data, sockets = ROOT / "data", ROOT / "sockets"
    marker = ROOT / "condere-owner.json"
    if ROOT.exists() and not marker.exists() and any(ROOT.iterdir()):
        raise SystemExit("Existing unowned PostgreSQL directory will not be adopted or replaced.")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    if marker.exists():
        if json.loads(marker.read_text()) != {"application": "condere-mda", "port": 15434}:
            raise SystemExit("Local cluster ownership marker does not match this task.")
    else:
        marker.write_text(json.dumps({"application": "condere-mda", "port": 15434}) + "\n")
    sockets.mkdir(exist_ok=True, mode=0o700)
    if args.stop:
        result = run(["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"])
        if result.returncode:
            raise SystemExit("Task PostgreSQL stop failed; no data was deleted.")
        print("Stopped the task PostgreSQL cluster; data retained.")
        return
    admin_password = os.environ["CONDERE_PG_ADMIN_PASSWORD"]
    if not (data / "PG_VERSION").exists():
        result = run(
            [
                "initdb",
                "-D",
                str(data),
                "--username=condere_admin",
                "--encoding=UTF8",
                "--locale=C",
                "--auth=scram-sha-256",
                "--pwfile=/dev/stdin",
            ],
            stdin=admin_password + "\n",
        )
        if result.returncode:
            raise SystemExit("Local cluster initialization failed; secret output was suppressed.")
    status = run(["pg_ctl", "-D", str(data), "status"])
    if status.returncode:
        options = shlex.join(["-h", "127.0.0.1", "-p", "15434", "-k", str(sockets)])
        result = run(
            [
                "pg_ctl",
                "-D",
                str(data),
                "-l",
                str(ROOT / "server.log"),
                "-o",
                options,
                "-w",
                "start",
            ]
        )
        if result.returncode:
            raise SystemExit(
                f"Task PostgreSQL startup failed; inspect the restricted log in {ROOT}."
            )
    with psycopg.connect(
        host="127.0.0.1",
        port=15434,
        dbname="postgres",
        user="condere_admin",
        password=admin_password,
        sslmode="disable",
        autocommit=True,
    ) as connection:
        for role, database, password_key in (
            ("condere_app", "condere_mda", "CONDERE_DATABASE_PASSWORD"),
            ("condere_checkpoint", "condere_mda_checkpoints", "CONDERE_CHECKPOINT_PASSWORD"),
        ):
            if not connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone():
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(os.environ[password_key])
                    )
                )
            if not connection.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
            ).fetchone():
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(database), sql.Identifier(role)
                    )
                )
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
            )
        version = connection.execute("SHOW server_version").fetchone()[0]
    for key, expected in (
        ("CONDERE_DATABASE_URL", ("condere_app", "condere_mda")),
        ("CONDERE_CHECKPOINT_URL", ("condere_checkpoint", "condere_mda_checkpoints")),
    ):
        with psycopg.connect(os.environ[key]) as connection:
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            assert (
                connection.execute("SELECT current_user, current_database()").fetchone() == expected
            )
    artifact = Path(__file__).resolve().parents[1] / ".artifacts/infrastructure/postgres.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "host": "127.0.0.1",
        "port": 15434,
        "server_version": version,
        "separate_application_and_checkpoint_databases": True,
        "separate_roles": True,
        "existing_hosted_database_changed": False,
    }
    artifact.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
