"""Back up and retire only completed campaign databases on its labeled container."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def main():
    root = Path("evaluation/resume-improvement/20261022-r1")
    private = Path(".cache/resume-20261022/database-backups")
    private.mkdir(parents=True, exist_ok=True)
    connection = json.loads(
        Path(".cache/resume-20261022/connection.json").read_text(encoding="utf-8")
    )
    if connection["host"] != "127.0.0.1" or int(connection["port"]) != 15422:
        raise ValueError("Not the campaign database endpoint")
    docker = str(
        Path(os.environ["LOCALAPPDATA"]) / "Programs/DockerDesktop/resources/bin/docker.exe"
    )
    container = "esa-resume-20261022-postgres"
    inspection = json.loads(subprocess.check_output([docker, "inspect", container]))[0]
    if inspection["Config"]["Labels"].get("esa.campaign") != "20261022-r1":
        raise ValueError("Container ownership mismatch")
    report_path = private / "manifest.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else []
    retired = {r["database"] for r in report if r["dropped"]}
    manifests = []
    for file in root.rglob("manifest.json"):
        value = json.loads(file.read_text(encoding="utf-8"))
        if value.get("status") == "complete" and value.get("database"):
            manifests.append((file, value))
    with psycopg.connect(make_conninfo(**connection), autocommit=True, connect_timeout=5) as conn:
        for file, value in manifests:
            database = value["database"]
            if database in retired:
                continue
            if not re.fullmatch(r"resume_[0-9a-f]{32}", database):
                raise ValueError("Unexpected database name")
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
            ).fetchone():
                raise ValueError("Database missing without recorded backup")
            if conn.execute(
                "SELECT 1 FROM pg_stat_activity WHERE datname=%s", (database,)
            ).fetchone():
                continue
            target = private / (database + ".dump")
            if target.exists():
                raise ValueError("Unreconciled backup; refuse overwrite")
            with target.open("xb") as handle:
                subprocess.run(
                    [
                        docker,
                        "exec",
                        container,
                        "pg_dump",
                        "-U",
                        connection["user"],
                        "-Fc",
                        database,
                    ],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=60,
                )
            # Read/decompress every archived object into a SQL stream, without executing it.
            with target.open("rb") as handle:
                subprocess.run(
                    [docker, "exec", "-i", container, "pg_restore", "--file=/dev/null"],
                    stdin=handle,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=60,
                )
            record = {
                "database": database,
                "run_manifest": file.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "bytes": target.stat().st_size,
                "archive_read_verified": True,
                "dropped": False,
            }
            report.append(record)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
            record["dropped"] = True
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "backed_up_and_retired": sum(r["dropped"] for r in report),
                "backup_bytes": sum(r["bytes"] for r in report),
                "scope": "completed owned experiment databases only; original raw reports and backups retained",
            }
        )
    )


if __name__ == "__main__":
    main()
