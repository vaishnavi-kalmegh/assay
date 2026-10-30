"""Export the demo API's OpenAPI document deterministically."""

import json
from pathlib import Path

from assay.demo_api import app

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "specs" / "openapi.json"


def main() -> None:
    """Write the sorted OpenAPI document to the repository."""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document = app.openapi()
    OUTPUT.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
