"""Unprivileged, project-scoped Redis in an existing Ubuntu 24.04 WSL distribution."""

import argparse
import json
import subprocess

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["start", "status", "stop"])
    p.add_argument("--port", type=int, default=16379, choices=[16379, 16380])
    p.add_argument("--distribution", default="Ubuntu-24.04")
    args = p.parse_args()
    if args.action == "start":
        script = f"""set -eu
mkdir -p "$HOME/.cache/enterprise-support-phase7/debs"
cd "$HOME/.cache/enterprise-support-phase7"
if [ ! -x runtime/usr/bin/redis-server ]; then
  cd debs
  apt download redis-server=5:7.0.15-1ubuntu0.24.04.4 redis-tools=5:7.0.15-1ubuntu0.24.04.4 libjemalloc2 liblua5.1-0 liblzf1 libatomic1
  find . -name '*.deb' -exec dpkg-deb -x {{}} ../runtime \\;
  cd ..
fi
export LD_LIBRARY_PATH="$PWD/runtime/usr/lib/x86_64-linux-gnu"
runtime/usr/bin/redis-server --bind 127.0.0.1 --port {args.port} --maxmemory 64mb --maxmemory-policy noeviction --save "" --appendonly no --daemonize yes --pidfile redis-{args.port}.pid --logfile redis-{args.port}.log
"""
        subprocess.run(
            ["wsl", "-d", args.distribution, "--", "sh", "-s"], input=script.encode(), check=True
        )
    with Redis(
        host="127.0.0.1",
        port=args.port,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    ) as redis:
        if args.action == "stop":
            info = redis.info("server")
            pid = (
                subprocess.check_output(
                    [
                        "wsl",
                        "-d",
                        args.distribution,
                        "--",
                        "sh",
                        "-c",
                        f'cat "$HOME/.cache/enterprise-support-phase7/redis-{args.port}.pid"',
                    ]
                )
                .decode()
                .strip()
            )
            if str(info["process_id"]) != pid:
                raise ValueError("Port does not match this project's recorded Redis process")
            try:
                redis.execute_command("SHUTDOWN", "NOSAVE")
            except (ConnectionError, TimeoutError):
                pass
            subprocess.run(
                [
                    "wsl",
                    "-d",
                    args.distribution,
                    "--",
                    "sh",
                    "-c",
                    f'test ! -e "$HOME/.cache/enterprise-support-phase7/redis-{args.port}.pid"',
                ],
                check=True,
            )
            print(json.dumps({"status": "stopped", "port": args.port}))
        else:
            print(
                json.dumps(
                    {
                        "status": "ready" if redis.ping() else "unavailable",
                        "version": redis.info("server")["redis_version"],
                        "port": args.port,
                        "memory": redis.config_get("maxmemory*"),
                        "bind": redis.config_get("bind"),
                    }
                )
            )


if __name__ == "__main__":
    main()
