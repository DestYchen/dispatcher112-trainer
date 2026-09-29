"""Render every boss line of data/dialogue/boss_graph.json in all four voices (offline, CPU).

Run after the teacher approves new lines; the app only plays the WAV files.
Usage: python scripts/build_dialogue_voices.py [--model .tools/silero-v5-cis-base-nostress.pt]
"""
import argparse
import json
import urllib.request
import wave
from pathlib import Path

import torch

from build_voices import MODEL_URL, VOICES

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=ROOT / ".tools/silero-v5-cis-base-nostress.pt")
    parser.add_argument("--graph", type=Path, default=ROOT / "data/dialogue/boss_graph.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/voices")
    args = parser.parse_args()
    if not args.model.is_file():
        args.model.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, args.model)
    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    stress = graph.get("stress", {})
    torch.manual_seed(0)
    model = torch.package.PackageImporter(str(args.model)).load_pickle("tts_models", "model")
    for voice, (speaker, _, _) in VOICES.items():
        target = args.output / voice / "dialogue"
        target.mkdir(parents=True, exist_ok=True)
        for node, spec in graph["nodes"].items():
            text = stress.get(spec["text"], spec["text"])
            with torch.inference_mode():
                audio = model.apply_tts(text=text, speaker=speaker, sample_rate=24000)
            pcm = audio.clamp(-1, 1).mul(32767).to(torch.int16).cpu().numpy().tobytes()
            with wave.open(str(target / f"{node}.wav"), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(24000)
                out.writeframes(pcm)
            print(f"{voice}/dialogue/{node}.wav {len(pcm) // 2 / 24000:.1f}s", flush=True)


if __name__ == "__main__":
    main()
