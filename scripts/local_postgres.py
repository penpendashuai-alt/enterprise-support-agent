"""Windows-only, unprivileged local PostgreSQL 17.11 development cluster.

Download from the official EDB distribution over HTTPS. The local SHA256 is an
inventory digest, not a comparison with a vendor-published checksum. Credentials
and data stay under the ignored .cache directory. No Windows service is installed.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import urllib.request
import zipfile
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
URL = "https://get.enterprisedb.com/postgresql/postgresql-17.11-1-windows-x64-binaries.zip"


def main(args):
    if os.name != "nt":
        raise ValueError("Use compose.postgres.yaml on other platforms")
    root = ROOT / ".cache/phase6-pg"
    root.mkdir(parents=True, exist_ok=True)
    data, config = root / "data", root / "connection.json"
    binary = root / "pgsql/bin"
    if not (binary / "pg_ctl.exe").exists():
        archive = root / "postgresql.zip"
        if not archive.exists():
            urllib.request.urlretrieve(URL, archive)
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                target = (root / member.filename).resolve()
                if not target.is_relative_to(root.resolve()):
                    raise ValueError("Unsafe archive path")
                if member.filename.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                    z.extract(member, root)
        (root / "download.json").write_text(
            json.dumps(
                {"url": URL, "sha256_local_only": hashlib.sha256(archive.read_bytes()).hexdigest()}
            ),
            encoding="utf-8",
        )
    version = subprocess.check_output(
        [str(binary / "postgres.exe"), "--version"], text=True
    ).strip()
    if version != "postgres (PostgreSQL) 17.11":
        raise ValueError("Expected PostgreSQL 17.11")
    ctl = [str(binary / "pg_ctl.exe"), "-D", str(data)]
    flags = subprocess.CREATE_NO_WINDOW
    if args.action == "stop":
        if not data.is_dir() or not config.is_file():
            raise ValueError("No owned local cluster")
        subprocess.run([*ctl, "stop", "-m", "fast", "-w"], check=True, creationflags=flags)
        return
    if not config.exists():
        if data.exists():
            raise ValueError("Existing data without credentials; do not overwrite")
        connection = {
            "host": "127.0.0.1",
            "port": args.port,
            "user": "support_demo",
            "password": secrets.token_urlsafe(32),
            "dbname": "enterprise_support_phase6",
        }
        config.write_text(json.dumps(connection), encoding="utf-8")
    connection = json.loads(config.read_text(encoding="utf-8"))
    if not (data / "PG_VERSION").exists():
        password_file = root / "init-password.private"
        password_file.write_text(connection["password"] + "\n", encoding="utf-8")
        subprocess.run(
            [
                str(binary / "initdb.exe"),
                "-D",
                str(data),
                "-U",
                connection["user"],
                "--encoding=UTF8",
                "--locale=C",
                "--auth=scram-sha-256",
                "--pwfile=" + str(password_file),
            ],
            check=True,
            creationflags=flags,
        )
        with (data / "postgresql.conf").open("a", encoding="utf-8") as f:
            f.write(
                f"\nlisten_addresses='127.0.0.1'\nport={connection['port']}\nmax_connections=40\nshared_buffers='128MB'\n"
            )
    status = subprocess.run([*ctl, "status"], capture_output=True, creationflags=flags)
    if status.returncode:
        with socket.socket() as s:
            s.bind(("127.0.0.1", connection["port"]))
        subprocess.run(
            [*ctl, "start", "-w", "-l", str(root / "server.log")],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    with psycopg.connect(
        make_conninfo(**{**connection, "dbname": "postgres"}), autocommit=True
    ) as conn:
        actual = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
        if actual != data.resolve():
            raise ValueError("Connected to a different cluster")
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (connection["dbname"],)
        ).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(connection["dbname"])))
    if args.activate:
        from dotenv import set_key

        env_file = ROOT / ".env"
        backup = root / "env-before-phase6.private"
        if env_file.exists() and not backup.exists():
            shutil.copy2(env_file, backup)
        for key, value in connection.items():
            name = {
                "user": "USER",
                "password": "PASSWORD",
                "host": "HOST",
                "port": "PORT",
                "dbname": "DB",
            }[key]
            set_key(str(env_file), "POSTGRES_" + name, str(value))
        set_key(str(env_file), "DATABASE_TYPE", "postgres")
        set_key(str(env_file), "SUPPORT_DEMO_SAMPLES", "false")
    print(
        json.dumps(
            {
                "status": "ready",
                "version": version,
                "connection_file": ".cache/phase6-pg/connection.json",
                "port": connection["port"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--port", type=int, default=15432)
    parser.add_argument(
        "--activate", action="store_true", help="Back up .env privately and select this cluster"
    )
    try:
        main(parser.parse_args())
    except Exception as exc:
        raise SystemExit(
            f"Local PostgreSQL operation failed: {type(exc).__name__}; inspect local logs"
        ) from None
