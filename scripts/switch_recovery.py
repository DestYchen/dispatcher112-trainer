"""Switch local gateway ports to a restored installation and preserve both audit histories."""

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import compose
import install_update
import technical_operations as technical

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.operations.update_files import read_record, write_record  # noqa: E402

PURPOSE = "recovery-gateway-switch-1"


def bridge(root, action, state, **extra):
    payload = {
        "action": action,
        "id": state["id"],
        "actor": state["actor"],
        "reason": state["reason"],
        **extra,
    }
    command = compose.command(
        root,
        ["exec", "-T", "backend", "python", "-m", "app.operations.recovery_switch"],
    )
    for attempt in range(3):
        try:
            return json.loads(
                technical.run(
                    command, incoming=json.dumps(payload).encode(), timeout=180
                )
            )
        except RuntimeError:
            # Fencing is idempotent; a scheduler transaction can briefly own the lock.
            if action != "fence" or attempt == 2:
                raise
            time.sleep(1)


def route(root):
    current = install_update.configuration(root)
    ports = [
        {name: port[name] for name in ("target", "published", "host_ip", "protocol")}
        for port in current["services"]["gateway"]["ports"]
    ]
    result = {
        "ports": ports,
        "cors_origins": json.loads(
            current["services"]["backend"]["environment"]["CORS_ORIGINS"]
        ),
    }
    compose.validate_gateway_route(result)
    return result


def runtime(root, *arguments):
    return technical.run(compose.command(root, list(arguments)), timeout=300)


def archive(root, state, label, destination, key):
    path = destination / f"{label}-audit.json"
    if not path.exists():
        history = bridge(root, "export", state, source=technical.project_name(root))
        write_record(path, history, key, "recovery-audit-history-1", exclusive=True)
    return read_record(path, key, "recovery-audit-history-1", limit=128 * 1024 * 1024)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["promote", "rollback", "status"])
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--actor", type=UUID)
    parser.add_argument("--reason")
    args = parser.parse_args()
    target = args.target.resolve()
    parent = (ROOT / ".recovery").resolve()
    if (
        not target.is_relative_to(parent)
        or target == parent
        or args.target.absolute() != target
    ):
        raise ValueError(
            "Select an existing direct path inside this installation's .recovery"
        )
    recovered = json.loads((target / "state.json").read_text(encoding="utf-8"))
    if recovered.get("status") != "ACTIVE" or recovered.get("stopped"):
        raise ValueError("Activate and start the recovered installation first")
    workspace = target / "workspace"
    if not workspace.is_dir() or workspace.resolve() != workspace:
        raise ValueError("Invalid restored workspace")
    key = (ROOT / ".secrets/backup-signing.key").read_bytes().strip()
    path = target / "gateway-switch.json"
    state = read_record(path, key, PURPOSE) if path.exists() else None
    if args.action == "status":
        print(
            json.dumps(
                {
                    "phase": state["phase"] if state else "NOT_STARTED",
                    "id": state["id"] if state else None,
                }
            )
        )
        return
    # The existing installer lock prevents switching concurrently with an update.
    with (
        install_update.installation_lock(ROOT),
        install_update.installation_lock(workspace),
    ):
        replaced = False
        if state and state["phase"] == "RETURNED" and args.action == "promote":
            if (
                args.actor is None
                or not args.reason
                or not 5 <= len(args.reason.strip()) <= 1000
            ):
                raise ValueError(
                    "A new switching cycle requires an administrator and reason"
                )
            if state["root"] != str(ROOT) or state["workspace"] != str(workspace):
                raise ValueError("Switch state belongs to another installation")
            history = target / f"switch-{UUID(state['id'])}"
            if history.resolve() != history:
                raise ValueError("History directory cannot use links")
            history.mkdir(exist_ok=True)
            completed = history / "completed.json"
            if completed.exists():
                if read_record(completed, key, PURPOSE) != state:
                    raise ValueError("Completed switching history has changed")
            else:
                write_record(completed, state, key, PURPOSE, exclusive=True)
            state = None
            replaced = True
        if state is None:
            if (
                args.action != "promote"
                or args.actor is None
                or not args.reason
                or len(args.reason.strip()) < 5
            ):
                raise ValueError(
                    "Promotion requires an administrator UUID and a reason"
                )
            state = {
                "id": str(uuid4()),
                "actor": str(args.actor),
                "reason": args.reason.strip(),
                "phase": "PREPARED",
                "root": str(ROOT),
                "workspace": str(workspace),
                "primary_route": route(ROOT),
                "restored_route": route(workspace),
            }
            write_record(path, state, key, PURPOSE, exclusive=not replaced)
        if state["root"] != str(ROOT) or state["workspace"] != str(workspace):
            raise ValueError("Switch state belongs to another installation")
        if (args.action == "promote" and state["phase"] == "ACTIVE") or (
            args.action == "rollback" and state["phase"] == "RETURNED"
        ):
            print(
                json.dumps(
                    {"id": state["id"], "phase": state["phase"], "repeated": True}
                )
            )
            return
        if args.action == "promote" and state["phase"] not in {"PREPARED", "PROMOTING"}:
            raise ValueError("This switch cannot be promoted again")
        for root in (ROOT, workspace):
            runtime(
                root,
                "exec",
                "-T",
                "backend",
                "python",
                "-c",
                "import app.operations.recovery_switch",
            )
        state["phase"] = "PROMOTING" if args.action == "promote" else "RETURNING"
        write_record(path, state, key, PURPOSE)
        for root in (ROOT, workspace):
            print(f"Fencing {'primary' if root == ROOT else 'restored'} installation", flush=True)
            bridge(root, "fence", state)
        # Closing both entry points prevents new login/audit events during transfer.
        for root in (ROOT, workspace):
            runtime(root, "stop", "gateway")
        destination = target / f"switch-{state['id']}" / args.action
        if destination.resolve() != destination:
            raise ValueError("History directory cannot use links")
        destination.mkdir(parents=True, exist_ok=True)
        source = ROOT if args.action == "promote" else workspace
        receiver = workspace if args.action == "promote" else ROOT
        history = archive(source, state, "source", destination, key)
        archive(receiver, state, "receiver", destination, key)
        print("Preserving audit histories", flush=True)
        bridge(receiver, "import", state, history=history)
        if args.action == "promote":
            compose.save_gateway_route(ROOT, {**state["primary_route"], "ports": []})
            compose.save_gateway_route(workspace, state["primary_route"])
        else:
            compose.save_gateway_route(ROOT, state["primary_route"])
            compose.save_gateway_route(workspace, state["restored_route"])
        for root in (ROOT, workspace):
            print(f"Applying {'primary' if root == ROOT else 'restored'} gateway route", flush=True)
            runtime(
                root,
                "up",
                "--detach",
                "--no-deps",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "120",
                "backend",
                "gateway",
            )
        bridge(
            receiver,
            "release",
            state,
            phase="ACTIVE" if args.action == "promote" else "RETURNED",
        )
        if args.action == "rollback":
            bridge(workspace, "release", state, phase="STANDBY")
        state["phase"] = "ACTIVE" if args.action == "promote" else "RETURNED"
        write_record(path, state, key, PURPOSE)
        print(
            json.dumps(
                {
                    "id": state["id"],
                    "phase": state["phase"],
                    "history": str(destination),
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        raise SystemExit(
            "Switch stopped. Databases, history and state are preserved; inspect status and resume or rollback."
        ) from None
