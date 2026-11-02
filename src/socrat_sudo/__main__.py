"""Wait for broker approval, then run sudo with the same arguments."""

from __future__ import annotations

import getpass
import os
import socket
import subprocess
import sys
import time
import urllib.parse

import httpx

_POLL_SECONDS = 1.0


def main() -> None:
    try:
        code = _run(sys.argv[1:])
    except Exception as exc:
        print(f"socrat-sudo: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


def _run(argv: list[str]) -> int:
    broker = os.environ["BROKER_URL"].rstrip("/")
    token = os.environ["INTERNAL_TOKEN"]
    wait_seconds = float(os.environ.get("SUDO_WAIT_SECONDS", "300"))
    user = getpass.getuser()
    node = socket.gethostname()
    headers = {"X-Internal-Token": token}
    query = urllib.parse.urlencode({"user": user, "node": node})
    deadline = time.monotonic() + wait_seconds

    with httpx.Client(timeout=30) as client:
        created = client.post(
            f"{broker}/v1/sudo",
            headers=headers,
            json={"user": user, "node": node, "argv": argv},
        )
        created.raise_for_status()
        request_id = created.json()["id"]
        status_url = f"{broker}/v1/sudo/{request_id}?{query}"

        while True:
            response = client.get(status_url, headers=headers)
            response.raise_for_status()
            body = response.json()
            if body.get("execute") is True:
                completed = subprocess.run(["sudo", *argv], check=False, shell=False)
                _report_exit(client, broker, request_id, query, headers, completed.returncode)
                return completed.returncode
            status = body.get("status")
            if status == "denied":
                print("socrat-sudo: запрос отклонён", file=sys.stderr)
                return 2
            if status == "expired":
                print("socrat-sudo: запрос истёк", file=sys.stderr)
                return 3
            if status == "consumed":
                print("socrat-sudo: запрос уже использован", file=sys.stderr)
                return 1
            if time.monotonic() >= deadline:
                print("socrat-sudo: время ожидания истекло", file=sys.stderr)
                return 3
            time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _report_exit(
    client: httpx.Client,
    broker: str,
    request_id: str,
    query: str,
    headers: dict[str, str],
    exit_code: int,
) -> None:
    try:
        result = client.post(
            f"{broker}/v1/sudo/{request_id}/result?{query}",
            headers=headers,
            json={"exit_code": exit_code},
        )
        result.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"socrat-sudo: не удалось записать код выхода: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
