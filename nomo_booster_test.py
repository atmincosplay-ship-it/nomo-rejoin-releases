#!/usr/bin/env python3
# NOMO BOOSTER TEST
# Separate GitHub test manager. It never starts NOMO REJOIN and never edits
# nomo_rejoin.py, nomo_pet_counter.lua, or the normal username_state.json files.

from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.request

VERSION = "0.2-github-test"

ROOT = Path("/storage/emulated/0/Download/nomo_booster_test")
LOCAL_PROBE = ROOT / "nomo_booster_probe.lua"
AUTOEXEC_PROBE = Path(
    "/storage/emulated/0/Delta/Autoexecute/nomo_booster_probe.lua"
)
PROBE_OUTPUT_ROOT = Path(
    "/storage/emulated/0/Delta/Workspace/nomo_rejoiner"
)

REPOSITORY = "atmincosplay-ship-it/nomo-rejoin-releases"
PY_URL = (
    "https://raw.githubusercontent.com/"
    f"{REPOSITORY}/main/nomo_booster_test.py"
)
LUA_URL = (
    "https://raw.githubusercontent.com/"
    f"{REPOSITORY}/main/nomo_booster_probe.lua"
)

PY_MARKER = "# NOMO BOOSTER TEST"
LUA_MARKER = 'Config.Version = "v0.1-phase1-safe"'


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NOMO-Booster-Test"},
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=90) as response:
        destination.write_bytes(response.read())


def backup_replace(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = destination.with_name(
            destination.name + f".bak_{stamp}"
        )
        shutil.copy2(destination, backup)
    os.replace(source, destination)


def validate_python(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if PY_MARKER not in text[:1000]:
        raise RuntimeError("test Python marker missing")
    compile(text, str(path), "exec")


def validate_lua(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if LUA_MARKER not in text:
        raise RuntimeError("Booster probe Lua marker missing")
    for forbidden in (
        "TeleportService",
        "syn.request",
        "queue_on_teleport",
    ):
        if forbidden in text:
            raise RuntimeError(
                f"unsafe Lua primitive detected: {forbidden}"
            )


def update() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="nomo_booster_update_"
    ) as tmp:
        tmp_root = Path(tmp)
        py_tmp = tmp_root / "nomo_booster_test.py"
        lua_tmp = tmp_root / "nomo_booster_probe.lua"

        print("Downloading Booster test files...")
        download(PY_URL, py_tmp)
        download(LUA_URL, lua_tmp)
        validate_python(py_tmp)
        validate_lua(lua_tmp)

        backup_replace(lua_tmp, LOCAL_PROBE)
        current = Path(__file__).resolve()
        if current != ROOT / "nomo_booster_test.py":
            backup_replace(
                py_tmp,
                ROOT / "nomo_booster_test.py",
            )
        else:
            # Replacing a running Python file is safe on Android/Linux.
            backup_replace(py_tmp, current)

    print("Booster test channel updated.")
    print("Run: nomoboost install")
    return 0


def install_probe() -> int:
    if not LOCAL_PROBE.exists():
        print("Local probe missing. Running update first...")
        result = update()
        if result != 0:
            return result

    validate_lua(LOCAL_PROBE)
    AUTOEXEC_PROBE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if AUTOEXEC_PROBE.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = AUTOEXEC_PROBE.with_name(
            AUTOEXEC_PROBE.name
            + f".before_test_{stamp}.bak"
        )
        shutil.copy2(AUTOEXEC_PROBE, backup)
        print(f"Backed up existing probe: {backup}")

    shutil.copy2(LOCAL_PROBE, AUTOEXEC_PROBE)
    print(f"Installed: {AUTOEXEC_PROBE}")
    print("Restart one Booster clone, then run: nomoboost status")
    return 0


def remove_probe() -> int:
    if not AUTOEXEC_PROBE.exists():
        print("Booster probe is not installed.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    disabled = AUTOEXEC_PROBE.with_name(
        AUTOEXEC_PROBE.name
        + f".disabled_{stamp}.bak"
    )
    AUTOEXEC_PROBE.rename(disabled)
    print(f"Disabled: {disabled}")
    print("Production NOMO files were not touched.")
    return 0


def age_text(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def read_probe(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("probe JSON is not an object")
    return value


def status(username: str = "", details: bool = False) -> int:
    if username:
        files = [
            PROBE_OUTPUT_ROOT
            / f"{username}_booster_probe.json"
        ]
    else:
        files = sorted(
            PROBE_OUTPUT_ROOT.glob(
                "*_booster_probe.json"
            )
        )

    installed = AUTOEXEC_PROBE.exists()
    print(
        "Booster probe: "
        + ("INSTALLED" if installed else "NOT INSTALLED")
    )
    print(f"AutoExec: {AUTOEXEC_PROBE}")
    print("Production NOMO: untouched")
    print("")

    if not files:
        print(
            f"No probe JSON found in {PROBE_OUTPUT_ROOT}"
        )
        return 1

    now = int(time.time())
    for path in files:
        print("=" * 68)
        print(path.name)
        try:
            data = read_probe(path)
        except Exception as exc:
            print(f"ERROR: {exc}")
            continue

        ts = int(data.get("ts", 0) or 0)
        age = max(0, now - ts) if ts else 999999
        print(
            f"user={data.get('username', '-')} "
            f"age={age_text(age)} "
            f"place={data.get('place_id', '-')} "
            f"ready={data.get('probe_ready', False)}"
        )
        print(
            f"inventory={data.get('inventory_entry_count', 0)} "
            f"parsed={data.get('parsed_pet_count', 0)} "
            f"valuable={data.get('valuable_pet_count', 0)}"
        )
        print(
            "missing="
            + json.dumps(
                data.get("missing_fields", {}),
                separators=(",", ":"),
            )
        )

        error = str(data.get("error", "") or "")
        if error:
            print(f"error={error}")

        matches = data.get("valuable_pets", [])
        if isinstance(matches, list):
            for index, item in enumerate(matches, 1):
                if not isinstance(item, dict):
                    continue
                reasons = ",".join(
                    item.get("valuable_reasons", [])
                )
                print(
                    f"{index:>3}. "
                    f"{item.get('name', '-')} "
                    f"age={item.get('age')} "
                    f"age1={item.get('base_weight_age1_kg')} "
                    f"mutation={item.get('mutation') or '-'} "
                    f"reason={reasons or '-'}"
                )

        if details:
            print(
                json.dumps(
                    data.get("sample_pets", []),
                    indent=2,
                    ensure_ascii=False,
                )
            )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Separate NOMO Booster GitHub test channel"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=(
            "status",
            "update",
            "install",
            "remove",
            "version",
        ),
    )
    parser.add_argument("username", nargs="?")
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()

    if args.command == "update":
        return update()
    if args.command == "install":
        return install_probe()
    if args.command == "remove":
        return remove_probe()
    if args.command == "version":
        print(VERSION)
        return 0
    return status(
        username=args.username or "",
        details=args.details,
    )


if __name__ == "__main__":
    raise SystemExit(main())
