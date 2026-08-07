"""Install the real uBlock Origin — the Manifest V2 build.

    uv run python scripts/install_ubo.py

Pulls `uBlock0_<version>.chromium.zip` from gorhill/uBlock's GitHub releases and
unpacks it into `~/.chamber/extensions/`.

**Not the Chrome Web Store.** The store serves uBO Lite, which is a different and
much weaker extension: MV3 removed blocking `webRequest`, so Lite cannot do what
uBO does, which is why it ships as a separate product rather than as an update. The
`.chromium` asset is the MV2 build with the full blocking engine. The
`manifest_version` assertion below exists so you cannot quietly end up with Lite.

Verified against Brave 151.1.93.132 with uBO 1.73.0 on 2026-08-06: loads, enables,
background page runs, blocking listeners installed.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chamber import paths

RELEASES = "https://api.github.com/repos/gorhill/uBlock/releases/latest"
UA = {"User-Agent": "chamber-installer"}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="reinstall even if present")
    args = ap.parse_args()

    dest = paths.extensions_dir() / "uBlock0.chromium"
    if dest.exists() and not args.force:
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        print(f"already installed: uBlock Origin {manifest['version']} (mv{manifest['manifest_version']})")
        print(f"  {dest}")
        print("  pass --force to reinstall")
        return 0

    print("looking up the latest release…")
    release = fetch_json(RELEASES)
    tag = release["tag_name"]

    asset = next(
        (a for a in release["assets"] if a["name"].endswith(".chromium.zip")),
        None,
    )
    if asset is None:
        names = ", ".join(a["name"] for a in release["assets"])
        print(f"error: no .chromium.zip in release {tag}. Assets were: {names}", file=sys.stderr)
        return 1

    size_mb = asset["size"] / 1_048_576
    print(f"downloading {asset['name']} ({size_mb:.1f} MB) from release {tag}…")
    req = urllib.request.Request(asset["browser_download_url"], headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp:
        blob = resp.read()

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        roots = {n.split("/")[0] for n in names if "/" in n}
        if len(roots) != 1:
            print(f"error: unexpected archive layout, roots={roots}", file=sys.stderr)
            return 1
        root = roots.pop()

        manifest = json.loads(zf.read(f"{root}/manifest.json"))
        mv = manifest.get("manifest_version")
        if mv != 2:
            # The whole point of taking this route rather than the Web Store.
            print(
                f"error: that archive is manifest v{mv}, not v2.\n"
                "uBlock Origin's blocking engine needs MV2's webRequest API. An MV3 "
                "build would be uBO Lite, which is a different product.",
                file=sys.stderr,
            )
            return 1
        if "webRequestBlocking" not in manifest.get("permissions", []):
            print(
                "error: the manifest has no webRequestBlocking permission — this is "
                "not the full uBlock Origin.",
                file=sys.stderr,
            )
            return 1

        if dest.exists():
            shutil.rmtree(dest)
        staging = dest.parent / f".{dest.name}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        zf.extractall(staging)
        (staging / root).rename(dest)
        shutil.rmtree(staging, ignore_errors=True)

    print()
    print(f"installed uBlock Origin {manifest['version']} (manifest v{mv})")
    print(f"  {dest}")
    print()
    print("Chamber loads it automatically on the next launch. Confirm with:")
    print("  uv run chamber doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
