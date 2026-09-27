"""Fresh PostgreSQL/Redis startup with bounded capabilities and disposable tmpfs data."""

import json
import secrets
import subprocess
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def docker(*arguments):
    result = subprocess.run(
        ["docker", *arguments], text=True, capture_output=True, timeout=30
    )
    if result.returncode:
        raise RuntimeError(f"Docker operation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def main():
    result = {
        "passed": False,
        "network": "none",
        "data": "disposable tmpfs",
        "services": [],
    }
    try:
        for service, image in (("postgres", "postgres:15"), ("redis", "redis:7")):
            name = f"dispatcher112-init-{service}-{uuid4().hex[:10]}"
            identity = None
            try:
                arguments = [
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--label",
                    "dispatcher112.acceptance=database-initialization",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
                    "--mount",
                    f"type=bind,source={ROOT / '.secrets/pki' / service},target=/run/tls,readonly",
                    "--mount",
                    f"type=bind,source={ROOT / 'infra/tls' / (service + '.sh')},target=/scripts/start.sh,readonly",
                    "--entrypoint",
                    "sh",
                ]
                capabilities = ["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"]
                if service == "postgres":
                    capabilities.append("FOWNER")
                    arguments += [
                        "--env",
                        "POSTGRES_HOST_AUTH_METHOD=trust",
                        "--tmpfs",
                        "/var/lib/postgresql/data:rw,nosuid,nodev,size=268435456",
                        "--tmpfs",
                        "/var/run/postgresql:rw,nosuid,nodev,size=16777216,mode=3777",
                        "--mount",
                        f"type=bind,source={ROOT / 'infra/tls/pg_hba.conf'},target=/etc/postgresql/tls_hba.conf,readonly",
                    ]
                    check = ["psql", "-U", "postgres", "-Atc", "SELECT current_user"]
                    expected = "postgres"
                else:
                    password = secrets.token_urlsafe(32)
                    arguments += [
                        "--env",
                        "REDIS_PASSWORD=" + password,
                        "--env",
                        "REDISCLI_AUTH=" + password,
                        "--tmpfs",
                        "/data:rw,nosuid,nodev,size=67108864",
                    ]
                    check = [
                        "redis-cli",
                        "--tls",
                        "--cacert",
                        "/run/tls/ca.crt",
                        "-h",
                        "localhost",
                        "ping",
                    ]
                    expected = "PONG"
                for capability in capabilities:
                    arguments += ["--cap-add", capability]
                identity = docker(*arguments, image, "/scripts/start.sh")
                started = time.monotonic()
                deadline = started + 60
                while time.monotonic() < deadline:
                    probe = subprocess.run(
                        ["docker", "exec", identity, *check],
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    if probe.returncode == 0 and probe.stdout.strip() == expected:
                        process_name = docker("exec", identity, "cat", "/proc/1/comm")
                        expected_process = (
                            "postgres" if service == "postgres" else "redis-server"
                        )
                        if process_name == expected_process:
                            break
                    time.sleep(1)
                else:
                    raise AssertionError(f"Fresh {service} did not become ready")
                status = dict(
                    line.split(":", 1)
                    for line in docker(
                        "exec", identity, "cat", "/proc/1/status"
                    ).splitlines()
                    if ":" in line
                )
                assert [int(value) for value in status["Uid"].split()] == [999] * 4
                assert int(status["CapEff"].strip(), 16) == 0
                assert status["NoNewPrivs"].strip() == "1"
                result["services"].append(
                    {
                        "service": service,
                        "startup_seconds": round(time.monotonic() - started, 3),
                        "uid": 999,
                        "effective_capabilities": 0,
                    }
                )
                print(
                    f"PASS: fresh {service} initialized and dropped privileges",
                    flush=True,
                )
            finally:
                if identity:
                    label = docker(
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "dispatcher112.acceptance"}}',
                        identity,
                    )
                    assert label == "database-initialization"
                    docker("stop", "--time", "5", identity)
        result["passed"] = True
    except Exception as error:
        result["failure"] = str(error)
        raise
    finally:
        (ROOT / "artifacts/ui-review/database-initialization.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
