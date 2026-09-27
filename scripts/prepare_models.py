"""Download the fixed local model during installation, never during a lesson."""

import json
import hashlib
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ollama/ollama:0.34.4"
MODEL = "qwen2.5:3b"
MANIFEST_SHA256 = "357c53fb659c5076de1d65ccb0b397446227b71a42be9d1603d46168015c9e4b"
NAME = "dispatcher112-model-preparation"


def run(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, encoding="utf-8")


def main() -> None:
    destination = ROOT / "data" / "llm"
    destination.mkdir(parents=True, exist_ok=True)
    manifest = destination / "models/manifests/registry.ollama.ai/library/qwen2.5/3b"
    if manifest.is_file() and (destination / "manifest.json").is_file():
        manifest_bytes = manifest.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() == MANIFEST_SHA256:
            data = json.loads(manifest_bytes)
            for layer in [data["config"], *data["layers"]]:
                expected = layer["digest"].removeprefix("sha256:")
                if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                    raise RuntimeError("Invalid model blob digest")
                path = destination / "models/blobs" / ("sha256-" + expected)
                if not path.is_file():
                    break
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                        raise RuntimeError(f"Model blob is corrupted: {path}")
            else:
                probe = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
                if probe.returncode == 0:
                    print("Local model and image already prepared; no network requests.")
                    return
    subprocess.run(["docker", "pull", IMAGE], check=True)
    run("run", "--detach", "--rm", "--name", NAME,
        "--mount", f"type=bind,source={destination},target=/root/.ollama",
        "--env", "OLLAMA_NO_CLOUD=1", IMAGE, "serve")
    try:
        for _ in range(30):
            probe = subprocess.run(["docker", "exec", NAME, "ollama", "list"],
                                   capture_output=True)
            if probe.returncode == 0:
                break
            time.sleep(1)
        subprocess.run(["docker", "exec", NAME, "ollama", "pull", MODEL], check=True)
        details = run("exec", NAME, "ollama", "show", MODEL, "--modelfile")
        license_text = run("exec", NAME, "ollama", "show", MODEL, "--license")
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != MANIFEST_SHA256:
            raise RuntimeError("Downloaded model manifest differs from the verified release")

        metadata = {
            "model": MODEL, "image": IMAGE,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "manifest": json.loads(manifest.read_text(encoding="utf-8")),
        }
        (destination / "manifest.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        licenses = ROOT / "docs" / "licenses"
        licenses.mkdir(parents=True, exist_ok=True)
        (licenses / "Qwen2.5-3B-LICENSE.txt").write_text(license_text, encoding="utf-8")
        (destination / "Modelfile.txt").write_text(details, encoding="utf-8")
        print("Prepared", MODEL, metadata["manifest_sha256"], flush=True)
    finally:
        run("stop", "--time", "10", NAME)


if __name__ == "__main__":
    main()
