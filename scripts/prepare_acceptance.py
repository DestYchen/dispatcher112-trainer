"""Create isolated audited accounts and save the public fixture on the host."""

import argparse
import json
import subprocess

from compose import ROOT, command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["browser", "load"])
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--cards", type=int, default=10)
    args = parser.parse_args()
    arguments = [
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "backend",
        "python",
        "-m",
        f"tests.prepare_{args.kind}_acceptance",
    ]
    if args.kind == "load":
        arguments += ["--users", str(args.users), "--cards", str(args.cards)]
    result = subprocess.run(
        command(ROOT, arguments), check=True, stdout=subprocess.PIPE
    )
    fixture = json.loads(result.stdout.decode("utf-8").splitlines()[-1])
    (ROOT / f"data/{args.kind}-acceptance.json").write_text(
        json.dumps(fixture), encoding="utf-8"
    )
    print(f"Prepared isolated {args.kind} acceptance fixture", flush=True)


if __name__ == "__main__":
    main()
