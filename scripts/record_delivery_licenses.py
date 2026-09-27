"""Record installed document-build dependencies and their original license files."""

import importlib.metadata
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    "python-docx",
    "python-pptx",
    "lxml",
    "XlsxWriter",
    "Pillow",
    "typing_extensions",
)


def main():
    output = ROOT / "docs/licenses/delivery-tools"
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in PACKAGES:
        distribution = importlib.metadata.distribution(name)
        licenses = []
        for item in distribution.files or []:
            if not any(
                token in item.name.lower()
                for token in ("license", "licence", "copyright")
            ):
                continue
            source = Path(distribution.locate_file(item)).resolve()
            if source.is_file():
                destination = output / name / item.name
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(source.read_bytes())
                licenses.append(destination.relative_to(ROOT).as_posix())
        rows.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "license": distribution.metadata.get("License-Expression")
                or distribution.metadata.get("License")
                or "; ".join(
                    value
                    for value in distribution.metadata.get_all("Classifier", [])
                    if value.startswith("License ::")
                ),
                "notices": licenses,
            }
        )
    (ROOT / "docs/licenses/DELIVERY-TOOLS.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Recorded {len(rows)} build-tool distributions")


if __name__ == "__main__":
    main()
