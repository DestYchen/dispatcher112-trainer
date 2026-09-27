"""Change backend replicas during maintenance; preserve the setting for subsequent starts."""

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

import compose
import install_update
import technical_operations as technical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicas", type=int, choices=range(1, 5), required=True)
    parser.add_argument("--actor", type=UUID, required=True)
    parser.add_argument("--installation", type=Path, default=compose.ROOT)
    args = parser.parse_args()
    root = args.installation.resolve()
    with install_update.installation_lock(root):
        path = root / "data/materials/.technical-host/topology.json"
        if path.resolve() != path:
            raise ValueError("Topology file cannot use links")
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if state.get("phase") == "APPLYING":
            if state["replicas"] != args.replicas or state["actor"] != str(args.actor):
                raise ValueError("Resume the previous topology operation first")
        else:
            state = {
                "id": str(uuid4()),
                "actor": str(args.actor),
                "replicas": args.replicas,
                "phase": "APPLYING",
                "previous_replicas": compose.backend_replicas(root) or 1,
            }

        def exchange(action):
            return json.loads(
                technical.run(
                    compose.command(
                        root,
                        [
                            "exec",
                            "-T",
                            "--index",
                            "1",
                            "backend",
                            "python",
                            "-m",
                            "app.operations.cluster_control",
                        ],
                    ),
                    incoming=json.dumps({**state, "action": action}).encode(),
                )
            )

        # Save the identity before prepare so interruption can resume the same operation.
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        if temporary.is_symlink():
            raise ValueError("Topology file cannot use links")
        previous = path.read_bytes() if path.exists() else None
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(path)
        try:
            prepared = exchange("prepare")
        except (RuntimeError, ValueError):
            confirmed = exchange("status")
            if (
                confirmed.get("id") != state["id"]
                or confirmed.get("phase") != "APPLYING"
            ):
                if previous is not None:
                    path.write_bytes(previous)
                else:
                    path.write_text(
                        json.dumps({**state, "phase": "FAILED", "enabled": False}),
                        encoding="utf-8",
                    )
            raise
        if prepared.get("phase") == "APPLIED":
            state.update(phase="APPLIED", enabled=True)
            path.write_text(json.dumps(state), encoding="utf-8")
            print(json.dumps(prepared))
            return
        state["enabled"] = True
        path.write_text(json.dumps(state), encoding="utf-8")
        technical.run(
            compose.command(
                root,
                [
                    "up",
                    "--detach",
                    "--no-build",
                    "--no-deps",
                    "--scale",
                    f"backend={args.replicas}",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "backend",
                ],
            ),
            timeout=240,
        )
        result = exchange("finish")
        state["phase"] = "APPLIED"
        path.write_text(json.dumps(state), encoding="utf-8")
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError):
        raise SystemExit(
            "Scaling stopped. Keep maintenance enabled and resume the same command."
        ) from None
