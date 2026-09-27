"""Inspect actual process credentials and exercise allowed/forbidden writes in live services."""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

SERVICES = ("backend", "worker", "sip_worker", "telephony")
EXTRA_SERVICES = {
    "frontend": (
        10001,
        ["/tmp", "/app/node_modules/.vite-temp"],
        ["/app", "/app/src", "/app/node_modules", "/run/tls"],
        True,
    ),
    "gateway": (10001, ["/tmp"], ["/etc/nginx", "/var/cache/nginx", "/run/tls"], True),
    "languagetool_tls": (
        10001,
        ["/tmp"],
        ["/etc/nginx", "/var/cache/nginx", "/run/tls"],
        True,
    ),
    "ollama_tls": (
        10001,
        ["/tmp"],
        ["/etc/nginx", "/var/cache/nginx", "/run/tls"],
        True,
    ),
    "languagetool": (10001, ["/tmp"], ["/opt/languagetool", "/etc"], False),
    "ollama": (10001, ["/tmp"], ["/models", "/etc"], False),
    "postgres": (
        999,
        ["/tmp", "/var/lib/postgresql/data", "/var/run/postgresql"],
        ["/etc", "/run/tls"],
        False,
    ),
    "redis": (999, ["/tmp", "/data"], ["/etc", "/run/tls"], False),
    "backup": (
        0,
        ["/tmp", "/backups", "/queue", "/status"],
        ["/source", "/config", "/scripts", "/run/tls", "/etc"],
        False,
    ),
}
WRITE_PROBE = """
target="$1/.dispatcher-permission-$2"
if (umask 077; set -C; : > "$target") 2>/dev/null; then
    rm -- "$target"
    printf writable
else
    printf denied
fi
"""
PROBE = r"""
import json, os, sys, tempfile
from pathlib import Path

service = sys.argv[1]
status = dict(line.split(':', 1) for line in Path('/proc/1/status').read_text().splitlines() if ':' in line)
uids = [int(value) for value in status['Uid'].split()]
assert os.geteuid() == 10001 and uids == [10001] * 4, (service, uids)
assert int(status['CapEff'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert not Path('/var/run/docker.sock').exists()
assert not Path('/secrets').exists() and not Path('/backups').exists()
assert not Path('/run/private/ca.key').exists()
assert 'POSTGRES_PASSWORD' not in os.environ and 'APP_DB_PASSWORD' not in os.environ
tls = Path('/run/credentials/tls' if service == 'telephony' else '/run/tls')
assert (tls / 'tls.key').stat().st_uid == 10001
assert (tls / 'tls.key').stat().st_mode & 0o777 == 0o600
assert (tls / 'ca.crt').is_file()
allowed = ['/tmp']
denied = [str(tls)]
if service != 'telephony':
    denied += ['/app', '/data', '/data/voices', '/data/backup-status', '/data/backup-operations/results']
    if service == 'backend':
        allowed += ['/data/materials', '/data/telephony/speech', '/data/backup-operations/requests']
        assert Path('/run/control/token').is_file() and Path('/run/backup-control/token').is_file()
    else:
        assert not Path('/run/control/token').exists() and not Path('/run/backup-control/token').exists()
    if service == 'sip_worker':
        allowed += ['/data/telephony', '/data/telephony/speech']
        denied += ['/data/telephony/recordings']
    if service == 'worker':
        denied += ['/data/materials', '/data/telephony', '/data/telephony/speech', '/data/backup-operations/requests']
else:
    allowed += ['/data/telephony/speech', '/data/telephony/recordings', '/run/asterisk']
    denied += ['/opt', '/etc/asterisk']
for path in allowed:
    with tempfile.TemporaryFile(prefix='.runtime-permission-', suffix='.probe', dir=path) as output:
        output.write(b'permission test')
        output.flush()
for path in denied:
    try:
        with tempfile.TemporaryFile(prefix='.runtime-permission-', suffix='.probe', dir=path):
            raise AssertionError('Unexpected writable path: ' + path)
    except PermissionError:
        continue
    except OSError as error:
        if error.errno != 30:
            raise
print(json.dumps({'service': service, 'uids': uids, 'effective_capabilities': 0, 'no_new_privileges': True, 'allowed_writes': allowed, 'denied_writes': denied}))
"""


def main():
    result = {"passed": False, "services": []}
    try:
        for service in SERVICES:
            completed = subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    service,
                    "python3",
                    "-c",
                    PROBE,
                    service,
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            if completed.returncode:
                raise AssertionError(f"{service}: {completed.stderr}")
            result["services"].append(json.loads(completed.stdout))
            print(
                f"PASS: {service} uses UID 10001, no effective capabilities and bounded writes",
                flush=True,
            )
        for service, (uid, allowed, denied, private_tls) in EXTRA_SERVICES.items():
            command = [
                "docker",
                "compose",
                "exec",
                "-T",
                "--user",
                f"{uid}:{uid}",
                service,
            ]
            completed = subprocess.run(
                [*command, "cat", "/proc/1/status"],
                text=True,
                capture_output=True,
                timeout=30,
                check=True,
            )
            status = dict(
                line.split(":", 1)
                for line in completed.stdout.splitlines()
                if ":" in line
            )
            uids = [int(value) for value in status["Uid"].split()]
            assert uids == [uid] * 4, (service, uids)
            assert int(status["CapEff"].strip(), 16) == 0, service
            assert status["NoNewPrivs"].strip() == "1", service
            subprocess.run(
                [
                    *command,
                    "sh",
                    "-eu",
                    "-c",
                    "test ! -e /var/run/docker.sock; test ! -e /run/control/token; test ! -e /run/backup-control/token",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            if private_tls:
                key = subprocess.run(
                    [*command, "stat", "-c", "%u %a", "/run/tls/tls.key"],
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                assert key.stdout.strip() == "10001 600", service
            for path in allowed + denied:
                probe = subprocess.run(
                    [
                        *command,
                        "sh",
                        "-eu",
                        "-c",
                        WRITE_PROBE,
                        "permission-test",
                        path,
                        uuid4().hex,
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                expected = "writable" if path in allowed else "denied"
                assert probe.stdout == expected, (service, path, probe.stdout)
            result["services"].append(
                {
                    "service": service,
                    "uids": uids,
                    "effective_capabilities": 0,
                    "no_new_privileges": True,
                    "allowed_writes": allowed,
                    "denied_writes": denied,
                }
            )
            print(
                f"PASS: {service}, UID {uid}, no effective capabilities, bounded writes",
                flush=True,
            )
        for service in (*SERVICES, *EXTRA_SERVICES):
            container = subprocess.run(
                ["docker", "compose", "ps", "-q", service],
                check=True,
                text=True,
                capture_output=True,
                timeout=30,
            ).stdout.strip()
            assert container, service
            readonly = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.HostConfig.ReadonlyRootfs}}",
                    container,
                ],
                check=True,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert readonly.stdout.strip() == "true", service
        result["all_roots_read_only"] = True
        result["passed"] = True
    except Exception as error:
        result["failure"] = str(error)
        raise
    finally:
        destination = (
            Path(__file__).resolve().parents[1]
            / "artifacts/ui-review/runtime-permissions.json"
        )
        destination.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
