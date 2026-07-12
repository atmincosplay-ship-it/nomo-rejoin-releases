#!/usr/bin/env python3
# NOMO REJOIN V4.33
#
# V4.33 — REJOIN ONLY MODE + LOCAL COOKIE TXT
# - ADD: REJOIN ONLY as a third runtime mode with per-package saved links.
# - SAFE: no pet routing, hatcher selection, or backend reporting in this mode.
# - ADD: package dead confirmation, optional heartbeat, loading-only visual checks.
# - CHANGE: cookie export writes one cookies/cookies.txt file, one raw cookie per line.
# - CHANGE: bulk cookie import chooses a TXT from the same local cookies folder.
# - FIX: fresh clones are opened once before the cookie DB injection retry.
#
# GitHub-ready one-file materializer:
# On first execution this pins the known V4.31 base commit, applies the V4.32
# cookie patch and V4.33 Rejoin Only patch, atomically replaces itself with the
# complete compiled NOMO source, then restarts into that full source.

from __future__ import annotations

import base64
import importlib.util
import os
import py_compile
import shutil
import sys
import tempfile
import time
import urllib.request
import zlib
from pathlib import Path

__version__ = "V4.33"

_BASE_COMMIT = "70279c0d0dfd61791c4b350b8b5192dc1ae3a919"
_BASE_URLS = (
    "https://raw.githubusercontent.com/atmincosplay-ship-it/"
    "nomo-rejoin-releases/" + _BASE_COMMIT + "/nomo_rejoin.py",
    "https://cdn.jsdelivr.net/gh/atmincosplay-ship-it/"
    "nomo-rejoin-releases@" + _BASE_COMMIT + "/nomo_rejoin.py",
)
_COOKIE_PATCH_Z = "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"
_REJOIN_PATCH_Z = "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"


def _download_base(destination: Path) -> str:
    errors = []
    for url in _BASE_URLS:
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "NOMO-Rejoin-V4.33-Materializer/1.0",
                    "Cache-Control": "no-cache",
                },
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise RuntimeError("downloaded source is unexpectedly large")
            text = data.decode("utf-8")
            if '# NOMO REJOIN V4.31' not in text[:3000] or '__version__ = "V4.31"' not in text[:10000]:
                raise RuntimeError("pinned source did not identify as NOMO V4.31")
            compile(text, str(destination), "exec")
            destination.write_text(text, encoding="utf-8")
            return url
        except Exception as exc:
            errors.append(f"{url} -> {exc}")
    raise RuntimeError("Could not download the pinned V4.31 base:\n" + "\n".join(errors))


def _load_and_run_patch(name: str, encoded: str, target: Path, folder: Path) -> None:
    patch_path = folder / f"{name}.py"
    patch_path.write_bytes(zlib.decompress(base64.b64decode(encoded)))
    spec = importlib.util.spec_from_file_location(f"_nomo_{name}", patch_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CANDIDATES = [target]
    module.main()


def _materialize() -> Path:
    target = Path(__file__).resolve()
    print("NOMO V4.33: building the complete script from pinned V4.31...")

    with tempfile.TemporaryDirectory(prefix="nomo_v433_") as tmp_name:
        tmp = Path(tmp_name)
        work = tmp / "nomo_rejoin.py"
        source_url = _download_base(work)
        print(f"Base downloaded: {source_url}")

        _load_and_run_patch("cookie_local_patch", _COOKIE_PATCH_Z, work, tmp)
        _load_and_run_patch("rejoin_only_patch", _REJOIN_PATCH_Z, work, tmp)

        final_text = work.read_text(encoding="utf-8")
        required = (
            '__version__ = "V4.33"',
            'REJOIN ONLY MODE — generic clone lifecycle',
            'Export selected clone cookies to one local cookie-only TXT file.',
        )
        missing = [item for item in required if item not in final_text]
        if missing:
            raise RuntimeError("materialized source is missing required markers: " + repr(missing))
        compile(final_text, str(work), "exec")
        py_compile.compile(str(work), doraise=True)

        install_tmp = target.with_name(".nomo_rejoin.v433.tmp.py")
        install_tmp.write_text(final_text, encoding="utf-8")
        py_compile.compile(str(install_tmp), doraise=True)
        os.chmod(install_tmp, 0o755)
        os.replace(str(install_tmp), str(target))
        os.chmod(target, 0o755)

    print("NOMO V4.33 complete source installed. Restarting...")
    time.sleep(0.4)
    return target


def main() -> None:
    try:
        target = _materialize()
    except Exception as exc:
        print(f"\nNOMO V4.33 setup failed: {exc}", file=sys.stderr)
        print("The current file was not replaced. Check internet access, then run nomo again.", file=sys.stderr)
        raise SystemExit(1)
    os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])


if __name__ == "__main__":
    main()
