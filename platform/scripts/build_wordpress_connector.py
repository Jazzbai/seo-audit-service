"""Build the uploadable WordPress connector from an explicit asset allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("forgeseo-connector.php", "readme.txt")


def build(output: Path) -> dict:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the installed plugin's identity stable for upgrades. Branding does
    # not rename routes, stored metadata or plugin activation identifiers.
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in ASSETS:
            data = (ROOT / "wordpress" / name).read_bytes()
            info = ZipInfo("forgeseo-connector/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": list(ASSETS)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "releases" / "jazzb-seo-connector-0.5.0.zip")
    print(json.dumps(build(parser.parse_args().output)))
