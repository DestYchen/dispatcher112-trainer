"""Download the offline Russian STT model once, at installation time (never during a lesson)."""
import urllib.request
import zipfile
from pathlib import Path

NAME = "vosk-model-small-ru-0.22"
URL = f"https://alphacephei.com/vosk/models/{NAME}.zip"


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "data" / "stt"
    if (target / NAME / "am").is_dir():
        print("STT model already prepared; no network requests.")
        return
    target.mkdir(parents=True, exist_ok=True)
    archive = target / f"{NAME}.zip"
    print(f"Downloading {URL}", flush=True)
    urllib.request.urlretrieve(URL, archive)
    with zipfile.ZipFile(archive) as z:
        z.extractall(target)
    archive.unlink()
    print(f"STT model ready: {target / NAME}")


if __name__ == "__main__":
    main()
