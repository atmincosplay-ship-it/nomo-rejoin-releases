#!/usr/bin/env python3
"""
NOMO Rejoin Clean Skeleton

This is a small, system-based rewrite base for the Redfinger/MuMu rejoin tool.
It is intentionally conservative:

- exact package PID stop only
- no broad package stop commands
- one shared rejoin engine for every mode
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


VERSION = "V0.2 DEV DELTA STATE"
BASE_DIR = Path("/storage/emulated/0/Download/nomo_rejoin_clean")
CONFIG_FILE = BASE_DIR / "config.json"
DELTA_STATE_DIR = Path("/storage/emulated/0/Delta/Workspace/nomo_rejoiner")
STATE_DIR = DELTA_STATE_DIR

PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.]+$")
DEFAULT_PACKAGE_REGEX = r"^(free\.noka|premium\.noka|com\.roblox)"


DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "active_mode": "market",
    "package_regex": DEFAULT_PACKAGE_REGEX,
    "state_dir": str(STATE_DIR),
    "state_dir_candidates": [
        "/storage/emulated/0/Delta/Workspace/nomo_rejoiner",
        "/storage/emulated/0/Download/nomo_rejoin/nomo_rejoiner",
        "/storage/emulated/0/Download/nomo_rejoiner",
    ],
    "identity_api_enabled": True,
    "identity_cache_seconds": 86400,
    "identity_cache": {},
    "open_cooldown_seconds": 8,
    "backend": {
        "provider": "cloudflare",
        "worker_url": "https://nomo-rejoin.atmincosplay.workers.dev",
        "secret": "",
        "timeout_seconds": 8,
        "enabled": False,
    },
    "routing": {
        "market_restock_below": 50,
        "market_ready_at": 200,
        "market_idle_min_pet_to_market": 50,
        "market_idle_no_gain_seconds": 900,
        "hatcher_min_ready_pets": 100,
        "backend_stale_seconds": 7200,
    },
    "stop": {
        "term_wait_seconds": 0.8,
        "kill_wait_seconds": 0.6,
        "tries": 3,
        "verify_sibling_pids": True,
    },
    "modes": {
        "market": {
            "label": "Market",
            "link": "roblox://placeId=129954712878723",
            "state_kind": "market",
            "stale_seconds": 180,
            "soft_first": False,
        },
        "gag": {
            "label": "Grow a Garden",
            "link": "roblox://placeId=126884695634066",
            "state_kind": "gag",
            "stale_seconds": 180,
            "soft_first": False,
        },
        "rejoin_only": {
            "label": "Rejoin Only",
            "link": "",
            "state_kind": "any",
            "stale_seconds": 180,
            "soft_first": False,
        },
    },
    "packages": [],
}


# ============================================================
# Core
# ============================================================


def now() -> int:
    return int(time.time())


def log(message: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def safe_package(pkg: str) -> str:
    pkg = str(pkg or "").strip()
    if not pkg or not PACKAGE_RE.match(pkg):
        raise ValueError(f"unsafe package name: {pkg!r}")
    return pkg


def sanitize_state_name(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"[^0-9A-Za-z_]", "_", value)
    return value or "unknown"


def run_cmd(args: List[str], timeout: int = 10) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") + "\nTIMEOUT"
    except Exception as exc:
        return 1, str(exc)


def run_root(script: str, timeout: int = 10) -> Tuple[int, str]:
    return run_cmd(["su", "-c", script], timeout=timeout)


def remove_file_quiet(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


def load_config(path: Path = CONFIG_FILE) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8"))
            deep_update(cfg, user_cfg)
        except Exception as exc:
            log(f"config load failed, using defaults: {exc}")
    return cfg


def save_config(cfg: Dict[str, Any], path: Path = CONFIG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def deep_update(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


# ============================================================
# Packages
# ============================================================


@dataclass
class PackageTarget:
    package: str
    name: str
    mode: str
    link: str
    enabled: bool = True
    state_file: str = ""


@dataclass
class StateSnapshot:
    username: str = ""
    pet_count: int = 0
    egg_total: int = 0
    age: int = 999999
    place_id: str = ""
    job_id: str = ""
    server_link: str = ""
    disconnected: bool = False
    raw: Optional[Dict[str, Any]] = None


@dataclass
class RouteDecision:
    target: str
    reason: str
    link: str = ""
    hard: bool = False
    report_backend: bool = False


def installed_packages(pattern: str = DEFAULT_PACKAGE_REGEX) -> List[str]:
    code, out = run_cmd(["pm", "list", "packages"], timeout=10)
    if code != 0:
        code, out = run_root("pm list packages", timeout=10)
    rx = re.compile(pattern)
    found: List[str] = []
    for line in out.splitlines():
        pkg = line.replace("package:", "").strip()
        if pkg and rx.search(pkg):
            found.append(pkg)
    return sorted(set(found))


# ============================================================
# Backend / Fleet Brain
# ============================================================


class BackendClient:
    """Cloudflare/D1 client.

    This is deliberately separate from the local rejoin engine. The backend can
    suggest where a clone should go, but only AndroidControl performs the stop
    and open.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.backend = cfg.get("backend", {}) or {}

    def enabled(self) -> bool:
        return bool(self.backend.get("enabled", False))

    def base_url(self) -> str:
        url = str(self.backend.get("worker_url") or "").strip().rstrip("/")
        for suffix in [
            "/hatcher/update",
            "/hatchers",
            "/accounts/register",
            "/accounts",
            "/admin/stats",
            "/admin/cleanup-stale",
            "/health",
        ]:
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
        return url

    def headers(self) -> Dict[str, str]:
        secret = str(self.backend.get("secret") or "").strip()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "nomo-rejoin-clean/cloudflare",
        }
        if secret:
            headers["X-Nomo-Secret"] = secret
            headers["x-nomo-secret"] = secret
        return headers

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if not self.enabled():
            return None, "backend disabled"

        base = self.base_url()
        secret = str(self.backend.get("secret") or "").strip()
        if not base:
            return None, "missing worker url"
        if not secret:
            return None, "missing backend secret"

        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        timeout = int(self.backend.get("timeout_seconds", 8) or 8)
        try:
            req = urllib.request.Request(
                base + path,
                data=body,
                method=str(method or "GET").upper(),
                headers=self.headers(),
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
            if not text.strip():
                return {}, None
            return json.loads(text), None
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                text = str(exc)
            return None, f"http {exc.code}: {text[:180]}"
        except Exception as exc:
            return None, str(exc)

    def health(self) -> Tuple[bool, str]:
        data, err = self.request("GET", "/health")
        if err:
            return False, err
        if isinstance(data, dict) and data.get("ok") is False:
            return False, str(data.get("error") or "backend health failed")
        return True, "backend ok"

    def register_accounts(self, accounts: List[Dict[str, Any]]) -> Tuple[bool, str]:
        data, err = self.request("POST", "/accounts/register", {"accounts": accounts})
        if err:
            return False, err
        if isinstance(data, dict) and data.get("ok") is False:
            return False, str(data.get("error") or "register failed")
        return True, "registered"

    def read_hatchers(self) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
        data, err = self.request("GET", "/hatchers")
        if err:
            return {}, err
        raw = (data or {}).get("hatchers", {})
        if isinstance(raw, dict):
            return {
                str(name): dict(item)
                for name, item in raw.items()
                if isinstance(item, dict)
            }, None
        if isinstance(raw, list):
            out: Dict[str, Dict[str, Any]] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(
                    item.get("name")
                    or item.get("hatcher_name")
                    or item.get("username")
                    or item.get("roblox_username")
                    or ""
                ).strip()
                if name:
                    out[name] = dict(item)
            return out, None
        return {}, "bad hatchers response"

    def update_worker(self, name: str, role: str, snapshot: StateSnapshot, link: str) -> Tuple[bool, str]:
        payload = {
            "name": name,
            "username": name,
            "hatcher_name": name,
            "role": role,
            "mode": role,
            "status": "ready" if snapshot.pet_count >= int((self.cfg.get("routing") or {}).get("hatcher_min_ready_pets", 100) or 100) else "low_stock",
            "pet_count": snapshot.pet_count,
            "egg_total": snapshot.egg_total,
            "place_id": snapshot.place_id,
            "job_id": snapshot.job_id,
            "server_link": link or snapshot.server_link,
            "updated_at": now(),
        }
        data, err = self.request("POST", "/hatcher/update", payload)
        if err:
            return False, err
        if isinstance(data, dict) and data.get("ok") is False:
            return False, str(data.get("error") or "worker update failed")
        return True, "updated"


def _backend_item_age_seconds(item: Dict[str, Any]) -> int:
    value = item.get("updated_at") or item.get("updated_at_seconds") or item.get("updated_at_ms") or 0
    try:
        n = int(value or 0)
        if n > 9999999999:
            n = n // 1000
        return max(0, now() - n) if n > 0 else 999999
    except Exception:
        return 999999


def choose_restock_server(cfg: Dict[str, Any], backend: BackendClient) -> Tuple[str, str]:
    routing = cfg.get("routing", {}) or {}
    min_pets = int(routing.get("hatcher_min_ready_pets", 100) or 100)
    stale_seconds = int(routing.get("backend_stale_seconds", 7200) or 7200)
    hatchers, err = backend.read_hatchers()
    if err:
        return "", err

    best_name = ""
    best_link = ""
    best_pets = -1
    for name, item in hatchers.items():
        if _backend_item_age_seconds(item) > stale_seconds:
            continue
        if str(item.get("status") or "").lower() in {"disabled", "removed", "no_server"}:
            continue
        pets = int(item.get("pet_count", 0) or 0)
        link = str(item.get("server_link") or item.get("link") or "").strip()
        if pets >= min_pets and link and pets > best_pets:
            best_name, best_link, best_pets = name, link, pets

    if best_link:
        return best_link, f"{best_name} pets={best_pets}"
    return "", "no ready restock server"


def configured_targets(cfg: Dict[str, Any]) -> List[PackageTarget]:
    modes = cfg.get("modes", {}) or {}
    active = str(cfg.get("active_mode") or "market")
    targets: List[PackageTarget] = []
    for i, item in enumerate(cfg.get("packages") or []):
        if not isinstance(item, dict):
            continue
        pkg = safe_package(item.get("package", ""))
        mode = str(item.get("mode") or active)
        mode_cfg = modes.get(mode, {}) or {}
        link = str(item.get("link") or mode_cfg.get("link") or "")
        targets.append(
            PackageTarget(
                package=pkg,
                name=str(item.get("name") or f"clone{i + 1}"),
                mode=mode,
                link=link,
                enabled=bool(item.get("enabled", True)),
                state_file=str(item.get("state_file") or ""),
            )
        )
    return targets


def bootstrap_packages(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if cfg.get("packages"):
        return cfg
    pkgs = installed_packages(str(cfg.get("package_regex") or DEFAULT_PACKAGE_REGEX))
    cfg["packages"] = [
        {"package": pkg, "name": f"clone{i + 1}", "mode": cfg.get("active_mode", "market"), "enabled": True}
        for i, pkg in enumerate(pkgs)
    ]
    return cfg


# ============================================================
# Identity
# ============================================================


def get_cookie_from_package(pkg: str) -> Optional[str]:
    """Read this package's Roblox WebView cookie without mutating login state."""
    pkg = safe_package(pkg)
    candidates = [
        f"/data/data/{pkg}/app_webview/Default/Cookies",
        f"/data/user/0/{pkg}/app_webview/Default/Cookies",
        f"/data/data/{pkg}/app_webview/Cookies",
        f"/data/user/0/{pkg}/app_webview/Cookies",
    ]
    tmp = Path(f"/sdcard/Download/tmp_nomo_cookie_{re.sub(r'[^A-Za-z0-9_.-]', '_', pkg)}_{os.getpid()}.db")

    try:
        for src in candidates:
            remove_file_quiet(tmp)
            code, _out = run_root(f"cp {shlex.quote(src)} {shlex.quote(str(tmp))}", timeout=15)
            if code != 0 or not tmp.exists():
                continue
            try:
                conn = sqlite3.connect(str(tmp), timeout=2)
                cur = conn.cursor()
                cur.execute(
                    "SELECT value FROM cookies "
                    "WHERE host_key LIKE '%.roblox.com' AND name='.ROBLOSECURITY' "
                    "ORDER BY rowid DESC LIMIT 1"
                )
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    return str(row[0])
            except Exception:
                continue
    finally:
        remove_file_quiet(tmp)

    return None


def get_username_from_cookie(cookie: str, timeout: int = 10) -> Tuple[str, str]:
    if not cookie:
        return "", ""
    url = "https://users.roblox.com/v1/users/authenticated"
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "User-Agent": "nomo-rejoin-clean/identity",
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        username = str(data.get("name") or "").strip()
        user_id = str(data.get("id") or "").strip()
        return username, user_id
    except Exception:
        return "", ""


def resolve_username_from_package(pkg: str) -> Tuple[str, str, str]:
    cookie = get_cookie_from_package(pkg)
    if not cookie:
        return "", "", "no package cookie"
    username, user_id = get_username_from_cookie(cookie, timeout=12)
    if username:
        return username, user_id, "cookie/API"
    return "", "", "cookie API failed"


def cached_identity(cfg: Dict[str, Any], pkg: str) -> Tuple[str, str]:
    cache = cfg.get("identity_cache", {}) or {}
    item = cache.get(pkg) if isinstance(cache, dict) else None
    if not isinstance(item, dict):
        return "", ""
    max_age = int(cfg.get("identity_cache_seconds", 86400) or 86400)
    ts = int(item.get("ts", 0) or 0)
    if max_age > 0 and ts > 0 and now() - ts > max_age:
        return "", ""
    return str(item.get("username") or ""), str(item.get("user_id") or "")


def remember_identity(cfg: Dict[str, Any], pkg: str, username: str, user_id: str, source: str) -> bool:
    username = str(username or "").strip()
    if not username:
        return False
    cache = cfg.setdefault("identity_cache", {})
    old = cache.get(pkg) if isinstance(cache, dict) else None
    new_item = {
        "username": username,
        "user_id": str(user_id or ""),
        "source": str(source or "unknown"),
        "ts": now(),
    }
    if old == new_item:
        return False
    cache[pkg] = new_item
    return True


def resolve_target_identity(target: PackageTarget, cfg: Dict[str, Any], refresh_api: bool = False) -> Tuple[str, str, str, bool]:
    username = str(target.name or "").strip()
    user_id = ""
    source = "target name"
    changed = False

    if username.lower().startswith("clone") or username == target.package:
        username = ""

    if not refresh_api:
        cached_name, cached_id = cached_identity(cfg, target.package)
        if cached_name:
            return cached_name, cached_id, "cache", False

    if cfg.get("identity_api_enabled", True):
        api_name, api_id, api_source = resolve_username_from_package(target.package)
        if api_name:
            changed = remember_identity(cfg, target.package, api_name, api_id, api_source)
            return api_name, api_id, api_source, changed
        source = api_source

    if username:
        changed = remember_identity(cfg, target.package, username, user_id, source)
        return username, user_id, source, changed

    return "", "", source, changed


# ============================================================
# Exact PID Android Control
# ============================================================


def _process_name_matches_package(name: str, pkg: str) -> bool:
    name = str(name or "").strip()
    pkg = str(pkg or "").strip()
    return bool(pkg) and (name == pkg or name.startswith(pkg + ":"))


def package_pids(pkg: str) -> List[int]:
    pkg = safe_package(pkg)
    code, out = run_root("ps -A -o PID,NAME 2>/dev/null", timeout=6)
    if code != 0 or not out:
        return []
    pids: List[int] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        name = parts[1].strip()
        if _process_name_matches_package(name, pkg):
            pids.append(pid)
    return sorted(set(pids))


def package_alive(pkg: str) -> bool:
    return bool(package_pids(pkg))


def kill_exact_package_pids(pkg: str, pids: Iterable[int], sig: str) -> None:
    pkg = safe_package(pkg)
    sig_arg = "-9" if str(sig).upper() == "KILL" else "-15"
    pid_words = " ".join(str(int(pid)) for pid in pids if int(pid) > 1)
    if not pid_words:
        return

    # Revalidate /proc/<pid>/cmdline before each exact-PID kill.
    script = (
        f"pkg={shlex.quote(pkg)}; "
        f"for pid in {pid_words}; do "
        "[ -r /proc/$pid/cmdline ] || continue; "
        "name=$(tr '\\000' '\\n' < /proc/$pid/cmdline 2>/dev/null | head -n 1); "
        f"case \"$name\" in \"$pkg\"|\"$pkg\":*) kill {sig_arg} \"$pid\" 2>/dev/null ;; esac; "
        "done"
    )
    run_root(script, timeout=8)


def stop_exact_package(pkg: str, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    pkg = safe_package(pkg)
    stop_cfg = cfg.get("stop", {}) or {}
    tries = int(stop_cfg.get("tries", 3) or 3)
    term_wait = float(stop_cfg.get("term_wait_seconds", 0.8) or 0.8)
    kill_wait = float(stop_cfg.get("kill_wait_seconds", 0.6) or 0.6)

    initial = package_pids(pkg)
    if not initial:
        return True, "already stopped"

    log(f"{pkg}: exact PID stop -> {','.join(map(str, initial))}")
    for _ in range(max(1, tries)):
        pids = package_pids(pkg)
        if not pids:
            return True, "stopped"
        kill_exact_package_pids(pkg, pids, "TERM")
        time.sleep(term_wait)
        survivors = package_pids(pkg)
        if survivors:
            kill_exact_package_pids(pkg, survivors, "KILL")
            time.sleep(kill_wait)
        if not package_pids(pkg):
            return True, "stopped"

    survivors = package_pids(pkg)
    if survivors:
        return False, "survived exact PID stop: " + ",".join(map(str, survivors))
    return True, "stopped"


def normalize_roblox_link(link: str) -> str:
    link = str(link or "").strip()
    if not link:
        return ""
    allowed = (
        link.startswith("roblox://"),
        link.startswith("https://www.roblox.com/"),
        link.startswith("https://roblox.com/"),
    )
    if not any(allowed):
        raise ValueError(f"unsafe launch link: {link!r}")
    return link


def open_package(pkg: str, link: str, soft: bool = False) -> Tuple[bool, str]:
    pkg = safe_package(pkg)
    link = normalize_roblox_link(link)
    if not link:
        return False, "no link"

    flags = "-f 0x24000000 " if soft else ""
    script = (
        "am start "
        f"{flags}-a android.intent.action.VIEW "
        f"-d {shlex.quote(link)} "
        f"-p {shlex.quote(pkg)}"
    )
    code, out = run_root(script, timeout=15)
    if code == 0:
        return True, "soft open" if soft else "opened"
    return False, (out or "open failed").strip()[:160]


# ============================================================
# Lua State
# ============================================================


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def state_dirs(cfg: Dict[str, Any]) -> List[Path]:
    raw: List[str] = []
    raw.append(str(cfg.get("state_dir") or STATE_DIR))
    for item in cfg.get("state_dir_candidates", []) or []:
        raw.append(str(item or ""))

    # Executor/workspace fallbacks from the stable rejoin source.
    raw.extend(
        [
            "/storage/emulated/0/Delta/Workspace/nomo_rejoiner",
            "/storage/emulated/0/Download/nomo_rejoin/nomo_rejoiner",
            "/storage/emulated/0/Download/nomo_rejoiner",
        ]
    )

    for index in range(1, 13):
        raw.append(
            f"/storage/emulated/0/RobloxClone{index:03d}/Arceus X/Workspace/nomo_rejoiner"
        )
        raw.append(
            f"/storage/emulated/0/RobloxClone{index:03d}/Delta/Workspace/nomo_rejoiner"
        )

    out: List[Path] = []
    seen = set()
    for value in raw:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(Path(value))
    return out


def newest_state_file(state_dir: Path) -> Optional[Path]:
    best: Optional[Path] = None
    best_ts = -1
    for path in state_dir.glob("*_state.json"):
        data = read_json(path) or {}
        ts = int(data.get("ts") or path.stat().st_mtime)
        if ts > best_ts:
            best = path
            best_ts = ts
    return best


def state_for_username(username: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    username = str(username or "").strip()
    if not username:
        return None

    safe_name = sanitize_state_name(username)
    for folder in state_dirs(cfg):
        direct = folder / f"{safe_name}_state.json"
        data = read_json(direct)
        if data is not None:
            return data

    # Fallback for older counters or case differences.
    want = username.lower()
    for folder in state_dirs(cfg):
        try:
            for path in folder.glob("*_state.json"):
                item = read_json(path) or {}
                found = str(item.get("username") or item.get("user_name") or "").strip().lower()
                if found == want:
                    return item
        except Exception:
            pass

    return None


def state_for_target(target: PackageTarget, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if target.state_file:
        data = read_json(Path(target.state_file))
        if data is not None:
            return data
    username, _user_id, _source, _changed = resolve_target_identity(target, cfg)
    return state_for_username(username, cfg)


def snapshot_for_target(target: PackageTarget, cfg: Dict[str, Any]) -> StateSnapshot:
    data = state_for_target(target, cfg) or {}
    username, _user_id, _source, _changed = resolve_target_identity(target, cfg)
    return StateSnapshot(
        username=str(data.get("username") or data.get("user_name") or username or target.name),
        pet_count=int(data.get("pet_count", 0) or 0),
        egg_total=int(data.get("egg_total", 0) or 0),
        age=int(data.get("age", 999999) or 999999),
        place_id=str(data.get("place_id") or ""),
        job_id=str(data.get("job_id") or ""),
        server_link=str(data.get("server_link") or data.get("rejoin_link") or ""),
        disconnected=bool(data.get("disconnected", False)),
        raw=data,
    )


# ============================================================
# Fleet Routing
# ============================================================


def decide_route(target: PackageTarget, snapshot: StateSnapshot, cfg: Dict[str, Any], backend: BackendClient) -> RouteDecision:
    """Decide the next destination.

    This function owns fleet intent only. It never stops or opens Android apps.
    """
    mode = str(target.mode or cfg.get("active_mode") or "market").lower()
    routing = cfg.get("routing", {}) or {}

    if snapshot.disconnected:
        return RouteDecision(target=mode, reason="disconnect recovery", link=target.link, hard=True)

    if mode == "rejoin_only":
        return RouteDecision(target="rejoin_only", reason="local recovery only", link=target.link)

    if mode in {"hatcher", "booster"}:
        return RouteDecision(
            target=mode,
            reason=f"{mode} report",
            link=target.link or snapshot.server_link,
            report_backend=True,
        )

    if mode != "market":
        return RouteDecision(target=mode, reason="unknown mode fallback", link=target.link)

    restock_below = int(routing.get("market_restock_below", 50) or 50)
    ready_at = int(routing.get("market_ready_at", 200) or 200)

    if snapshot.pet_count < restock_below and backend.enabled():
        link, why = choose_restock_server(cfg, backend)
        if link:
            return RouteDecision(target="restock", reason=f"pets<{restock_below}; {why}", link=link)
        return RouteDecision(target="market", reason=f"low pets but {why}", link=target.link)

    if snapshot.pet_count >= ready_at:
        return RouteDecision(target="market", reason=f"pets>={ready_at}", link=target.link)

    return RouteDecision(target="market", reason="stay", link=target.link)


# ============================================================
# Shared Rejoin Engine
# ============================================================


class RejoinEngine:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    def restart(self, target: PackageTarget, soft: bool = False) -> Tuple[bool, str]:
        mode_cfg = (self.cfg.get("modes", {}) or {}).get(target.mode, {}) or {}
        link = target.link or str(mode_cfg.get("link") or "")
        return self.open_link(target, link, soft=soft)

    def open_link(self, target: PackageTarget, link: str, soft: bool = False) -> Tuple[bool, str]:
        if not soft:
            stopped, stop_note = stop_exact_package(target.package, self.cfg)
            if not stopped:
                return False, f"stop failed: {stop_note}"
        ok, open_note = open_package(target.package, link, soft=soft)
        return ok, open_note

    def apply_decision(self, target: PackageTarget, decision: RouteDecision) -> Tuple[bool, str]:
        if decision.report_backend and decision.target in {"hatcher", "booster"}:
            return True, decision.reason
        if not decision.link:
            return True, decision.reason
        if decision.target == "market" and decision.reason == "stay":
            return True, "stay"
        return self.open_link(target, decision.link, soft=not decision.hard)

    def restart_all(self, targets: Iterable[PackageTarget]) -> None:
        cooldown = float(self.cfg.get("open_cooldown_seconds", 8) or 8)
        for target in targets:
            if not target.enabled:
                continue
            ok, note = self.restart(target, soft=False)
            log(f"{target.name} {target.package}: {note}")
            time.sleep(cooldown)


# ============================================================
# CLI
# ============================================================


def cmd_init(args: argparse.Namespace) -> int:
    cfg = bootstrap_packages(load_config(args.config))
    save_config(cfg, args.config)
    log(f"created/updated {args.config}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = bootstrap_packages(load_config(args.config))
    backend = BackendClient(cfg)
    targets = configured_targets(cfg)
    changed = False
    for target in targets:
        username, user_id, source, did_change = resolve_target_identity(
            target,
            cfg,
            refresh_api=bool(getattr(args, "refresh_api", False)),
        )
        changed = changed or did_change
        snapshot = snapshot_for_target(target, cfg)
        fresh = "fresh" if snapshot.raw and snapshot.age < 180 else "stale"
        alive = "alive" if package_alive(target.package) else "dead"
        decision = decide_route(target, snapshot, cfg, backend)
        identity = username or snapshot.username or target.name
        uid_note = f"/{user_id}" if user_id else ""
        print(
            f"{target.name:8} {identity + uid_note:18} {target.package:26} {target.mode:8} "
            f"{alive:5} {fresh:5} pets={snapshot.pet_count:<4} -> {decision.target} ({decision.reason})"
        )
        if source not in {"cache", "target name"}:
            print(f"  identity: {source}")
    if changed:
        save_config(cfg, args.config)
        log("identity cache updated")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = bootstrap_packages(load_config(args.config))
    backend = BackendClient(cfg)
    if not backend.enabled():
        log("backend disabled")
        return 1
    targets = filter_targets(configured_targets(cfg), args.target)
    ok_all = True
    for target in targets:
        if target.mode not in {"hatcher", "booster"}:
            log(f"{target.name}: skipped non-worker mode {target.mode}")
            continue
        snapshot = snapshot_for_target(target, cfg)
        ok, note = backend.update_worker(target.name, target.mode, snapshot, target.link)
        ok_all = ok_all and ok
        log(f"{target.name}: backend {note}")
    return 0 if ok_all else 1


def cmd_route(args: argparse.Namespace) -> int:
    cfg = bootstrap_packages(load_config(args.config))
    backend = BackendClient(cfg)
    engine = RejoinEngine(cfg)
    targets = filter_targets(configured_targets(cfg), args.target)
    ok_all = True
    for target in targets:
        snapshot = snapshot_for_target(target, cfg)
        decision = decide_route(target, snapshot, cfg, backend)
        log(f"{target.name}: decision -> {decision.target} ({decision.reason})")
        ok, note = engine.apply_decision(target, decision)
        ok_all = ok_all and ok
        log(f"{target.name}: {note}")
    return 0 if ok_all else 1


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = bootstrap_packages(load_config(args.config))
    targets = filter_targets(configured_targets(cfg), args.target)
    for target in targets:
        ok, note = stop_exact_package(target.package, cfg)
        log(f"{target.name} {target.package}: {note}")
        if not ok:
            return 1
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cfg = bootstrap_packages(load_config(args.config))
    engine = RejoinEngine(cfg)
    targets = filter_targets(configured_targets(cfg), args.target)
    for target in targets:
        ok, note = engine.restart(target, soft=bool(args.soft))
        log(f"{target.name} {target.package}: {note}")
        if not ok:
            return 1
        time.sleep(float(cfg.get("open_cooldown_seconds", 8) or 8))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    text = Path(__file__).read_text(encoding="utf-8")
    banned = ["am " + "force-stop", "p" + "kill", "kill" + "all"]
    found = [word for word in banned if word in text]
    cfg = load_config(args.config)
    print(f"NOMO Rejoin Clean {VERSION}")
    print(f"config: {args.config}")
    print("forbidden stop commands: " + ("FOUND " + ", ".join(found) if found else "none"))
    print("state folders:")
    for folder in state_dirs(cfg)[:8]:
        status = "OK" if folder.exists() else "-"
        print(f"  {status} {folder}")
    return 1 if found else 0


def filter_targets(targets: List[PackageTarget], selector: str) -> List[PackageTarget]:
    selector = str(selector or "all").strip()
    if selector == "all":
        return [target for target in targets if target.enabled]
    out = [
        target
        for target in targets
        if target.package == selector or target.name == selector
    ]
    if not out:
        raise SystemExit(f"target not found: {selector}")
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"NOMO Rejoin Clean {VERSION}")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--refresh-api", action="store_true")
    list_cmd.set_defaults(func=cmd_list)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    report = sub.add_parser("report")
    report.add_argument("target", nargs="?", default="all")
    report.set_defaults(func=cmd_report)

    route = sub.add_parser("route")
    route.add_argument("target", nargs="?", default="all")
    route.set_defaults(func=cmd_route)

    stop = sub.add_parser("stop")
    stop.add_argument("target", nargs="?", default="all")
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser("restart")
    restart.add_argument("target", nargs="?", default="all")
    restart.add_argument("--soft", action="store_true")
    restart.set_defaults(func=cmd_restart)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
