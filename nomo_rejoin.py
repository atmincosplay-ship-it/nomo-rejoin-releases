#!/usr/bin/env python3
# NOMO REJOIN V4.12
#
# V4.12 — PACKAGE-SCOPED CAPTCHA UI OVERRIDE
# - FIX: A visible Roblox "Verifying you're not a bot" / "Start Puzzle"
#        screen now overrides a fresh Lua heartbeat. Fresh state no longer makes
#        a blocked clone appear Online/Ingame.
# - SAFE: Automatic detection trusts package-scoped uiautomator text only; text
#         from one floating clone is never broadcast to the other packages.
# - SOLVER: A visible challenge starts one solver job without pre-restarting the
#           package. Other clones keep running and queued opens for that package
#           are cancelled while the challenge is visible.
# - FIX: Provider NO_CAPTCHA is treated as a false negative when the same
#        package-scoped verification UI is still visible. The package is held
#        and retried only after the existing 10-minute cooldown.
#
# V4.11 — CUSTOM LUA INPUT TERMINATOR FIX
# - FIX: AutoExec Manager option 3 now finishes pasted scripts with a line
#        containing only STOP instead of END.
# - FIX: Normal Lua `end` lines are preserved and no longer stop input early.
# - SCOPE: QOL-only; no rejoin, PID, solver, backend, or routing logic changed.
#
import os
import re
import sys
import json
import html
import time
import shlex
import shutil
import select
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import sqlite3
import threading
import getpass
import hashlib
import platform
import zipfile
from pathlib import Path
from datetime import datetime

# Single source of truth for the build number. Bump this on every update — it is
# stamped into the Termux banner so each Redfinger instance shows which build it
# runs. If two RF instances behave differently (one 11h session, one rejoin loop)
# this line tells you at a glance whether they're even on the same code.
__version__ = "V4.12"

LEGACY_BASE_DIR = Path("/storage/emulated/0/Download/gag_lite_rejoiner")
BASE_DIR = Path("/storage/emulated/0/Download/nomo_rejoin")
NOMO_APP_FILE = BASE_DIR / "nomo_rejoin.py"
NOMO_BACKUP_DIR = BASE_DIR / "backups"
NOMO_UPDATE_URL = (
    "https://raw.githubusercontent.com/atmincosplay-ship-it/"
    "nomo-rejoin-releases/refs/heads/main/nomo_rejoin.py"
)
NOMO_UPDATE_MAX_BYTES = 8 * 1024 * 1024


def _migrate_legacy_base_dir_once():
    """Copy persistent local data into the renamed folder without deleting old data."""
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        if not LEGACY_BASE_DIR.exists() or LEGACY_BASE_DIR.resolve() == BASE_DIR.resolve():
            return
        names = (
            "nomo.json", "config.json", "global_config_template.json",
            "runtime.json", "hatcher_reporter_config.json",
            "hatcher_reporter_runtime.json", "cookie_cache.json",
            "captcha_hold.json", "activity.log", "cookie_export.txt",
            "cookie_auth.txt", "cookies_only.txt"
        )
        for name in names:
            src = LEGACY_BASE_DIR / name
            dst = BASE_DIR / name
            if src.exists() and not dst.exists():
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
    except Exception:
        pass


_migrate_legacy_base_dir_once()
CONFIG_FILE = BASE_DIR / "config.json"
CONFIG_TEMPLATE_FILE = BASE_DIR / "global_config_template.json"
RUNTIME_FILE = BASE_DIR / "runtime.json"
HATCHER_CONFIG_FILE = BASE_DIR / "hatcher_reporter_config.json"
HATCHER_RUNTIME_FILE = BASE_DIR / "hatcher_reporter_runtime.json"

# --------------------------------------------------------------------------
# UNIFIED CONFIG FILE
# One physical file (nomo.json) holds market config, hatcher config, and both
# runtimes as sections. The four *_FILE constants above become LOGICAL keys:
# save_json/load_json redirect them to sections of nomo.json, so every existing
# call site and all migration logic keep working unchanged. On first run the
# old separate files are auto-imported and backed up.
# --------------------------------------------------------------------------
NOMO_FILE = BASE_DIR / "nomo.json"

# Map each logical file constant -> its section key inside nomo.json.
_MERGED_SECTIONS = {
    str(CONFIG_FILE): "market",
    str(HATCHER_CONFIG_FILE): "hatcher",
    str(RUNTIME_FILE): "runtime_market",
    str(HATCHER_RUNTIME_FILE): "runtime_hatcher",
}

_NOMO_CACHE = {"data": None}


def _nomo_read_all():
    """Read the whole unified file (cached in-process)."""
    if _NOMO_CACHE["data"] is None:
        try:
            if NOMO_FILE.exists():
                _NOMO_CACHE["data"] = json.loads(NOMO_FILE.read_text())
            else:
                _NOMO_CACHE["data"] = {}
        except Exception:
            _NOMO_CACHE["data"] = {}
    return _NOMO_CACHE["data"]


def _nomo_write_all(data):
    _NOMO_CACHE["data"] = data
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NOMO_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(str(tmp), str(NOMO_FILE))
    except Exception:
        NOMO_FILE.write_text(json.dumps(data, indent=2))


def _migrate_legacy_files():
    """First-run: import the 4 old separate files into nomo.json, back them up."""
    if NOMO_FILE.exists():
        return  # already migrated
    merged = {}
    imported_any = False
    for legacy_path, section in _MERGED_SECTIONS.items():
        p = Path(legacy_path)
        if p.exists():
            try:
                merged[section] = json.loads(p.read_text())
                imported_any = True
            except Exception:
                merged[section] = {}
    if imported_any:
        _nomo_write_all(merged)
        # Back up (rename) the old files so we don't read them again but keep them.
        for legacy_path in _MERGED_SECTIONS:
            p = Path(legacy_path)
            if p.exists():
                try:
                    bak = p.with_suffix(p.suffix + ".bak")
                    os.replace(str(p), str(bak))
                except Exception:
                    pass

COOKIE_CACHE = BASE_DIR / "cookie_cache.json"

STATE_FOLDER_NAME = "nomo_rejoiner"
LEGACY_STATE_FOLDER_NAME = "gag_rejoiner"

_STOP_REQUESTED = False

def request_stop():
    global _STOP_REQUESTED
    _STOP_REQUESTED = True

DEFAULT_MARKET_LINK = "roblox://experiences/start?placeId=129954712878723"
DEFAULT_RESTOCK_LINK = "roblox://experiences/start?placeId=126884695634066"

DEFAULT_WORKSPACE_SYNC_COMMAND = (
    'cd /storage/emulated/0 && '
    '[ -f config.zip ] || exit 0; '
    'for i in 001 002 003 004; do '
    'mkdir -p "RobloxClone$i" && unzip -oq config.zip -d "RobloxClone$i"; '
    'done && '
    '[ -d "RobloxClone001/Arceus X/Workspace" ] && '
    'mkdir -p "Delta" && cp -rf "RobloxClone001/Arceus X/Workspace" "Delta/" || true'
)

DEFAULT_TABS = [
    {
        "enabled": True,
        "package": "premium.nokaA",
        "user_name": "nomomarket1",
        "stat_file": "/storage/emulated/0/RobloxClone001/Arceus X/Workspace/nomo_rejoiner/state.json",
        "restock_link": DEFAULT_RESTOCK_LINK
    },
    {
        "enabled": True,
        "package": "premium.nokaB",
        "user_name": "nomomarket2",
        "stat_file": "/storage/emulated/0/RobloxClone002/Arceus X/Workspace/nomo_rejoiner/state.json",
        "restock_link": DEFAULT_RESTOCK_LINK
    },
    {
        "enabled": True,
        "package": "premium.nokaC",
        "user_name": "nomostock1",
        "stat_file": "/storage/emulated/0/RobloxClone003/Arceus X/Workspace/nomo_rejoiner/state.json",
        "restock_link": DEFAULT_RESTOCK_LINK
    },
    {
        "enabled": True,
        "package": "premium.nokaD",
        "user_name": "nomostock2",
        "stat_file": "/storage/emulated/0/RobloxClone004/Arceus X/Workspace/nomo_rejoiner/state.json",
        "restock_link": DEFAULT_RESTOCK_LINK
    }
]

DEFAULT_CONFIG = {
    "use_su": True,
    "use_color": True,
    "update_source_url": NOMO_UPDATE_URL,
    "update_timeout_seconds": 45,

    "cookie_webhook_url": "",   # stored locally, never hardcoded
    "login_challenge_detection_enabled": True,
    "login_challenge_api_detection_enabled": True,
    "login_challenge_ui_detection_enabled": True,
    "login_challenge_skip_blocked_packages": True,
    "login_challenge_webhook_url": "",
    "login_challenge_alert_cooldown_seconds": 1800,

    # V4.12: package-scoped Android UI text can reveal a live Roblox verification
    # overlay even while the Lua heartbeat remains fresh behind it. This signal
    # overrides Online/Ingame, but never falls back to unscoped text automatically.
    "captcha_ui_override_enabled": True,
    "captcha_ui_retry_seconds": 600,
    "captcha_ui_require_package_scope": True,

    # CAPTCHA solver. A bare host uses /api/captcha/solve; a URL that already
    # contains a path is used exactly as entered. Automatic jobs start only after
    # a real challenge is detected and run outside the dashboard/main loop.
    "solver_enabled": False,
    "solver_endpoint": "https://solver.wintercode.dev",
    "solver_api_key": "",
    "solver_place_id": "126884695634066",
    "solver_timeout_seconds": 180,
    # Provider submissions are expensive and the provider explicitly forbids
    # repeatedly sending solved/NO_CAPTCHA cookies. Keep at least 10 minutes.
    "solver_retry_cooldown_seconds": 600,
    "solver_min_resubmit_seconds": 600,
    "solver_probe_after_seconds": 45,
    # If the package stays stale/no-state, re-run the provider/UI probe after
    # this cooldown. V3.82 accidentally made the startup probe a one-shot that
    # could also be skipped forever when opened_at was 0.
    # V3.84: retained for compatibility, but automatic provider checks are now
    # once per actual open/rejoin generation instead of periodic stale-state scans.
    "solver_probe_repeat_seconds": 600,
    "solver_probe_once_per_open": True,
    "solver_require_cookie_precheck": True,
    "solver_probe_stale_state_enabled": False,
    # Native Roblox CAPTCHA text is often invisible to Lua/uiautomator. Once a
    # package has produced no state for solver_probe_after_seconds, allow the
    # configured provider to make the authoritative check once for that open.
    "solver_provider_probe_on_no_state": True,
    "solver_rejoin_on_success": True,
    # NO_CAPTCHA means the provider found nothing to solve. Do not kill/open the
    # package a second time after the current open already started.
    "solver_rejoin_on_no_captcha": False,
    # After CAPTCHA_SUCCESS, allow one clean rejoin. If that rejoin
    # still produces no fresh state, isolate only that package instead of
    # entering another solver/homepage hard-retry loop.
    "manual_hold_after_solver_rejoin_timeout": True,

    "active_rejoin_mode": "market",
    "market_mode_enabled": True,
    "hatcher_mode_enabled": False,

    # Hatcher safety: when HATCHER mode is enabled, disable scheduled hop /
    # teleport-loop features that belong to Market mode. Soft open stays enabled
    # because it is needed for stale/public-server recovery without force-stopping.
    "hatcher_disable_scheduled_hop_on_enable": True,
    "hatcher_disable_soft_hop_fallback_on_enable": True,
    "hatcher_disable_clear_cache_soft_hop_on_enable": True,

    "ui_width": 110,
    "check_interval": 10,
    "delay_between_open": 45,
    "min_seconds_between_reopen": 60,

    "alive_old_state_hard_seconds": 180,
    # V3.79: how long a clone that is ALIVE at hatcher startup may sit without a
    # readable state file before the table stops showing "waiting" and surfaces
    # an actionable "check Lua/username" note. Short so it never looks stuck.
    "hatcher_startup_grace_seconds": 75,
    # V3.79: automatically resolve each package's real username from its cookie
    # (Roblox API) -> fallback state file, so the table always matches the
    # account actually logged in. Makes the manual "get username" step optional.
    "auto_resolve_usernames_enabled": True,
    "username_resolve_interval_seconds": 600,
    # V3.79: MARKET phantom-age ceiling (mirror of the hatcher's
    # hatcher_alive_old_state_max_valid_seconds). A missing `ts` in the state
    # file makes age compute to an impossible value (e.g. 277h). Any age above
    # this is treated as invalid and IGNORED — never drives a kill+open. This is
    # the guard the market path was missing while the hatcher path had it, which
    # is why one instance ran 11h and another rejoin-looped on the same 277h age.
    "alive_old_state_max_valid_seconds": 86400,

    "open_all_on_start": True,
    "open_only_closed_on_start": True,
    "smart_open_queue": True,
    # Safety for clone builds where pidof/ps can miss a running Roblox process.
    # If state.json is fresh, Start Rejoin treats the clone as already open.
    # If Android says a clone is alive but state is stale/missing, reopen only
    # that affected package instead of trusting the stale process.
    "start_skip_if_state_fresh_enabled": True,
    "start_skip_fresh_state_seconds": 45,
    "start_reopen_alive_without_fresh_state": True,
    "start_alive_without_fresh_state_mode": "hard_force",
    "wait_fresh_after_open": True,
    "open_wait_fresh_seconds": 300,
    "open_wait_check_seconds": 5,
    "post_open_grace_seconds": 360,

    # Homepage / cold-start recovery:
    # Some Android clone builds accept the roblox:// intent, open Roblox, but stay
    # on the Roblox home page without creating a fresh state.json. Wait long enough
    # for slow Redfinger loading, then queue ONE hard force-stop relaunch.
    "homepage_stuck_hard_fallback_enabled": True,

    # Ditch soft rejoin entirely: every open is a hard kill+open. Soft landed on
    # the homepage and triggered a hard fallback anyway, so this removes the
    # wasted attempt + timeout. `kill` is isolated so hard never cascades.
    "disable_soft_rejoin": True,

    # Pool-wide stagger: minimum seconds between any two hard opens across ALL
    # clones. Spaces out restarts so reloads don't overlap (memory spikes) and
    # a multi-clone recovery doesn't look like a mass restart. 0 = disabled.
    "open_stagger_seconds": 20,

    # V3.79: LAST-SECOND HEALTH RECHECK before any hard kill+open.
    # A queued item can sit for minutes while an earlier package finishes its
    # loading/fresh-wait. By then that clone may have healed itself. Without this
    # gate, ONE stuck clone timing out drags every other queued package into a
    # force-stop, which looks like a mass restart of the whole pool.
    "recheck_before_hard_open": True,
    # Items younger than this skip the recheck (front-of-queue retries for the
    # clone we just killed must fire immediately, not get cancelled).
    "recheck_min_queue_age_seconds": 20,

    # V3.79: Only ONE package may be inside a kill / open / wait-for-fresh cycle
    # at any moment. Everything else waits its turn in the queue.
    "single_flight_open": True,
    # Safety release for the single-flight lock if a cycle dies without cleanup.
    "single_flight_max_seconds": 420,

    # Show a live activity log panel below the status table (every rejoiner
    # action). Also written to activity.log. Set false to hide the panel.
    "show_activity_log": True,
    "activity_log_lines": 6,
    "homepage_stuck_fallback_seconds": 240,
    "homepage_stuck_max_hard_retries": 1,

    # Queue/runtime self-heal:
    # If Termux is stopped during an open/wait cycle, runtime.json may keep a
    # stale queued/cooldown/loading note and block the next run until manually
    # deleting runtime.json. This clears only temporary queue/cooldown fields
    # after a short safety window; it does NOT delete config.json.
    "queue_stuck_self_heal_enabled": True,
    "queue_stuck_reset_seconds": 360,

    # Auto-delete legacy/orphan state files (old naming) once a fresh
    # <username>_state.json is confirmed. Off by default; safe when on
    # (only deletes stale, non-current-format files).
    "auto_delete_orphan_state": False,
    "orphan_state_stale_seconds": 3600,

    # Stronger watchdog for the exact case where the screen stays Queued for a
    # long time and manual recovery is `rm -f runtime.json && nomo`. This does a
    # safer in-script reset of temporary runtime/open-queue state after a long
    # stuck period. It does NOT touch config.json.
    "runtime_stuck_reset_enabled": True,
    "runtime_stuck_reset_seconds": 420,
    "runtime_stuck_reset_min_tabs": 1,

    "rejoin_if_crash": True,
    "ignore_alive_stale_state": True,

    "restock_below": 50,
    "ready_market_at": 200,
    "idle_no_gain_seconds": 300,
    "idle_min_pet_to_market": 50,
    "state_stale_seconds": 180,
    "stale_reopen_after_seconds": 60,
    "alive_old_state_rejoin_after_seconds": 60,
    "force_rejoin_if_stale_seconds": 180,

    # Disconnect popup / Error 288 recovery:
    # Roblox can stay alive on the Android side while the in-game Lua state writer
    # stops updating because a Disconnected popup is covering the client.
    # Default is soft rejoin first; option 6 is still the manual hard force-stop.
    "disconnect_popup_rejoin_enabled": True,
    "disconnect_popup_stale_seconds": 180,
    "force_stop_alive_on_disconnect_popup": False,

    # V3.88: Python-side fallback for Roblox native Error 288 / disconnect
    # dialogs that some executors do not expose to Lua. One uiautomator dump is
    # cached and shared across every package in the dashboard cycle. Detection is
    # package-scoped only, so text from one clone can never mark all clones.
    "android_disconnect_ui_detection_enabled": True,
    "android_ui_snapshot_cache_seconds": 25,
    "android_ui_snapshot_timeout_seconds": 15,
    # Require the same package-scoped native popup in two dashboard snapshots.
    # This blocks transient/stale accessibility dumps from restarting a healthy clone.
    "android_disconnect_confirmations_required": 2,
    "android_disconnect_confirmation_window_seconds": 30,
    # Direct Lua flags are trusted only from Counter v2.5+ or when they carry a
    # recent observation timestamp. Older counters could latch disconnected=true.
    "disconnect_observation_max_age_seconds": 45,
    # Verify that sibling clone PIDs remain alive after stopping one target.
    "verify_sibling_pids_on_stop": True,

    # Direct Lua UI popup detection. If the state writer sees Roblox's
    # Disconnected/Kicked/Error dialog, it writes disconnected=true.
    # This catches cases where ts/pet_count still update, so stale detection
    # would never trigger. Default = soft rejoin first.
    "disconnect_ui_rejoin_enabled": True,
    "disconnect_ui_retry_seconds": 20,
    "disconnect_ui_hard_after_seconds": 180,
    "disconnect_ui_soft_first_seconds": 0,
    "minimized_window_detection_enabled": True,
    "minimized_window_reopen_enabled": False,  # status-only; dumpsys is not package-safe enough
    "minimized_window_reopen_mode": "soft",
    # Redfinger uses Android freeform/floating tasks. A plain `am start` can hide
    # every peer window even though their processes and Lua heartbeats remain
    # alive. Ask ActivityManager to launch the recovered clone in freeform mode.
    "preserve_multiwindow_on_launch": True,
    "launch_windowing_mode": 5,

    # Hatcher popup/session-expired handling:
    # If the Roblox-side state writer directly sees a kicked/disconnected/session-expired
    # popup while hatcher mode is active, hard force-stop + reopen the hatcher server.
    # Soft intent often cannot recover from PandoraHub session-expired popups.
    "hatcher_disconnect_ui_hard_force": True,
    "hatcher_disconnect_ui_retry_seconds": 15,

    # V3.75: Hatcher-only hard recovery for the real Redfinger/Roblox case where
    # Android still reports Roblox alive, but the state file is very old or missing
    # because Roblox is stuck on Connection Failed / disconnected modal / home page.
    # This does NOT use generic popup text, so it avoids V3.70/V3.72 false rejoins.
    "hatcher_alive_old_state_hard_force_enabled": True,
    "hatcher_alive_old_state_hard_force_seconds": 300,
    "hatcher_alive_old_state_max_valid_seconds": 86400,
    "hatcher_alive_old_state_after_open_grace_seconds": 180,
    "hatcher_alive_old_state_hard_force_cooldown_seconds": 900,

    "market_link": DEFAULT_MARKET_LINK,
    "restock_link": DEFAULT_RESTOCK_LINK,

    # Shared hatcher backend. backend_provider = jsonbin or cloudflare.
    # Keep jsonbin_hatchers_enabled as the shared on/off key for backward compatibility.
    "backend_provider": "jsonbin",
    "jsonbin_hatchers_enabled": False,
    "jsonbin_bin_id": "",
    "jsonbin_api_key": "",
    "jsonbin_key_header": "X-Master-Key",
    "jsonbin_timeout_seconds": 8,
    "cloudflare_worker_url": "",
    "cloudflare_secret": "",
    "cloudflare_timeout_seconds": 8,
    "jsonbin_cache_seconds": 600,
    "jsonbin_stale_seconds": 7200,
    "jsonbin_min_hatcher_pets": 100,
    "jsonbin_no_hatcher_action": "stay_market",  # stay_market / fallback_restock
    "jsonbin_fallback_restock": False,  # old compatibility key

    "prefer_experience_start_links": True,
    "prefer_https_game_links": False,
    "soft_hop_enabled": True,
    "soft_hop_fallback_hard": False,
    "soft_hop_wait_fresh_seconds": 240,
    "scheduled_hop_enabled": False,
    "scheduled_hop_interval_seconds": 600,
    "scheduled_hop_jitter_seconds": 30,
    "scheduled_hop_delay_if_pets_drop_seconds": 60,
    "periodic_hard_refresh_enabled": False,
    "periodic_hard_refresh_seconds": 3600,
    "periodic_hard_refresh_include_manual": True,

    "clear_cache_enabled": False,
    "clear_cache_on_hard_open": True,
    "clear_cache_on_soft_hop": False,
    "clear_cache_min_interval_seconds": 1800,

    # Optional local file hook. Useful when config.zip must be expanded into clone
    # workspaces before Roblox/executor loads. Guarded command skips if config.zip
    # does not exist. Cooldown prevents heavy unzip/copy spam.
    "workspace_sync_enabled": True,
    "workspace_sync_market_only": True,
    "workspace_sync_command": DEFAULT_WORKSPACE_SYNC_COMMAND,
    "workspace_sync_on_start": True,
    "workspace_sync_before_open": True,
    "workspace_sync_periodic_enabled": False,
    "workspace_sync_interval_seconds": 10800,
    "workspace_sync_timeout_seconds": 90,

    # Safety: never force-stop an app that Android still reports alive.
    # Route changes/stale recovery become soft-open instead; hard force-stop is only for dead packages.
    "no_force_stop_alive": True,
    "alive_open_mode": "soft",

    "tabs": DEFAULT_TABS
}

DEFAULT_HATCHER_PROFILE = {
    "enabled": True,
    "package": "premium.nokaA",
    "hatcher_name": "nomohatch1",
    "server_link": "YOUR_HATCHING_PRIVATE_SERVER_LINK",
    "state_file": "/storage/emulated/0/RobloxClone001/Arceus X/Workspace/nomo_rejoiner/state.json",

    # Simple default: ready is based on pet_count only.
    # Eggs are still uploaded fully for future sniper logic.
    "ready_mode": "pet_only",
    "ready_pet_count": 200,

    # Future/advanced egg-ready logic. Disabled by default.
    "enable_egg_ready": False,
    "required_eggs": {},
    "status_when_not_ready": "busy"
}

DEFAULT_HATCHER_PROFILES = [
    {
        **DEFAULT_HATCHER_PROFILE,
        "package": "premium.nokaA",
        "hatcher_name": "nomohatch1",
        "state_file": "/storage/emulated/0/RobloxClone001/Arceus X/Workspace/nomo_rejoiner/state.json",
    },
    {
        **DEFAULT_HATCHER_PROFILE,
        "package": "premium.nokaB",
        "hatcher_name": "nomohatch2",
        "state_file": "/storage/emulated/0/RobloxClone002/Arceus X/Workspace/nomo_rejoiner/state.json",
    },
    {
        **DEFAULT_HATCHER_PROFILE,
        "package": "premium.nokaC",
        "hatcher_name": "nomohatch3",
        "state_file": "/storage/emulated/0/RobloxClone003/Arceus X/Workspace/nomo_rejoiner/state.json",
    },
    {
        **DEFAULT_HATCHER_PROFILE,
        "package": "premium.nokaD",
        "hatcher_name": "nomohatch4",
        "state_file": "/storage/emulated/0/RobloxClone004/Arceus X/Workspace/nomo_rejoiner/state.json",
    },
]

DEFAULT_HATCHER_CONFIG = {
    "enabled": True,

    # low-request reporting:
    # local state is checked every heartbeat_interval seconds;
    # JSONBin only uploads on status/config signature change, plus a slow forced heartbeat.
    "update_only_on_status_change": True,
    "force_update_every_seconds": 3600,
    # Keep Cloudflare cheap: do not upload transient kick/disconnect states.
    # Hatcher rejoiner handles those locally; server_link does not change.
    "upload_disconnect_state": False,
    "heartbeat_interval": 60,

    # Market side can use a hatcher once it has at least this many pets.
    # This uploads only when crossing the threshold, not on every pet change.
    "upload_pet_threshold": 100,

    # safe hatcher keep-alive:
    # default does NOT re-open an alive Roblox just because state is stale.
    # It only queues a reopen when Android says the package is actually dead.
    "hatcher_rejoin_alive_stale": False,
    "hatcher_dead_confirm_seconds": 60,

    # V3.75: strict hatcher hard recovery for alive-but-old/missing state.
    # This is separate from hatcher_rejoin_alive_stale; it force-stops only after
    # the state is old for a long time and then waits on a cooldown to avoid loops.
    "hatcher_alive_old_state_hard_force_enabled": True,
    "hatcher_alive_old_state_hard_force_seconds": 300,
    "hatcher_alive_old_state_max_valid_seconds": 86400,  # FIX V3.78: was 81400
    "hatcher_alive_old_state_after_open_grace_seconds": 180,
    "hatcher_alive_old_state_hard_force_cooldown_seconds": 900,

    # Teleport/server detection. Public/private detection can false-positive on
    # some Roblox/private-server share links because game.PrivateServerId may be
    # blank even after joining the user's private server. Keep it OFF by default.
    # Wrong-place detection stays ON because place_id is reliable.
    "detect_public_server": False,
    "detect_wrong_place": True,
    "rejoin_public_server": True,
    "public_server_rejoin_after_seconds": 20,
    "expected_place_id": "126884695634066",

    "backend_provider": "jsonbin",
    "jsonbin_bin_id": "YOUR_BIN_ID",
    "jsonbin_api_key": "YOUR_KEY",
    "jsonbin_key_header": "X-Master-Key",
    "jsonbin_timeout_seconds": 10,
    "cloudflare_worker_url": "",
    "cloudflare_secret": "",
    "cloudflare_timeout_seconds": 8,

    # per package / per hatcher server config
    "hatchers": DEFAULT_HATCHER_PROFILES
}

# ============================================================
# COLORS
# ============================================================

_COLOR_ON = sys.stdout.isatty()

RESET = "\033[0m"
BOLD = "1"
DIM = "2"
RED = "91"
GREEN = "92"
YELLOW = "93"
BLUE = "94"
MAGENTA = "95"
CYAN = "96"
WHITE = "97"


def set_color_enabled(cfg):
    global _COLOR_ON
    _COLOR_ON = bool(cfg.get("use_color", True)) and sys.stdout.isatty()


def col(text, code):
    if not _COLOR_ON or not code:
        return str(text)
    return f"\033[{code}m{text}{RESET}"


def pad(text, width, code=None, right=False):
    """Pad on the PLAIN text, then colorize, so columns stay aligned."""
    t = cut(str(text), width)
    t = t.rjust(width) if right else t.ljust(width)
    return col(t, code)


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def vis_len(s):
    """Visible length, ignoring ANSI color codes."""
    return len(_ANSI_RE.sub("", str(s)))


# ============================================================
# BANNER + SYSTEM USAGE
# ============================================================

# NOMO REJOIN banner. Keep it clear on narrow Termux screens.
BANNER_ART = r""" _   _  ___  __  __  ___
| \ | |/ _ \|  \/  |/ _ \
|  \| | | | | |\/| | | | |
| |\  | |_| | |  | | |_| |
|_| \_|\___/|_|  |_|\___/
 ____  _____     _  ___ ___ _   _
|  _ \| ____|   | |/ _ \_ _| \ | |
| |_) |  _|  _  | | | | | ||  \| |
|  _ <| |___| |_| | |_| | || |\  |
|_| \_\_____|\___/ \___/___|_| \_|"""


def print_banner(cfg):
    w = term_width(cfg)
    for ln in BANNER_ART.split("\n"):
        print(col(ln[:w], CYAN))


_prev_cpu = None


def read_cpu_percent():
    """CPU % from /proc/stat deltas between calls. First call returns None."""
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)

        if _prev_cpu is None:
            _prev_cpu = (idle, total)
            return None

        p_idle, p_total = _prev_cpu
        _prev_cpu = (idle, total)

        d_total = total - p_total
        d_idle = idle - p_idle

        if d_total <= 0:
            return None

        return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))
    except Exception:
        return None


def read_ram_gb():
    """Return (used_gb, total_gb) from /proc/meminfo, or (None, None)."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for row in f:
                name = row.split(":")[0]
                num = int(row.split()[1])  # kB
                info[name] = num
        total = info.get("MemTotal", 0) / 1024 / 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024 / 1024
        used = total - avail
        return used, total
    except Exception:
        return None, None


# ============================================================
# BOX / TABLE DRAWING
# ============================================================

def draw_boxed_menu(rows, cfg, num_w=3, cmd_w=None, border=CYAN):
    """rows: list of (number_str, label, num_color, label_color)."""
    w = term_width(cfg)
    if cmd_w is None:
        cmd_w = w - num_w - 7            # borders + separators + padding
        cmd_w = max(20, cmd_w)

    seg1 = "─" * (num_w + 2)
    seg2 = "─" * (cmd_w + 2)
    bar = col("│", border)

    print(col("┌" + seg1 + "┬" + seg2 + "┐", border))
    print(bar + " " + pad("No", num_w, BOLD) + " " + bar
          + " " + pad("Command", cmd_w, BOLD) + " " + bar)
    print(col("├" + seg1 + "┼" + seg2 + "┤", border))

    for num, label, nc, lc in rows:
        print(bar + " " + pad(num, num_w, nc) + " " + bar
              + " " + pad(label, cmd_w, lc) + " " + bar)

    print(col("└" + seg1 + "┴" + seg2 + "┘", border))


def draw_table(headers, rows, widths, cfg, border=CYAN):
    """headers: list[str]; rows: list of list[(text, color, right?)]; widths: list[int]."""
    def hline(l, m, r):
        segs = ["─" * (wd + 2) for wd in widths]
        return col(l + m.join(segs) + r, border)

    bar = col("│", border)

    print(hline("┌", "┬", "┐"))
    hcells = [" " + pad(h, wd, BOLD) + " " for h, wd in zip(headers, widths)]
    print(bar + bar.join(hcells) + bar)
    print(hline("├", "┼", "┤"))

    for row in rows:
        cells = []
        for cell, wd in zip(row, widths):
            txt, cl = cell[0], cell[1]
            right = cell[2] if len(cell) > 2 else False
            cells.append(" " + pad(txt, wd, cl, right=right) + " ")
        print(bar + bar.join(cells) + bar)

    print(hline("└", "┴", "┘"))


# ============================================================
# BASIC
# ============================================================

def clear():
    os.system("clear")


def now():
    return int(time.time())


def time_text():
    return datetime.now().strftime("%H:%M:%S")


def date_time_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# ACTIVITY LOG — every action the rejoiner takes is recorded here
# and rendered live below the status table.
# ============================================================
_ACTIVITY_LOG = []          # list of (ts, package, action, color)
_ACTIVITY_MAX = 200         # keep last N in memory
_ACTIVITY_ROTATE_LAST_CHECK = 0
_ACTIVITY_LOG_MAX_BYTES = 1 * 1024 * 1024
_ACTIVITY_LOG_BACKUPS = 2
_ACTIVITY_LOG_ROTATE_CHECK_SECONDS = 60


def _rotate_activity_log_if_needed():
    """Rotate activity.log cheaply, checking at most once per minute."""
    global _ACTIVITY_ROTATE_LAST_CHECK
    t = now()
    if t - int(_ACTIVITY_ROTATE_LAST_CHECK or 0) < _ACTIVITY_LOG_ROTATE_CHECK_SECONDS:
        return
    _ACTIVITY_ROTATE_LAST_CHECK = t

    path = BASE_DIR / "activity.log"
    try:
        if not path.exists() or path.stat().st_size < _ACTIVITY_LOG_MAX_BYTES:
            return

        # activity.log.2 is oldest, activity.log.1 is newest rotated copy.
        oldest = BASE_DIR / f"activity.log.{_ACTIVITY_LOG_BACKUPS}"
        if oldest.exists():
            oldest.unlink()
        for idx in range(_ACTIVITY_LOG_BACKUPS - 1, 0, -1):
            src = BASE_DIR / f"activity.log.{idx}"
            dst = BASE_DIR / f"activity.log.{idx + 1}"
            if src.exists():
                os.replace(str(src), str(dst))
        os.replace(str(path), str(BASE_DIR / "activity.log.1"))
    except Exception:
        pass


def log_activity(action, pkg="", color=None):
    """Record an action the rejoiner performed. Shown live in the UI log panel.

    action: short human-readable text ("opening -> market", "kill+open", etc.)
    pkg:    which clone (optional)
    color:  ANSI color code; auto-derived from the action text if omitted.
    """
    global _ACTIVITY_LOG
    if color is None:
        try:
            color = note_color(action)
        except Exception:
            color = WHITE
    _ACTIVITY_LOG.append((now(), str(pkg or ""), str(action or ""), color))
    if len(_ACTIVITY_LOG) > _ACTIVITY_MAX:
        _ACTIVITY_LOG = _ACTIVITY_LOG[-_ACTIVITY_MAX:]

    # Also write to a rolling log file so history survives restarts.
    try:
        _rotate_activity_log_if_needed()
        line = f"[{date_time_text()}] {short_pkg(pkg) if pkg else '-':<8} {action}\n"
        with open(BASE_DIR / "activity.log", "a") as f:
            f.write(line)
    except Exception:
        pass


def render_activity_log(cfg, lines=6):
    """Print the last `lines` activity entries, color-coded, below the table."""
    if not cfg.get("show_activity_log", True):
        return
    recent = _ACTIVITY_LOG[-lines:]
    if not recent:
        return
    print("")
    print(col("  Activity:", BOLD))
    for ts, pkg, action, color in recent:
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        pkg_txt = short_pkg(pkg) if pkg else "-"
        print(f"  {col(t, DIM)} {col(pad(pkg_txt, 8), CYAN)} {col(action, color)}")


def term_width(cfg=None):
    real = shutil.get_terminal_size((62, 24)).columns

    if cfg:
        wanted = int(cfg.get("ui_width", 62))
    else:
        wanted = 62

    return max(48, min(real, wanted))


def cut(text, size):
    text = str(text)

    if len(text) <= size:
        return text

    if size <= 3:
        return text[:size]

    return text[:size - 1] + "…"


def pause():
    drain_stdin()
    time.sleep(0.1)  # give a moment
    input(col("\nENTER...", DIM))

def pause_with_timeout(seconds=3, msg="Press ENTER to continue (or wait)"):
    """Pause with a countdown; press ENTER to skip, otherwise auto-continue."""
    drain_stdin()
    print(col(f"{msg} ({seconds}s)...", DIM), end="", flush=True)
    start = time.time()
    while time.time() - start < seconds:
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.2)
            if r:
                line = sys.stdin.readline()
                if line.strip().lower() in ("", "enter"):
                    break
        except:
            pass
    print("\r" + " " * 40 + "\r", end="", flush=True)  # clear line


# Keys allowed inside global_config_template.json.
# This keeps secrets OUT of the script while still letting a new install prefill JSONBin.
CONFIG_TEMPLATE_KEYS = [
    "backend_provider",
    "jsonbin_hatchers_enabled",
    "jsonbin_bin_id",
    "jsonbin_api_key",
    "jsonbin_key_header",
    "jsonbin_stale_seconds",
    "jsonbin_cache_seconds",
    "jsonbin_timeout_seconds",
    "jsonbin_no_hatcher_action",
    "cloudflare_worker_url",
    "cloudflare_secret",
    "cloudflare_timeout_seconds",
]


def bool_from_any(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ["1", "true", "yes", "y", "on"]


def coerce_template_value(key, value):
    default = DEFAULT_CONFIG.get(key)
    if isinstance(default, bool):
        return bool_from_any(value)
    if isinstance(default, int):
        try:
            return int(value)
        except Exception:
            return default
    if value is None:
        return ""
    return str(value)


def load_global_template():
    try:
        if CONFIG_TEMPLATE_FILE.exists():
            data = json.loads(CONFIG_TEMPLATE_FILE.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def apply_global_template_to_config(cfg, force=False):
    """Apply local template values to config.

    force=False only fills blank/missing values and is used for safe auto-fill.
    force=True overwrites the current config and is used by the template menu.
    """
    tpl = load_global_template()
    if not tpl:
        return False

    changed = False
    for key in CONFIG_TEMPLATE_KEYS:
        if key not in tpl:
            continue
        if key not in DEFAULT_CONFIG:
            continue

        current = cfg.get(key)
        is_blank = current is None or current == "" or str(current).strip().startswith("YOUR_")
        if force or key not in cfg or is_blank:
            new_value = coerce_template_value(key, tpl.get(key))
            if cfg.get(key) != new_value:
                cfg[key] = new_value
                changed = True

    return changed


def save_current_backend_as_template(cfg):
    tpl = {key: cfg.get(key, DEFAULT_CONFIG.get(key)) for key in CONFIG_TEMPLATE_KEYS}
    # Make new installs usable by default once the template has a real bin/key.
    tpl["jsonbin_hatchers_enabled"] = bool(cfg.get("jsonbin_hatchers_enabled", False))
    save_json(CONFIG_TEMPLATE_FILE, tpl)

def save_json(path, data):
    # Redirect the 4 merged files to sections of the unified nomo.json.
    key = str(path)
    if key in _MERGED_SECTIONS:
        alld = _nomo_read_all()
        alld[_MERGED_SECTIONS[key]] = data
        _nomo_write_all(alld)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: serialize to a temp file, then rename over the target.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(str(tmp), str(path))
    except Exception:
        path.write_text(json.dumps(data, indent=2))
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def load_json(path, default):
    # Redirect the 4 merged files to sections of the unified nomo.json.
    key = str(path)
    if key in _MERGED_SECTIONS:
        _migrate_legacy_files()
        alld = _nomo_read_all()
        section = _MERGED_SECTIONS[key]
        if section in alld:
            return alld[section]
        return default

    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass

    return default



NOMO_CONFIG_MIGRATION_VERSION = 361


def _int_cfg(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def apply_update_migrations(cfg):
    """Apply safe config fixes that should happen automatically after script updates.

    This replaces the old manual Termux patch. It only overwrites known old/broken
    defaults, blank values, or unsafe-long stale timers. Custom private-server share
    links are left alone.
    """
    changed = False

    def set_cfg(key, value):
        nonlocal changed
        if cfg.get(key) != value:
            cfg[key] = value
            changed = True

    if _int_cfg(cfg.get("ui_width"), 62) < 100:
        set_cfg("ui_width", 110)

    # Old configs set min_seconds_between_reopen to 300 (5 min), which delays
    # every non-forced recovery. Clamp anything above 120 down to 60.
    if _int_cfg(cfg.get("min_seconds_between_reopen"), 60) > 120:
        set_cfg("min_seconds_between_reopen", 60)

    # FIX V3.78: Align hard-force cooldown clamp with the V3.77 migration target (was 30, now 15).
    if _int_cfg(cfg.get("hatcher_alive_old_state_hard_force_cooldown_seconds"), 15) > 30:
        set_cfg("hatcher_alive_old_state_hard_force_cooldown_seconds", 15)
    # FIX V3.78: Align migration clamp to 300 (was 180, inconsistent with V3.77 default).
    if _int_cfg(cfg.get("hatcher_alive_old_state_hard_force_seconds"), 300) > 300:
        set_cfg("hatcher_alive_old_state_hard_force_seconds", 300)
    # True 24h ceiling for ignoring copied/ancient state.
    if _int_cfg(cfg.get("hatcher_alive_old_state_max_valid_seconds"), 86400) < 86400:
        set_cfg("hatcher_alive_old_state_max_valid_seconds", 86400)
    # Ensure the 5m old-state hard rule is actually ON (a saved False disables
    # the whole fallback, leaving clones stuck on "alive old state").
    if not bool_from_any(cfg.get("hatcher_alive_old_state_hard_force_enabled", True)):
        set_cfg("hatcher_alive_old_state_hard_force_enabled", True)
    # Also enable the generic alive-stale rejoin as a second safety net.
    set_cfg("hatcher_rejoin_alive_stale", True)

    market_old = str(cfg.get("market_link", "") or "").strip()
    market_known_old = {
        "",
        "roblox://placeID=129954712878723",
        "roblox://experiences/start?placeid=129954712878723",
        "https://www.roblox.com/games/129954712878723/Grow-a-Garden-Trade-World",
        "https://www.roblox.com/games/129954712878723/",
        "129954712878723",
    }
    if market_old in market_known_old or market_old.lower() in market_known_old:
        set_cfg("market_link", DEFAULT_MARKET_LINK)
    else:
        # If the user pasted another normal /games/<placeId> link, normalize it to
        # roblox://experiences/start?placeId=... so clones join the game instead of home.
        normalized = android_safe_roblox_link(market_old, cfg)
        if normalized and normalized != market_old and "share?code=" not in market_old:
            set_cfg("market_link", normalized)

    restock_old = str(cfg.get("restock_link", "") or "").strip()
    restock_known_old = {
        "",
        "roblox://placeID=126884695634066",
        "roblox://experiences/start?placeid=126884695634066",
        "https://www.roblox.com/games/126884695634066/Grow-a-Garden",
        "https://www.roblox.com/games/126884695634066/",
        "126884695634066",
    }
    if restock_old in restock_known_old or restock_old.lower() in restock_known_old:
        set_cfg("restock_link", DEFAULT_RESTOCK_LINK)
    else:
        normalized = android_safe_roblox_link(restock_old, cfg)
        if normalized and normalized != restock_old and "share?code=" not in restock_old:
            set_cfg("restock_link", normalized)

    # Save the launch style values instead of requiring a Termux patch every update.
    set_cfg("prefer_experience_start_links", True)
    set_cfg("prefer_https_game_links", False)

    # Stale-safe defaults: don't wait too long on alive-old-state.
    if _int_cfg(cfg.get("state_stale_seconds"), 999999) > 180:
        set_cfg("state_stale_seconds", 180)
    if _int_cfg(cfg.get("alive_old_state_rejoin_after_seconds"), 999999) > 60:
        set_cfg("alive_old_state_rejoin_after_seconds", 60)
    if _int_cfg(cfg.get("stale_reopen_after_seconds"), 999999) > 60:
        set_cfg("stale_reopen_after_seconds", 60)
    if _int_cfg(cfg.get("force_rejoin_if_stale_seconds"), 999999) > 180:
        set_cfg("force_rejoin_if_stale_seconds", 180)

    # V3.60: safer Redfinger defaults. Some Roblox clones can take 2+ minutes
    # to load into the game, so do NOT hard-rejoin too early. V3.58/3.59 used
    # aggressive 75-120s timers which can false-detect slow loading as homepage
    # stuck and cause unnecessary rejoin loops. Raise only known-aggressive values.
    if _int_cfg(cfg.get("open_wait_fresh_seconds"), 0) <= 120:
        set_cfg("open_wait_fresh_seconds", 300)
    if _int_cfg(cfg.get("soft_hop_wait_fresh_seconds"), 0) <= 90:
        set_cfg("soft_hop_wait_fresh_seconds", 240)
    if _int_cfg(cfg.get("post_open_grace_seconds"), 0) <= 180:
        set_cfg("post_open_grace_seconds", 360)
    # V3.79: MARKET phantom-age ceiling must exist and be sane (>= 24h). Without
    # it, a 277h missing-ts age passes the 180s floor and force-loops the pool.
    if _int_cfg(cfg.get("alive_old_state_max_valid_seconds"), 0) < 86400:
        set_cfg("alive_old_state_max_valid_seconds", 86400)

    # V3.79: short startup grace so the hatcher table doesn't sit on "waiting"
    # for the full 6-minute post-open window.
    if _int_cfg(cfg.get("hatcher_startup_grace_seconds"), 0) <= 0:
        set_cfg("hatcher_startup_grace_seconds", 75)

    # V3.79: auto username resolution ON by default so the table matches the
    # logged-in account without the manual step.
    if "auto_resolve_usernames_enabled" not in cfg:
        set_cfg("auto_resolve_usernames_enabled", True)
    if _int_cfg(cfg.get("username_resolve_interval_seconds"), 0) <= 0:
        set_cfg("username_resolve_interval_seconds", 600)

    # V3.81: non-blocking solver defaults for existing nomo.json files.
    if _int_cfg(cfg.get("solver_timeout_seconds"), 0) <= 0:
        set_cfg("solver_timeout_seconds", 180)
    # V3.84: provider owner requested no continuous solved/NO_CAPTCHA submits.
    # Enforce a 10-minute floor and tie probes to actual open generations only.
    if _int_cfg(cfg.get("solver_retry_cooldown_seconds"), 0) < 600:
        set_cfg("solver_retry_cooldown_seconds", 600)
    if _int_cfg(cfg.get("solver_min_resubmit_seconds"), 0) < 600:
        set_cfg("solver_min_resubmit_seconds", 600)
    if _int_cfg(cfg.get("solver_probe_after_seconds"), 0) <= 0:
        set_cfg("solver_probe_after_seconds", 45)
    if _int_cfg(cfg.get("solver_probe_repeat_seconds"), 0) < 600:
        set_cfg("solver_probe_repeat_seconds", 600)
    set_cfg("solver_probe_once_per_open", True)
    set_cfg("solver_require_cookie_precheck", True)
    # Disable V3.83's pool-wide stale scan. The check now happens only after a
    # real NOMO open/rejoin fails to produce fresh state.
    set_cfg("solver_probe_stale_state_enabled", False)
    if "solver_provider_probe_on_no_state" not in cfg:
        set_cfg("solver_provider_probe_on_no_state", True)
    if "solver_rejoin_on_success" not in cfg:
        set_cfg("solver_rejoin_on_success", True)
    # V4.09: force the safe default. Older builds treated NO_CAPTCHA exactly like
    # CAPTCHA_SUCCESS and immediately hard-restarted a package that had just opened.
    if cfg.get("solver_rejoin_on_no_captcha") is not False:
        set_cfg("solver_rejoin_on_no_captcha", False)
    if "manual_hold_after_solver_rejoin_timeout" not in cfg:
        set_cfg("manual_hold_after_solver_rejoin_timeout", True)

    # V3.79: one-clone-at-a-time gates. Force ON for existing configs — the whole
    # point is to stop mass restarts, so a stale config must not opt out.
    if cfg.get("recheck_before_hard_open") is not True:
        set_cfg("recheck_before_hard_open", True)
    if _int_cfg(cfg.get("recheck_min_queue_age_seconds"), 0) <= 0:
        set_cfg("recheck_min_queue_age_seconds", 20)
    if cfg.get("single_flight_open") is not True:
        set_cfg("single_flight_open", True)
    if _int_cfg(cfg.get("single_flight_max_seconds"), 0) <= 0:
        set_cfg("single_flight_max_seconds", 420)

    if "homepage_stuck_hard_fallback_enabled" not in cfg:
        set_cfg("homepage_stuck_hard_fallback_enabled", True)
    # Ditch soft rejoin: force every open to hard kill+open (one-time).
    if not cfg.get("_soft_rejoin_disabled_once", False):
        set_cfg("disable_soft_rejoin", True)
        set_cfg("scheduled_hop_enabled", False)
        set_cfg("_soft_rejoin_disabled_once", True)
    if _int_cfg(cfg.get("homepage_stuck_fallback_seconds"), 0) <= 75:
        set_cfg("homepage_stuck_fallback_seconds", 240)
    if "homepage_stuck_max_hard_retries" not in cfg:
        set_cfg("homepage_stuck_max_hard_retries", 1)

    if "login_challenge_detection_enabled" not in cfg:
        set_cfg("login_challenge_detection_enabled", True)
    if "login_challenge_api_detection_enabled" not in cfg:
        set_cfg("login_challenge_api_detection_enabled", True)
    if "login_challenge_ui_detection_enabled" not in cfg:
        set_cfg("login_challenge_ui_detection_enabled", True)
    if "login_challenge_skip_blocked_packages" not in cfg:
        set_cfg("login_challenge_skip_blocked_packages", True)
    if "login_challenge_webhook_url" not in cfg:
        set_cfg("login_challenge_webhook_url", "")
    if _int_cfg(cfg.get("login_challenge_alert_cooldown_seconds"), 0) <= 0:
        set_cfg("login_challenge_alert_cooldown_seconds", 1800)
    if "captcha_ui_override_enabled" not in cfg:
        set_cfg("captcha_ui_override_enabled", True)
    if _int_cfg(cfg.get("captcha_ui_retry_seconds"), 0) < 600:
        set_cfg("captcha_ui_retry_seconds", 600)
    if "captcha_ui_require_package_scope" not in cfg:
        set_cfg("captcha_ui_require_package_scope", True)

    # V3.59/V3.60: stale runtime queue self-heal. This replaces manual
    # rm runtime.json for interrupted open/wait cycles, but waits longer so it
    # does not interfere with normal slow Roblox loading.
    if "queue_stuck_self_heal_enabled" not in cfg:
        set_cfg("queue_stuck_self_heal_enabled", True)
    if _int_cfg(cfg.get("queue_stuck_reset_seconds"), 0) <= 180:
        set_cfg("queue_stuck_reset_seconds", 360)
    if "runtime_stuck_reset_enabled" not in cfg:
        set_cfg("runtime_stuck_reset_enabled", True)
    if _int_cfg(cfg.get("runtime_stuck_reset_seconds"), 0) < 300:
        set_cfg("runtime_stuck_reset_seconds", 420)
    if _int_cfg(cfg.get("runtime_stuck_reset_min_tabs"), 0) < 1:
        set_cfg("runtime_stuck_reset_min_tabs", 1)

    # V3.61: default workspace/config sync ON for MARKET mode only.
    if _int_cfg(cfg.get("_nomo_config_migration_version"), 0) < 361:
        set_cfg("workspace_sync_enabled", True)
        set_cfg("workspace_sync_market_only", True)
        set_cfg("workspace_sync_on_start", True)
        set_cfg("workspace_sync_before_open", True)
        set_cfg("workspace_sync_periodic_enabled", False)
        if _int_cfg(cfg.get("workspace_sync_interval_seconds"), 0) <= 300:
            set_cfg("workspace_sync_interval_seconds", 10800)
        if "workspace_sync_timeout_seconds" not in cfg:
            set_cfg("workspace_sync_timeout_seconds", 90)

    # Error 288 / disconnect popup recovery.
    if "disconnect_popup_rejoin_enabled" not in cfg:
        set_cfg("disconnect_popup_rejoin_enabled", True)
    if "force_stop_alive_on_disconnect_popup" not in cfg:
        set_cfg("force_stop_alive_on_disconnect_popup", False)
    if "disconnect_ui_rejoin_enabled" not in cfg:
        set_cfg("disconnect_ui_rejoin_enabled", True)
    if "disconnect_ui_retry_seconds" not in cfg:
        set_cfg("disconnect_ui_retry_seconds", 60)
    if "disconnect_ui_hard_after_seconds" not in cfg:
        set_cfg("disconnect_ui_hard_after_seconds", 180)
    if "minimized_window_detection_enabled" not in cfg:
        set_cfg("minimized_window_detection_enabled", True)
    # V3.86 safety migration: dumpsys window visibility is not reliable in
    # Redfinger multi-window mode. A non-focused clone can look "minimized", and
    # the old soft reopen could be promoted to a hard stop by disable_soft_rejoin.
    # Keep window detection informational only; stale-state recovery remains exact.
    set_cfg("minimized_window_reopen_enabled", False)
    if "minimized_window_reopen_mode" not in cfg:
        set_cfg("minimized_window_reopen_mode", "soft")
    if "preserve_multiwindow_on_launch" not in cfg:
        set_cfg("preserve_multiwindow_on_launch", True)
    if _int_cfg(cfg.get("launch_windowing_mode"), 0) <= 0:
        set_cfg("launch_windowing_mode", 5)

    if "start_skip_if_state_fresh_enabled" not in cfg:
        set_cfg("start_skip_if_state_fresh_enabled", True)
    if "start_skip_fresh_state_seconds" not in cfg:
        set_cfg("start_skip_fresh_state_seconds", 45)
    if "start_reopen_alive_without_fresh_state" not in cfg:
        set_cfg("start_reopen_alive_without_fresh_state", True)
    if "start_alive_without_fresh_state_mode" not in cfg:
        set_cfg("start_alive_without_fresh_state_mode", "hard_force")
    if "hatcher_disconnect_ui_hard_force" not in cfg:
        set_cfg("hatcher_disconnect_ui_hard_force", True)
    if "hatcher_disconnect_ui_retry_seconds" not in cfg:
        set_cfg("hatcher_disconnect_ui_retry_seconds", 15)
    # V4.08 performance-only migration: keep the package-scoped Android UI
    # fallback, but avoid an expensive uiautomator dump every 10-second cycle.
    # Only replace the known old/default 1..5 second values; custom slower values
    # are preserved. Direct Lua popup flags remain immediate.
    if _int_cfg(cfg.get("_nomo_perf_migration"), 0) < 408:
        if _int_cfg(cfg.get("android_ui_snapshot_cache_seconds"), 5) <= 5:
            set_cfg("android_ui_snapshot_cache_seconds", 25)
        set_cfg("_nomo_perf_migration", 408)

    if _int_cfg(cfg.get("android_disconnect_confirmations_required"), 0) < 2:
        set_cfg("android_disconnect_confirmations_required", 2)
    if _int_cfg(cfg.get("android_disconnect_confirmation_window_seconds"), 0) <= 0:
        set_cfg("android_disconnect_confirmation_window_seconds", 30)
    if _int_cfg(cfg.get("disconnect_observation_max_age_seconds"), 0) <= 0:
        set_cfg("disconnect_observation_max_age_seconds", 45)
    if "verify_sibling_pids_on_stop" not in cfg:
        set_cfg("verify_sibling_pids_on_stop", True)
    if "periodic_hard_refresh_enabled" not in cfg:
        set_cfg("periodic_hard_refresh_enabled", False)
    if not cfg.get("_periodic_refresh_disabled_once", False):
        set_cfg("periodic_hard_refresh_enabled", False)
        set_cfg("_periodic_refresh_disabled_once", True)
    if "periodic_hard_refresh_seconds" not in cfg:
        set_cfg("periodic_hard_refresh_seconds", 3600)
    if "periodic_hard_refresh_include_manual" not in cfg:
        set_cfg("periodic_hard_refresh_include_manual", True)

    # V3.75: add but do not make this dependent on the old soft stale flag.
    if "hatcher_alive_old_state_hard_force_enabled" not in cfg:
        set_cfg("hatcher_alive_old_state_hard_force_enabled", True)
    if "hatcher_alive_old_state_hard_force_seconds" not in cfg:
        set_cfg("hatcher_alive_old_state_hard_force_seconds", 300)
    if "hatcher_alive_old_state_max_valid_seconds" not in cfg:
        set_cfg("hatcher_alive_old_state_max_valid_seconds", 86400)
    if "hatcher_alive_old_state_after_open_grace_seconds" not in cfg:
        set_cfg("hatcher_alive_old_state_after_open_grace_seconds", 180)
    if "hatcher_alive_old_state_hard_force_cooldown_seconds" not in cfg:
        set_cfg("hatcher_alive_old_state_hard_force_cooldown_seconds", 900)

    # V3.78: force clean 5m old-state hard rule ON + fix max_valid_seconds typo.
    if _int_cfg(cfg.get("_nomo_old_state_hard_force_migration"), 0) < 378:
        set_cfg("hatcher_alive_old_state_hard_force_enabled", True)
        set_cfg("hatcher_alive_old_state_hard_force_seconds", 300)
        set_cfg("hatcher_alive_old_state_max_valid_seconds", 86400)  # FIX: was 81400
        set_cfg("hatcher_alive_old_state_after_open_grace_seconds", 180)
        set_cfg("hatcher_alive_old_state_hard_force_cooldown_seconds", 900)
        set_cfg("_nomo_old_state_hard_force_migration", 378)

    # V3.63: Market should use hatcher servers once they have 100+ pets.
    if _int_cfg(cfg.get("jsonbin_min_hatcher_pets"), 200) > 100:
        set_cfg("jsonbin_min_hatcher_pets", 100)

    if _int_cfg(cfg.get("disconnect_popup_stale_seconds"), 999999) > 180:
        set_cfg("disconnect_popup_stale_seconds", 180)

    # Keep per-tab restock links from old configs fixed too.
    tabs = cfg.get("tabs")
    if isinstance(tabs, list):
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            link = str(tab.get("restock_link", "") or "").strip()
            link_l = link.lower()
            if link in restock_known_old or link_l in restock_known_old:
                if tab.get("restock_link") != cfg.get("restock_link", DEFAULT_RESTOCK_LINK):
                    tab["restock_link"] = cfg.get("restock_link", DEFAULT_RESTOCK_LINK)
                    changed = True
            elif link and "share?code=" not in link:
                normalized = android_safe_roblox_link(link, cfg)
                if normalized and normalized != link:
                    tab["restock_link"] = normalized
                    changed = True

    if _int_cfg(cfg.get("_nomo_config_migration_version"), 0) < NOMO_CONFIG_MIGRATION_VERSION:
        set_cfg("_nomo_config_migration_version", NOMO_CONFIG_MIGRATION_VERSION)

    return changed

def _merged_section_exists(path):
    """True if the given logical file's section exists in nomo.json."""
    _migrate_legacy_files()
    alld = _nomo_read_all()
    return _MERGED_SECTIONS.get(str(path)) in alld


def load_config():
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    created_new = False
    if not _merged_section_exists(CONFIG_FILE):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        # Fresh install: if a local global_config_template.json exists, apply it now.
        apply_global_template_to_config(cfg, force=True)
        save_json(CONFIG_FILE, cfg)
        created_new = True

    cfg = load_json(CONFIG_FILE, DEFAULT_CONFIG)
    changed = False

    # If config was just created, make sure template values are saved after any load fallback.
    if created_new and apply_global_template_to_config(cfg, force=True):
        changed = True

    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
            changed = True


    # V3.44: normalize old/default game links to Roblox's current experience-start URI.
    for link_key, default_link in (("market_link", DEFAULT_MARKET_LINK), ("restock_link", DEFAULT_RESTOCK_LINK)):
        old_link = str(cfg.get(link_key, "")).strip()
        if old_link in [
            "roblox://placeID=129954712878723",
            "https://www.roblox.com/games/129954712878723/Grow-a-Garden-Trade-World",
            "129954712878723",
        ] and link_key == "market_link":
            cfg[link_key] = default_link
            changed = True
        elif old_link in [
            "roblox://placeID=126884695634066",
            "https://www.roblox.com/games/126884695634066/Grow-a-Garden",
            "126884695634066",
        ] and link_key == "restock_link":
            cfg[link_key] = default_link
            changed = True

    # V3.44: old configs waited too long (120+300s) on alive old state.
    if int(cfg.get("stale_reopen_after_seconds", 300) or 300) > 60:
        cfg["stale_reopen_after_seconds"] = 60
        changed = True
    if "alive_old_state_rejoin_after_seconds" not in cfg:
        cfg["alive_old_state_rejoin_after_seconds"] = 60
        changed = True
    if "disconnect_popup_rejoin_enabled" not in cfg:
        cfg["disconnect_popup_rejoin_enabled"] = True
        changed = True
    if "force_stop_alive_on_disconnect_popup" not in cfg:
        cfg["force_stop_alive_on_disconnect_popup"] = False
        changed = True
    if "hatcher_disconnect_ui_hard_force" not in cfg:
        cfg["hatcher_disconnect_ui_hard_force"] = True
        changed = True
    if int(cfg.get("disconnect_popup_stale_seconds", 180) or 180) > 180:
        cfg["disconnect_popup_stale_seconds"] = 180
        changed = True
    cfg["prefer_experience_start_links"] = True
    cfg["prefer_https_game_links"] = False

    # If a template was created after config.json already existed, still auto-fill
    # when the current backend is blank.
    provider_for_blank = str(cfg.get("backend_provider", "jsonbin") or "jsonbin").strip().lower()
    if provider_for_blank == "cloudflare":
        backend_blank = (
            not str(cfg.get("cloudflare_worker_url", "")).strip()
            or str(cfg.get("cloudflare_worker_url", "")).strip().startswith("YOUR_")
            or not str(cfg.get("cloudflare_secret", "")).strip()
            or str(cfg.get("cloudflare_secret", "")).strip().startswith("YOUR_")
        )
    else:
        backend_blank = (
            not str(cfg.get("jsonbin_bin_id", "")).strip()
            or str(cfg.get("jsonbin_bin_id", "")).strip().startswith("YOUR_")
            or not str(cfg.get("jsonbin_api_key", "")).strip()
            or str(cfg.get("jsonbin_api_key", "")).strip().startswith("YOUR_")
        )
    if backend_blank and CONFIG_TEMPLATE_FILE.exists():
        if apply_global_template_to_config(cfg, force=True):
            changed = True

    if "tabs" not in cfg or not isinstance(cfg["tabs"], list) or len(cfg["tabs"]) == 0:
        cfg["tabs"] = DEFAULT_TABS
        changed = True

    for tab in cfg["tabs"]:
        old_state_path = str(tab.get("stat_file", "") or "")
        if f"/{LEGACY_STATE_FOLDER_NAME}/" in old_state_path:
            tab["stat_file"] = old_state_path.replace(
                f"/{LEGACY_STATE_FOLDER_NAME}/", f"/{STATE_FOLDER_NAME}/"
            )
            changed = True
        if "restock_link" not in tab:
            tab["restock_link"] = cfg.get("restock_link", DEFAULT_RESTOCK_LINK)
            changed = True

        if "enabled" not in tab:
            tab["enabled"] = True
            changed = True

    if normalize_active_mode_flags(cfg):
        changed = True

    action = str(cfg.get("jsonbin_no_hatcher_action", "stay_market") or "stay_market").strip().lower()
    if action in ["fallback", "restock"]:
        cfg["jsonbin_no_hatcher_action"] = "fallback_restock"
        changed = True
    elif action not in ["stay_market", "fallback_restock"]:
        cfg["jsonbin_no_hatcher_action"] = "stay_market"
        changed = True

    if apply_update_migrations(cfg):
        changed = True

    if changed:
        save_json(CONFIG_FILE, cfg)

    set_color_enabled(cfg)
    return cfg


def save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def load_runtime():
    return load_json(RUNTIME_FILE, {})


def save_runtime(rt):
    save_json(RUNTIME_FILE, rt)


def get_workspace_sync_runtime(rt):
    if rt is None:
        return {}
    if "_workspace_sync" not in rt or not isinstance(rt.get("_workspace_sync"), dict):
        rt["_workspace_sync"] = {}
    return rt["_workspace_sync"]


def workspace_sync_allowed_for_mode(cfg):
    """Workspace sync is useful for Market clones, but should not disturb Hatcher mode."""
    if not cfg.get("workspace_sync_market_only", True):
        return True
    return active_rejoin_mode(cfg) == "market"


def run_workspace_sync(cfg, rt=None, reason="", force=False):
    """Run optional local workspace/config sync hook."""
    if not cfg.get("workspace_sync_enabled", False):
        return False, "workspace sync off"

    if not workspace_sync_allowed_for_mode(cfg):
        return False, "workspace sync market-only"

    cmd = str(cfg.get("workspace_sync_command", "") or "").strip()
    if not cmd:
        return False, "workspace sync command empty"

    sync_rt = get_workspace_sync_runtime(rt)
    interval = int(cfg.get("workspace_sync_interval_seconds", 300) or 0)
    last = int(sync_rt.get("last_run", 0) or 0)

    if not force and interval > 0 and last > 0 and now() - last < interval:
        left = interval - (now() - last)
        return False, f"workspace sync cooldown {left}s"

    timeout = int(cfg.get("workspace_sync_timeout_seconds", 90) or 90)
    code, out = shell_timeout(cmd, cfg, capture=True, timeout=timeout)

    sync_rt["last_run"] = now()
    sync_rt["last_reason"] = str(reason or "")
    sync_rt["last_code"] = int(code)
    sync_rt["last_msg"] = cut(out, 160)

    if rt is not None:
        save_runtime(rt)

    if code == 0:
        return True, "workspace sync ok"
    return False, f"workspace sync err {code}: {cut(out, 80)}"


def workspace_sync_screen(cfg, reason, msg=""):
    clear()
    banner("WORKSPACE SYNC", cfg)
    print(col("Running local config.zip -> clone Workspace sync.", CYAN))
    print(col(f"Reason: {reason}", DIM))
    if msg:
        print(msg)


def shell(cmd, cfg, capture=True):
    if cfg.get("use_su", True):
        cmd = "su -c " + shlex.quote(cmd)

    p = subprocess.run(
        cmd,
        shell=True,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if capture else subprocess.DEVNULL
    )

    out = ""
    if capture and p.stdout:
        out = p.stdout.strip()

    return p.returncode, out


def shell_timeout(cmd, cfg, capture=True, timeout=None):
    if cfg.get("use_su", True):
        cmd = "su -c " + shlex.quote(cmd)

    try:
        p = subprocess.run(
            cmd,
            shell=True,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
            timeout=timeout,
        )
        out = ""
        if capture and p.stdout:
            out = p.stdout.strip()
        return p.returncode, out
    except subprocess.TimeoutExpired as e:
        out = ""
        if capture and e.stdout:
            out = str(e.stdout).strip()
        return 124, out or f"timeout after {timeout}s"


def reset_terminal():
    """su can leave the tty in raw/no-echo mode. Put it back."""
    try:
        os.system("stty sane 2>/dev/null")
    except Exception:
        pass


def drain_stdin():
    """Throw away leftover keypresses so the next input() starts clean."""
    try:
        while select.select([sys.stdin], [], [], 0)[0]:
            if not sys.stdin.readline():
                break
    except Exception:
        pass


_BRACKETED_PASTE_RE = re.compile(r"\x1b\[(?:200|201)~")
_MENU_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def clean_terminal_input(value):
    """Remove invisible terminal bytes that can turn a valid choice into invalid."""
    s = str(value or "").translate(_FULLWIDTH_DIGITS)
    s = _BRACKETED_PASTE_RE.sub("", s)
    s = _MENU_ANSI_RE.sub("", s)
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    s = "".join(ch for ch in s if ch in "\t\n\r" or ord(ch) >= 32)
    return s.strip()


def normalize_menu_choice(value):
    """Normalize common pasted/typed menu forms: 'Option 3', '3.', '3)'."""
    s = clean_terminal_input(value).lower()
    s = re.sub(r"^(?:option|opt|choice|choose|select)\s*[:#-]?\s*", "", s)
    m = re.fullmatch(r"(\d+)\s*[.)]?", s)
    if m:
        return m.group(1)
    return s


def read_menu_choice(prompt, valid=None, *, lower=True, allow_blank=False):
    """Read a sanitized menu choice; return None for invalid input."""
    raw = input(prompt)
    ch = normalize_menu_choice(raw)
    if not lower:
        ch = clean_terminal_input(raw)
    if not ch and allow_blank:
        return ""
    if valid is not None and ch not in set(valid):
        return None
    return ch


def short_pkg(pkg):
    pkg = str(pkg)

    if pkg.startswith("premium."):
        return pkg.replace("premium.", "")

    return pkg


def short_link(link):
    if not link:
        return "none"

    s = str(link)

    if len(s) <= 26:
        return s

    return s[:11] + "…" + s[-10:]


def stop_requested():
    if _STOP_REQUESTED:
        return True

    try:
        r, _, _ = select.select([sys.stdin], [], [], 0)

        if r:
            cmd = sys.stdin.readline().strip().lower()
            if cmd in ["q", "quit", "stop", "exit"]:
                request_stop()
                return True
    except Exception:
        pass

    return False


# ============================================================
# ANDROID
# ============================================================

_PROC_CACHE = {"ts": 0, "names": set(), "ok": False}


def refresh_process_list(cfg, max_age=4, force=False):
    """Populate a cached set of exact Android process names with one `ps`."""
    if not force and (now() - int(_PROC_CACHE.get("ts", 0) or 0)) <= max_age and _PROC_CACHE.get("ok"):
        return
    code, out = shell_timeout("ps -A -o NAME 2>/dev/null", cfg, capture=True, timeout=5)
    if code == 0 and out:
        _PROC_CACHE["names"] = set(x.strip() for x in out.split() if x.strip())
        _PROC_CACHE["ok"] = True
    else:
        _PROC_CACHE["ok"] = False
    _PROC_CACHE["ts"] = now()


def _process_name_matches_package(name, pkg):
    name = str(name or "").strip()
    pkg = str(pkg or "").strip()
    return bool(pkg) and (name == pkg or name.startswith(pkg + ":"))


def package_pids(pkg, cfg):
    """Return only PIDs whose Android process NAME exactly belongs to pkg.

    We intentionally do not trust a broad `pidof` result for cloned APKs. The
    exact package name (or package:subprocess) is read from `ps` first, then only
    those numeric PIDs are eligible for kill.
    """
    pkg = str(pkg or "").strip()
    if not pkg:
        return []
    code, out = shell_timeout("ps -A -o PID,NAME 2>/dev/null", cfg, capture=True, timeout=6)
    if code != 0 or not out:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, name = int(parts[0]), parts[1].strip()
        if _process_name_matches_package(name, pkg):
            pids.append(pid)
    return sorted(set(pids))


def package_alive(pkg, cfg, fresh=False):
    """Return True only when an exact package PID is running."""
    pkg = str(pkg or "").strip()
    if not pkg:
        return False

    if not fresh:
        refresh_process_list(cfg)
        if _PROC_CACHE.get("ok"):
            return any(_process_name_matches_package(name, pkg) for name in _PROC_CACHE["names"])

    return bool(package_pids(pkg, cfg))


_WINDOW_DUMP_CACHE = {"ts": 0, "text": "", "ok": False}


def package_visible_window(pkg, cfg):
    """Best-effort visible-window check for minimized/floating clone icons."""
    if not cfg.get("minimized_window_detection_enabled", True):
        return None, "window detect disabled"

    pkg = str(pkg or "").strip()
    if not pkg:
        return None, "no package"

    cache_age = now() - int(_WINDOW_DUMP_CACHE.get("ts", 0) or 0)
    if cache_age > 3:
        code, out = shell_timeout("dumpsys window windows", cfg, capture=True, timeout=5)
        _WINDOW_DUMP_CACHE["ts"] = now()
        _WINDOW_DUMP_CACHE["ok"] = code == 0 and bool(out)
        _WINDOW_DUMP_CACHE["text"] = out or ""

    if not _WINDOW_DUMP_CACHE.get("ok"):
        return None, "window dump unavailable"

    low = str(_WINDOW_DUMP_CACHE.get("text", "")).lower()
    if pkg.lower() in low:
        return True, "visible window"
    return False, "minimized/no visible window"


def _kill_exact_package_pids(pkg, pids, signal_name, cfg):
    """Kill listed PIDs only after re-validating each PID's exact package name."""
    if not pids:
        return
    sig = "-9" if str(signal_name).upper() == "KILL" else "-15"
    qpkg = shlex.quote(str(pkg))
    pid_words = " ".join(str(int(x)) for x in pids if int(x) > 1)
    cmd = (
        f"pkg={qpkg}; "
        f"for pid in {pid_words}; do "
        "[ -r /proc/$pid/cmdline ] || continue; "
        "name=$(tr '\\000' '\\n' < /proc/$pid/cmdline 2>/dev/null | head -n 1); "
        "case \"$name\" in \"$pkg\"|\"$pkg\":*) kill " + sig + " \"$pid\" 2>/dev/null ;; esac; "
        "done"
    )
    shell_timeout(cmd, cfg, capture=True, timeout=8)



def _configured_clone_packages(cfg):
    packages = set()
    for tab in (cfg.get("tabs", []) or []):
        p = str((tab or {}).get("package", "") or "").strip()
        if p:
            packages.add(p)
    try:
        hcfg = load_hatcher_config()
        for prof in hatcher_profiles(hcfg, enabled_only=False):
            p = str((prof or {}).get("package", "") or "").strip()
            if p:
                packages.add(p)
    except Exception:
        pass
    return sorted(packages)


def _sibling_pid_snapshot(target_pkg, cfg):
    if not cfg.get("verify_sibling_pids_on_stop", True):
        return {}
    snap = {}
    for p in _configured_clone_packages(cfg):
        if p != target_pkg:
            ids = package_pids(p, cfg)
            if ids:
                snap[p] = ids
    return snap


def _verify_sibling_pid_snapshot(before, cfg, target_pkg):
    if not before:
        return True, ""
    lost = []
    for p, old_ids in before.items():
        current = set(package_pids(p, cfg))
        missing = [pid for pid in old_ids if pid not in current]
        if missing and not current:
            lost.append(f"{p}:{','.join(map(str, missing))}")
    if lost:
        msg = "peer PID loss observed after target stop -> " + " | ".join(lost)
        log_activity(msg, target_pkg, RED)
        return False, msg
    return True, "peer PIDs intact"


def force_stop_package(pkg, cfg, tries=3, wait_after=0.8, settle=1.0):
    """Stop exactly ONE clone using verified package PIDs only.

    There is deliberately no `am force-stop`, `killall`, or `pkill` fallback.
    If exact PIDs cannot be found or survive SIGKILL, the function returns False
    instead of claiming success and risking another package.
    """
    pkg = str(pkg or "").strip()
    if not pkg:
        return False, "no package"

    sibling_before = _sibling_pid_snapshot(pkg, cfg)
    initial = package_pids(pkg, cfg)
    if not initial:
        return True, "already stopped (no exact PID)"

    log_activity("PID-only stop -> " + ",".join(map(str, initial)), pkg, YELLOW)

    for _ in range(max(1, int(tries or 1))):
        pids = package_pids(pkg, cfg)
        if not pids:
            time.sleep(float(settle or 1.0))
            _PROC_CACHE["ts"] = 0
            log_activity("PID-only stopped", pkg, YELLOW)
            _verify_sibling_pid_snapshot(sibling_before, cfg, pkg)
            return True, "stopped (exact PID)"

        _kill_exact_package_pids(pkg, pids, "TERM", cfg)
        time.sleep(float(wait_after or 0.8))

        survivors = package_pids(pkg, cfg)
        if survivors:
            _kill_exact_package_pids(pkg, survivors, "KILL", cfg)
            time.sleep(0.6)

        for _wait in range(6):
            survivors = package_pids(pkg, cfg)
            if not survivors:
                time.sleep(float(settle or 1.0))
                _PROC_CACHE["ts"] = 0
                log_activity("PID-only stopped", pkg, YELLOW)
                _verify_sibling_pid_snapshot(sibling_before, cfg, pkg)
                return True, "stopped (exact PID)"
            time.sleep(0.4)

    survivors = package_pids(pkg, cfg)
    msg = "PID stop failed; survivors=" + ",".join(map(str, survivors))
    log_activity(msg, pkg, RED)
    return False, msg


def clear_package_cache(pkg, cfg, rt_tab=None, reason=""):
    """Safe cache cleanup only."""
    if not cfg.get("clear_cache_enabled", False):
        return False, "cache disabled"

    interval = int(cfg.get("clear_cache_min_interval_seconds", 1800))
    last = int((rt_tab or {}).get("last_cache_clear", 0) or 0)

    if interval > 0 and last > 0 and now() - last < interval:
        return False, "cache cooldown"

    paths = [
        f"/data/data/{pkg}/cache",
        f"/data/user/0/{pkg}/cache",
        f"/storage/emulated/0/Android/data/{pkg}/cache",
    ]

    cmd = "for d in " + " ".join(shlex.quote(p) for p in paths) + "; do " \
          "[ -d \"$d\" ] && rm -rf \"$d\"/* 2>/dev/null; " \
          "done"

    code, out = shell(cmd, cfg)

    if rt_tab is not None:
        rt_tab["last_cache_clear"] = now()
        rt_tab["last_cache_reason"] = reason

    return code == 0, "cache cleared" if code == 0 else cut(out, 80)


def should_clear_cache_for_open(cfg, soft):
    if not cfg.get("clear_cache_enabled", False):
        return False

    if soft:
        return bool(cfg.get("clear_cache_on_soft_hop", False))

    return bool(cfg.get("clear_cache_on_hard_open", True))



def extract_place_id_from_link(link):
    raw = str(link or "").strip()
    if not raw:
        return ""

    if raw.isdigit():
        return raw

    # roblox://experiences/start?placeId=123 / roblox://placeID=123 / robloxmobile://placeID=123
    m = re.search(r"(?:placeId|placeID|placeid)=([0-9]+)", raw, re.I)
    if m:
        return m.group(1)

    # https://www.roblox.com/games/123/name
    m = re.search(r"roblox\.com/games/([0-9]+)", raw, re.I)
    if m:
        return m.group(1)

    return ""


def android_safe_roblox_link(link, cfg=None):
    """Normalize join links before Android am start without dropping private codes."""
    raw = str(link or "").strip()
    if not raw:
        return raw

    cfg = cfg or {}
    low = raw.lower()

    # V3.90: preserve/normalize private-server deep-link parameters. The old
    # normalizer extracted only placeId and silently discarded linkCode, turning
    # a valid private-server link into a public-server join.
    pid = extract_place_id_from_link(raw)
    parsed_code = ""
    parsed_access = ""
    try:
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        for key in ("linkCode", "privateServerLinkCode", "privateServerLinkcode"):
            vals = qs.get(key)
            if vals and vals[0]:
                parsed_code = str(vals[0]).strip()
                break
        vals = qs.get("accessCode")
        if vals and vals[0]:
            parsed_access = str(vals[0]).strip()
    except Exception:
        pass

    # Handle roblox:// links whose parameters may not parse cleanly on every
    # Python build because of mixed-case/custom schemes.
    if not parsed_code:
        m = re.search(r"(?:privateServerLinkCode|linkCode)=([^&#]+)", raw, re.I)
        if m:
            parsed_code = urllib.parse.unquote(m.group(1))
    if not parsed_access:
        m = re.search(r"accessCode=([^&#]+)", raw, re.I)
        if m:
            parsed_access = urllib.parse.unquote(m.group(1))

    if pid and parsed_code:
        return (
            "roblox://experiences/start?placeId=" + str(pid)
            + "&linkCode=" + urllib.parse.quote(parsed_code, safe="")
        )
    if pid and parsed_access:
        return (
            "roblox://experiences/start?placeId=" + str(pid)
            + "&accessCode=" + urllib.parse.quote(parsed_access, safe="")
        )

    # Keep modern Roblox share links intact; the app resolves their share code.
    if "roblox.com/share?" in low or "type=server" in low or ("private" in low and "share" in low):
        return raw

    if not cfg.get("prefer_experience_start_links", True):
        return raw

    if pid:
        return f"roblox://experiences/start?placeId={pid}"

    return raw

def open_roblox(pkg, link, cfg, soft=False, rt_tab=None, reason="", require_stop=True, skip_force_stop=False):
    if not link or str(link).startswith("PUT_"):
        return False, "no link"

    link = android_safe_roblox_link(link, cfg)

    if not soft and require_stop and not skip_force_stop:
        stopped, stop_note = force_stop_package(pkg, cfg, tries=3, wait_after=0.8, settle=1.0)
        if not stopped:
            time.sleep(1.0)

    if should_clear_cache_for_open(cfg, soft):
        clear_package_cache(pkg, cfg, rt_tab=rt_tab, reason=reason)

    base_args = (
        "-a android.intent.action.VIEW "
        f"-d {shlex.quote(link)} "
        f"-p {shlex.quote(pkg)}"
    )

    # V3.87: Redfinger's floating windows can all disappear when a plain
    # ActivityManager start changes the task/windowing mode. Their fresh Lua
    # states prove the peers are still alive; this is a window-layout collapse,
    # not a broad PID stop. Request Android freeform mode for the recovered app.
    use_freeform = bool(cfg.get("preserve_multiwindow_on_launch", True))
    windowing_mode = int(cfg.get("launch_windowing_mode", 5) or 5)
    if use_freeform and windowing_mode > 0:
        cmd = f"am start --windowingMode {windowing_mode} {base_args}"
        code, out = shell_timeout(cmd, cfg, capture=True, timeout=15)
        if code == 0:
            log_activity(f"opened in windowingMode {windowing_mode}", pkg, GREEN)
            return True, "soft hop freeform" if soft else "opened freeform"

        # Some older/custom Android builds reject --windowingMode. Fall back to
        # the old launch command rather than leaving the selected package closed.
        log_activity("freeform launch rejected; plain launch fallback", pkg, YELLOW)

    cmd = "am start " + base_args
    code, out = shell_timeout(cmd, cfg, capture=True, timeout=15)
    if code == 0:
        return True, "soft hop" if soft else "opened"
    return False, cut(out, 60)


# ============================================================
# STATE
# ============================================================

def _sanitize_state_name(s):
    """Match the Lua sanitizer: letters/digits/underscore only."""
    s = str(s or "")
    s = re.sub(r"[^0-9A-Za-z_]", "_", s)
    return s or "unknown"


def _state_folder_candidates(folder):
    """Return new-folder first, then legacy sibling, without duplicates."""
    folder = Path(folder)
    candidates = []
    if folder.name in (STATE_FOLDER_NAME, LEGACY_STATE_FOLDER_NAME):
        parent = folder.parent
        candidates.extend([parent / STATE_FOLDER_NAME, parent / LEGACY_STATE_FOLDER_NAME])
    else:
        candidates.append(folder)
    out = []
    seen = set()
    for item in candidates:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _freshest_state_file(folder):
    """Return the newest *_state.json across new and legacy state folders."""
    try:
        best, best_ts = None, -1
        for candidate in _state_folder_candidates(folder):
            if not candidate.exists():
                continue
            for f in candidate.glob("*_state.json"):
                try:
                    d = json.loads(f.read_text())
                    ts = int(d.get("ts", 0) or 0)
                except Exception:
                    ts = int(f.stat().st_mtime)
                if ts > best_ts:
                    best, best_ts = f, ts
        return best
    except Exception:
        return None


def _state_file_freshness(path):
    """Return a comparable timestamp for a state file (JSON ts, then mtime)."""
    if path is None:
        return -1
    try:
        path = Path(path)
        data = json.loads(path.read_text())
        ts = int(data.get("ts", 0) or 0)
        if ts > 0:
            return ts
        return int(path.stat().st_mtime)
    except Exception:
        try:
            return int(Path(path).stat().st_mtime)
        except Exception:
            return -1


def resolve_state_path(tab):
    """Return the freshest matching state across nomo_rejoiner and legacy gag_rejoiner."""
    raw = str(tab.get("stat_file", "") or "")
    if not raw:
        return None
    configured = Path(raw)
    folders = _state_folder_candidates(configured.parent)
    user = tab.get("user_name") or tab.get("username") or ""

    per_user_candidates = []
    if user:
        safe = _sanitize_state_name(user)
        per_user_candidates = [folder / f"{safe}_state.json" for folder in folders]

    freshest = _freshest_state_file(configured.parent)
    existing_per_user = [p for p in per_user_candidates if p.exists()]
    if existing_per_user:
        per_user = max(existing_per_user, key=_state_file_freshness)
        if freshest is None or _state_file_freshness(per_user) >= _state_file_freshness(freshest):
            return per_user

    if freshest is not None:
        return freshest

    # Legacy state.json fallback in either folder.
    for folder in folders:
        legacy = folder / configured.name
        if legacy.exists():
            return legacy

    # Stable missing target always points at the new canonical folder.
    canonical = folders[0] if folders else configured.parent
    if user:
        return canonical / f"{_sanitize_state_name(user)}_state.json"
    return canonical / configured.name

def cleanup_orphan_state_files(tab, cfg):
    """Delete ONLY proven-orphan state files in this clone's NOMO state folder."""
    if not cfg.get("auto_delete_orphan_state", False):
        return 0

    raw = str(tab.get("stat_file", "") or "")
    if not raw:
        return 0
    folder = Path(raw).parent
    if not folder.exists() or folder.name not in (STATE_FOLDER_NAME, LEGACY_STATE_FOLDER_NAME):
        return 0

    user = tab.get("user_name") or tab.get("username") or ""
    if not user:
        return 0
    current_name = f"{_sanitize_state_name(user)}_state.json"
    current_path = folder / current_name

    stale_after = int(cfg.get("orphan_state_stale_seconds", 3600) or 3600)

    try:
        if not current_path.exists():
            return 0
        if (time.time() - current_path.stat().st_mtime) > stale_after:
            return 0
    except Exception:
        return 0

    deleted = 0
    try:
        for f in folder.glob("*.json"):
            if f.name == current_name:
                continue
            if f.name.endswith("_state.json"):
                continue
            try:
                if (time.time() - f.stat().st_mtime) <= stale_after:
                    continue
                f.unlink()
                deleted += 1
            except Exception:
                continue
    except Exception:
        return deleted
    return deleted


def update_clone_session(rt_tab, status, cfg):
    """Track per-clone session uptime."""
    s = str(status or "").lower()
    active = s in ("ingame", "online")
    resetting = s in ("queued", "loading", "offline", "stale", "kicked", "no state")

    cur = int(rt_tab.get("session_start", 0) or 0)
    if active:
        if cur <= 0:
            rt_tab["session_start"] = now()
    elif resetting:
        rt_tab["session_start"] = 0
    return int(rt_tab.get("session_start", 0) or 0)


def format_session(rt_tab):
    start = int(rt_tab.get("session_start", 0) or 0)
    if start <= 0:
        return "-"
    return format_uptime(now() - start)


def read_state(tab):
    path = resolve_state_path(tab)

    if path is None or not path.exists():
        return None, "missing"

    try:
        data = json.loads(path.read_text())

        pet_count = int(data.get("pet_count", 0))
        state_ts = int(data.get("ts", 0))
        age = now() - state_ts if state_ts > 0 else 999999

        private_keys = [
            "private_server_id", "privateServerId", "PrivateServerId",
            "private_server", "PrivateServer", "is_private_server", "isPrivateServer"
        ]
        private_known = any(k in data for k in private_keys)

        private_server_id = (
            data.get("private_server_id")
            or data.get("privateServerId")
            or data.get("PrivateServerId")
            or ""
        )

        is_private = data.get("is_private_server", data.get("isPrivateServer", None))
        if is_private is None and private_known:
            is_private = bool(str(private_server_id or "").strip())
        elif isinstance(is_private, str):
            is_private = is_private.strip().lower() in ["1", "true", "yes", "y", "on", "private"]

        return {
            "username": data.get("username", tab.get("user_name", "")),
            "pet_count": pet_count,
            "age": age,
            "ts": state_ts,
            "write_seq": int(data.get("write_seq", 0) or 0),
            "script_uptime": int(data.get("script_uptime", 0) or 0),
            "place_id": data.get("place_id", None),
            "job_id": data.get("job_id", data.get("JobId", "")),
            "private_server_id": private_server_id,
            "private_server_owner_id": data.get("private_server_owner_id", data.get("PrivateServerOwnerId", "")),
            "private_server_known": bool(private_known),
            "is_private_server": bool(is_private) if private_known else None,
            "server_type": data.get("server_type", data.get("serverType", "")),
            "counter_version": data.get("counter_version", data.get("counterVersion", "")),
            "disconnected": bool(data.get("disconnected", data.get("is_disconnected", data.get("disconnect_detected", data.get("kicked", False))))),
            "disconnect_observed_ts": int(data.get("disconnect_observed_ts", data.get("disconnectObservedTs", 0)) or 0),
            "disconnect_reason": data.get("disconnect_reason", data.get("disconnectReason", "")),
            "disconnect_title": data.get("disconnect_title", data.get("disconnectTitle", "")),
            "disconnect_text": data.get("disconnect_text", data.get("disconnectText", data.get("disconnect_message", ""))),
            "disconnect_code": str(data.get("disconnect_code", data.get("disconnectCode", "")) or ""),
            "egg_total": int(data.get("egg_total", 0) or 0),
            "eggs": data.get("eggs", {}) if isinstance(data.get("eggs", {}), dict) else {}
        }, None

    except Exception as e:
        return None, str(e)


def expected_state_name(tab):
    """The state filename the harness EXPECTS for this clone. Compare against the
    Lua console line '[NOMO COUNTER] started ... Writing to <path>' to catch a
    username mismatch (the #1 cause of 'in-game but no state')."""
    user = tab.get("user_name") or tab.get("username") or ""
    if user:
        return f"{_sanitize_state_name(user)}_state.json"
    raw = str(tab.get("stat_file", "") or "")
    return Path(raw).name if raw else "state.json"


def auto_find_state_files():
    results = []
    seen = set()
    try:
        root = Path("/storage/emulated/0")
        for folder_name in (STATE_FOLDER_NAME, LEGACY_STATE_FOLDER_NAME):
            for p in root.rglob(f"{folder_name}/*.json"):
                s = str(p)
                if s not in seen:
                    seen.add(s)
                    results.append(s)
    except Exception:
        pass
    return results



# ============================================================
# ACTIVE MODE
# ============================================================

def normalize_active_mode_flags(cfg):
    """Keep Market/Hatcher active flags exclusive."""
    changed = False

    active = str(cfg.get("active_rejoin_mode", "market") or "market").lower().strip()
    if active not in ["market", "hatcher"]:
        active = "market"
        changed = True

    market_on = bool(cfg.get("market_mode_enabled", active == "market"))
    hatcher_on = bool(cfg.get("hatcher_mode_enabled", active == "hatcher"))

    if market_on and hatcher_on:
        if active == "hatcher":
            market_on = False
        else:
            hatcher_on = False
        changed = True

    if not market_on and not hatcher_on:
        if active == "hatcher":
            hatcher_on = True
        else:
            market_on = True
        changed = True

    active2 = "hatcher" if hatcher_on else "market"
    if cfg.get("active_rejoin_mode") != active2:
        cfg["active_rejoin_mode"] = active2
        changed = True
    if cfg.get("market_mode_enabled") != market_on:
        cfg["market_mode_enabled"] = market_on
        changed = True
    if cfg.get("hatcher_mode_enabled") != hatcher_on:
        cfg["hatcher_mode_enabled"] = hatcher_on
        changed = True

    return changed


def active_rejoin_mode(cfg):
    normalize_active_mode_flags(cfg)
    return "hatcher" if cfg.get("hatcher_mode_enabled") else "market"


def set_active_rejoin_mode(mode, cfg=None):
    if cfg is None:
        cfg = load_config()
    mode = str(mode or "market").lower().strip()
    if mode not in ["market", "hatcher"]:
        mode = "market"

    cfg["active_rejoin_mode"] = mode
    cfg["market_mode_enabled"] = mode == "market"
    cfg["hatcher_mode_enabled"] = mode == "hatcher"

    if mode == "hatcher":
        if cfg.get("hatcher_disable_scheduled_hop_on_enable", True):
            cfg["scheduled_hop_enabled"] = False
        if cfg.get("hatcher_disable_soft_hop_fallback_on_enable", True):
            cfg["soft_hop_fallback_hard"] = False
        if cfg.get("hatcher_disable_clear_cache_soft_hop_on_enable", True):
            cfg["clear_cache_on_soft_hop"] = False
        cfg["no_force_stop_alive"] = True
        cfg["alive_open_mode"] = "soft"

    save_config(cfg)
    return cfg


def active_mode_label(cfg):
    return "HATCHER" if active_rejoin_mode(cfg) == "hatcher" else "MARKET"

# ============================================================
# UI
# ============================================================

def line(cfg, code=DIM):
    print(col("─" * term_width(cfg), code))


def banner(title, cfg=None, code=CYAN):
    w = term_width(cfg)
    print(col("═" * w, code))
    print(col(cut(title, w).center(w), BOLD))
    print(col("═" * w, code))


MAIN_MENU_ITEMS = [
    ("1",  "Start rejoin"),
    ("2",  "Mode (Market/Hatcher)"),
    ("3",  "Package manager"),
    ("4",  "Refresh usernames"),
    ("5",  "Set private server"),
    ("6",  "Force restart active tabs"),
    ("7",  "Export cookies"),
    ("8",  "Login via Cookie"),
    ("9",  "Recovery tools"),
    ("10", "Captcha Solver"),
    ("11", "Global config"),
    ("12", "AutoExec manager"),
    ("13", "New Redfinger setup wizard"),
    ("14", "Export diagnostics ZIP"),
    ("15", "NOMO update manager"),
    ("0",  "Exit"),
]

MARKET_MENU_ITEMS = [
    ("1",  "Enable MARKET mode"),
    ("2",  "Package manager (shared)"),
    ("11", "Market-only config"),
    ("0",  "Back"),
]


def main_menu(cfg):
    reset_terminal()
    drain_stdin()
    clear()
    print_banner(cfg)
    print(col(f">>> NOMO REJOIN {__version__} <<<".center(term_width(cfg)), DIM))
    print("")

    rows = []
    for num, label in MAIN_MENU_ITEMS:
        nc = RED if num == "0" else CYAN
        rows.append((num, label, nc, WHITE))
    draw_boxed_menu(rows, cfg)

    print("")
    active = active_mode_label(cfg)
    print(col("Active mode:", BOLD), col(active, GREEN))
    print("")


def format_age(age):
    if age == "-" or age is None:
        return "-"

    try:
        age = int(age)
    except Exception:
        return "-"

    if age < 60:
        return f"{age}s"

    if age < 3600:
        return f"{age // 60}m"

    return f"{age // 3600}h"


def format_uptime(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def pet_color(pets, cfg):
    if pets == "-" or pets is None:
        return DIM
    try:
        p = int(pets)
    except Exception:
        return DIM
    if p < int(cfg["restock_below"]):
        return RED
    if p >= int(cfg["ready_market_at"]):
        return GREEN
    return YELLOW


def onoff(flag):
    return col("Enable", GREEN) if flag else col("Disable", RED)


def status_word(r, cfg):
    """Human status + color, derived from alive/state/note/target."""
    alive = r["alive"] == "ON"
    note = str(r.get("note", "") or "")
    low_note = note.lower()
    stale_limit = int(cfg.get("state_stale_seconds", 180))

    try:
        age_stale = r["age"] != "-" and int(r["age"]) > stale_limit
    except Exception:
        age_stale = False

    if "queued" in low_note:
        return "Queued", YELLOW
    if "manual login" in low_note or "captcha" in low_note or "challenge" in low_note:
        return "Manual", RED
    if "loading" in low_note or "waiting fresh" in low_note or "grace" in low_note:
        return "Loading", YELLOW
    if "crash" in low_note or "dead" in low_note:
        return "Opening", YELLOW
    if "cooldown" in low_note:
        return "Cooldown", DIM
    if "no link" in low_note:
        return "No link", RED
    if (
        low_note.startswith("state ")
        or low_note.startswith("no state")
        or "state missing" in low_note
        or "missing state" in low_note
        or "ui dump unavailable" in low_note
    ):
        return "No state", RED
    if not alive:
        return "Offline", RED
    if age_stale:
        if alive and cfg.get("no_force_stop_alive", True):
            return "Ingame", GREEN
        if "stale wait" in note or "alive stale" in note or "old state" in note:
            return "Ingame", GREEN
        return "Stale", YELLOW
    if "to restock" in note or "restock" in r.get("target", ""):
        return "Restock", BLUE
    if "market" in note:
        return "Market", MAGENTA
    return "Ingame", GREEN


def status_color(status):
    low = str(status or "").lower()
    if low in ("ingame", "online"):
        return GREEN
    if low in ("manual", "captcha", "kicked", "offline", "no state", "no server", "wrong server"):
        return RED
    if low in ("queued", "loading", "opening", "stale", "cooldown"):
        return YELLOW
    if low == "restock":
        return BLUE
    if low == "market":
        return MAGENTA
    return WHITE


def note_color(note):
    """Color-code a note by its lifecycle/status keywords for readability."""
    n = str(note or "").lower()
    if n in ("", "-"):
        return DIM

    # CYAN — in-progress actions FIRST
    for k in ("opening", "loading", "waiting", "fresh", "sending", "solving",
              "sent to solver", "grace", "stagger", "kill+open", "reopen"):
        if k in n:
            return CYAN

    # RED — unresolved problems
    for k in ("captcha on screen", "kicked", "disconnect", "expired", "offline",
              "dead", "error", "fail", "invalid", "banned", "no server",
              "crash", "challenge", "verification", "manual"):
        if k in n:
            return RED

    # YELLOW — queued / retry / stale
    for k in ("queued", "retry", "stale", "cooldown", "hold", "wait", "old ", "kick"):
        if k in n:
            return YELLOW

    # MAGENTA / BLUE — routing
    if "market" in n:
        return MAGENTA
    if "restock" in n:
        return BLUE

    # GREEN — success / healthy
    for k in ("ok", "ingame", "online", "solved", "success", "skipped", "alive"):
        if k in n:
            return GREEN

    return WHITE


def status_screen(rows, cfg, session_start, loops):
    clear()
    w = term_width(cfg)

    print_banner(cfg)
    print(col(f">>> NOMO REJOIN {__version__} <<<".center(w), DIM))
    print("")

    # config summary block
    up = format_uptime(now() - session_start)
    print(f"  {col('METHOD', DIM)} : Online     "
          f"{col('UPTIME', DIM)} : {col(up, CYAN)}   "
          f"{col('CHECKS', DIM)} : {loops}")
    print(f"  {col('BLOCK', DIM)}  : {onoff(cfg.get('rejoin_if_crash'))}    "
          f"{col('SORT', DIM)}  : {onoff(cfg.get('ignore_alive_stale_state'))}    "
          f"{col('COLOR', DIM)} : {onoff(cfg.get('use_color'))}")

    cpu = read_cpu_percent()
    used, total = read_ram_gb()
    cpu_txt = f"{cpu:4.1f} %" if cpu is not None else "  -- "
    if used is not None:
        ram_txt = f"{used:.2f} / {total:.2f} GB"
        ram_pct = used / total if total else 0
        ram_code = RED if ram_pct > 0.9 else (YELLOW if ram_pct > 0.75 else GREEN)
    else:
        ram_txt, ram_code = "-- / -- GB", DIM
    cpu_code = RED if (cpu or 0) > 90 else (YELLOW if (cpu or 0) > 70 else GREEN)

    print(f"  {col('CPU usage', DIM)} : {col(cpu_txt, cpu_code)}     "
          f"{col('RAM usage', DIM)} : {col(ram_txt, ram_code)}")
    print("")

    table_rows = []
    widths = [2,  13,   10,   4,  4,  9,     6,  7,   18]
    table_overhead = (len(widths) + 1) + (2 * len(widths))
    spare = max(0, w - table_overhead - sum(widths))
    widths[-1] = max(widths[-1], min(40, widths[-1] + spare))

    for i, r in enumerate(rows, 1):
        if r.get("status"):
            st = str(r.get("status"))
            st_code = status_color(st)
        else:
            st, st_code = status_word(r, cfg)
        table_rows.append([
            (str(i), CYAN),
            (r["user"], WHITE),
            (short_pkg(r["pkg"]), WHITE),
            (r["pets"], pet_color(r["pets"], cfg), True),
            (str(r.get("eggs", "-")), WHITE, True),
            (st, st_code),
            (format_age(r.get("age")), DIM),
            (str(r.get("session", "-")), CYAN),
            (cut(r.get("note", ""), widths[-1]), note_color(r.get("note", ""))),
        ])

    draw_table(
        ["No", "Username", "Package", "Pet", "Egg", "Status", "Age", "Session", "Note"],
        table_rows,
        widths,
        cfg,
    )

    render_activity_log(cfg, lines=int(cfg.get("activity_log_lines", 6) or 6))

    print("")
    print(col(f"  <{cfg['restock_below']} restock", RED)
          + col(f"   >={cfg['ready_market_at']} market", GREEN)
          + col(f"   refresh {cfg['check_interval']}s", DIM))
    print(col("  Type Q + ENTER to stop / return to menu", DIM))


def opening_screen(tab, target, cfg, index, total, mode="hard"):
    if cfg.get("verbose_open_log", False):
        print(col(f"  opening {tab.get('user_name') or tab.get('package')} -> {target} ({mode})", DIM))


def wait_fresh_screen(tab, cfg, elapsed, timeout, alive, state, err, status_note=""):
    if cfg.get("verbose_open_log", False):
        u = tab.get("user_name") or tab.get("package")
        print(col(f"  {u}: {status_note} ({elapsed}/{timeout}s)", DIM))


# ============================================================
# MENU ACTIONS
# ============================================================

def package_template_menu_title(kind, cfg=None):
    clear()
    banner(f"SELECT {kind.upper()} PACKAGE TEMPLATE", cfg)
    print(col("This changes package names only. It keeps usernames, state_file, and server links.", DIM))
    print("")


def natural_package_key(pkg):
    """Sort premium.nokaA/B/C/D naturally instead of random pm order."""
    s = str(pkg or "")
    m = re.match(r"^(.*?)([A-Z])$", s)
    if m:
        return (m.group(1), ord(m.group(2)) - 65, s)
    m = re.match(r"^(.*?)(\d+)$", s)
    if m:
        return (m.group(1), int(m.group(2)), s)
    return (s, 0, s)


def detect_installed_noka_packages(cfg=None):
    """Return installed packages containing .noka, e.g. premium.nokaA-C actually installed."""
    tmp_cfg = cfg if isinstance(cfg, dict) else {"use_su": True}
    rc, out = shell("pm list packages", tmp_cfg, capture=True)
    if rc != 0 or not out:
        return []

    pkgs = []
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("package:"):
            pkg = line.split(":", 1)[1].strip()
        else:
            pkg = line.strip()
        if ".noka" not in pkg:
            continue
        if pkg in seen:
            continue
        seen.add(pkg)
        pkgs.append(pkg)

    return sorted(pkgs, key=natural_package_key)


def make_template_packages(base=None, count=4, suffix_mode="letters", packages=None):
    if packages:
        return [str(x).strip() for x in packages if str(x).strip()]
    base = str(base or "").strip()
    count = int(count or 4)
    if not base:
        return []
    if suffix_mode == "single" or count <= 1:
        return [base]
    return [base + chr(65 + i) for i in range(count)]


def is_installed_package(pkg, cfg=None):
    """Return True if Android reports this package as installed."""
    pkg = str(pkg or "").strip()
    if not pkg:
        return False
    tmp_cfg = cfg if isinstance(cfg, dict) else {"use_su": True}
    rc, out = shell("pm list packages " + shlex.quote(pkg), tmp_cfg, capture=True)
    if rc != 0 or not out:
        return False
    target = "package:" + pkg
    return any(line.strip() == target for line in out.splitlines())


def get_package_templates(store=None):
    """Saved user templates only."""
    templates = []
    custom = {}
    if isinstance(store, dict):
        custom = store.get("package_templates", {}) or {}
    if isinstance(custom, dict):
        for name, val in custom.items():
            if isinstance(val, dict):
                templates.append({
                    "name": str(name),
                    "base": str(val.get("base", name)),
                    "count": int(val.get("count", 4)),
                    "suffix_mode": str(val.get("suffix_mode", "letters")),
                    "packages": val.get("packages"),
                })
    return templates


def parse_package_names(text):
    """Parse comma/space/newline separated Android package names."""
    raw = str(text or "").replace("\n", " ").replace(",", " ").split()
    out = []
    seen = set()
    for x in raw:
        pkg = x.strip().strip('"\'')
        if not pkg or pkg in seen:
            continue
        seen.add(pkg)
        out.append(pkg)
    return out


def add_custom_package_names(cfg=None):
    package_template_menu_title("custom", cfg)
    print("Use custom package name(s) now")
    print(col("Examples:", DIM))
    print(col("  com.roblox", DIM))
    print(col("  premium.nokaA premium.nokaB premium.nokaC", DIM))
    print(col("  your.clone.pkg1, your.clone.pkg2", DIM))
    print("")
    text = input("Package name(s): ").strip()
    pkgs = parse_package_names(text)
    if not pkgs:
        return None
    return {"name": "Custom package name(s)", "packages": pkgs}


def add_package_template(store, cfg=None):
    package_template_menu_title("custom", cfg)
    print("Save custom package template")
    print(col("Option A: exact package list, e.g. premium.nokaA premium.nokaB", DIM))
    print(col("Option B: base + count, e.g. premium.noka + 4 -> premium.nokaA-D", DIM))
    print("")
    name = input("Template name: ").strip()
    if not name:
        return False

    exact = input("Exact package names/list [blank to use base+count]: ").strip()
    pkgs = parse_package_names(exact)
    if pkgs:
        store.setdefault("package_templates", {})[name] = {"packages": pkgs, "base": name, "count": len(pkgs), "suffix_mode": "custom"}
        return True

    base = input("Package base/name: ").strip()
    if not base:
        return False
    cnt = input("How many packages? 1-8 [4]: ").strip()
    try:
        count = int(cnt) if cnt else 4
    except Exception:
        count = 4
    count = max(1, min(8, count))
    suffix_mode = "single" if count <= 1 else "letters"
    store.setdefault("package_templates", {})[name] = {"base": base, "count": count, "suffix_mode": suffix_mode}
    return True

def select_packages_menu(cfg=None):
    if cfg is None:
        cfg = load_config()
    return choose_packages_common(cfg, "SELECT PACKAGES FOR EXPORT", multi=True,
                                  installed_only=True, include_discovered=True)

def select_package_template_common(store, cfg=None, kind="market"):
    while True:
        package_template_menu_title(kind, cfg)
        detected = detect_installed_noka_packages(cfg)
        if detected:
            preview = ", ".join(short_pkg(x) for x in detected[:8])
            print(f"1. Auto-detect installed .noka       ->  {preview}")
        else:
            print(f"1. Auto-detect installed .noka       ->  {col('none found', YELLOW)}")

        roblox_note = col("installed", GREEN) if is_installed_package("com.roblox", cfg) else col("not detected", YELLOW)
        print(f"2. com.roblox                       ->  {roblox_note}")

        templates = get_package_templates(store)
        start_idx = 3
        for i, t in enumerate(templates, start_idx):
            pkgs = make_template_packages(t.get("base"), t.get("count", 4), t.get("suffix_mode", "letters"), t.get("packages"))
            preview = ", ".join(short_pkg(x) for x in pkgs[:8])
            print(f"{i}. Saved: {t['name']}  ->  {preview}")

        print("C. Use custom package name(s) now")
        print("A. Save custom package template")
        print("0. Back")
        drain_stdin()
        ch = input("\nChoose template: ").strip().lower()
        if ch == "0":
            return None
        if ch == "1":
            if not detected:
                print(col("No installed .noka packages detected.", RED))
                pause()
                continue
            return {"name": "Auto-detected installed .noka", "packages": detected}
        if ch == "2":
            return {"name": "com.roblox", "packages": ["com.roblox"]}
        if ch == "c":
            tmpl = add_custom_package_names(cfg)
            if tmpl:
                return tmpl
            continue
        if ch == "a":
            if add_package_template(store, cfg):
                return "__SAVE_ONLY__"
            continue
        if ch.isdigit():
            idx = int(ch) - start_idx
            if 0 <= idx < len(templates):
                return templates[idx]

def apply_market_package_template(cfg, tmpl):
    pkgs = make_template_packages(tmpl.get("base"), tmpl.get("count", 4), tmpl.get("suffix_mode", "letters"), tmpl.get("packages"))
    if not pkgs:
        return
    old_tabs = cfg.get("tabs", [])
    new_tabs = []
    for i, pkg in enumerate(pkgs):
        if i < len(old_tabs) and isinstance(old_tabs[i], dict):
            tab = dict(old_tabs[i])
        elif i < len(DEFAULT_TABS):
            tab = json.loads(json.dumps(DEFAULT_TABS[i]))
        else:
            tab = json.loads(json.dumps(DEFAULT_TABS[-1]))
            tab["user_name"] = f"account{i+1}"
            tab["stat_file"] = f"/storage/emulated/0/RobloxClone{str(i+1).zfill(3)}/Arceus X/Workspace/nomo_rejoiner/state.json"
        tab["package"] = pkg
        new_tabs.append(tab)
    cfg["tabs"] = new_tabs


def select_market_package_template(cfg):
    tmpl = select_package_template_common(cfg, cfg, "market")
    if tmpl is None:
        return
    if tmpl == "__SAVE_ONLY__":
        save_config(cfg)
        print(col("Template saved.", GREEN))
        pause()
        return
    apply_market_package_template(cfg, tmpl)
    # Sync hatcher profiles
    hcfg = load_hatcher_config()
    sync_hatcher_profiles_with_tabs(cfg, hcfg)
    save_config(cfg)
    print(col("Market packages updated. Hatcher profiles synced.", GREEN))
    pause()


def apply_hatcher_package_template(hcfg, tmpl):
    pkgs = make_template_packages(tmpl.get("base"), tmpl.get("count", 4), tmpl.get("suffix_mode", "letters"), tmpl.get("packages"))
    if not pkgs:
        return
    old = hcfg.get("hatchers", [])
    new = []
    for i, pkg in enumerate(pkgs):
        if i < len(old) and isinstance(old[i], dict):
            prof = dict(old[i])
        elif i < len(DEFAULT_HATCHER_PROFILES):
            prof = json.loads(json.dumps(DEFAULT_HATCHER_PROFILES[i]))
        else:
            prof = json.loads(json.dumps(DEFAULT_HATCHER_PROFILE))
            prof["hatcher_name"] = f"nomohatch{i+1}"
            prof["state_file"] = f"/storage/emulated/0/RobloxClone{str(i+1).zfill(3)}/Arceus X/Workspace/nomo_rejoiner/state.json"
        prof["package"] = pkg
        new.append(normalize_hatcher_profile(prof, i))
    hcfg["hatchers"] = new


def select_hatcher_package_template(main_cfg=None):
    hcfg = load_hatcher_config()
    tmpl = select_package_template_common(hcfg, main_cfg, "hatcher")
    if tmpl is None:
        return
    if tmpl == "__SAVE_ONLY__":
        save_hatcher_config(hcfg)
        print(col("Template saved.", GREEN))
        pause()
        return
    apply_hatcher_package_template(hcfg, tmpl)
    # Sync Market tabs
    cfg = load_config()
    pkgs = [prof.get("package") for prof in hcfg.get("hatchers", []) if prof.get("package")]
    old_tabs = cfg.get("tabs", [])
    old_map = {tab.get("package"): tab for tab in old_tabs}
    new_tabs = []
    for pkg in pkgs:
        if pkg in old_map:
            tab = old_map[pkg]
        else:
            api_user, _ = get_username_from_package_api(pkg)
            tab = {
                "enabled": True,
                "package": pkg,
                "user_name": api_user or pkg,
                "stat_file": f"/storage/emulated/0/RobloxClone{len(new_tabs)+1:03d}/Arceus X/Workspace/nomo_rejoiner/state.json",
                "restock_link": DEFAULT_RESTOCK_LINK
            }
        new_tabs.append(tab)
    cfg["tabs"] = new_tabs
    save_config(cfg)
    print(col("Hatcher packages updated. Market tabs synced.", GREEN))
    pause()

def reset_packages(cfg):
    cfg["tabs"] = json.loads(json.dumps(DEFAULT_TABS))
    save_config(cfg)
    print(col("Reset package list to default premium.nokaA-D.", GREEN))
    pause()


def toggle_packages(cfg):
    while True:
        clear()
        banner("ENABLE / DISABLE", cfg)

        for i, t in enumerate(cfg["tabs"], 1):
            on = t.get("enabled", True)
            mark = col("ON ", GREEN) if on else col("OFF", RED)
            print(f"{i}. [{mark}] {t.get('package')} | {t.get('user_name')}")

        print("0. Save/back")

        drain_stdin()
        choice = input("\nIndex: ").strip()
        if choice == "0":
            save_config(cfg)
            hcfg = load_hatcher_config()
            sync_hatcher_profiles_with_tabs(cfg, hcfg)
            return
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(cfg["tabs"]):
                cfg["tabs"][idx]["enabled"] = not cfg["tabs"][idx].get("enabled", True)
                save_config(cfg)
                hcfg = load_hatcher_config()
                sync_hatcher_profiles_with_tabs(cfg, hcfg)



def toggle_hatcher_packages(main_cfg=None):
    hcfg = load_hatcher_config()
    while True:
        clear()
        banner("ENABLE / DISABLE HATCHERS", main_cfg)

        profiles = hatcher_profiles(hcfg)
        for i, p in enumerate(profiles, 1):
            on = p.get("enabled", True)
            mark = col("ON ", GREEN) if on else col("OFF", RED)
            print(f"{i}. [{mark}] {p.get('package')} | {p.get('hatcher_name')} | {short_link(p.get('server_link', ''))}")

        print("0. Save/back")

        drain_stdin()
        choice = input("\nIndex: ").strip()

        if choice == "0":
            hcfg["hatchers"] = profiles
            save_hatcher_config(hcfg)
            return

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(profiles):
                profiles[idx]["enabled"] = not profiles[idx].get("enabled", True)
                hcfg["hatchers"] = profiles
                save_hatcher_config(hcfg)

def get_username_from_cookie(cookie, timeout=10):
    """Resolve the Roblox username from a .ROBLOSECURITY cookie."""
    if not cookie:
        return None, None
    url = "https://users.roblox.com/v1/users/authenticated"
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "User-Agent": "NOMO-Rejoin/1.0",
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        name = data.get("name")
        uid = data.get("id")
        if name:
            return str(name), uid
        return None, None
    except urllib.error.HTTPError:
        return None, None
    except Exception:
        return None, None


def get_username_from_package_api(package, timeout=10):
    """Extract the package cookie and resolve its username via the Roblox API."""
    cookie = get_cookie_from_package(package)
    if not cookie:
        return None, None
    return get_username_from_cookie(cookie, timeout=timeout)


def _usable_detected_username(value):
    name = str(value or "").strip()
    if not name or name.lower() in {"unknown", "none", "null", "no state"}:
        return ""
    return name


def _persist_market_usernames_only(cfg):
    """Persist only package->username mappings, never runtime-only overrides."""
    try:
        stored = load_config()
    except Exception:
        stored = {}

    wanted = {
        t.get("package"): _usable_detected_username(t.get("user_name"))
        for t in cfg.get("tabs", [])
        if t.get("package")
    }
    changed = False
    for tab in stored.get("tabs", []):
        pkg = tab.get("package")
        username = wanted.get(pkg, "")
        if username and tab.get("user_name") != username:
            tab["user_name"] = username
            changed = True
    if changed:
        save_config(stored)
    return changed


def sync_detected_username_for_package(hcfg, package, username, tab=None, source="state"):
    """Apply one detected account name to the package and persist it safely."""
    username = _usable_detected_username(username)
    package = str(package or "").strip()
    if not package or not username:
        return username or (tab or {}).get("user_name", package), False

    if isinstance(tab, dict):
        tab["user_name"] = username

    changed_hatcher = False
    for prof in hatcher_profiles(hcfg, enabled_only=False):
        if prof.get("package") == package and prof.get("hatcher_name") != username:
            prof["hatcher_name"] = username
            changed_hatcher = True
            break

    changed_market = False
    try:
        stored = load_config()
        for market_tab in stored.get("tabs", []):
            if market_tab.get("package") == package:
                if market_tab.get("user_name") != username:
                    market_tab["user_name"] = username
                    changed_market = True
                break
        if changed_market:
            save_config(stored)
    except Exception:
        changed_market = False

    if changed_hatcher:
        save_hatcher_config(hcfg)

    if changed_market or changed_hatcher:
        log_activity(f"username -> {username} [{source}]", package, GREEN)

    return username, (changed_market or changed_hatcher)


def get_all_usernames_via_api(cfg=None):
    """Manual username refresh for selected configured packages."""
    if cfg is None:
        cfg = load_config()
    selected = choose_packages_common(
        cfg, "REFRESH USERNAMES", multi=True,
        include_discovered=False, configured_only=True
    )
    if not selected:
        return
    clear()
    banner("REFRESH USERNAMES", cfg)
    print(col("Cookie -> Roblox API, with freshest state fallback.", DIM))
    print("")
    refresh_usernames_for_packages(cfg, selected)
    pause()

def resolve_usernames_auto(cfg, hcfg=None, rt=None, force=False, quiet=True):
    """AUTOMATIC version of option 4. Resolves each package's real username from
    its cookie (Roblox API), falling back to the freshest state file, and writes
    it into tab['user_name'] / profile['hatcher_name'] so state matching lines up
    without the user ever running the manual step. Cached + rate-limited via rt.

    Fully guarded: any network/root failure just leaves names as-is.
    """
    if not cfg.get("auto_resolve_usernames_enabled", True):
        return False
    if hcfg is None:
        try:
            hcfg = load_hatcher_config()
        except Exception:
            hcfg = {}

    interval = int(cfg.get("username_resolve_interval_seconds", 600) or 600)
    if not force and isinstance(rt, dict):
        last = int(rt.get("_last_username_resolve", 0) or 0)
        if last > 0 and (now() - last) < interval:
            return False

    tabs = cfg.get("tabs", [])
    profiles = hatcher_profiles(hcfg, enabled_only=False)
    profile_map = {p.get("package"): p for p in profiles if p.get("package")}

    # Resolve across BOTH market tabs and hatcher profiles by package.
    packages = {}
    for tab in tabs:
        pkg = tab.get("package")
        if pkg:
            packages.setdefault(pkg, {})["tab"] = tab
    for pkg, prof in profile_map.items():
        packages.setdefault(pkg, {})["prof"] = prof

    changed_market = False
    changed_hatcher = False

    for pkg, refs in packages.items():
        tab = refs.get("tab")
        prof = refs.get("prof")
        # Build a tab-like object for state fallback if only a profile exists.
        state_tab = tab or (hatcher_profile_to_tab(prof) if prof else None)

        username = None
        try:
            username, _uid = get_username_from_package_api(pkg)
        except Exception:
            username = None

        if not username and state_tab is not None:
            try:
                state, _err = read_state(state_tab)  # uses freshest-file fallback
                if state and state.get("username"):
                    username = state.get("username")
            except Exception:
                username = None

        if not username:
            continue

        if tab is not None and tab.get("user_name") != username:
            tab["user_name"] = username
            changed_market = True
        if prof is not None and prof.get("hatcher_name") != username:
            prof["hatcher_name"] = username
            changed_hatcher = True
        if not quiet:
            print(f"{pad(short_pkg(pkg), 6, CYAN)} -> {col(username, GREEN)}")

    if changed_market:
        try:
            _persist_market_usernames_only(cfg)
        except Exception:
            pass
    if changed_hatcher:
        try:
            save_hatcher_config(hcfg)
        except Exception:
            pass
    if isinstance(rt, dict):
        rt["_last_username_resolve"] = now()

    return changed_market or changed_hatcher



    banner("GET USERNAME", cfg)

    print("")
    for t in cfg["tabs"]:
        state, err = read_state(t)

        if state:
            t["user_name"] = state.get("username", t.get("user_name", ""))
            print(f"{pad(short_pkg(t['package']), 6, CYAN)} -> {col(t['user_name'], GREEN)} | pets={state.get('pet_count')}")
        else:
            print(f"{pad(short_pkg(t['package']), 6, CYAN)} -> {col('no state: ' + str(err), RED)}")

    save_config(cfg)

    print("\nJSON files found:")
    found = auto_find_state_files()

    if not found:
        print(col("none", DIM))

    for p in found[:20]:
        print(col(p, DIM))

    pause()




def get_all_usernames_from_state(cfg=None):
    """Global username refresh. Updates Market tabs and Hatcher profiles from their state files."""
    if cfg is None:
        cfg = load_config()
    hcfg = load_hatcher_config()

    clear()
    banner("GET USERNAME FROM STATE", cfg)
    print(col("Updates both Market usernames and Hatcher names from nomo_rejoiner/*.json (legacy gag_rejoiner also supported).", DIM))
    print("")

    print(col("JSON files found:", BOLD))
    found = auto_find_state_files()
    if not found:
        print(col("  none", DIM))
    else:
        for p in found[:20]:
            print(col("  " + p, DIM))
    print("")

    tabs = cfg.get("tabs", [])
    profiles = hatcher_profiles(hcfg, enabled_only=False)
    profile_map = {p.get("package"): p for p in profiles if p.get("package")}

    rows = []
    changed_market = False
    changed_hatcher = False

    for tab in tabs:
        pkg = tab.get("package")
        state, err = read_state(tab)
        if state:
            username = state.get("username", tab.get("user_name", ""))
            if tab.get("user_name") != username:
                tab["user_name"] = username
                changed_market = True
            pets = str(state.get("pet_count", 0))
            eggs = str(state.get("egg_total", 0))
        else:
            username = "no state"
            pets = "-"
            eggs = cut(str(err), 12)

        if pkg in profile_map:
            prof = profile_map[pkg]
            if state and prof.get("hatcher_name") != username:
                prof["hatcher_name"] = username
                changed_hatcher = True
        rows.append([
            (short_pkg(pkg), CYAN),
            (username, GREEN if state else RED),
            (pets, WHITE, True),
            (eggs, WHITE, True),
        ])

    draw_table(["Package", "Username", "Pets", "Eggs"], rows, [8, 16, 6, 16], cfg)

    if changed_market:
        save_config(cfg)
    if changed_hatcher:
        save_hatcher_config(hcfg)

    pause()

def set_hatcher_servers(main_cfg=None):
    if main_cfg is None:
        main_cfg = load_config()
    hcfg = load_hatcher_config()
    selected = choose_packages_common(
        main_cfg, "SET HATCHER SERVER LINK", multi=True,
        include_discovered=False, configured_only=True
    )
    if not selected:
        return
    profiles = hatcher_profiles(hcfg, enabled_only=False)
    selected_profiles = [p for p in profiles if p.get("package") in set(selected)]
    if not selected_profiles:
        print(col("No matching Hatcher profiles.", RED))
        pause()
        return
    print("")
    for prof in selected_profiles:
        print(f"{short_pkg(prof.get('package'))}: {short_link(prof.get('server_link', ''))}")
    link = input("\nPaste Hatcher/private-server link for selected package(s):\n> ").strip()
    if not link:
        return
    for prof in selected_profiles:
        prof["server_link"] = link
    hcfg["hatchers"] = profiles
    save_hatcher_config(hcfg)
    print(col(f"Saved for {len(selected_profiles)} Hatcher package(s).", GREEN))
    pause()

def set_market_game_link(cfg):
    """Edit the normal MARKET/Trade World join link."""
    while True:
        cfg = load_config()
        clear()
        banner("SET MARKET GAME LINK", cfg)
        cur = str(cfg.get("market_link", DEFAULT_MARKET_LINK) or DEFAULT_MARKET_LINK)
        launch = android_safe_roblox_link(cur, cfg)
        print(col("This is the link used when Market mode returns/opens to Trade World.", DIM))
        print("")
        print(f"Current : {cur}")
        print(f"Opens as: {launch}")
        print("")
        print("Paste one of these:")
        print("- 129954712878723")
        print("- https://www.roblox.com/games/129954712878723/Grow-a-Garden-Trade-World")
        print("- roblox://experiences/start?placeId=129954712878723")
        print("")
        print("1. Edit MARKET game link")
        print("R. Reset to default Trade World")
        print("0. Back")
        drain_stdin()
        ch = input("\nChoose: ").strip().lower()

        if ch == "0":
            return

        if ch == "r":
            cfg["market_link"] = DEFAULT_MARKET_LINK
            save_config(cfg)
            print(col("\nMarket link reset to default Trade World.", GREEN))
            pause()
            continue

        if ch == "1":
            link = input("\nPaste MARKET game link/placeId:\n> ").strip()
            if link:
                cfg["market_link"] = android_safe_roblox_link(link, cfg)
                save_config(cfg)
                print(col("\nSaved MARKET game link.", GREEN))
                print(f"Opens as: {cfg['market_link']}")
                pause()


def register_market_accounts_to_d1(cfg=None):
    """One-time registration of local Market accounts (username + user ID only)."""
    if cfg is None:
        cfg = load_config()

    if backend_provider(cfg) != "cloudflare":
        print(col("Cloudflare backend is not enabled in Market config.", RED))
        pause()
        return

    selected = choose_packages_common(cfg, "REGISTER MARKET ACCOUNTS TO D1", multi=True,
                                      installed_only=True, include_discovered=True)
    if not selected:
        return
    print(col("Uploads username + Roblox user ID only. Cookies never leave this device.", DIM))

    cache = load_cookie_cache()
    accounts = []
    results = []
    tabs_by_pkg = {str(t.get("package") or ""): t for t in cfg.get("tabs", [])}

    for pkg in selected:
        print(col(f"\n[{pkg}]", CYAN))
        cookie = get_cookie_from_package(pkg)
        if not cookie:
            cookie = str((cache.get(pkg) or {}).get("cookie") or "").strip()
        if not cookie:
            print(col("No cookie found; skipped.", RED))
            results.append((pkg, "NO COOKIE"))
            continue
        username, user_id = get_username_from_cookie(cookie, timeout=12)
        if not username or not str(user_id or "").isdigit():
            print(col("Cookie could not resolve username/user ID; skipped.", RED))
            results.append((pkg, "BAD COOKIE"))
            continue
        tab = tabs_by_pkg.get(pkg)
        if tab is not None:
            tab["user_name"] = username
        accounts.append({
            "username": username,
            "user_id": str(user_id),
            "role": "market",
            "package_name": pkg,
            "source_name": "nomo-rejoin-v3.95",
        })
        print(col(f"READY: {username} ({user_id})", GREEN))
        results.append((pkg, "READY"))

    if not accounts:
        print(col("\nNothing to register.", RED))
        pause()
        return

    ok, msg, data = cloudflare_register_accounts(cfg, accounts)
    if ok:
        save_config(cfg)
        print(col(f"\nREGISTERED: {int(data.get('registered_count', len(accounts)) or len(accounts))} Market accounts", GREEN))
    else:
        print(col(f"\nD1 registration failed: {msg}", RED))
        if "not_found" in str(msg).lower() or "404" in str(msg):
            print(col("Deploy NOMO D1 Worker v3 and run schema_v3.sql first.", YELLOW))
    pause()


def set_global_private_server_menu(cfg=None):
    """Main-menu shortcut for Market link, restock links, and Hatcher links."""
    if cfg is None:
        cfg = load_config()

    while True:
        cfg = load_config()
        clear()
        banner("SET PRIVATE SERVER / LINKS", cfg)
        print(col("Market game link, Market restock links, and Hatcher links stay separate.", DIM))
        print("")
        print(f"Active mode: {col(active_mode_label(cfg), GREEN)}")
        print(f"Market link : {col(short_link(cfg.get('market_link', DEFAULT_MARKET_LINK)), CYAN)}")
        print(f"Restock link: {col(short_link(cfg.get('restock_link', DEFAULT_RESTOCK_LINK)), CYAN)}")
        print("")
        print("1. Edit MARKET game link")
        print("2. Edit MARKET restock/private servers")
        print("3. Edit HATCHER private servers")
        print("4. Fetch/create HATCHER servers + sync Market access")
        print("5. Register MARKET accounts to D1 (one-time)")
        print("0. Back")
        drain_stdin()
        ch = read_menu_choice("\nChoose: ", {"0", "1", "2", "3", "4", "5", "q", "b", "back"})
        if ch in {"0", "q", "b", "back"}:
            return
        try:
            if ch == "1":
                set_market_game_link(cfg)
            elif ch == "2":
                set_private_server(cfg)
            elif ch == "3":
                set_hatcher_servers(cfg)
            elif ch == "4":
                auto_fetch_private_servers(cfg)
            elif ch == "5":
                register_market_accounts_to_d1(cfg)
            else:
                print("Invalid choice.")
                time.sleep(1)
        except Exception as e:
            print(col(f"Error: {e}", RED))
            pause()


def force_restart_active_tabs_once(cfg=None):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    if cfg is None:
        cfg = load_config()
    selected = choose_packages_common(cfg, "PID-RESTART ENABLED PACKAGES", multi=True,
                                      enabled_only=True, include_discovered=False,
                                      configured_only=True)
    if not selected:
        return
    if active_rejoin_mode(cfg) == "hatcher":
        open_all_hatcher_tabs_once(cfg, selected_packages=selected)
    else:
        open_all_tabs_once(cfg, selected_packages=selected)

def set_private_server(cfg):
    selected = choose_packages_common(
        cfg, "SET MARKET RESTOCK LINK", multi=True,
        include_discovered=False, configured_only=True
    )
    if not selected:
        return
    wanted = set(selected)
    chosen_tabs = [t for t in cfg.get("tabs", []) if t.get("package") in wanted]
    print("")
    for tab in chosen_tabs:
        print(f"{short_pkg(tab.get('package'))}: {short_link(tab.get('restock_link', cfg.get('restock_link', '')))}")
    link = input("\nPaste MARKET restock/private-server link for selected package(s):\n> ").strip()
    if not link:
        return
    for tab in chosen_tabs:
        tab["restock_link"] = link
    if len(chosen_tabs) == len(cfg.get("tabs", [])):
        cfg["restock_link"] = link
    save_config(cfg)
    print(col(f"Saved for {len(chosen_tabs)} Market package(s).", GREEN))
    pause()

def manual_force_restart_tab(tab, rt_tab, cfg, target, reason, rt=None):
    """Manual option-6 open."""
    link = target_link(tab, cfg, target, rt_tab, rt)

    if not link:
        rt_tab["note"] = "no link"
        return False, "no link"

    ok, note = open_roblox(tab["package"], link, cfg, soft=False, rt_tab=rt_tab, reason=reason)

    rt_tab["target"] = target
    rt_tab["last_open"] = now()
    rt_tab["last_open_mode"] = "manual-hard"
    rt_tab["note"] = reason

    return ok, note


def open_all_tabs_once(cfg, selected_packages=None):
    rt = load_runtime()
    wanted = set(selected_packages or [])
    enabled_tabs = [t for t in cfg["tabs"] if t.get("enabled", True) and (not wanted or t.get("package") in wanted)]
    if not enabled_tabs:
        print(col("No enabled tabs to open.", YELLOW))
        pause()
        return
    total = len(enabled_tabs)

    for idx, tab in enumerate(enabled_tabs, 1):
        if stop_requested():
            save_runtime(rt)
            break

        rt_tab = get_runtime_tab(rt, tab["package"])
        target = rt_tab.get("target", "market")
        opening_screen(tab, target, cfg, idx, total, mode="hard")
        print(col("Manual option 6: force-stop first, then open. Auto rejoin safety is unchanged.", YELLOW))
        print(col("Type Q + ENTER to cancel after this open / during delay.", DIM))
        manual_force_restart_tab(tab, rt_tab, cfg, target, "manual force restart", rt=rt)
        save_runtime(rt)

        if idx < total:
            if not wait_seconds(int(cfg.get("delay_between_open", 45)), rt):
                break

    print(col("\nDone opening tabs.", GREEN))
    pause()


def show_config_value(key, value):
    kl = str(key).lower()
    if "api_key" in kl or "webhook" in kl or "secret" in kl:
        return mask_secret(value)
    return value


def edit_config_group(cfg, title, items):
    while True:
        clear()
        banner(title, cfg)

        for i, (key, label) in enumerate(items, 1):
            print(f"{i}. {pad(label, 28, CYAN)} = {show_config_value(key, cfg.get(key))}")
        print("0. Back")

        drain_stdin()
        ch = input("\nChoose: ").strip()

        if ch == "0":
            save_config(cfg)
            set_color_enabled(cfg)
            return

        try:
            idx = int(ch) - 1
        except Exception:
            continue

        if idx < 0 or idx >= len(items):
            continue

        key, label = items[idx]
        current = cfg.get(key)
        val = input(f"New value for {key} [{current}] (blank ENTER toggles bool): ").strip()

        if isinstance(current, bool):
            if not val:
                cfg[key] = not current
            else:
                cfg[key] = val.lower() in ["1", "true", "yes", "y", "on"]
        elif key == "jsonbin_no_hatcher_action":
            if not val:
                cur = str(current or "stay_market").strip().lower()
                cfg[key] = "fallback_restock" if cur == "stay_market" else "stay_market"
            else:
                v = val.strip().lower().replace("-", "_").replace(" ", "_")
                if v in ["1", "true", "yes", "y", "on", "fallback", "restock", "fallback_restock"]:
                    cfg[key] = "fallback_restock"
                elif v in ["0", "false", "no", "n", "off", "stay", "market", "stay_market"]:
                    cfg[key] = "stay_market"
                else:
                    print("Use stay_market or fallback_restock.")
                    time.sleep(1)
                    continue
        elif isinstance(current, int):
            if not val:
                continue
            try:
                cfg[key] = int(val)
            except Exception:
                print("Need number.")
                time.sleep(1)
                continue
        else:
            if not val:
                continue
            cfg[key] = val

        save_config(cfg)


def market_only_settings(cfg):
    groups = {
        "1": (
            "MARKET PET ROUTING / RESTOCK",
            [
                ("restock_below", "restock_below_pet_count"),
                ("ready_market_at", "return_to_market_at"),
                ("idle_no_gain_seconds", "idle_no_gain_seconds"),
                ("idle_min_pet_to_market", "idle_min_pet_to_market"),
                ("market_link", "market_link"),
                ("restock_link", "fallback_restock_link"),
            ],
        ),
        "2": (
            "MARKET JSONBIN HATCHER PICKER",
            [
                ("jsonbin_min_hatcher_pets", "min_hatcher_pets_to_use"),
                ("jsonbin_no_hatcher_action", "if_no_valid_hatcher"),
            ],
        ),
    }

    while True:
        clear()
        banner("MARKET-ONLY CONFIG", cfg)
        print(col("Only Market mode uses these settings.", DIM))
        print(col("Global/shared stuff stays in main menu option 11.", DIM))
        print("")
        print("1. Pet routing / restock")
        print("2. JSONBin hatcher picker")
        print("0. Save/back")
        print(col("Tip: on true/false settings, choose it and press ENTER to toggle.", DIM))

        drain_stdin()
        ch = input("\nChoose: ").strip()

        if ch == "0":
            save_config(cfg)
            set_color_enabled(cfg)
            return

        group = groups.get(ch)
        if group:
            title, items = group
            edit_config_group(cfg, title, items)
            cfg = load_config()



def config_template_menu(cfg):
    while True:
        clear()
        banner("GLOBAL CONFIG TEMPLATE", cfg)
        print(col("This is local only, not hardcoded in the script.", DIM))
        print(col("New config.json installs auto-use this template if it exists.", DIM))
        print("")
        print(f"Template path: {CONFIG_TEMPLATE_FILE}")
        print(f"Exists       : {CONFIG_TEMPLATE_FILE.exists()}")
        print("")
        if CONFIG_TEMPLATE_FILE.exists():
            tpl = load_global_template()
            print("Current template:")
            for key in CONFIG_TEMPLATE_KEYS:
                if key in tpl:
                    print(f"  {key}: {show_config_value(key, tpl.get(key))}")
            print("")
        print("1. Apply template to current config now")
        print("2. Save current JSONBin/backend settings as template")
        print("3. Show Termux edit command")
        print("0. Back")

        drain_stdin()
        ch = input("\nChoose: ").strip()

        if ch == "0":
            save_config(cfg)
            return

        if ch == "1":
            if apply_global_template_to_config(cfg, force=True):
                save_config(cfg)
                print(col("Applied template to current config.", GREEN))
            else:
                print(col("No template found, or no changes.", YELLOW))
            pause()
            cfg = load_config()

        elif ch == "2":
            save_current_backend_as_template(cfg)
            print(col("Saved current backend settings as template.", GREEN))
            print(str(CONFIG_TEMPLATE_FILE))
            pause()

        elif ch == "3":
            print("")
            print("Edit with nano:")
            print(f'nano "{CONFIG_TEMPLATE_FILE}"')
            print("")
            print("Or create quickly:")
            print(f'cat > "{CONFIG_TEMPLATE_FILE}" <<\'JSON\'')
            print(json.dumps({
                "jsonbin_hatchers_enabled": True,
                "jsonbin_bin_id": "PASTE_PRIVATE_BIN_ID_HERE",
                "jsonbin_api_key": "PASTE_MASTER_KEY_HERE",
                "jsonbin_key_header": "X-Master-Key",
                "jsonbin_stale_seconds": 7200,
                "jsonbin_cache_seconds": 600,
                "jsonbin_timeout_seconds": 8,
                "jsonbin_no_hatcher_action": "stay_market",
            }, indent=2))
            print("JSON")
            pause()




def _admin_ms_text(ms):
    try:
        ms = int(ms or 0)
    except Exception:
        ms = 0
    if ms <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def _admin_age_from_item(item):
    try:
        if item.get("age_seconds") is not None:
            return max(0, int(item.get("age_seconds") or 0))
        ms = int(item.get("updated_at_ms", 0) or 0)
        return max(0, now() - ms // 1000) if ms > 0 else None
    except Exception:
        return None


def _d1_admin_require_cloudflare(cfg):
    if backend_provider(cfg) != "cloudflare":
        print(col("D1 Admin requires backend_provider=cloudflare.", RED))
        pause()
        return False
    if is_placeholder_value(cloudflare_base_url(cfg)) or is_placeholder_value(cfg.get("cloudflare_secret")):
        print(col("Worker URL or NOMO secret is missing.", RED))
        pause()
        return False
    return True


def d1_admin_show_stats(cfg):
    clear()
    banner("D1 ADMIN STATISTICS", cfg)
    stale = int(cfg.get("jsonbin_stale_seconds", 7200) or 7200)
    data, err = cloudflare_admin_stats(cfg, stale_after_seconds=stale)
    if err:
        print(col(f"Stats failed: {err}", RED))
        if "404" in str(err) or "not_found" in str(err):
            print(col("Deploy NOMO D1 Worker v4 first.", YELLOW))
        pause()
        return
    h = data.get("hatchers", {}) if isinstance(data.get("hatchers"), dict) else {}
    a = data.get("accounts", {}) if isinstance(data.get("accounts"), dict) else {}
    print(f"Worker version : {data.get('version', '-')}")
    print(f"Stale threshold: {format_age(data.get('stale_after_seconds', stale))}")
    print(f"Estimated rows : {data.get('estimated_rows', 0)}")
    print("")
    print(col("Hatchers", BOLD))
    print(f"  Total: {h.get('total', 0)} | Fresh: {h.get('fresh', 0)} | Stale: {h.get('stale', 0)}")
    print(f"  Oldest: {_admin_ms_text(h.get('oldest_updated_at_ms'))}")
    print(f"  Newest: {_admin_ms_text(h.get('newest_updated_at_ms'))}")
    statuses = h.get("by_status", {}) if isinstance(h.get("by_status"), dict) else {}
    if statuses:
        print("  Status: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    print("")
    print(col("Accounts", BOLD))
    print(f"  Total: {a.get('total', 0)}")
    print(f"  Oldest: {_admin_ms_text(a.get('oldest_updated_at_ms'))}")
    print(f"  Newest: {_admin_ms_text(a.get('newest_updated_at_ms'))}")
    roles = a.get("by_role", {}) if isinstance(a.get("by_role"), dict) else {}
    if roles:
        print("  Roles : " + ", ".join(f"{k}={v}" for k, v in sorted(roles.items())))
    pause()


def _d1_print_hatchers(rows, cfg):
    table = []
    for i, item in enumerate(rows, 1):
        age = _admin_age_from_item(item)
        stale = bool(item.get("stale"))
        table.append([
            (str(i), CYAN),
            (str(item.get("username") or item.get("username_key") or ""), WHITE),
            (short_pkg(item.get("package_name", "")), DIM),
            (str(item.get("status") or ""), RED if stale else GREEN),
            (str(item.get("pet_count") if item.get("pet_count") is not None else "-"), WHITE, True),
            (format_age(age) if age is not None else "-", RED if stale else DIM),
        ])
    if table:
        draw_table(["No", "Username", "Package", "Status", "Pet", "Age"], table,
                   [3, 18, 10, 11, 5, 7], cfg)
    else:
        print(col("No Hatcher records.", YELLOW))


def _d1_print_accounts(rows, cfg):
    table = []
    for i, item in enumerate(rows, 1):
        age = _admin_age_from_item(item)
        table.append([
            (str(i), CYAN),
            (str(item.get("username") or item.get("username_key") or ""), WHITE),
            (str(item.get("user_id") or ""), DIM),
            (str(item.get("role") or ""), GREEN),
            (short_pkg(item.get("package_name", "")), DIM),
            (format_age(age) if age is not None else "-", DIM),
        ])
    if table:
        draw_table(["No", "Username", "User ID", "Role", "Package", "Age"], table,
                   [3, 18, 13, 8, 10, 7], cfg)
    else:
        print(col("No account records.", YELLOW))


def d1_admin_list_hatchers(cfg, pause_after=True):
    stale = int(cfg.get("jsonbin_stale_seconds", 7200) or 7200)
    rows, err = cloudflare_admin_hatchers(cfg, stale_after_seconds=stale)
    if err:
        print(col(f"Hatcher list failed: {err}", RED))
        if pause_after:
            pause()
        return []
    _d1_print_hatchers(rows, cfg)
    if pause_after:
        pause()
    return rows


def d1_admin_list_accounts(cfg, pause_after=True):
    rows, err = cloudflare_admin_accounts(cfg, role="market")
    if err:
        print(col(f"Account list failed: {err}", RED))
        if pause_after:
            pause()
        return []
    _d1_print_accounts(rows, cfg)
    if pause_after:
        pause()
    return rows


def d1_admin_delete_selected(cfg, kind):
    clear()
    title = "DELETE D1 HATCHERS" if kind == "hatcher" else "DELETE D1 MARKET ACCOUNTS"
    banner(title, cfg)
    rows = d1_admin_list_hatchers(cfg, pause_after=False) if kind == "hatcher" else d1_admin_list_accounts(cfg, pause_after=False)
    if not rows:
        pause()
        return
    print("")
    print(col("Choose numbers/ranges (1,3-5), A=all, 0=back.", DIM))
    raw = clean_terminal_input(input("Delete which records: "))
    if raw.lower() in {"", "0", "q", "b", "back"}:
        return
    idxs = _parse_index_selection(raw, len(rows), allow_all=True)
    if not idxs:
        print(col("Invalid selection.", RED))
        pause()
        return
    selected = [rows[i] for i in idxs]
    print("")
    for item in selected:
        print(" - " + str(item.get("username") or item.get("username_key") or ""))
    if clean_terminal_input(input(f"Type DELETE to remove {len(selected)} record(s): ")) != "DELETE":
        print(col("Cancelled.", YELLOW))
        pause()
        return
    deleted = 0
    failed = []
    for item in selected:
        username = str(item.get("username") or item.get("username_key") or "")
        ok, msg, data = cloudflare_admin_delete(cfg, kind, username)
        if ok and data.get("deleted"):
            deleted += 1
        elif not ok:
            failed.append(f"{username}: {msg}")
    print(col(f"Deleted {deleted}/{len(selected)} record(s).", GREEN if deleted == len(selected) else YELLOW))
    for text in failed[:8]:
        print(col(text, RED))
    pause()


def d1_admin_cleanup_menu(cfg):
    clear()
    banner("D1 STALE CLEANUP", cfg)
    print("1. Hatcher records only (recommended)")
    print("2. Market account registry")
    print("3. Both")
    print("0. Back")
    target_choice = read_menu_choice("\nTarget: ", {"0", "1", "2", "3"})
    if target_choice in {None, "0"}:
        return
    target = {"1": "hatchers", "2": "accounts", "3": "all"}[target_choice]
    if target in {"accounts", "all"}:
        print(col("WARNING: Market account registrations do not heartbeat continuously.", RED))
        print(col("Delete them only when those accounts are truly retired.", YELLOW))
    print("")
    print("1. Older than 7 days")
    print("2. Older than 14 days")
    print("3. Older than 30 days")
    print("4. Custom days")
    age_choice = read_menu_choice("\nAge: ", {"1", "2", "3", "4"})
    if age_choice is None:
        return
    days = {"1": 7, "2": 14, "3": 30}.get(age_choice)
    if days is None:
        try:
            days = int(clean_terminal_input(input("Days (minimum 1): ")))
        except Exception:
            days = 0
    if days < 1:
        print(col("Invalid number of days.", RED))
        pause()
        return
    preview, err = cloudflare_admin_cleanup(cfg, target, days * 86400, dry_run=True)
    if err:
        print(col(f"Preview failed: {err}", RED))
        if "404" in str(err) or "not_found" in str(err):
            print(col("Deploy NOMO D1 Worker v4 first.", YELLOW))
        pause()
        return
    candidates = preview.get("candidates", {}) if isinstance(preview.get("candidates"), dict) else {}
    hatchers = candidates.get("hatchers", []) if isinstance(candidates.get("hatchers"), list) else []
    accounts = candidates.get("accounts", []) if isinstance(candidates.get("accounts"), list) else []
    clear()
    banner("D1 CLEANUP PREVIEW", cfg)
    print(f"Target: {target} | Older than: {days} day(s)")
    print(f"Candidates: {preview.get('candidate_count', len(hatchers)+len(accounts))}")
    if hatchers:
        print("\nHatchers:")
        _d1_print_hatchers(hatchers[:100], cfg)
    if accounts:
        print("\nAccounts:")
        _d1_print_accounts(accounts[:100], cfg)
    if not hatchers and not accounts:
        print(col("Nothing qualifies for cleanup.", GREEN))
        pause()
        return
    print("")
    if clean_terminal_input(input("Type DELETE to execute this cleanup: ")) != "DELETE":
        print(col("Cancelled; preview only.", YELLOW))
        pause()
        return
    result, err = cloudflare_admin_cleanup(cfg, target, days * 86400, dry_run=False, confirm="DELETE")
    if err:
        print(col(f"Cleanup failed: {err}", RED))
    else:
        print(col(
            f"Cleanup complete: hatchers={result.get('deleted_hatchers', 0)} "
            f"accounts={result.get('deleted_accounts', 0)}",
            GREEN,
        ))
    pause()


def d1_admin_menu(cfg):
    if not _d1_admin_require_cloudflare(cfg):
        return
    while True:
        cfg = load_config()
        clear()
        banner("D1 BACKEND ADMIN", cfg)
        print(col("Destructive actions always preview/confirm first.", DIM))
        print("")
        print("1. Backend health")
        print("2. D1 statistics")
        print("3. List Hatcher records")
        print("4. List Market accounts")
        print("5. Delete selected Hatcher records")
        print("6. Delete selected Market accounts")
        print("7. Clean stale records")
        print("8. Force Hatcher upload")
        print("0. Back")
        ch = read_menu_choice("\nChoose: ", {"0", "1", "2", "3", "4", "5", "6", "7", "8", "q", "b", "back"})
        if ch in {"0", "q", "b", "back"}:
            return
        if ch == "1":
            data, err = cloudflare_request(cfg, "GET", "/health")
            if err:
                print(col(f"Health error: {err}", RED))
            else:
                good = bool(data.get("ok") and data.get("d1_bound") and data.get("d1_schema_ready"))
                print(col("HEALTHY" if good else "NOT READY", GREEN if good else RED))
                print(f"Worker : {data.get('version', '-')}")
                print(f"D1     : bound={data.get('d1_bound')} schema={data.get('d1_schema_ready')}")
                print(f"Secret : {data.get('secret_configured')}")
            pause()
        elif ch == "2":
            d1_admin_show_stats(cfg)
        elif ch == "3":
            clear(); banner("D1 HATCHER RECORDS", cfg); d1_admin_list_hatchers(cfg)
        elif ch == "4":
            clear(); banner("D1 MARKET ACCOUNTS", cfg); d1_admin_list_accounts(cfg)
        elif ch == "5":
            d1_admin_delete_selected(cfg, "hatcher")
        elif ch == "6":
            d1_admin_delete_selected(cfg, "account")
        elif ch == "7":
            d1_admin_cleanup_menu(cfg)
        elif ch == "8":
            hatcher_test_upload_screen(cfg)


def global_backend_settings(cfg):
    """Shared backend settings for Market + Hatcher."""
    items = [
        ("backend_provider", "provider"),
        ("jsonbin_hatchers_enabled", "enabled"),
        ("cloudflare_worker_url", "cf_worker_url"),
        ("cloudflare_secret", "cf_secret"),
        ("cloudflare_timeout_seconds", "cf_timeout"),
        ("jsonbin_bin_id", "jsonbin_bin_id"),
        ("jsonbin_api_key", "jsonbin_api_key"),
        ("jsonbin_key_header", "jsonbin_key_header"),
        ("jsonbin_stale_seconds", "stale_seconds"),
        ("jsonbin_cache_seconds", "cache_seconds"),
    ]

    while True:
        clear()
        banner("GLOBAL BACKEND", cfg)
        print(col("Shared by Market + Hatcher. Provider can be jsonbin or cloudflare.", DIM))
        print(col("Cloudflare uses worker URL + secret. JSONBin stays as fallback.", DIM))
        print("")
        for i, (key, label) in enumerate(items, 1):
            print(f"{i}. {pad(label, 28, CYAN)} = {show_config_value(key, cfg.get(key))}")
        print("")
        print("7. Apply template to current config now")
        print("8. Save current backend settings as template")
        print("9. Show Termux template edit command")
        print("H. Health check backend")
        print("A. D1 admin tools")
        print("0. Back")
        print(col("Tip: choose true/false and press ENTER to toggle. Provider toggles jsonbin/cloudflare on blank ENTER.", DIM))

        drain_stdin()
        ch = input("\nChoose: ").strip().lower()

        if ch == "0":
            save_config(cfg)
            set_color_enabled(cfg)
            return

        if ch == "h":
            ok, msg = backend_health_check(cfg)
            print((col("OK", GREEN) if ok else col("ERR", RED)) + f": {msg}")
            pause()
            continue

        if ch == "a":
            d1_admin_menu(cfg)
            cfg = load_config()
            continue

        if ch == "7":
            if apply_global_template_to_config(cfg, force=True):
                save_config(cfg)
                print(col("Applied template to current config.", GREEN))
            else:
                print(col("No template found, or no changes.", YELLOW))
            pause()
            cfg = load_config()
            continue

        if ch == "8":
            save_current_backend_as_template(cfg)
            print(col("Saved current backend settings as template.", GREEN))
            print(str(CONFIG_TEMPLATE_FILE))
            pause()
            continue

        if ch == "9":
            print("")
            print("Edit with nano:")
            print(f'nano "{CONFIG_TEMPLATE_FILE}"')
            print("")
            print("Cloudflare template example:")
            print(f'cat > "{CONFIG_TEMPLATE_FILE}" <<\'JSON\'')
            print(json.dumps({
                "backend_provider": "cloudflare",
                "jsonbin_hatchers_enabled": True,
                "cloudflare_worker_url": "https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev",
                "cloudflare_secret": "PASTE_LOCAL_SECRET_HERE",
                "cloudflare_timeout_seconds": 8,
                "jsonbin_stale_seconds": 7200,
                "jsonbin_cache_seconds": 60,
                "jsonbin_no_hatcher_action": "stay_market",
            }, indent=2))
            print("JSON")
            print("")
            print("JSONBin template example:")
            print(json.dumps({
                "backend_provider": "jsonbin",
                "jsonbin_hatchers_enabled": True,
                "jsonbin_bin_id": "PASTE_PRIVATE_BIN_ID_HERE",
                "jsonbin_api_key": "PASTE_MASTER_KEY_HERE",
                "jsonbin_key_header": "X-Master-Key",
                "jsonbin_stale_seconds": 7200,
                "jsonbin_cache_seconds": 600,
                "jsonbin_timeout_seconds": 8,
                "jsonbin_no_hatcher_action": "stay_market",
            }, indent=2))
            pause()
            continue

        try:
            idx = int(ch) - 1
        except Exception:
            continue

        if idx < 0 or idx >= len(items):
            continue

        key, label = items[idx]
        current = cfg.get(key)
        val = input(f"New value for {key} [{current}] (blank ENTER toggles bool/provider): ").strip()

        if key == "backend_provider":
            if not val:
                cur = str(current or "jsonbin").strip().lower()
                cfg[key] = "cloudflare" if cur != "cloudflare" else "jsonbin"
            else:
                v = val.strip().lower()
                if v in ["cf", "cloudflare", "worker"]:
                    cfg[key] = "cloudflare"
                elif v in ["jb", "jsonbin", "json"]:
                    cfg[key] = "jsonbin"
                else:
                    print("Use jsonbin or cloudflare.")
                    time.sleep(1)
                    continue
        elif isinstance(current, bool):
            if not val:
                cfg[key] = not current
            else:
                cfg[key] = val.lower() in ["1", "true", "yes", "y", "on"]
        elif isinstance(current, int):
            if not val:
                continue
            try:
                cfg[key] = int(val)
            except Exception:
                print("Need number.")
                time.sleep(1)
                continue
        else:
            if not val:
                continue
            cfg[key] = val

        save_config(cfg)

def config_settings(cfg):
    groups = {
        "1": (
            "CORE TIMING",
            [
                ("check_interval", "check_interval"),
                ("delay_between_open", "delay_between_open"),
                ("min_seconds_between_reopen", "min_reopen_cooldown"),
                ("ui_width", "ui_width"),
                ("use_color", "use_color"),
            ],
        ),
        "2": (
            "REJOIN / STALE RECOVERY",
            [
                ("rejoin_if_crash", "rejoin_if_crash"),
                ("ignore_alive_stale_state", "ignore_alive_stale"),
                ("state_stale_seconds", "normal_stale_after"),
                ("alive_old_state_rejoin_after_seconds", "alive_old_state_rejoin_after"),
                ("stale_reopen_after_seconds", "compat_reopen_after_stale"),
                ("disconnect_popup_rejoin_enabled", "stale_popup_rejoin"),
                ("disconnect_popup_stale_seconds", "stale_popup_after"),
                ("force_stop_alive_on_disconnect_popup", "hard_force_stale_popup"),
                ("disconnect_ui_rejoin_enabled", "kick_popup_rejoin"),
                ("disconnect_ui_retry_seconds", "kick_popup_retry"),
                ("disconnect_ui_hard_after_seconds", "kick_popup_hard_after"),
                ("minimized_window_detection_enabled", "minimized_detect"),
                ("minimized_window_reopen_enabled", "minimized_soft_reopen"),
                ("minimized_window_reopen_mode", "minimized_reopen_mode"),
                ("post_open_grace_seconds", "post_open_grace"),
            ],
        ),
        "3": (
            "STARTUP / OPEN QUEUE",
            [
                ("open_all_on_start", "open_all_on_start"),
                ("open_only_closed_on_start", "only_closed_on_start"),
                ("smart_open_queue", "smart_open_queue"),
                ("wait_fresh_after_open", "wait_fresh_after_open"),
                ("open_wait_fresh_seconds", "wait_fresh_timeout"),
                ("open_wait_check_seconds", "wait_check_seconds"),
                ("homepage_stuck_hard_fallback_enabled", "homepage_hard_fallback"),
                ("homepage_stuck_fallback_seconds", "homepage_fallback_after"),
                ("homepage_stuck_max_hard_retries", "homepage_max_hard_retry"),
                ("queue_stuck_self_heal_enabled", "queue_self_heal"),
                ("queue_stuck_reset_seconds", "queue_reset_after"),
            ],
        ),
        "4": (
            "GLOBAL BACKEND",
            [
                ("backend_provider", "provider"),
                ("jsonbin_hatchers_enabled", "enabled"),
                ("cloudflare_worker_url", "cf_worker_url"),
                ("cloudflare_secret", "cf_secret"),
                ("jsonbin_bin_id", "jsonbin_bin_id"),
                ("jsonbin_api_key", "jsonbin_api_key"),
                ("jsonbin_key_header", "jsonbin_key_header"),
                ("jsonbin_stale_seconds", "stale_seconds"),
                ("jsonbin_cache_seconds", "cache_seconds"),
            ],
        ),
        "5": (
            "SOFT / SCHEDULED HOP",
            [
                ("soft_hop_enabled", "soft_hop_enabled"),
                ("prefer_experience_start_links", "use_experience_start_link"),
                ("soft_hop_fallback_hard", "fallback_hard_if_failed"),
                ("soft_hop_wait_fresh_seconds", "soft_hop_wait_fresh"),
                ("scheduled_hop_enabled", "scheduled_hop_enabled"),
                ("scheduled_hop_interval_seconds", "hop_interval_seconds"),
                ("scheduled_hop_jitter_seconds", "hop_jitter_seconds"),
                ("scheduled_hop_delay_if_pets_drop_seconds", "delay_if_pets_drop"),
                ("periodic_hard_refresh_enabled", "periodic_hard_refresh"),
                ("periodic_hard_refresh_seconds", "hard_refresh_seconds"),
                ("periodic_hard_refresh_include_manual", "hard_refresh_manual"),
            ],
        ),
        "6": (
            "CACHE CLEANUP",
            [
                ("clear_cache_enabled", "enabled"),
                ("clear_cache_on_hard_open", "on_hard_reopen"),
                ("clear_cache_on_soft_hop", "on_soft_hop"),
                ("clear_cache_min_interval_seconds", "min_interval_seconds"),
            ],
        ),
        "8": (
            "WORKSPACE / CONFIG SYNC",
            [
                ("workspace_sync_enabled", "enabled"),
                ("workspace_sync_market_only", "market_mode_only"),
                ("workspace_sync_on_start", "run_on_start"),
                ("workspace_sync_before_open", "run_before_each_rejoin"),
                ("workspace_sync_periodic_enabled", "run_every_few_minutes"),
                ("workspace_sync_interval_seconds", "cooldown_or_interval_seconds"),
                ("workspace_sync_timeout_seconds", "timeout_seconds"),
                ("workspace_sync_command", "command"),
            ],
        ),
        "9": (
            "COOKIE WEBHOOK",
            [
                ("cookie_webhook_url", "webhook_url"),
            ],
        ),
    }

    while True:
        clear()
        banner("GLOBAL CONFIG", cfg)
        print(col(f"Active mode: {active_mode_label(cfg)}", GREEN))
        print(col("This menu is shared by Market + Hatcher only.", DIM))
        print(col("Market restock pet amount is in: 2 -> 11", DIM))
        print(col("Hatcher report/ready settings are in: 3 -> 11", DIM))
        print("")
        print("1. Core timing")
        print("2. Rejoin / stale recovery")
        print("3. Startup / smart open queue")
        print("4. Global JSONBin / backend")
        print("5. Soft / scheduled hop")
        print("6. Cache cleanup")
        print("7. Config template tools")
        print("8. Workspace / config sync")
        print("0. Save/back")
        print(col("Tip: on true/false settings, choose it and press ENTER to toggle.", DIM))

        drain_stdin()
        ch = input("\nChoose: ").strip()

        if ch == "0":
            save_config(cfg)
            set_color_enabled(cfg)
            return

        if ch == "4":
            global_backend_settings(cfg)
            cfg = load_config()
            continue

        if ch == "7":
            config_template_menu(cfg)
            cfg = load_config()
            continue

        if ch == "9":
            edit_config_group(cfg, "COOKIE WEBHOOK", [("cookie_webhook_url", "webhook_url")])
            cfg = load_config()
            continue

        group = groups.get(ch)
        if group:
            title, items = group
            edit_config_group(cfg, title, items)
            cfg = load_config()



# ============================================================
# JSONBIN HATCHER ROUTING
# ============================================================

def mask_secret(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


def jsonbin_headers(cfg, write=False):
    key = str(cfg.get("jsonbin_api_key", "")).strip()
    header_name = str(cfg.get("jsonbin_key_header", "X-Master-Key") or "X-Master-Key").strip()

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "nomo-rejoin/jsonbin"
    }

    if key:
        headers[header_name] = key

    if not write:
        headers["X-Bin-Meta"] = "false"

    return headers


def jsonbin_record_from_response(data):
    if isinstance(data, dict) and "record" in data and isinstance(data.get("record"), dict):
        return data["record"]
    if isinstance(data, dict):
        return data
    return {}


def jsonbin_read_bin(cfg):
    bin_id = str(cfg.get("jsonbin_bin_id", "")).strip()

    if is_placeholder_value(bin_id):
        return None, "missing bin id"

    timeout = int(cfg.get("jsonbin_timeout_seconds", 8))
    headers = jsonbin_headers(cfg, write=False)

    urls = [
        f"https://api.jsonbin.io/v3/b/{bin_id}/latest",
        f"https://api.jsonbin.io/v3/b/{bin_id}",
    ]

    last_err = ""

    for url in urls:
        try:
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", "replace")
            data = json.loads(text)
            return jsonbin_record_from_response(data), None

        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = str(e)
            last_err = f"http {e.code}: {cut(body, 80)}"

        except Exception as e:
            last_err = str(e)

    return None, last_err or "jsonbin read failed"


def get_jsonbin_cache(rt):
    if "_jsonbin_hatchers_cache" not in rt:
        rt["_jsonbin_hatchers_cache"] = {
            "ts": 0,
            "data": None,
            "err": "not loaded"
        }
    return rt["_jsonbin_hatchers_cache"]


def read_hatchers_cached(cfg, rt, force=False):
    cache = get_jsonbin_cache(rt)
    cache_age = now() - int(cache.get("ts", 0) or 0)
    cache_seconds = int(cfg.get("jsonbin_cache_seconds", 20))

    if not force and cache.get("data") is not None and cache_age < cache_seconds:
        return cache.get("data"), cache.get("err")

    data, err = backend_read_hatchers(cfg)
    cache["ts"] = now()
    cache["data"] = data
    cache["err"] = err
    save_runtime(rt)
    return data, err


def pick_jsonbin_hatcher(cfg, rt):
    if not cfg.get("jsonbin_hatchers_enabled", False):
        return None, None, "jsonbin off"

    data, err = read_hatchers_cached(cfg, rt)

    if err or not data:
        return None, None, f"jsonbin {err}"

    hatchers = data.get("hatchers", data if isinstance(data, dict) else {})

    if not isinstance(hatchers, dict):
        return None, None, "jsonbin bad format"

    stale_seconds = int(cfg.get("jsonbin_stale_seconds", 180))
    min_pets = int(cfg.get("jsonbin_min_hatcher_pets", 200))
    choices = []

    for name, h in hatchers.items():
        if not isinstance(h, dict):
            continue

        if not h.get("enabled", True):
            continue

        status = str(h.get("status", "ready")).lower()
        if status not in ["ready", "idle", "available", "ok", "online", "ingame"]:
            continue

        link = str(h.get("server_link", "") or h.get("restock_link", "")).strip()
        if not link or link.startswith("PUT_"):
            continue

        try:
            pets = int(h.get("pet_count", 0))
        except Exception:
            pets = 0

        ready_by_egg = bool(h.get("ready_by_egg", False))
        if pets < min_pets and not ready_by_egg:
            continue

        try:
            updated_at = int(h.get("updated_at", 0))
        except Exception:
            updated_at = 0

        age = now() - updated_at if updated_at else 999999
        if age > stale_seconds:
            continue

        choices.append({
            "name": str(name),
            "link": link,
            "pets": pets,
            "age": age,
            "updated_at": updated_at,
        })

    if not choices:
        return None, None, "no ready hatcher"

    choices.sort(key=lambda x: (x["pets"], x["updated_at"]), reverse=True)
    best = choices[0]
    return best["link"], best, None


def resolve_restock_link(tab, rt_tab, cfg, rt=None):
    fallback = tab.get("restock_link") or cfg.get("restock_link")

    if cfg.get("jsonbin_hatchers_enabled", False) and rt is not None:
        link, hatcher, err = pick_jsonbin_hatcher(cfg, rt)

        if link:
            rt_tab["last_hatcher"] = hatcher.get("name") if hatcher else "jsonbin"
            rt_tab["last_hatcher_pets"] = hatcher.get("pets") if hatcher else ""
            rt_tab["last_hatcher_age"] = hatcher.get("age") if hatcher else ""
            rt_tab["jsonbin_last_error"] = ""
            return link, f"jsonbin:{rt_tab['last_hatcher']}"

        rt_tab["jsonbin_last_error"] = err

        action = str(cfg.get("jsonbin_no_hatcher_action", "stay_market") or "stay_market").strip().lower()
        if action not in ["fallback_restock", "fallback", "restock"]:
            return "", f"jsonbin:{err};stay_market"

    return fallback, "config"

# ============================================================
# REJOIN LOOP
# ============================================================

def get_runtime_tab(rt, pkg):
    if pkg not in rt:
        rt[pkg] = {
            "target": "market",
            "last_open": 0,
            "last_pet_count": -1,
            "last_gain_ts": now(),
            "note": "init"
        }

    return rt[pkg]


def can_open(rt_tab, cfg):
    return now() - int(rt_tab.get("last_open", 0)) >= int(cfg.get("min_seconds_between_reopen", 60))


def target_link(tab, cfg, target, rt_tab=None, rt=None):
    if target == "hatcher":
        return tab.get("server_link") or tab.get("restock_link") or cfg.get("market_link")

    if target == "restock":
        if rt_tab is not None:
            link, source = resolve_restock_link(tab, rt_tab, cfg, rt)
            rt_tab["restock_source"] = source
            return link
        return tab.get("restock_link") or cfg.get("restock_link")

    return cfg.get("market_link")


def open_target(tab, rt_tab, cfg, target, reason, force=False, rt=None, mode="hard"):
    if not force and not can_open(rt_tab, cfg):
        return False, "cooldown"

    link = target_link(tab, cfg, target, rt_tab, rt)

    if not link:
        rt_tab["note"] = "no restock link" if target == "restock" else "no link"
        return False, rt_tab["note"]

    mode_s = str(mode or "hard").lower()
    soft = (mode_s == "soft")
    hard_force = mode_s in ["hard_force", "force", "force_stop", "force-stop"]
    pkg_alive = package_alive(tab["package"], cfg, fresh=True)

    # MASTER SWITCH: when soft rejoin is disabled, EVERY open is a hard kill+open.
    if cfg.get("disable_soft_rejoin", True):
        soft = False
        hard_force = True
        require_stop = True
        ok, note = open_roblox(tab["package"], link, cfg, soft=False, rt_tab=rt_tab, reason=reason, require_stop=True)
        rt_tab["target"] = target
        rt_tab["last_open"] = now()
        rt_tab["last_open_mode"] = "hard"
        rt_tab["note"] = reason
        if target == "restock":
            rt_tab["last_gain_ts"] = now()
        return ok, note

    # Soft hop only makes sense when the package is alive.
    if soft and not pkg_alive:
        soft = False

    # Safety mode: do not force-stop a clone that is still alive.
    if (not soft) and pkg_alive and cfg.get("no_force_stop_alive", True) and not hard_force:
        alive_mode = str(cfg.get("alive_open_mode", "soft") or "soft").lower()
        if alive_mode == "skip":
            rt_tab["note"] = "alive skip hard"
            return False, "alive skip hard"
        if alive_mode == "soft" and cfg.get("soft_hop_enabled", True):
            soft = True
        elif alive_mode != "hard":
            rt_tab["note"] = "alive skip hard"
            return False, "alive skip hard"
    require_stop = not soft

    ok, note = open_roblox(tab["package"], link, cfg, soft=soft, rt_tab=rt_tab, reason=reason, require_stop=require_stop)

    rt_tab["target"] = target
    rt_tab["last_open"] = now()
    rt_tab["last_open_mode"] = "soft" if soft else "hard"
    rt_tab["note"] = reason

    if target == "restock":
        rt_tab["last_gain_ts"] = now()

    return ok, note


def wait_seconds(seconds, rt=None):
    for _ in range(int(seconds)):
        if stop_requested():
            if rt is not None:
                save_runtime(rt)
            return False

        try:
            time.sleep(1)
        except KeyboardInterrupt:
            request_stop()
            if rt is not None:
                save_runtime(rt)
            reset_terminal()
            print("\n[!] Stopped. Returning to Termux...")
            return False

    return True


def queue_has(open_queue, pkg):
    return any(item.get("tab", {}).get("package") == pkg for item in open_queue)


def queue_position(open_queue, pkg):
    """1-based FIFO position for a package, or 0 when not queued."""
    for idx, item in enumerate(open_queue or [], 1):
        if item.get("tab", {}).get("package") == pkg:
            return idx
    return 0


def queue_open(open_queue, tab, target, reason, force=False, skip_if_alive=False, mode="hard", front=False, bypass_manual=False, metadata=None):
    pkg = tab.get("package")
    if queue_has(open_queue, pkg):
        return False, "already queued"
    item = {
        "tab": tab,
        "target": target,
        "reason": reason,
        "force": bool(force),
        "skip_if_alive": bool(skip_if_alive),
        "mode": str(mode or "hard"),
        "queued_at": now(),
        "bypass_manual": bool(bypass_manual),
        "retries": 0,
    }
    if isinstance(metadata, dict):
        item.update(metadata)

    if front:
        open_queue.insert(0, item)
    else:
        open_queue.append(item)

    log_activity(f"queued {mode} -> {target}: {reason}", pkg)
    return True, "queued"


def queue_stuck_self_heal(open_queue, cfg, rt):
    """Clear stale temporary queue/cooldown runtime without deleting config."""
    if not cfg.get("queue_stuck_self_heal_enabled", True):
        return False

    try:
        reset_after = int(cfg.get("queue_stuck_reset_seconds", 180) or 180)
    except Exception:
        reset_after = 180

    if reset_after <= 0 or not isinstance(rt, dict):
        return False

    active_pkgs = set()
    for item in open_queue or []:
        try:
            active_pkgs.add(item.get("tab", {}).get("package"))
        except Exception:
            pass

    q_words = [
        "queued",
        "cooldown queued",
        "already queued",
        "loading grace",
        "waiting fresh",
        "fresh timeout",
        "hard retry",
    ]

    changed = False
    t = now()

    for pkg, rt_tab in list(rt.items()):
        if not isinstance(rt_tab, dict):
            continue
        if pkg in active_pkgs:
            continue

        note = str(rt_tab.get("note", "") or "")
        note_l = note.lower()
        if not any(w in note_l for w in q_words):
            continue

        last_open = int(rt_tab.get("last_open", 0) or 0)
        last_reset = int(rt_tab.get("last_queue_self_heal", 0) or 0)

        age = (t - last_open) if last_open > 0 else reset_after + 1

        if last_reset > 0 and t - last_reset < reset_after:
            continue

        if age >= reset_after:
            rt_tab["last_open"] = 0
            rt_tab["last_open_mode"] = ""
            rt_tab["note"] = "queue self-heal"
            rt_tab["last_queue_self_heal"] = t
            changed = True

    if changed:
        save_runtime(rt)

    return changed




def _runtime_temp_note(note):
    note_l = str(note or "").lower()
    words = [
        "queued",
        "cooldown queued",
        "already queued",
        "loading grace",
        "waiting fresh",
        "fresh timeout",
        "hard retry",
        "opening",
    ]
    return any(w in note_l for w in words)


def clear_runtime_temp_state(rt, reason="runtime watchdog"):
    """Clear only temporary per-package runtime state, not config.json."""
    if not isinstance(rt, dict):
        return False

    changed = False
    t = now()
    for pkg, rt_tab in list(rt.items()):
        if not isinstance(rt_tab, dict):
            continue
        if str(pkg).startswith("_"):
            continue

        if _runtime_temp_note(rt_tab.get("note", "")) or int(rt_tab.get("last_open", 0) or 0) > 0:
            rt_tab["last_open"] = 0
            rt_tab["last_open_mode"] = ""
            rt_tab["last_open_reason"] = ""
            rt_tab["last_open_target"] = ""
            rt_tab["last_open_ok"] = False
            rt_tab["last_open_msg"] = ""
            rt_tab["homepage_hard_retries"] = 0
            rt_tab["note"] = str(reason or "runtime watchdog")
            rt_tab["last_runtime_watchdog_reset"] = t
            changed = True

    rt.setdefault("_runtime_watch", {})["last_reset"] = t
    rt["_runtime_watch"]["last_reset_reason"] = str(reason or "runtime watchdog")
    return changed


def runtime_stuck_watchdog(open_queue, cfg, rt, enabled_tabs=None):
    """Reset stuck runtime/open_queue after a long Queued state."""
    if not cfg.get("runtime_stuck_reset_enabled", True):
        return False
    if not isinstance(rt, dict):
        return False

    try:
        reset_after = int(cfg.get("runtime_stuck_reset_seconds", 420) or 420)
    except Exception:
        reset_after = 420
    try:
        min_tabs = int(cfg.get("runtime_stuck_reset_min_tabs", 1) or 1)
    except Exception:
        min_tabs = 1

    if reset_after <= 0:
        return False

    watch = rt.setdefault("_runtime_watch", {})
    t = now()

    wanted_pkgs = set()
    try:
        for tab in enabled_tabs or []:
            pkg = str(tab.get("package", "") or "")
            if pkg:
                wanted_pkgs.add(pkg)
    except Exception:
        wanted_pkgs = set()

    queued_pkgs = []
    for pkg, rt_tab in list(rt.items()):
        if not isinstance(rt_tab, dict):
            continue
        if str(pkg).startswith("_"):
            continue
        if wanted_pkgs and pkg not in wanted_pkgs:
            continue
        if _runtime_temp_note(rt_tab.get("note", "")):
            queued_pkgs.append(pkg)

    if len(queued_pkgs) < max(1, min_tabs):
        watch.pop("stuck_since", None)
        watch.pop("stuck_pkgs", None)
        return False

    prev_pkgs = watch.get("stuck_pkgs", [])
    if set(prev_pkgs) != set(queued_pkgs):
        watch["stuck_since"] = t
        watch["stuck_pkgs"] = queued_pkgs
        save_runtime(rt)
        return False

    stuck_since = int(watch.get("stuck_since", t) or t)
    if t - stuck_since < reset_after:
        return False

    if isinstance(open_queue, list):
        open_queue.clear()

    changed = clear_runtime_temp_state(rt, reason="runtime auto-reset")
    watch["stuck_since"] = t
    watch["stuck_pkgs"] = []
    watch["last_auto_reset_pkgs"] = queued_pkgs
    save_runtime(rt)
    return changed

def stale_reopen_age(cfg):
    """Age where alive old-state becomes an actual queued soft rejoin."""
    stale_limit = int(cfg.get("state_stale_seconds", 180))

    extra = cfg.get("alive_old_state_rejoin_after_seconds", cfg.get("stale_reopen_after_seconds", 60))
    try:
        extra = int(extra)
    except Exception:
        extra = 60

    return stale_limit + max(0, extra)


def disconnect_popup_age(cfg):
    """Age where alive+stale state is treated as a Roblox disconnect popup."""
    try:
        return int(cfg.get("disconnect_popup_stale_seconds", 180) or 180)
    except Exception:
        return 180


def should_force_disconnect_rejoin(alive, age, cfg):
    if not alive:
        return False
    if not cfg.get("disconnect_popup_rejoin_enabled", True):
        return False
    try:
        age_i = int(age)
    except Exception:
        return False
    return age_i >= disconnect_popup_age(cfg)



def _counter_version_tuple(state):
    raw = str((state or {}).get("counter_version", "") or "")
    m = re.search(r"v?(\d+)\.(\d+)", raw, re.I)
    if not m:
        return (0, 0)
    try:
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return (0, 0)


def state_disconnect_ui(state):
    """True only for a CURRENT, high-confidence disconnect observation.

    Counter <=2.4 could latch GuiService/Rejoin text forever while continuing to
    write fresh heartbeats. Those legacy flags are ignored unless Python itself
    confirmed the native popup for this exact package.
    """
    if not state:
        return False
    if bool(state.get("_android_disconnect_confirmed")):
        return True

    disconnected = bool(state.get("disconnected"))
    text = " ".join(str(state.get(k, "")) for k in [
        "disconnect_title", "disconnect_text", "disconnect_code"
    ])
    low = text.lower()
    has_strong_text = any(x in low for x in [
        "disconnected from the experience", "server has shut down",
        "you have been kicked", "kicked by this experience",
        "connection failed", "failed to connect", "no response from server",
        "lost connection", "error code", "session expired",
    ])
    if not disconnected and not has_strong_text:
        return False

    # Counter v2.5+ actively clears a vanished prompt, so its current boolean is safe.
    if _counter_version_tuple(state) >= (2, 5):
        return True

    # Transitional support for any counter that writes a real observation epoch.
    try:
        seen = int(state.get("disconnect_observed_ts", 0) or 0)
    except Exception:
        seen = 0
    if seen > 0 and abs(now() - seen) <= 45:
        return True

    # Legacy v2.4 and older: do not trust sticky state without Android confirmation.
    return False


def state_disconnect_note(state):
    code = str((state or {}).get("disconnect_code", "") or "").strip()
    txt = str((state or {}).get("disconnect_text", "") or (state or {}).get("disconnect_title", "") or "").strip()
    if code:
        return f"kick/error {code}"
    if "session" in txt.lower():
        return "session expired"
    if "kicked" in txt.lower():
        return "kicked popup"
    return "disconnect popup"


def state_login_challenge_detail(state):
    """Read only high-confidence CAPTCHA/human-check signals from Lua state."""
    if not state:
        return ""
    if bool((state or {}).get("login_challenge")):
        detail = str((state or {}).get("login_challenge_text", "") or "").strip()
        return "lua captcha" + ((": " + cut(detail, 100)) if detail else "")
    keys = [
        "login_challenge_text", "login_challenge_reason",
        "ui_title", "ui_text", "popup_title", "popup_text",
        "screen_text", "text", "note"
    ]
    low = " ".join(str((state or {}).get(k, "") or "") for k in keys).lower()
    strong_terms = [
        "captcha", "not a bot", "you are not a bot", "verify you are human",
        "human verification", "real person", "start puzzle", "solve this puzzle",
        "solve this challenge", "complete the challenge", "arkose", "fun captcha"
    ]
    hits = [t for t in strong_terms if t in low]
    if hits:
        return "lua captcha " + ",".join(hits[:5])
    return ""


def hatcher_alive_old_state_hard_settings(hcfg, cfg):
    """Return (enabled, age_seconds, max_valid_seconds, cooldown_seconds) for V3.75 hatcher recovery."""
    enabled = bool_from_any(hcfg.get(
        "hatcher_alive_old_state_hard_force_enabled",
        cfg.get("hatcher_alive_old_state_hard_force_enabled", True)
    ))
    try:
        age_seconds = int(hcfg.get(
            "hatcher_alive_old_state_hard_force_seconds",
            cfg.get("hatcher_alive_old_state_hard_force_seconds", 180)
        ) or 180)
    except Exception:
        age_seconds = 180
    try:
        max_valid_seconds = int(hcfg.get(
            "hatcher_alive_old_state_max_valid_seconds",
            cfg.get("hatcher_alive_old_state_max_valid_seconds", 86400)
        ) or 86400)
    except Exception:
        max_valid_seconds = 86400
    try:
        cooldown_seconds = int(hcfg.get(
            "hatcher_alive_old_state_hard_force_cooldown_seconds",
            cfg.get("hatcher_alive_old_state_hard_force_cooldown_seconds", 15)
        ) or 15)
    except Exception:
        cooldown_seconds = 15
    if age_seconds < 120:
        age_seconds = 120
    if max_valid_seconds < age_seconds:
        max_valid_seconds = age_seconds
    if cooldown_seconds < 15:
        cooldown_seconds = 15
    return enabled, age_seconds, max_valid_seconds, cooldown_seconds


def queue_hatcher_alive_old_state_hard(open_queue, tab, rt_tab, hcfg, cfg, age_or_seconds, reason):
    enabled, age_seconds, max_valid_seconds, cooldown_seconds = hatcher_alive_old_state_hard_settings(hcfg, cfg)
    pkg = str((tab or {}).get("package", "") or "")
    if pkg and solver_job_running(pkg):
        return False, "solver running", False
    if not enabled:
        return False, "old-state hard disabled", False
    try:
        age_i = int(age_or_seconds)
    except Exception:
        age_i = 0
    if age_i < age_seconds:
        return False, "alive old state", False
    if age_i > max_valid_seconds:
        return False, f"invalid old state ignored {age_i}s", False

    t = now()
    last = int(rt_tab.get("hatcher_alive_old_state_hard_last", 0) or 0)
    if last > 0 and (t - last) < cooldown_seconds:
        left = max(1, cooldown_seconds - (t - last))
        return False, f"old-state hard cooldown {left}s", False

    rt_tab["hatcher_alive_old_state_hard_last"] = t
    rt_tab["hatcher_alive_old_state_hard_age"] = age_i
    rt_tab["hatcher_alive_old_state_hard_reason"] = str(reason or "alive old state")
    added, _ = queue_open(
        open_queue, tab, "hatcher",
        str(reason or "hatcher alive old state hard"),
        force=True, mode="hard_force", front=False
    )
    if added:
        return True, "old-state hard queued", True
    return False, "already queued", True


def queue_disconnect_ui_rejoin(open_queue, tab, target, rt_tab, cfg):
    if not cfg.get("disconnect_ui_rejoin_enabled", True):
        return False, "kick popup wait"

    t = now()
    if not int(rt_tab.get("disconnect_ui_since", 0) or 0):
        rt_tab["disconnect_ui_since"] = t

    retry = int(cfg.get("disconnect_ui_retry_seconds", 60) or 60)
    if target == "hatcher":
        retry = int(cfg.get("hatcher_disconnect_ui_retry_seconds", retry) or retry)
    last = int(rt_tab.get("last_disconnect_ui_open", 0) or 0)
    if last and t - last < retry:
        return False, f"kick wait {max(0, retry - (t - last))}s"

    mode = "hard_force"

    soft_first = int(cfg.get("disconnect_ui_soft_first_seconds", 0) or 0)
    if soft_first > 0:
        since = int(rt_tab.get("disconnect_ui_since", t) or t)
        if t - since < soft_first:
            mode = "soft"

    # V3.88: FIFO, not front insertion. A later D popup must not jump ahead of
    # an already-waiting B popup. Single-flight still recovers only one package.
    added, anote = queue_open(open_queue, tab, target, "kick/disconnect popup", force=True, mode=mode, front=False)
    if added:
        rt_tab["last_disconnect_ui_open"] = t
        return True, (f"kick {mode} queued" if mode != "soft" else "kick soft queued")
    if anote == "already queued":
        return False, "already queued"
    return False, f"kick not-queued ({anote})"

def in_post_open_grace(rt_tab, cfg):
    """Only ignore stale right after this rejoiner opened/hopped the package."""
    last_open = int(rt_tab.get("last_open", 0) or 0)
    grace = int(cfg.get("post_open_grace_seconds", cfg.get("open_wait_fresh_seconds", 300)))

    if last_open <= 0 or grace <= 0:
        return False

    return (now() - last_open) < grace


def ensure_scheduled_hop(tab, rt_tab, cfg, index=0):
    if not cfg.get("scheduled_hop_enabled", False):
        return

    interval = int(cfg.get("scheduled_hop_interval_seconds", 600))
    jitter = int(cfg.get("scheduled_hop_jitter_seconds", 30))

    if int(rt_tab.get("next_scheduled_hop", 0) or 0) <= 0:
        rt_tab["next_scheduled_hop"] = now() + interval + (index * max(0, jitter))


def scheduled_hop_due(rt_tab, cfg):
    if not cfg.get("scheduled_hop_enabled", False):
        return False

    return now() >= int(rt_tab.get("next_scheduled_hop", 0) or 0)


def schedule_next_soft_hop(rt_tab, cfg, delay=None):
    if delay is None:
        delay = int(cfg.get("scheduled_hop_interval_seconds", 600))

    rt_tab["next_scheduled_hop"] = now() + int(delay)


def periodic_hard_refresh_due(rt_tab, cfg):
    if not cfg.get("periodic_hard_refresh_enabled", True):
        return False, 0
    try:
        interval = int(cfg.get("periodic_hard_refresh_seconds", 3600) or 3600)
    except Exception:
        interval = 3600
    if interval < 1800:
        interval = 1800

    last = int(rt_tab.get("last_periodic_hard_refresh", 0) or 0)
    if last <= 0:
        rt_tab["last_periodic_hard_refresh"] = now()
        return False, interval

    left = interval - (now() - last)
    return left <= 0, max(0, left)


def mark_periodic_hard_refresh(rt_tab):
    rt_tab["last_periodic_hard_refresh"] = now()


def should_delay_hop_for_selling(rt_tab, cfg):
    delay = int(cfg.get("scheduled_hop_delay_if_pets_drop_seconds", 60))
    if delay <= 0:
        return False, 0

    last_drop = int(rt_tab.get("last_pet_drop_ts", 0) or 0)
    if last_drop <= 0:
        return False, 0

    left = delay - (now() - last_drop)
    return left > 0, max(1, left)


def state_is_old_after_open(state, rt_tab):
    last_open = int(rt_tab.get("last_open", 0) or 0)
    state_ts = int(state.get("ts", 0) or 0)

    return last_open > 0 and state_ts < last_open - 2


def state_fresh_after_open(tab, cfg, opened_at):
    state, err = read_state(tab)

    if not state:
        return False, state, err

    state_ts = int(state.get("ts", 0))
    age = int(state.get("age", 999999))

    if state_ts < int(opened_at) - 2:
        return False, state, "old state"

    if age > int(cfg.get("state_stale_seconds", 180)):
        return False, state, "stale state"

    return True, state, None



def state_recent_enough_for_alive(tab, cfg, seconds=None):
    """Return True when state.json is fresh enough to prove the clone is running."""
    if not cfg.get("start_skip_if_state_fresh_enabled", True):
        return False

    if seconds is None:
        try:
            seconds = int(cfg.get("start_skip_fresh_state_seconds", 45) or 45)
        except Exception:
            seconds = 45

    if int(seconds or 0) <= 0:
        return False

    state, err = read_state(tab)
    if not state:
        return False

    if state_disconnect_ui(state) or state_login_challenge_detail(state):
        return False

    try:
        age = int(state.get("age", 999999) or 999999)
    except Exception:
        age = 999999

    return age <= int(seconds)


def effective_package_alive(tab, cfg, raw_alive=None):
    alive = package_alive(tab["package"], cfg, fresh=True) if raw_alive is None else bool(raw_alive)
    stale_limit = int(cfg.get("state_stale_seconds", 180) or 180)
    if alive:
        if cfg.get("start_reopen_alive_without_fresh_state", True):
            return state_recent_enough_for_alive(tab, cfg, seconds=stale_limit)
        return True
    return state_recent_enough_for_alive(tab, cfg, seconds=stale_limit)


def package_has_fresh_healthy_state(pkg, cfg, seconds=None):
    tabs = []
    for tab in (cfg or {}).get("tabs", []):
        if tab.get("package") == pkg:
            tabs.append(tab)
    try:
        hcfg = load_hatcher_config()
        for prof in hatcher_profiles(hcfg, enabled_only=True):
            if prof.get("package") == pkg:
                tabs.append(hatcher_profile_to_tab(prof))
    except Exception:
        pass

    for tab in tabs:
        if state_recent_enough_for_alive(tab, cfg, seconds=seconds):
            return True
    return False


def state_age_seconds(state):
    try:
        return int((state or {}).get("age", 999999) or 999999)
    except Exception:
        return 999999


def state_is_fresh(state, cfg, seconds=None):
    if not state:
        return False
    if seconds is None:
        seconds = cfg.get("state_stale_seconds", 180)
    try:
        seconds = int(seconds)
    except Exception:
        seconds = 180
    return state_age_seconds(state) <= seconds


def state_is_clean(state):
    return bool(state) and (not state_disconnect_ui(state)) and (not state_login_challenge_detail(state))


def state_is_clean_fresh(state, cfg, seconds=None):
    return state_is_fresh(state, cfg, seconds=seconds) and state_is_clean(state)


def evaluate_package_health(tab, cfg, rt_tab, mode="market", hcfg=None, prof=None, raw_alive=None, state=None, err=None):
    """One shared answer for Market/Hatcher package health."""
    pkg = tab.get("package")
    if raw_alive is None:
        raw_alive = package_alive(pkg, cfg)
    if state is None and err is None:
        state, err = read_state(tab)

    # V3.88: Roblox's native Error 288 dialog may be invisible to the executor,
    # so a fresh Lua heartbeat is not sufficient proof that the client is still
    # connected. A package-scoped Android UI hit overrides fresh/ancient state.
    if raw_alive:
        android_popup = android_disconnect_ui_detail(pkg, cfg)
        if android_popup:
            signature = "|".join([
                str(android_popup.get("code", "") or ""),
                ",".join(android_popup.get("hits", []) or []),
            ])
            last_sig = str(rt_tab.get("android_disconnect_candidate_sig", "") or "")
            last_at = int(rt_tab.get("android_disconnect_candidate_at", 0) or 0)
            window = max(5, int(cfg.get("android_disconnect_confirmation_window_seconds", 30) or 30))
            if signature and signature == last_sig and now() - last_at <= window:
                count = int(rt_tab.get("android_disconnect_candidate_count", 0) or 0) + 1
            else:
                count = 1
            rt_tab["android_disconnect_candidate_sig"] = signature
            rt_tab["android_disconnect_candidate_at"] = now()
            rt_tab["android_disconnect_candidate_count"] = count

            required = max(2, int(cfg.get("android_disconnect_confirmations_required", 2) or 2))
            if count >= required:
                merged_state = dict(state or {})
                merged_state["disconnected"] = True
                merged_state["disconnect_title"] = android_popup.get("title", "Roblox Disconnect")
                merged_state["disconnect_text"] = android_popup.get("text", "")
                merged_state["disconnect_code"] = android_popup.get("code", "")
                merged_state["disconnect_reason"] = android_popup.get("reason", "android_package_scoped_ui")
                merged_state["_android_disconnect_confirmed"] = True
                state = merged_state
                err = None
        else:
            rt_tab["android_disconnect_candidate_sig"] = ""
            rt_tab["android_disconnect_candidate_at"] = 0
            rt_tab["android_disconnect_candidate_count"] = 0

    # V4.12: a visible package-scoped verification overlay outranks a fresh Lua
    # heartbeat. Do this before clean_fresh/manual checks so blocked accounts can
    # never be displayed as healthy merely because scripts still run behind it.
    captcha_ui_alive = bool(raw_alive) and package_alive(pkg, cfg)
    captcha_ui = android_login_challenge_ui_detail(pkg, cfg, force=False) if captcha_ui_alive else None
    if captcha_ui:
        detail = ",".join(captcha_ui.get("hits", []) or []) or "verification UI"
        rt_tab["captcha_ui_visible"] = True
        rt_tab["captcha_ui_detail"] = detail
        rt_tab["captcha_ui_last_seen_at"] = now()
        return {
            "pkg": pkg, "user": tab.get("user_name", pkg), "alive": bool(raw_alive),
            "state": state, "state_err": err, "fresh": False, "clean_fresh": False,
            "pets": int(state.get("pet_count", 0) or 0) if state else "-",
            "eggs": int(state.get("egg_total", 0) or 0) if state else "-",
            "age": state_age_seconds(state) if state else "-",
            "status": "Captcha", "note": "verification UI detected",
            "bad": "ui_challenge", "visible_window": True,
            "ui_challenge_detail": captcha_ui,
        }
    elif rt_tab.get("captcha_ui_visible"):
        # The shared snapshot has moved past the challenge. Fresh state below can
        # clear any false-negative hold without reopening the package.
        rt_tab["captcha_ui_visible"] = False
        rt_tab["captcha_ui_detail"] = ""

    age = state_age_seconds(state) if state else "-"
    pets = int(state.get("pet_count", 0) or 0) if state else "-"
    eggs = int(state.get("egg_total", 0) or 0) if state else "-"
    note = "ok"
    status = "Ingame" if raw_alive else "Offline"
    bad_kind = ""
    clean_fresh = state_is_clean_fresh(state, cfg) if state else False
    fresh = state_is_fresh(state, cfg) if state else False
    if clean_fresh:
        # A genuinely healthy heartbeat starts a new disconnect-recovery cycle.
        # Do not let an old popup cooldown delay a later, unrelated Error 288.
        rt_tab["disconnect_ui_since"] = 0
        rt_tab["last_disconnect_ui_open"] = 0
    visible_window = None
    visible_note = ""
    if raw_alive:
        visible_window, visible_note = package_visible_window(pkg, cfg)

    if manual_login_blocked(rt_tab, cfg) and not state_login_challenge_detail(state):
        return {
            "pkg": pkg, "user": tab.get("user_name", pkg), "alive": bool(raw_alive),
            "state": state, "state_err": err, "fresh": fresh, "clean_fresh": clean_fresh,
            "pets": pets, "eggs": eggs, "age": age, "status": "Manual",
            "note": rt_tab.get("note") or "needs manual login", "bad": "manual",
            "visible_window": visible_window,
        }

    if state:
        challenge_detail = state_login_challenge_detail(state)
        if challenge_detail:
            status = "Manual"
            note = "join captcha/manual"
            bad_kind = "challenge"
        elif state_disconnect_ui(state):
            status = "Kicked"
            note = state_disconnect_note(state)
            bad_kind = "disconnect"
        elif clean_fresh:
            # V3.86: a live Lua heartbeat is authoritative. dumpsys may omit
            # non-focused/floating clone windows and must never override health.
            status = "Ingame" if raw_alive else "Loading"
            note = "ok"
        elif raw_alive and visible_window is False:
            status = "Minimized"
            note = visible_note
            bad_kind = "minimized"
        elif raw_alive:
            if in_post_open_grace(rt_tab, cfg) and state_is_old_after_open(state, rt_tab):
                status = "Loading"
                note = "loading grace"
            else:
                status = "Ingame"
                note = "alive old state"
        else:
            status = "Stale" if fresh else "Offline"
            note = "state stale/offline"
    else:
        note = f"state {err}"
        if raw_alive:
            if rt_tab.get("last_open") and in_post_open_grace(rt_tab, cfg):
                status = "Loading"
                note = "no state grace"
            else:
                status = "No state"
                note = "alive no-state wait"
                bad_kind = "no_state"
        else:
            status = "Offline"
            bad_kind = "dead"

    if mode == "hatcher" and state and not bad_kind:
        problem_code, problem_note = hatcher_teleport_problem(tab, state, hcfg or {}, cfg)
        if problem_code:
            status = "Wrong server"
            note = problem_note
            bad_kind = problem_code

    return {
        "pkg": pkg,
        "user": tab.get("user_name", pkg),
        "alive": bool(raw_alive),
        "state": state,
        "state_err": err,
        "fresh": fresh,
        "clean_fresh": clean_fresh,
        "pets": pets,
        "eggs": eggs,
        "age": age,
        "status": status,
        "note": note,
        "bad": bad_kind,
        "visible_window": visible_window,
    }


def apply_common_health_action(open_queue, tab, target, rt_tab, cfg, rt, health, mode="market"):
    """Queue/mark the common recovery action for a health result."""
    pkg = tab.get("package")
    bad = str(health.get("bad") or "")
    state = health.get("state")

    if bad in ("manual", "challenge"):
        return health.get("status", ""), health.get("note", ""), False

    if bad == "disconnect" and cfg.get("rejoin_if_crash", True):
        added, dnote = queue_disconnect_ui_rejoin(open_queue, tab, target, rt_tab, cfg)
        status = "Queued" if (added or dnote == "already queued") else "Kicked"
        note = f"{state_disconnect_note(state)} {dnote}".strip()
        return status, note, True

    if bad == "minimized":
        # V3.86: status-only. Android dumpsys does not reliably distinguish a
        # deliberately minimized clone from another non-focused floating clone.
        # Never queue/open/stop from this signal; exact stale-state recovery owns it.
        return "Minimized", "minimized status only; waiting for state", True

    if bad == "no_state" and cfg.get("rejoin_if_crash", True):
        since_key = f"{mode}_no_state_since"
        no_state_since = int(rt_tab.get(since_key, 0) or 0)
        if no_state_since <= 0:
            rt_tab[since_key] = now()
            save_runtime(rt)
            return "No state", "alive no-state wait", True
        no_state_for = max(0, now() - int(no_state_since))
        hard_after = int(cfg.get("hatcher_alive_old_state_hard_force_seconds", 300) or 300)
        if no_state_for >= hard_after and not open_queue and not queue_has(open_queue, pkg):
            added, _ = queue_open(open_queue, tab, target, f"{mode} alive no-state hard",
                                  force=True, mode="hard_force")
            return ("Queued" if added else "No state"), ("no-state hard queued" if added else "already queued"), True
        return "No state", f"alive no-state {format_age(no_state_for)}/{format_age(hard_after)}", True

    if bad == "dead" and cfg.get("rejoin_if_crash", True):
        rt_tab[f"{mode}_no_state_since"] = 0
        if cfg.get("smart_open_queue", True):
            added, _ = queue_open(open_queue, tab, target, "crash/dead", skip_if_alive=True)
            return ("Queued" if added else "Offline"), ("crash queued" if added else "already queued"), True
        ok, msg = open_target(tab, rt_tab, cfg, target, "crash/dead", rt=rt)
        return ("Loading" if ok else "Offline"), ("crash open" if ok else msg), True

    return health.get("status", ""), health.get("note", ""), False



def maybe_queue_solver_busy_retry(open_queue, tab, target, rt_tab, cfg, health):
    """Retry SERVER_BUSY only by opening the package again after cooldown.

    Never contact the provider from a healthy middle session. The provider call
    remains inside wait-after-open and therefore happens once for that retry open.
    """
    if not rt_tab.get("solver_busy_retry_pending"):
        return None

    if health.get("clean_fresh"):
        rt_tab["solver_busy_retry_pending"] = False
        rt_tab["solver_busy_retry_at"] = 0
        rt_tab["note"] = "fresh state; solver busy retry cancelled"
        return None

    retry_at = int(rt_tab.get("solver_busy_retry_at", 0) or 0)
    if retry_at <= 0 or now() < retry_at:
        left = max(1, retry_at - now()) if retry_at else 600
        return health.get("status", "Loading"), f"solver busy; retry rejoin in {format_age(left)}", True

    pkg = str((tab or {}).get("package", "") or "")
    if solver_job_running(pkg):
        return "Solving", solver_job_note(pkg), True
    if queue_has(open_queue, pkg):
        return "Queued", "solver busy retry already queued", True

    added, _ = queue_open(
        open_queue, tab, target, "solver SERVER_BUSY retry",
        force=True, mode="hard_force", bypass_manual=True,
        metadata={
            "solver_busy_retry": True,
            "bypass_recheck": True,
        },
    )
    if added:
        rt_tab["solver_busy_retry_pending"] = False
        rt_tab["solver_busy_retry_at"] = 0
        rt_tab["note"] = "SERVER_BUSY retry rejoin queued"
        log_activity("SERVER_BUSY cooldown done; retry rejoin queued", pkg, YELLOW)
        return "Queued", "SERVER_BUSY retry rejoin queued", True
    return health.get("status", "Loading"), "SERVER_BUSY retry queue failed", True


def apply_visible_captcha_ui_action(open_queue, tab, target, rt_tab, cfg, rt, health):
    """Handle one package-scoped visible challenge without restarting the clone."""
    if str((health or {}).get("bad") or "") != "ui_challenge":
        return None

    pkg = str((tab or {}).get("package", "") or "")
    cancel_queued_package(open_queue, pkg)
    rt_tab["captcha_ui_visible"] = True
    rt_tab["captcha_ui_last_seen_at"] = now()

    if solver_job_running(pkg):
        note = solver_job_note(pkg)
        rt_tab["note"] = note
        return "Solving", note, True

    retry_at = int(rt_tab.get("captcha_ui_retry_at", 0) or 0)
    if retry_at > now():
        left = max(1, retry_at - now())
        note = f"verification UI; solver retry in {format_age(left)}"
        rt_tab["note"] = note
        return "Captcha", note, True

    started, note = start_solver_job(
        tab, cfg, rt, rt_tab,
        "visible package-scoped verification UI",
    )
    if started:
        clear_hold(pkg)
        rt_tab["manual_login_needed"] = False
        rt_tab["manual_login_reason"] = ""
        rt_tab["manual_login_detail"] = ""
        rt_tab["note"] = note
        save_runtime(rt)
        return "Solving", note, True

    low = str(note or "").lower()
    if "retry in" in low or "cooldown" in low:
        cooldown = max(600, int(cfg.get("captcha_ui_retry_seconds", 600) or 600))
        last_attempt = int(rt_tab.get("solver_last_attempt", 0) or 0)
        rt_tab["captcha_ui_retry_at"] = max(now() + 1, last_attempt + cooldown)
        rt_tab["note"] = note
        return "Captcha", note, True

    # Solver is disabled/misconfigured or this package has no usable cookie. Hold
    # only this clone; do not let stale/dead routing start a hard-rejoin loop.
    rt_tab["manual_login_needed"] = True
    rt_tab["manual_login_reason"] = "visible verification UI"
    rt_tab["manual_login_detail"] = str(note or "solver unavailable")
    rt_tab["manual_login_detected_at"] = now()
    rt_tab["note"] = str(note or "verification requires manual action")
    set_hold(pkg, rt_tab["note"])
    save_runtime(rt)
    return "Captcha", rt_tab["note"], True


def apply_rejoin_action(open_queue, tab, target, rt_tab, cfg, rt, health, hcfg=None, mode="market"):
    """SHARED rejoin engine for both Market and Hatcher."""
    pkg = tab.get("package")
    state = health.get("state")
    alive = bool(health.get("alive"))
    bad = str(health.get("bad") or "")

    captcha_action = apply_visible_captcha_ui_action(
        open_queue, tab, target, rt_tab, cfg, rt, health
    )
    if captcha_action is not None:
        return captcha_action

    busy_action = maybe_queue_solver_busy_retry(open_queue, tab, target, rt_tab, cfg, health)
    if busy_action is not None:
        return busy_action

    if not cfg.get("rejoin_if_crash", True):
        if not cfg.get("alive_old_state_rejoin_in_safe_mode", True):
            return health.get("status", ""), "SAFEMODE-off " + str(health.get("note", "")), False

    trigger = int(cfg.get("alive_old_state_hard_seconds", 180) or 180)
    max_valid = int(cfg.get("alive_old_state_max_valid_seconds", 86400) or 86400)
    stale_limit = int(cfg.get("state_stale_seconds", 180) or 180)
    grace = int(cfg.get("post_open_grace_seconds", 360) or 360)

    # --- join/login challenge: provider calls are event-bound to a rejoin ---
    if bad == "challenge" or (state and state_login_challenge_detail(state)):
        cancel_queued_package(open_queue, pkg)
        added, _ = queue_open(
            open_queue, tab, target, "join challenge pre-solver rejoin",
            force=True, mode="hard_force", bypass_manual=True,
            metadata={"bypass_recheck": True},
        )
        return ("Queued" if added else "Manual"),                ("challenge rejoin queued; solver checks after open" if added else "challenge already queued"), True

    # --- disconnect / kick popup: always kill+open ---
    if bad == "disconnect" or (state and state_disconnect_ui(state)):
        added, dnote = queue_disconnect_ui_rejoin(open_queue, tab, target, rt_tab, cfg)
        status = "Queued" if (added or dnote == "already queued") else "Kicked"
        note = f"{state_disconnect_note(state)} {dnote}".strip()
        return status, note, True

    # --- protect loads: if WE opened it recently, don't touch during grace ---
    last_open = int(rt_tab.get("last_open", 0) or 0)
    in_grace = last_open > 0 and (now() - last_open) < grace

    if state:
        age = state_age_seconds(state)

        # V3.79: PHANTOM AGE GUARD (mirror of the hatcher path).
        # A missing `ts` field makes age compute to an impossible value like 277h.
        # Never drive a kill+open off that — ignore it and trust the live package
        # check instead. Checked BEFORE grace/trigger so a garbage age can't force
        # a rejoin under any branch. This is the exact guard the market path was
        # missing while the hatcher had it.
        if max_valid > 0 and age > max_valid:
            return ("Ingame" if alive else "Stale"), \
                   f"invalid old state ignored {format_age(age)}", False

        # fresh -> healthy, skip
        if age <= stale_limit and state_is_clean(state):
            return ("Ingame" if alive else "Loading"), "ok", False

        # old state but still loading (inside our grace window) -> wait
        if in_grace:
            return "Loading", f"loading grace {format_age(now() - last_open)}", True

        # old past trigger -> kill + open THIS one clone
        if age >= trigger:
            added, _ = queue_open(open_queue, tab, target,
                                  f"{mode} alive old state {format_age(age)}",
                                  force=True, mode="hard", skip_if_alive=False)
            return ("Queued" if added else "Stale"), \
                   (f"old {format_age(age)} kill+open" if added else "already queued"), True

        # stale but under trigger -> just report, don't act yet
        return ("Ingame" if alive else "Stale"), f"stale a{age}/t{trigger}/s{stale_limit} rc{int(bool(cfg.get('rejoin_if_crash', True)))}", True

    # --- no state at all ---
    if alive:
        if in_grace:
            return "Loading", "no state grace", True
        added, _ = queue_open(open_queue, tab, target, f"{mode} alive no-state hard",
                              force=True, mode="hard", skip_if_alive=False)
        return ("Queued" if added else "No state"), \
               ("no-state kill+open" if added else "already queued"), True

    # dead -> kill + open
    added, _ = queue_open(open_queue, tab, target, f"{mode} crash/dead",
                          force=True, mode="hard", skip_if_alive=True)
    return ("Queued" if added else "Offline"), \
           ("crash kill+open" if added else "already queued"), True


def manual_login_blocked(rt_tab, cfg):
    if not cfg.get("login_challenge_skip_blocked_packages", True):
        return False
    return bool(rt_tab.get("manual_login_needed", False))


def clear_manual_login_block(rt_tab):
    if not rt_tab.get("manual_login_needed"):
        return False
    rt_tab["manual_login_needed"] = False
    rt_tab["manual_login_reason"] = ""
    rt_tab["manual_login_detail"] = ""
    rt_tab["manual_login_detected_at"] = 0
    rt_tab.pop("solver_attempted", None)  # legacy V3.80 field
    rt_tab["note"] = "manual login cleared"
    return True


def cached_cookie_for_package(pkg):
    if not COOKIE_CACHE.exists():
        return ""
    try:
        with open(COOKIE_CACHE) as f:
            cache = json.load(f)
        ent = cache.get(pkg, {}) if isinstance(cache, dict) else {}
        return str(ent.get("cookie", "") or "").strip()
    except Exception:
        return ""


def roblox_cookie_detection(cookie):
    if not cookie:
        return None, "no cached cookie"
    status = check_cookie_challenge(cookie)
    if status == "valid":
        info = get_roblox_user_info(cookie)
        username = info.get("username", "") if info else ""
        return False, f"api valid {username}".strip()
    elif status == "challenge":
        return True, "api challenge (captcha required)"
    elif status == "invalid":
        # An expired cookie is not a CAPTCHA and cannot be fixed by the solver.
        return None, "api invalid/expired"
    else:
        return None, f"api {status}"


# One cached Android accessibility snapshot shared by every package check in a
# dashboard cycle. uiautomator is relatively expensive, so never run it once per
# clone. The cache is also important for consistency: A/B/C/D are judged from the
# same screen moment.
_ANDROID_UI_SNAPSHOT_CACHE = {
    "ts": 0,
    "by_pkg": {},
    "all_text": [],
    "error": "not captured",
}


def _parse_android_ui_xml(xml_text):
    by_pkg = {}
    all_text = []
    node_re = re.compile(r'<node\b[^>]*>', re.I)
    attr_re = re.compile(r'(text|content-desc|package)="([^"]*)"', re.I)

    for node in node_re.findall(str(xml_text or "")):
        attrs = {k.lower(): html.unescape(v) for k, v in attr_re.findall(node)}
        parts = [attrs.get("text", ""), attrs.get("content-desc", "")]
        value = " ".join(x.strip() for x in parts if str(x or "").strip())
        if not value:
            continue
        all_text.append(value)
        pkg = str(attrs.get("package", "") or "").strip()
        if pkg:
            by_pkg.setdefault(pkg, []).append(value)

    return by_pkg, all_text


def capture_android_ui_snapshot(cfg, force=False):
    """Return one package-indexed uiautomator text snapshot.

    Only package-scoped text is trusted for disconnect recovery. `all_text` is
    retained for the explicit/manual CAPTCHA test path, where an unscoped note is
    useful, but it is never broadcast to multiple clone packages.
    """
    global _ANDROID_UI_SNAPSHOT_CACHE

    ui_needed = (
        cfg.get("android_disconnect_ui_detection_enabled", True)
        or cfg.get("captcha_ui_override_enabled", True)
        or cfg.get("login_challenge_ui_detection_enabled", True)
    )
    if not ui_needed and not force:
        return _ANDROID_UI_SNAPSHOT_CACHE

    t = now()
    cache_seconds = int(cfg.get("android_ui_snapshot_cache_seconds", 25) or 25)
    if not force and int(_ANDROID_UI_SNAPSHOT_CACHE.get("ts", 0) or 0) > 0:
        if t - int(_ANDROID_UI_SNAPSHOT_CACHE.get("ts", 0) or 0) < max(1, cache_seconds):
            return _ANDROID_UI_SNAPSHOT_CACHE

    dump_path = f"/sdcard/Download/nomo_ui_snapshot_{os.getpid()}.xml"
    cmd = (
        f"uiautomator dump {shlex.quote(dump_path)} >/dev/null 2>&1 && "
        f"cat {shlex.quote(dump_path)}"
    )
    timeout = int(cfg.get("android_ui_snapshot_timeout_seconds", 15) or 15)
    code, out = shell_timeout(cmd, cfg, capture=True, timeout=max(3, timeout))

    if code != 0 or not out:
        _ANDROID_UI_SNAPSHOT_CACHE = {
            "ts": t,
            "by_pkg": {},
            "all_text": [],
            "error": "ui dump unavailable",
        }
        return _ANDROID_UI_SNAPSHOT_CACHE

    by_pkg, all_text = _parse_android_ui_xml(out)
    _ANDROID_UI_SNAPSHOT_CACHE = {
        "ts": t,
        "by_pkg": by_pkg,
        "all_text": all_text,
        "error": "" if (by_pkg or all_text) else "ui has no accessible text",
    }
    return _ANDROID_UI_SNAPSHOT_CACHE


def android_ui_text_for_package(pkg, cfg, force=False):
    snapshot = capture_android_ui_snapshot(cfg, force=force)
    by_pkg = snapshot.get("by_pkg", {}) if isinstance(snapshot, dict) else {}
    texts = []
    for node_pkg, values in (by_pkg or {}).items():
        if node_pkg == pkg or node_pkg.startswith(str(pkg) + ":"):
            texts.extend(values if isinstance(values, list) else [str(values)])
    return texts, str(snapshot.get("error", "") or "")


def android_disconnect_ui_detail(pkg, cfg):
    """High-confidence package-scoped native Roblox disconnect detection."""
    if not cfg.get("android_disconnect_ui_detection_enabled", True):
        return None

    texts, _ = android_ui_text_for_package(pkg, cfg, force=False)
    if not texts:
        return None

    joined = "\n".join(str(x) for x in texts)
    low = joined.lower()
    strong_terms = [
        "disconnected from the experience",
        "the server has shut down",
        "you have been kicked",
        "kicked by this experience",
        "connection failed",
        "failed to connect",
        "no response from server",
        "lost connection",
        "unexpectedly disconnected",
        "session expired",
        "error code: 288",
        "error code 288",
    ]
    hits = [term for term in strong_terms if term in low]
    # "Disconnected" by itself is accepted only when paired with a real popup
    # companion such as Leave/Reconnect/Error Code, preventing random page text.
    if not hits and "disconnected" in low:
        if any(term in low for term in ["leave", "reconnect", "error code", "server has shut down"]):
            hits = ["disconnected"]

    if not hits:
        return None

    code_match = re.search(r"error\s*code\s*:?\s*(\d+)", joined, re.I)
    return {
        "title": "Roblox Disconnect",
        "text": joined,
        "code": code_match.group(1) if code_match else "",
        "reason": "android_package_scoped_ui",
        "hits": hits[:5],
    }


def android_login_challenge_ui_detail(pkg, cfg, force=False):
    """Return a strong package-scoped Roblox verification signal.

    Automatic dashboard decisions must never use snapshot.all_text because four
    floating clone windows can be present at once. Only nodes whose Android
    package matches this exact clone are trusted here.
    """
    if not cfg.get("captcha_ui_override_enabled", True):
        return None
    if not cfg.get("login_challenge_ui_detection_enabled", True):
        return None

    texts, _ = android_ui_text_for_package(str(pkg or ""), cfg, force=force)
    if not texts:
        return None

    joined = "\n".join(str(x) for x in texts if str(x or "").strip())
    low = joined.lower()
    strong_terms = [
        "verifying you're not a bot",
        "verifying you are not a bot",
        "please solve this challenge so we know you are a real person",
        "please solve this challenge",
        "start puzzle",
        "solve this puzzle",
        "solve this challenge",
        "complete the challenge",
        "human verification",
        "prove you are human",
        "arkose",
        "fun captcha",
    ]
    hits = [term for term in strong_terms if term in low]

    # Permit the exact screenshot wording split across separate nodes. Generic
    # "Verification" or "Security" by itself is intentionally never enough.
    if not hits and "verification" in low and "real person" in low:
        hits = ["verification + real person"]
    if not hits and "not a bot" in low and ("verification" in low or "security" in low):
        hits = ["not a bot"]
    if not hits:
        return None

    return {
        "title": "Roblox Verification",
        "text": joined,
        "reason": "android_package_scoped_captcha_ui",
        "hits": hits[:5],
    }


def clear_captcha_ui_runtime(rt_tab):
    changed = False
    for key, value in (
        ("captcha_ui_visible", False),
        ("captcha_ui_detail", ""),
        ("captcha_ui_last_seen_at", 0),
        ("captcha_ui_false_negative", False),
        ("captcha_ui_retry_at", 0),
    ):
        if rt_tab.get(key) != value:
            rt_tab[key] = value
            changed = True
    return changed


def ui_login_challenge_detection(tab, cfg, force=True, allow_unscoped=True):
    """Package-scoped Android UI CAPTCHA detection.

    Roblox's native/WebView challenge is not always exposed to uiautomator. A
    positive result therefore requires a strong human-check phrase; generic
    login, sign-up, continue, security, or verify text is never enough.
    """
    pkg = str((tab or {}).get("package", "") or "")
    snapshot = capture_android_ui_snapshot(cfg, force=force)
    scoped, _ = android_ui_text_for_package(pkg, cfg, force=False)
    all_text = list(snapshot.get("all_text", []) or [])
    source = scoped if scoped else (all_text if allow_unscoped else [])
    low = " ".join(source).lower()

    # These screens block joining but are not CAPTCHA challenges. Sending them
    # to the solver only returns NO_CAPTCHA and creates a rejoin loop.
    manual_gate_terms = [
        "access to popular games has changed", "check your age",
        "verify your age", "age verification", "parental controls",
        "parental restriction", "account restrictions"
    ]
    manual_hits = [t for t in manual_gate_terms if t in low]
    scope_note = "package-scoped" if scoped else ("unscoped" if allow_unscoped else "no package-scoped text")
    if manual_hits:
        return True, f"ui manual join block {scope_note} " + ",".join(manual_hits[:4])

    strong_terms = [
        "captcha", "not a bot", "you are not a bot", "verify you are human",
        "prove you are human", "human verification", "real person",
        "start puzzle", "solve this puzzle", "solve this challenge",
        "complete the challenge", "arkose", "fun captcha"
    ]
    hits = [t for t in strong_terms if t in low]
    if hits:
        return True, f"ui captcha {scope_note} " + ",".join(hits[:5])

    login_only = [t for t in ["log in", "login", "sign up"] if t in low]
    if login_only:
        return False, f"ui login text only ({scope_note}); not captcha"
    if not source:
        return None, "ui has no accessible text"
    return False, f"ui no strong captcha text ({scope_note})"


def send_login_challenge_alert(cfg, tab, rt_tab, detail):
    webhook = str(cfg.get("login_challenge_webhook_url") or cfg.get("cookie_webhook_url") or "").strip()
    if not webhook:
        return False, "no webhook"

    cooldown = int(cfg.get("login_challenge_alert_cooldown_seconds", 1800) or 1800)
    last = int(rt_tab.get("manual_login_last_alert", 0) or 0)
    if last > 0 and now() - last < cooldown:
        return False, "alert cooldown"

    content = (
        f"NOMO REJOIN needs manual login/CAPTCHA\n"
        f"Package: `{tab.get('package')}`\n"
        f"User: `{tab.get('user_name', tab.get('package'))}`\n"
        f"Detail: {detail}"
    )
    payload = json.dumps({"content": content}).encode("utf-8")
    try:
        req = urllib.request.Request(webhook, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "NOMO-Rejoin/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            rt_tab["manual_login_last_alert"] = now()
            return resp.getcode() in (200, 204), f"webhook {resp.getcode()}"
    except Exception as e:
        return False, f"webhook error {cut(str(e), 80)}"


def detect_and_mark_manual_login(tab, cfg, rt, rt_tab, reason):
    if not cfg.get("login_challenge_detection_enabled", True):
        return False, "detection disabled"

    cookie = cached_cookie_for_package(tab.get("package", ""))
    details = []
    detected = False

    # API detection
    if cookie and cfg.get("login_challenge_api_detection_enabled", True):
        api_hit, api_detail = roblox_cookie_detection(cookie)
        details.append(api_detail)
        if api_hit is True:
            detected = True
    elif cfg.get("login_challenge_api_detection_enabled", True):
        details.append("api skipped no cookie")

    # UI detection (optional)
    if cfg.get("login_challenge_ui_detection_enabled", True):
        ui_hit, ui_detail = ui_login_challenge_detection(tab, cfg, force=False, allow_unscoped=False)
        details.append(ui_detail)
        if ui_hit is True:
            detected = True

    if not detected:
        return False, "; ".join(details)

    manual_join_block = any("manual join block" in str(x).lower() for x in details)

    # Only genuine CAPTCHA/challenge signals go to the provider. Age gates,
    # parental restrictions, and other manual join blocks are isolated locally.
    if not manual_join_block and cfg.get("solver_enabled", False) and cookie:
        status, note = handle_detected_solver_challenge(tab, cfg, rt, rt_tab, reason)
        if status == "Solving":
            return True, note

    # If solver failed/not enabled, or this is a non-CAPTCHA gate, mark manual hold.
    detail = f"{reason}; " + "; ".join(x for x in details if x)
    rt_tab["manual_login_needed"] = True
    rt_tab["manual_login_reason"] = reason
    rt_tab["manual_login_detail"] = detail
    rt_tab["manual_login_detected_at"] = now()
    low_detail = detail.lower()
    rt_tab["note"] = "join captcha/manual" if ("captcha" in low_detail or "puzzle" in low_detail or "not a bot" in low_detail) else "needs manual login"
    send_login_challenge_alert(cfg, tab, rt_tab, detail)
    save_runtime(rt)
    return True, detail


def maybe_detect_manual_login(tab, cfg, rt, rt_tab, reason, min_interval=60):
    if manual_login_blocked(rt_tab, cfg):
        return True, "already marked"

    last = int(rt_tab.get("manual_login_last_detect_check", 0) or 0)
    if last > 0 and now() - last < int(min_interval or 60):
        return False, "detect cooldown"

    rt_tab["manual_login_last_detect_check"] = now()
    return detect_and_mark_manual_login(tab, cfg, rt, rt_tab, reason)


def maybe_clear_manual_login_prejoin(tab, cfg, rt, rt_tab, min_interval=120):
    if not manual_login_blocked(rt_tab, cfg):
        return True, "not manual"

    last = int(rt_tab.get("manual_login_last_recover_check", 0) or 0)
    if last > 0 and now() - last < int(min_interval or 120):
        return False, "manual recover cooldown"

    rt_tab["manual_login_last_recover_check"] = now()
    cookie = cached_cookie_for_package(tab.get("package", ""))
    api_hit, api_detail = roblox_cookie_detection(cookie) if cookie else (None, "api skipped no cookie")
    ui_hit, ui_detail = ui_login_challenge_detection(tab, cfg, force=True, allow_unscoped=False) if cfg.get("login_challenge_ui_detection_enabled", True) else (None, "ui skipped")

    if api_hit is False and ui_hit is not True:
        clear_manual_login_block(rt_tab)
        clear_hold(tab.get("package", ""))
        rt_tab["note"] = "login ok, waiting ingame"
        save_runtime(rt)
        return True, f"{api_detail}; {ui_detail}"

    save_runtime(rt)
    return False, f"{api_detail}; {ui_detail}"


def select_enabled_tab_menu(cfg, title="SELECT PACKAGE"):
    selected = choose_packages_common(cfg, title, multi=False, enabled_only=True,
                                      include_discovered=False, configured_only=True)
    if not selected:
        return None
    pkg = selected[0]
    return next((t for t in cfg.get("tabs", []) if t.get("package") == pkg), None)

def test_login_challenge_detection_menu(cfg):
    while True:
        selected = choose_packages_common(cfg, "TEST LOGIN / CAPTCHA DETECTION", multi=False,
                                          installed_only=True, include_discovered=True)
        if not selected:
            return
        pkg = selected[0]
        tab = next((t for t in cfg.get("tabs", []) if t.get("package") == pkg), None) or {"package": pkg, "user_name": pkg}
        open_first = input("Bring package to foreground before testing? (y/N): ").strip().lower() == "y"
        if open_first:
            link = tab.get("server_link") or tab.get("restock_link") or cfg.get("market_link") or DEFAULT_MARKET_LINK
            open_roblox(pkg, link, cfg, soft=True, reason="manual login detection test")
            time.sleep(2)
        cookie = get_cookie_from_package(pkg) or cached_cookie_for_package(pkg)
        api_hit, api_detail = roblox_cookie_detection(cookie) if cookie else (None, "no cookie")
        ui_hit, ui_detail = ui_login_challenge_detection(tab, cfg)
        print("")
        print(f"API: {api_hit} | {api_detail}")
        print(f"UI : {ui_hit} | {ui_detail}")
        if api_hit is True or ui_hit is True:
            print(col("Challenge/manual-login signal detected.", YELLOW))
        elif api_hit is False and ui_hit is not True:
            print(col("No challenge detected.", GREEN))
        else:
            print(col("Detection inconclusive.", YELLOW))
        pause()

def wait_until_fresh_after_open(tab, cfg, rt, opened_at, timeout_override=None, allow_solver_probe=True):
    if not cfg.get("wait_fresh_after_open", True):
        return True, "fresh wait disabled"

    timeout = int(timeout_override if timeout_override is not None else cfg.get("open_wait_fresh_seconds", 240))
    check_every = max(1, int(cfg.get("open_wait_check_seconds", 5)))
    start = now()
    pkg = tab.get("package")
    rt_tab = get_runtime_tab(rt, pkg)

    # V3.81: never call the solver blindly here. A job is started below only
    # when the Lua state explicitly reports a join/login challenge.

    # Wait loop
    while now() - start < timeout:
        elapsed = now() - start
        if stop_requested():
            save_runtime(rt)
            return False, "stop"

        alive = package_alive(tab["package"], cfg)
        fresh, state, err = state_fresh_after_open(tab, cfg, opened_at)

        # V3.84: one provider check per actual open/rejoin. An old state file may
        # still exist, so use `not fresh` rather than only `not state`.
        probe_after = max(10, int(cfg.get("solver_probe_after_seconds", 45) or 45))
        if allow_solver_probe and not fresh and elapsed >= probe_after and not solver_job_running(pkg):
            started, probe_note = start_challenge_probe_job(
                tab, cfg, rt, rt_tab,
                probe_token=opened_at,
                reason="wait-after-open no fresh state",
            )
            if not started and (
                "already checked this rejoin" in probe_note
                or "provider cooldown" in probe_note
            ):
                # V3.87: this is not a loading failure. Keep waiting for Lua for
                # the full configured timeout; otherwise a post-solver recovery
                # is judged dead only ~45s after launch.
                rt_tab["note"] = probe_note
                save_runtime(rt)

        completion = solver_job_completion_state(pkg)
        if completion in ("no_challenge", "challenge_result"):
            # V3.86: do not consume the completed job with a disposable [] queue.
            # _do_open_cycle applies it once to the real shared open_queue.
            return False, "solver result"

        # build a status note for the table
        status_note = ""
        visible_captcha = android_login_challenge_ui_detail(pkg, cfg, force=False) if alive else None
        if visible_captcha:
            solver_status, solver_note = handle_detected_solver_challenge(
                tab, cfg, rt, rt_tab, "visible package-scoped verification UI"
            )
            rt_tab["captcha_ui_visible"] = True
            rt_tab["captcha_ui_last_seen_at"] = now()
            rt_tab["note"] = solver_note
            save_runtime(rt)
            return False, "solver pending" if solver_status == "Solving" else "manual challenge"
        if state_disconnect_ui(state):
            status_note = state_disconnect_note(state)
        elif state_login_challenge_detail(state):
            solver_status, solver_note = handle_detected_solver_challenge(
                tab, cfg, rt, rt_tab, state_login_challenge_detail(state)
            )
            rt_tab["note"] = solver_note
            save_runtime(rt)
            # Release the single-flight open lock immediately. The background
            # solver continues while the next clone is allowed to load.
            return False, "solver pending" if solver_status == "Solving" else "manual challenge"
        elif solver_job_running(pkg):
            status_note = solver_job_note(pkg)
        elif not alive:
            status_note = "loading (not alive yet)"
        else:
            status_note = "waiting fresh state"

        # Surface it in the table's Note column
        rt_tab["note"] = f"{status_note} {elapsed}/{timeout}s"
        save_runtime(rt)

        wait_fresh_screen(tab, cfg, elapsed, timeout, alive, state, err, status_note)

        if fresh:
            clear_manual_login_block(rt_tab)
            clear_captcha_ui_runtime(rt_tab)
            rt_tab["solver_busy_retry_pending"] = False
            rt_tab["solver_busy_retry_at"] = 0
            save_runtime(rt)
            log_activity("fresh state - rejoin complete", pkg, GREEN)
            return True, "fresh"
    
        if not alive and elapsed >= 20:
            return False, "died while loading"

        for _ in range(check_every):
            if stop_requested():
                save_runtime(rt)
                return False, "stop"
            time.sleep(1)

    return False, "fresh timeout"


def process_open_queue(open_queue, cfg, rt, session_start=None, loops=0):
    if not open_queue:
        return False

    item = open_queue.pop(0)
    tab = item["tab"]
    pkg = tab["package"]
    rt_tab = get_runtime_tab(rt, pkg)
    target = item.get("target", rt_tab.get("target", "market"))
    reason = item.get("reason", "queued open")
    mode = str(item.get("mode", "hard") or "hard").lower()

    if manual_login_blocked(rt_tab, cfg) and not item.get("bypass_manual"):
        recovered, detail = maybe_clear_manual_login_prejoin(tab, cfg, rt, rt_tab)
        if not recovered:
            rt_tab["note"] = rt_tab.get("note") or "needs manual login"
            save_runtime(rt)
            return True

    if item.get("skip_if_alive") and effective_package_alive(tab, cfg):
        rt_tab["note"] = "queue skip alive"
        save_runtime(rt)
        return True

    # If cooldown blocks this package, do not block other packages behind it.
    if not item.get("force") and not can_open(rt_tab, cfg):
        rt_tab["note"] = "cooldown queued"
        save_runtime(rt)
        return True

    item_mode = str(item.get("mode", "hard") or "hard").lower()
    is_hard = item_mode != "soft"

    # V3.79: SINGLE-FLIGHT GATE
    # Never start a kill/open while another package is still inside its own
    # kill -> open -> wait-for-fresh cycle. One clone at a time, always.
    if cfg.get("single_flight_open", True):
        holder = str(rt.get("_open_lock_pkg", "") or "")
        held_at = int(rt.get("_open_lock_at", 0) or 0)
        max_hold = int(cfg.get("single_flight_max_seconds", 420) or 420)
        if holder and holder != pkg:
            if max_hold > 0 and now() - held_at >= max_hold:
                # Stale lock (crash mid-cycle). Release it and let this item run.
                log_activity(f"open lock expired, released ({holder})", holder, YELLOW)
                rt["_open_lock_pkg"] = ""
                rt["_open_lock_at"] = 0
            else:
                open_queue.insert(0, item)
                rt_tab["note"] = f"waiting for {holder}"
                save_runtime(rt)
                return True

    # V3.79: LAST-SECOND HEALTH RECHECK
    # The clone may have recovered while it sat in the queue. Do not force-stop a
    # package that is alive AND writing fresh state right now.
    if is_hard and cfg.get("recheck_before_hard_open", True) and not item.get("bypass_recheck"):
        queued_at = int(item.get("queued_at", 0) or 0)
        min_age = int(cfg.get("recheck_min_queue_age_seconds", 20) or 0)
        if queued_at > 0 and (now() - queued_at) >= min_age:
            stale_s = int(cfg.get("state_stale_seconds", 180) or 180)
            if package_alive(pkg, cfg, fresh=True) and state_recent_enough_for_alive(tab, cfg, seconds=stale_s):
                rt_tab["note"] = "healed - open cancelled"
                log_activity("open cancelled: healed while queued", pkg, GREEN)
                save_runtime(rt)
                return True

    # POOL-WIDE STAGGER
    stagger = int(cfg.get("open_stagger_seconds", 20) or 0)
    if stagger > 0 and is_hard:
        last_pool_open = int(rt.get("_last_pool_hard_open", 0) or 0)
        since = now() - last_pool_open
        if last_pool_open > 0 and since < stagger:
            open_queue.insert(0, item)
            rt_tab["note"] = f"stagger wait {max(1, stagger - since)}s"
            save_runtime(rt)
            return True

    if cfg.get("workspace_sync_enabled", False) and cfg.get("workspace_sync_before_open", True) and workspace_sync_allowed_for_mode(cfg):
        run_workspace_sync(cfg, rt, reason=f"before open {pkg}", force=False)

    # V3.79: take the single-flight lock. Released in the finally below, so no
    # other package can be killed until this one settles or times out.
    if cfg.get("single_flight_open", True):
        rt["_open_lock_pkg"] = pkg
        rt["_open_lock_at"] = now()
        save_runtime(rt)

    try:
        return _do_open_cycle(open_queue, item, tab, rt_tab, pkg, target, reason,
                              mode, is_hard, cfg, rt)
    finally:
        if cfg.get("single_flight_open", True) and str(rt.get("_open_lock_pkg", "")) == pkg:
            rt["_open_lock_pkg"] = ""
            rt["_open_lock_at"] = 0
            save_runtime(rt)


def _do_open_cycle(open_queue, item, tab, rt_tab, pkg, target, reason, mode, is_hard, cfg, rt):
    """The actual kill -> open -> wait-for-fresh cycle for exactly ONE package."""
    opening_screen(tab, target, cfg, 1, max(1, len(open_queue) + 1), mode=mode)
    rt_tab["note"] = f"opening -> {target}"
    save_runtime(rt)
    log_activity(f"opening -> {target} ({mode})", pkg)
    ok, msg = open_target(tab, rt_tab, cfg, target, reason, force=item.get("force", False), rt=rt, mode=mode)
    opened_at = int(rt_tab.get("last_open", now()))
    # Record pool-wide hard-open time for the stagger gate.
    if is_hard and ok:
        rt["_last_pool_hard_open"] = now()
    log_activity(f"open {'ok' if ok else 'FAILED'}: {msg}", pkg,
                 GREEN if ok else RED)
    save_runtime(rt)

    if ok:
        actual_open_mode = str(rt_tab.get("last_open_mode", mode) or mode).lower()

        if actual_open_mode == "soft":
            timeout = int(cfg.get("soft_hop_wait_fresh_seconds", 240))
        elif cfg.get("homepage_stuck_hard_fallback_enabled", True):
            normal_timeout = int(cfg.get("open_wait_fresh_seconds", 240) or 240)
            homepage_timeout = int(cfg.get("homepage_stuck_fallback_seconds", 75) or 75)
            timeout = max(20, min(normal_timeout, homepage_timeout))
        else:
            timeout = None

        fresh_ok, fresh_msg = wait_until_fresh_after_open(
            tab, cfg, rt, opened_at, timeout_override=timeout,
            allow_solver_probe=not bool(item.get("skip_solver_probe")),
        )
        rt_tab["note"] = ("soft " + fresh_msg) if actual_open_mode == "soft" else fresh_msg
        save_runtime(rt)

        if fresh_msg == "solver result":
            poll_solver_jobs(cfg, rt, open_queue)

        if not fresh_ok and fresh_msg not in ("stop", "solver pending", "manual challenge", "solver result"):
            if rt_tab.get("solver_busy_retry_pending"):
                retry_at = int(rt_tab.get("solver_busy_retry_at", 0) or 0)
                left = max(1, retry_at - now()) if retry_at else 600
                rt_tab["note"] = f"SERVER_BUSY; waiting {format_age(left)} for retry rejoin"
                save_runtime(rt)
                return True

            # V3.87: A provider clear gets exactly one recovery open. If that
            # package still cannot create fresh state, another hard retry cannot
            # fix an age gate/homepage/account restriction and only disturbs the
            # Redfinger window layout. Isolate this package and continue others.
            if item.get("solver_recovery") and cfg.get("manual_hold_after_solver_rejoin_timeout", True):
                solver_result = str(item.get("solver_result", "solver clear") or "solver clear")
                cancel_queued_package(open_queue, pkg)
                rt_tab["manual_login_needed"] = True
                rt_tab["manual_login_reason"] = "join blocked after solver recovery"
                rt_tab["manual_login_detail"] = f"{solver_result}; {fresh_msg}; no fresh state after recovery"
                rt_tab["manual_login_detected_at"] = now()
                rt_tab["note"] = f"{solver_result} but still no state - check homepage/age gate"
                set_hold(pkg, "join blocked after solver recovery")
                log_activity(
                    f"{solver_result} recovery still no state; package held (no more hard loop)",
                    pkg, RED,
                )
                save_runtime(rt)
                return True

            retries_done = int(item.get("homepage_hard_retries", 0) or 0)
            max_retries = int(cfg.get("homepage_stuck_max_hard_retries", 1) or 0)

            if cfg.get("homepage_stuck_hard_fallback_enabled", True) and retries_done < max_retries:
                added, _ = queue_open(
                    open_queue,
                    tab,
                    target,
                    f"homepage/no-state hard retry after {fresh_msg}",
                    force=True,
                    mode="hard_force",
                    front=False,
                )
                if added:
                    retry_item = next(
                        (q for q in reversed(open_queue)
                         if q.get("tab", {}).get("package") == pkg
                         and str(q.get("reason", "")).startswith("homepage/no-state hard retry")),
                        None,
                    )
                    if retry_item is not None:
                        retry_item["homepage_hard_retries"] = retries_done + 1
                        retry_item["bypass_recheck"] = True
                    rt_tab["note"] = f"{fresh_msg}; hard retry {retries_done + 1}/{max_retries}"
                    save_runtime(rt)

            elif actual_open_mode == "soft" and cfg.get("soft_hop_fallback_hard", True):
                queue_open(open_queue, tab, target, "soft fallback hard", force=True, mode="hard_force", front=True)
    else:
        rt_tab["note"] = msg
        save_runtime(rt)

        if mode == "soft" and cfg.get("soft_hop_fallback_hard", True):
            queue_open(open_queue, tab, target, "soft failed hard", force=True, mode="hard", front=True)

    return True

def api_check_package(package, cache=None, cfg=None):
    if cache is None:
        cache = load_cookie_cache()
    if package not in cache:
        print(f"[API] No cached cookie for {package}.")
        return
    cookie = cache[package].get("cookie")
    if not cookie:
        print(f"[API] Cookie empty for {package}.")
        return

    status = check_cookie_challenge(cookie)

    if status in ["challenge", "invalid"] and cfg and package_has_fresh_healthy_state(
        package, cfg, seconds=int(cfg.get("state_stale_seconds", 180) or 180)
    ):
        clear_hold(package)
        rt = load_runtime()
        rt_tab = get_runtime_tab(rt, package)
        clear_manual_login_block(rt_tab)
        rt_tab["note"] = f"api {status} ignored fresh state"
        save_runtime(rt)
        print(col(f"  {package}: API {status} ignored; fresh state is healthy", GREEN))
        return

    if status == "valid":
        clear_hold(package)
        rt = load_runtime()
        rt_tab = get_runtime_tab(rt, package)
        clear_manual_login_block(rt_tab)
        save_runtime(rt)
        print(col(f"  {package}: Valid cookie", GREEN))
    elif status == "challenge":
        if cfg and cfg.get("solver_enabled", False):
            print(col(f"[SOLVER] Attempting to solve challenge for {package}...", CYAN))
            ok, msg = solve_captcha(cookie, cfg)
            if ok:
                print(col(f"[SOLVER] Solved! Re-checking API...", GREEN))
                status2 = check_cookie_challenge(cookie)
                if status2 == "valid":
                    clear_hold(package)
                    rt = load_runtime()
                    rt_tab = get_runtime_tab(rt, package)
                    clear_manual_login_block(rt_tab)
                    save_runtime(rt)
                    print(col(f"  {package}: Solved and valid", GREEN))
                    return
                else:
                    print(col(f"[SOLVER] Still challenged after solving: {status2}", YELLOW))
                    set_hold(package, "challenge")
                    rt = load_runtime()
                    rt_tab = get_runtime_tab(rt, package)
                    rt_tab["manual_login_needed"] = True
                    rt_tab["manual_login_reason"] = "challenge after solve"
                    rt_tab["manual_login_detail"] = "solver failed to clear challenge"
                    rt_tab["manual_login_detected_at"] = now()
                    rt_tab["note"] = "needs manual login"
                    save_runtime(rt)
                    print(col(f"  {package}: CHALLENGE detected (placed on hold)", YELLOW))
                    return
            else:
                print(col(f"[SOLVER] Failed: {msg}", RED))
                set_hold(package, "challenge")
                rt = load_runtime()
                rt_tab = get_runtime_tab(rt, package)
                rt_tab["manual_login_needed"] = True
                rt_tab["manual_login_reason"] = "solver failed"
                rt_tab["manual_login_detail"] = msg
                rt_tab["manual_login_detected_at"] = now()
                rt_tab["note"] = "needs manual login"
                save_runtime(rt)
                print(col(f"  {package}: CHALLENGE detected (placed on hold)", YELLOW))
                return
        else:
            set_hold(package, "challenge")
            rt = load_runtime()
            rt_tab = get_runtime_tab(rt, package)
            rt_tab["manual_login_needed"] = True
            rt_tab["manual_login_reason"] = "api challenge"
            rt_tab["manual_login_detail"] = "challenge detected, solver disabled"
            rt_tab["manual_login_detected_at"] = now()
            rt_tab["note"] = "needs manual login"
            save_runtime(rt)
            print(col(f"  {package}: CHALLENGE detected (placed on hold)", YELLOW))
    elif status == "invalid":
        set_hold(package, "invalid")
        print(col(f"  {package}: Invalid/Expired cookie (placed on hold)", RED))
    else:
        print(col(f"  {package}: Unknown status: {status}", RED))

def start_rejoin(cfg):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False

    rt = load_runtime()
    session_start = now()
    loops = 0
    open_queue = []

    enabled_tabs = [t for t in cfg["tabs"] if t.get("enabled", True)]

    # Clean stale queued/loading runtime
    queue_stuck_self_heal(open_queue, cfg, rt)
    runtime_stuck_watchdog(open_queue, cfg, rt, enabled_tabs)

    if cfg.get("workspace_sync_enabled", False) and cfg.get("workspace_sync_on_start", True) and workspace_sync_allowed_for_mode(cfg):
        workspace_sync_screen(cfg, "start rejoin")
        ok, msg = run_workspace_sync(cfg, rt, reason="start rejoin", force=True)
        print(col(msg, GREEN if ok else YELLOW))
        time.sleep(1)

    # --------------------------------------------------------
    # SMART STARTUP
    # --------------------------------------------------------
    if cfg.get("open_all_on_start", True):
        if cfg.get("open_only_closed_on_start", True):
            stale_limit = int(cfg.get("state_stale_seconds", 180) or 180)
            tabs_to_open = []
            for tab in enabled_tabs:
                rt_tab = get_runtime_tab(rt, tab["package"])
                raw_alive = package_alive(tab["package"], cfg)
                fresh_state = state_recent_enough_for_alive(tab, cfg, seconds=stale_limit)

                if raw_alive or fresh_state:
                    rt_tab["note"] = "start alive - skipped"
                    save_runtime(rt)
                else:
                    tabs_to_open.append((tab, "hard", True))
        else:
            tabs_to_open = enabled_tabs

        if not tabs_to_open:
            clear()
            banner("START CHECK", cfg)
            print(col("All enabled packages are already open.", GREEN))
            print(col("Skipping start reopen.", DIM))
            time.sleep(1)
        else:
            for entry in tabs_to_open:
                if isinstance(entry, tuple):
                    tab, start_mode, start_skip_if_alive = entry
                else:
                    tab, start_mode, start_skip_if_alive = entry, "hard", cfg.get("open_only_closed_on_start", True)
                rt_tab = get_runtime_tab(rt, tab["package"])
                target = rt_tab.get("target", "market")
                queue_open(
                    open_queue,
                    tab,
                    target,
                    "start",
                    force=True,
                    skip_if_alive=bool(start_skip_if_alive),
                    mode=start_mode
                )

            while open_queue:
                if stop_requested():
                    save_runtime(rt)
                    return
                process_open_queue(open_queue, cfg, rt, session_start, loops)
                if open_queue:
                    if not wait_seconds(int(cfg.get("delay_between_open", 45)), rt):
                        return

    # --------------------------------------------------------
    # AUTO LOOP
    # --------------------------------------------------------
    while True:
        if stop_requested():
            save_runtime(rt)
            return

        loops += 1

        queue_stuck_self_heal(open_queue, cfg, rt)
        runtime_stuck_watchdog(open_queue, cfg, rt, enabled_tabs)
        poll_solver_jobs(cfg, rt, open_queue)

        if cfg.get("workspace_sync_enabled", False) and cfg.get("workspace_sync_periodic_enabled", False) and workspace_sync_allowed_for_mode(cfg):
            run_workspace_sync(cfg, rt, reason="periodic", force=False)

        # #1: one process-list snapshot per tick
        refresh_process_list(cfg, force=True)

        rows = []

        for tab_index, tab in enumerate(enabled_tabs):
            pkg = tab["package"]
            user = tab.get("user_name", pkg)
            rt_tab = get_runtime_tab(rt, pkg)
            ensure_scheduled_hop(tab, rt_tab, cfg, tab_index)

            # One-time orphan state cleanup per session
            if cfg.get("auto_delete_orphan_state", False) and not rt_tab.get("orphan_cleanup_done"):
                removed = cleanup_orphan_state_files(tab, cfg)
                if removed >= 0:
                    rt_tab["orphan_cleanup_done"] = True

            raw_alive = package_alive(pkg, cfg)
            state, err = read_state(tab)
            alive = raw_alive or (state is not None and not state_disconnect_ui(state) and int(state.get("age", 999999) or 999999) <= int(cfg.get("state_stale_seconds", 180) or 180))
            health = evaluate_package_health(
                tab, cfg, rt_tab, mode="market", raw_alive=raw_alive, state=state, err=err
            )

            pets = "-"
            eggs = "-"
            age = "-"
            note = health.get("note", "")
            status = health.get("status", "")
            target = rt_tab.get("target", "market")

            if state:
                pets = int(state.get("pet_count", 0) or 0)
                eggs = int(state.get("egg_total", 0) or 0)
                age = int(state.get("age", 999999) or 999999)
                if health.get("clean_fresh"):
                    clear_manual_login_block(rt_tab)
                    clear_captcha_ui_runtime(rt_tab)

            # -----------------------------------------------------
            # SHARED REJOIN ENGINE
            # -----------------------------------------------------
            rj_status, rj_note, rj_handled = apply_rejoin_action(
                open_queue, tab, target, rt_tab, cfg, rt, health, mode="market"
            )
            if rj_handled:
                status = rj_status or status
                note = rj_note or note

            # V3.84: provider probing is intentionally NOT done from the pool
            # dashboard. It runs once only after this package is actually opened.

            # -----------------------------------------------------
            # MARKET-ONLY: pet routing + scheduled hop.
            # -----------------------------------------------------
            if state and not rj_handled and health.get("clean_fresh"):
                status = "Ingame"
                note = note or "ok"

                last_pet = int(rt_tab.get("last_pet_count", -1))
                if last_pet >= 0 and pets < last_pet:
                    rt_tab["last_pet_drop_ts"] = now()
                if pets > last_pet:
                    rt_tab["last_gain_ts"] = now()
                rt_tab["last_pet_count"] = pets
                idle_no_gain = now() - int(rt_tab.get("last_gain_ts", now()))

                if pets < int(cfg["restock_below"]) and target != "restock":
                    added, _ = queue_open(open_queue, tab, "restock", f"pets<{cfg['restock_below']}")
                    note = "restock queued" if added else "already queued"
                    status = "Queued" if added else status
                elif pets >= int(cfg["ready_market_at"]) and target != "market":
                    added, _ = queue_open(open_queue, tab, "market", f"pets>={cfg['ready_market_at']}")
                    note = "market queued" if added else "already queued"
                    status = "Queued" if added else status
                elif (target == "restock" and pets >= int(cfg["idle_min_pet_to_market"])
                      and idle_no_gain >= int(cfg["idle_no_gain_seconds"])):
                    added, _ = queue_open(open_queue, tab, "market", "idle no gain")
                    note = "idle queued" if added else "already queued"
                    status = "Queued" if added else status

                # Scheduled server hop
                if (
                    cfg.get("scheduled_hop_enabled", False)
                    and cfg.get("soft_hop_enabled", True)
                    and not queue_has(open_queue, pkg)
                    and scheduled_hop_due(rt_tab, cfg)
                ):
                    delay_needed, delay_left = should_delay_hop_for_selling(rt_tab, cfg)
                    if delay_needed:
                        schedule_next_soft_hop(rt_tab, cfg, delay_left)
                        if note in ["ok", ""]:
                            note = f"hop delay {delay_left}s"
                    else:
                        added, _ = queue_open(open_queue, tab, target, "scheduled soft hop", mode="soft")
                        schedule_next_soft_hop(rt_tab, cfg)
                        if added and note in ["ok", ""]:
                            note = "hop queued"

            due_refresh, refresh_left = periodic_hard_refresh_due(rt_tab, cfg)
            if due_refresh and not rj_handled and not open_queue and not queue_has(open_queue, pkg) and not solver_job_running(pkg):
                if (not manual_login_blocked(rt_tab, cfg)) or cfg.get("periodic_hard_refresh_include_manual", True):
                    added, _ = queue_open(
                        open_queue, tab, target, "periodic hard refresh",
                        force=True, mode="hard_force", bypass_manual=True
                    )
                    if added:
                        mark_periodic_hard_refresh(rt_tab)
                        note = "periodic hard queued"
                        status = "Queued"
                elif note in ["ok", ""]:
                    note = f"refresh in {format_age(refresh_left)}"

            # V3.88: two disconnected packages may legitimately be queued at
            # once, but single-flight still processes only one. Show FIFO position
            # instead of making both look actively reopening.
            qpos = queue_position(open_queue, pkg)
            if qpos > 0:
                if qpos == 1:
                    status = "Next"
                    if "queued" not in str(note).lower():
                        note = f"next recovery; {note}".strip("; ")
                else:
                    status = "Waiting"
                    note = f"queue #{qpos}; {note}".strip("; ")

            update_clone_session(rt_tab, status, cfg)

            rows.append({
                "user": user,
                "pkg": pkg,
                "alive": "ON" if alive else "OFF",
                "pets": pets,
                "eggs": eggs,
                "status": status,
                "target": rt_tab.get("target", "market"),
                "age": age,
                "session": format_session(rt_tab),
                "note": note
            })

        # #2: persist runtime ONCE per tick
        save_runtime(rt)

        status_screen(rows, cfg, session_start, loops)

        if open_queue and cfg.get("smart_open_queue", True):
            if not wait_seconds(2, rt):
                return
            process_open_queue(open_queue, cfg, rt, session_start, loops)
            continue

        if not wait_seconds(int(cfg.get("check_interval", 10)), rt):
            return



# ============================================================
# HATCHING MODE / JSONBIN REPORTER
# ============================================================

HATCHER_PROFILE_KEYS = [
    "enabled", "package", "hatcher_name", "server_link", "state_file",
    "ready_mode", "ready_pet_count", "enable_egg_ready", "required_eggs", "status_when_not_ready"
]


def normalize_hatcher_profile(prof, idx=0):
    base = dict(DEFAULT_HATCHER_PROFILE)
    if idx < len(DEFAULT_HATCHER_PROFILES):
        base.update(DEFAULT_HATCHER_PROFILES[idx])
    if isinstance(prof, dict):
        base.update(prof)

    for k, v in DEFAULT_HATCHER_PROFILE.items():
        if k not in base:
            base[k] = v

    base["enabled"] = bool(base.get("enabled", True))
    base["package"] = str(base.get("package", f"premium.noka{chr(65+idx)}") or f"premium.noka{chr(65+idx)}")
    base["hatcher_name"] = str(base.get("hatcher_name", f"nomohatch{idx+1}") or f"nomohatch{idx+1}")
    base["server_link"] = str(base.get("server_link", "") or "")
    base["state_file"] = str(base.get("state_file", "") or "")

    mode = str(base.get("ready_mode", "pet_or_egg") or "pet_or_egg").lower().strip()
    if mode not in ["pet_only", "egg_only", "pet_or_egg", "pet_and_egg"]:
        mode = "pet_or_egg"
    base["ready_mode"] = mode

    try:
        base["ready_pet_count"] = int(base.get("ready_pet_count", 200))
    except Exception:
        base["ready_pet_count"] = 200

    base["enable_egg_ready"] = bool(base.get("enable_egg_ready", False))

    if not isinstance(base.get("required_eggs"), dict):
        base["required_eggs"] = {}

    base["status_when_not_ready"] = str(base.get("status_when_not_ready", "busy") or "busy")
    return base


def set_hold(package, reason):
    hold = {}
    hold_file = BASE_DIR / "captcha_hold.json"
    if hold_file.exists():
        try:
            with open(hold_file) as f:
                hold = json.load(f)
        except:
            pass
    hold[package] = {"reason": reason, "timestamp": time.time()}
    with open(hold_file, "w") as f:
        json.dump(hold, f, indent=2)

def clear_hold(package):
    hold_file = BASE_DIR / "captcha_hold.json"
    if hold_file.exists():
        try:
            with open(hold_file) as f:
                hold = json.load(f)
            if package in hold:
                del hold[package]
                with open(hold_file, "w") as f:
                    json.dump(hold, f, indent=2)
        except:
            pass

def clear_all_holds():
    hold_file = BASE_DIR / "captcha_hold.json"
    if hold_file.exists():
        try:
            os.remove(hold_file)
        except:
            pass

def is_on_hold(package):
    hold_file = BASE_DIR / "captcha_hold.json"
    if not hold_file.exists():
        return False
    try:
        with open(hold_file) as f:
            hold = json.load(f)
        return package in hold
    except Exception:
        return False

def get_hold_reason(package):
    hold_file = BASE_DIR / "captcha_hold.json"
    if not hold_file.exists():
        return None
    try:
        with open(hold_file) as f:
            hold = json.load(f)
        return hold.get(package, {}).get("reason")
    except:
        return None

def is_placeholder_value(v):
    v = str(v or "").strip()
    return (not v) or v.startswith("YOUR_") or v.startswith("PUT_") or v.startswith("PASTE_")


def hatcher_backend_missing(hcfg):
    if backend_provider(hcfg) == "cloudflare":
        return is_placeholder_value(hcfg.get("cloudflare_worker_url")) or is_placeholder_value(hcfg.get("cloudflare_secret"))
    return is_placeholder_value(hcfg.get("jsonbin_bin_id")) or is_placeholder_value(hcfg.get("jsonbin_api_key"))

def sync_hatcher_profiles_with_tabs(cfg, hcfg, create_missing=True):
    """Ensure every tab has a matching hatcher profile."""
    changed = False
    tabs = cfg.get("tabs", [])
    profiles = hcfg.get("hatchers", [])

    profile_map = {}
    for prof in profiles:
        pkg = prof.get("package")
        if pkg:
            profile_map[pkg] = prof

    new_profiles = []
    for tab in tabs:
        pkg = tab.get("package")
        if pkg in profile_map:
            prof = profile_map[pkg]
            if prof.get("enabled") != tab.get("enabled", True):
                prof["enabled"] = tab.get("enabled", True)
                changed = True
            new_profiles.append(prof)
            del profile_map[pkg]
        elif create_missing:
            new_prof = {
                "enabled": tab.get("enabled", True),
                "package": pkg,
                "hatcher_name": tab.get("user_name", pkg),
                "server_link": "YOUR_HATCHING_PRIVATE_SERVER_LINK",
                "state_file": tab.get("stat_file", ""),
                "ready_mode": "pet_only",
                "ready_pet_count": 200,
                "enable_egg_ready": False,
                "required_eggs": {},
                "status_when_not_ready": "busy"
            }
            new_profiles.append(new_prof)
            changed = True

    if profile_map or len(new_profiles) != len(profiles):
        changed = True

    if changed:
        hcfg["hatchers"] = new_profiles
        save_hatcher_config(hcfg)

    return changed


def hatcher_apply_main_backend_if_missing(hcfg, main_cfg=None, save=True):
    """Use the global JSONBin settings from main config for hatcher too."""
    if main_cfg is None:
        try:
            main_cfg = load_config()
        except Exception:
            main_cfg = {}

    changed = False
    for k, default in [
        ("backend_provider", "jsonbin"),
        ("jsonbin_bin_id", ""),
        ("jsonbin_api_key", ""),
        ("jsonbin_key_header", "X-Master-Key"),
        ("jsonbin_timeout_seconds", 10),
        ("cloudflare_worker_url", ""),
        ("cloudflare_secret", ""),
        ("cloudflare_timeout_seconds", 8),
    ]:
        val = main_cfg.get(k, default)
        if k == "jsonbin_timeout_seconds":
            val = main_cfg.get(k, hcfg.get(k, default))
        if hcfg.get(k) != val:
            hcfg[k] = val
            changed = True

    if not hcfg.get("jsonbin_key_header"):
        hcfg["jsonbin_key_header"] = "X-Master-Key"
        changed = True

    if changed and save:
        save_hatcher_config(hcfg)
    return changed

def load_hatcher_config():
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    if not _merged_section_exists(HATCHER_CONFIG_FILE):
        save_json(HATCHER_CONFIG_FILE, DEFAULT_HATCHER_CONFIG)

    cfg = load_json(HATCHER_CONFIG_FILE, DEFAULT_HATCHER_CONFIG)
    changed = False

    # Fill global keys
    for k, v in DEFAULT_HATCHER_CONFIG.items():
        if k == "hatchers":
            continue
        if k not in cfg:
            cfg[k] = v
            changed = True

    # Safety: hatcher_rejoin_alive_stale should be False
    if cfg.get("hatcher_rejoin_alive_stale", False) is not False:
        cfg["hatcher_rejoin_alive_stale"] = False
        changed = True

    # V3.78: force clean 5m old-state hard rule ON + fix max_valid_seconds typo 81400->86400
    if int(cfg.get("_nomo_hatcher_old_state_hard_force_migration", 0) or 0) < 378:
        cfg["hatcher_alive_old_state_hard_force_enabled"] = True
        cfg["hatcher_alive_old_state_hard_force_seconds"] = 300
        cfg["hatcher_alive_old_state_max_valid_seconds"] = 86400  # FIX: was 81400
        cfg["hatcher_alive_old_state_after_open_grace_seconds"] = 180
        cfg["hatcher_alive_old_state_hard_force_cooldown_seconds"] = 900
        cfg["_nomo_hatcher_old_state_hard_force_migration"] = 378
        changed = True

    # V3.64: keep Cloudflare writes low.
    if cfg.get("upload_disconnect_state", False) is not False:
        cfg["upload_disconnect_state"] = False
        changed = True

    # V3.51: public/private detection false-positive guard
    if int(cfg.get("_nomo_hatcher_migration_version", 0) or 0) < 351:
        cfg["detect_public_server"] = False
        cfg["_nomo_hatcher_migration_version"] = 351
        changed = True

    # Migrate old single-hatcher config into hatchers[0]
    if "hatchers" not in cfg or not isinstance(cfg.get("hatchers"), list):
        old = {}
        for k in HATCHER_PROFILE_KEYS:
            if k in cfg:
                old[k] = cfg.get(k)
        if old:
            cfg["hatchers"] = [normalize_hatcher_profile(old, 0)]
        else:
            cfg["hatchers"] = [dict(x) for x in DEFAULT_HATCHER_PROFILES]
        changed = True

    profiles = []
    for i, prof in enumerate(cfg.get("hatchers", [])):
        normalized = normalize_hatcher_profile(prof, i)
        old_state_path = str(normalized.get("state_file", "") or "")
        if f"/{LEGACY_STATE_FOLDER_NAME}/" in old_state_path:
            normalized["state_file"] = old_state_path.replace(
                f"/{LEGACY_STATE_FOLDER_NAME}/", f"/{STATE_FOLDER_NAME}/"
            )
        profiles.append(normalized)

    if not profiles:
        profiles = [dict(x) for x in DEFAULT_HATCHER_PROFILES]
        changed = True

    if profiles != cfg.get("hatchers"):
        cfg["hatchers"] = profiles
        changed = True

    # If hatcher backend is blank/placeholder, reuse main/market JSONBin config automatically.
    if hatcher_backend_missing(cfg):
        try:
            if hatcher_apply_main_backend_if_missing(cfg, None, save=False):
                changed = True
        except Exception:
            pass

    if changed:
        save_json(HATCHER_CONFIG_FILE, cfg)

    return cfg


def save_hatcher_config(hcfg):
    save_json(HATCHER_CONFIG_FILE, hcfg)


def hatcher_profiles(hcfg, enabled_only=False):
    """Return normalized REAL profile dictionaries from hcfg.

    Before V3.80 this returned detached copies. Callers changed the copy to the
    detected username, then saved the untouched hcfg, so names always reverted
    to nomohatch1/2/3/4. Normalization is now applied in place and every returned
    profile is the object that will actually be written to nomo.json.
    """
    raw_profiles = hcfg.get("hatchers", [])
    if not isinstance(raw_profiles, list):
        raw_profiles = []
        hcfg["hatchers"] = raw_profiles

    out = []
    for i, prof in enumerate(raw_profiles):
        normalized = normalize_hatcher_profile(prof, i)
        if isinstance(prof, dict):
            if prof != normalized:
                prof.clear()
                prof.update(normalized)
            p = prof
        else:
            raw_profiles[i] = normalized
            p = normalized

        if enabled_only and not p.get("enabled", True):
            continue
        out.append(p)
    return out


def hatcher_effective(hcfg, prof):
    eff = dict(hcfg)
    eff.update(prof)
    return eff


def hatcher_state_tab(hcfg_or_prof):
    return {
        "user_name": hcfg_or_prof.get("hatcher_name", "hatcher"),
        "stat_file": hcfg_or_prof.get("state_file", "")
    }


def hatcher_read_state(hcfg_or_prof):
    return read_state(hatcher_state_tab(hcfg_or_prof))


def jsonbin_update_bin(cfg, record):
    bin_id = str(cfg.get("jsonbin_bin_id", "")).strip()

    if is_placeholder_value(bin_id):
        return False, "missing bin id"

    timeout = int(cfg.get("jsonbin_timeout_seconds", 10))
    url = f"https://api.jsonbin.io/v3/b/{bin_id}"
    body = json.dumps(record).encode("utf-8")
    headers = jsonbin_headers(cfg, write=True)
    headers["X-Bin-Versioning"] = "false"

    try:
        req = urllib.request.Request(url, data=body, method="PUT", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, "updated"

    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = str(e)
        return False, f"http {e.code}: {cut(body, 180)}"

    except Exception as e:
        return False, str(e)



# ============================================================
# GENERIC HATCHER BACKEND (JSONBIN / CLOUDFLARE)
# ============================================================

def backend_provider(cfg):
    p = str(cfg.get("backend_provider", "jsonbin") or "jsonbin").strip().lower()
    if p in ["cf", "worker", "cloudflare_worker"]:
        p = "cloudflare"
    if p not in ["jsonbin", "cloudflare"]:
        p = "jsonbin"
    return p



def cloudflare_base_url(cfg):
    url = str(cfg.get("cloudflare_worker_url", "") or "").strip().rstrip("/")

    for suffix in [
        "/hatcher/update", "/hatchers", "/accounts/register", "/accounts",
        "/admin/stats", "/admin/cleanup-stale", "/health"
    ]:
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")

    return url


def cloudflare_headers(cfg):
    secret = str(cfg.get("cloudflare_secret", "") or "").strip()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "nomo-rejoin/cloudflare"
    }
    if secret:
        headers["X-Nomo-Secret"] = secret
        headers["x-nomo-secret"] = secret
    return headers


def cloudflare_request(cfg, method, path, payload=None):
    base = cloudflare_base_url(cfg)
    if is_placeholder_value(base):
        return None, "missing Cloudflare worker url"

    secret = str(cfg.get("cloudflare_secret", "") or "").strip()
    if is_placeholder_value(secret):
        return None, "missing Cloudflare secret"

    timeout = int(cfg.get("cloudflare_timeout_seconds", cfg.get("jsonbin_timeout_seconds", 8)) or 8)
    url = base + path

    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=cloudflare_headers(cfg)
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")

        if not text.strip():
            return {}, None

        return json.loads(text), None

    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = str(e)
        return None, f"http {e.code}: {cut(text, 180)}"

    except Exception as e:
        return None, str(e)


def cloudflare_register_accounts(cfg, accounts):
    payload = {"accounts": list(accounts or [])}
    data, err = cloudflare_request(cfg, "POST", "/accounts/register", payload)
    if err:
        return False, err, {}
    if not isinstance(data, dict) or data.get("ok") is False:
        return False, str((data or {}).get("error") or "worker error"), data or {}
    return True, "registered", data


def cloudflare_read_accounts(cfg, role="market"):
    role_q = urllib.parse.quote(str(role or "market"), safe="")
    data, err = cloudflare_request(cfg, "GET", f"/accounts?role={role_q}&limit=1000")
    if err:
        return None, err
    if not isinstance(data, dict) or data.get("ok") is False:
        return None, str((data or {}).get("error") or "bad accounts response")
    raw = data.get("accounts", [])
    if not isinstance(raw, list):
        return None, "bad accounts format"
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "").strip()
        user_id = str(item.get("user_id") or item.get("userId") or "").strip()
        if not username or not user_id.isdigit() or user_id in seen:
            continue
        seen.add(user_id)
        out.append({
            "username": username,
            "user_id": user_id,
            "role": str(item.get("role") or role),
            "package_name": str(item.get("package_name") or ""),
        })
    return out, None



def cloudflare_admin_stats(cfg, stale_after_seconds=7200):
    stale = max(30, int(stale_after_seconds or 7200))
    data, err = cloudflare_request(cfg, "GET", f"/admin/stats?stale_after_seconds={stale}")
    if err:
        return None, err
    if not isinstance(data, dict) or data.get("ok") is False:
        return None, str((data or {}).get("error") or "bad admin stats response")
    return data, None


def cloudflare_admin_delete(cfg, kind, username):
    kind = "account" if str(kind).lower().startswith("account") else "hatcher"
    encoded = urllib.parse.quote(str(username or "").strip(), safe="")
    if not encoded:
        return False, "missing username", {}
    data, err = cloudflare_request(cfg, "DELETE", f"/admin/{kind}/{encoded}")
    if err:
        return False, err, {}
    if not isinstance(data, dict) or data.get("ok") is False:
        return False, str((data or {}).get("error") or "delete failed"), data or {}
    return True, "deleted" if data.get("deleted") else str(data.get("reason") or "not found"), data


def cloudflare_admin_cleanup(cfg, target="hatchers", older_than_seconds=30 * 86400,
                             dry_run=True, confirm=""):
    payload = {
        "target": str(target or "hatchers"),
        "older_than_seconds": max(86400, int(older_than_seconds or 30 * 86400)),
    }
    if not dry_run:
        payload["confirm"] = str(confirm or "")
    suffix = "?dry_run=1" if dry_run else ""
    data, err = cloudflare_request(cfg, "POST", "/admin/cleanup-stale" + suffix, payload)
    if err:
        return None, err
    if not isinstance(data, dict) or data.get("ok") is False:
        return None, str((data or {}).get("error") or "cleanup failed")
    return data, None


def cloudflare_admin_hatchers(cfg, stale_after_seconds=7200):
    stale = max(30, int(stale_after_seconds or 7200))
    path = f"/hatchers?include_disabled=1&limit=1000&stale_after_seconds={stale}"
    data, err = cloudflare_request(cfg, "GET", path)
    if err:
        return None, err
    if not isinstance(data, dict) or data.get("ok") is False:
        return None, str((data or {}).get("error") or "bad hatchers response")
    rows = data.get("hatchers", [])
    if not isinstance(rows, list):
        return None, "bad hatchers format"
    return rows, None


def cloudflare_admin_accounts(cfg, role=""):
    role_q = urllib.parse.quote(str(role or ""), safe="")
    path = f"/accounts?limit=1000" + (f"&role={role_q}" if role_q else "")
    data, err = cloudflare_request(cfg, "GET", path)
    if err:
        return None, err
    if not isinstance(data, dict) or data.get("ok") is False:
        return None, str((data or {}).get("error") or "bad accounts response")
    rows = data.get("accounts", [])
    if not isinstance(rows, list):
        return None, "bad accounts format"
    return rows, None


def _cloudflare_item_updated_seconds(item):
    if not isinstance(item, dict):
        return 0

    try:
        ms = int(item.get("updated_at_ms", 0) or 0)
        if ms > 0:
            return ms // 1000
    except Exception:
        pass

    try:
        val = item.get("updated_at", 0)
        if isinstance(val, (int, float)) or str(val).isdigit():
            n = int(val)
            return n // 1000 if n > 9999999999 else n
    except Exception:
        pass

    return 0


def _cloudflare_hatcher_key(item):
    if not isinstance(item, dict):
        return ""

    for key in ["name", "hatcher_name", "username", "username_key", "roblox_username"]:
        value = str(item.get(key, "") or "").strip()
        if value:
            return value

    return ""


def cloudflare_normalize_hatchers_response(data):
    if not isinstance(data, dict):
        return None, "bad Cloudflare response"

    raw = data.get("hatchers", {})

    if isinstance(raw, dict):
        hatchers = {}
        for name, item in raw.items():
            if not isinstance(item, dict):
                continue
            h = dict(item)
            h.setdefault("name", str(name))
            h["updated_at"] = _cloudflare_item_updated_seconds(h) or int(h.get("updated_at", 0) or 0)
            hatchers[str(name)] = h
        out = dict(data)
        out["hatchers"] = hatchers
        return out, None

    if isinstance(raw, list):
        hatchers = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            h = dict(item)
            name = _cloudflare_hatcher_key(h)
            if not name:
                continue
            h.setdefault("name", name)
            h["updated_at"] = _cloudflare_item_updated_seconds(h)
            hatchers[name] = h

        out = dict(data)
        out["hatchers"] = hatchers
        return out, None

    return None, "bad Cloudflare hatchers format"


def cloudflare_read_hatchers(cfg):
    data, err = cloudflare_request(cfg, "GET", "/hatchers")
    if err:
        return None, err

    return cloudflare_normalize_hatchers_response(data)


def cloudflare_update_one_hatcher(cfg, name, entry):
    name = str(name or "hatcher").strip() or "hatcher"
    payload = dict(entry or {})

    roblox_username = str(payload.get("username", "") or "").strip()

    payload["username"] = name
    payload["hatcher_name"] = name
    payload["name"] = name
    payload["mode"] = "hatcher"

    if roblox_username and roblox_username != name:
        payload["roblox_username"] = roblox_username

    data, err = cloudflare_request(cfg, "POST", "/hatcher/update", payload)
    if err:
        return False, err

    if isinstance(data, dict) and data.get("ok") is False:
        return False, str(data.get("error") or "worker error")

    return True, data


def cloudflare_update_removed_hatcher(cfg, name, status="disabled"):
    name = str(name or "hatcher").strip() or "hatcher"

    payload = {
        "username": name,
        "hatcher_name": name,
        "name": name,
        "mode": "hatcher",
        "enabled": False,
        "status": str(status or "disabled"),
        "server_link": "",
        "pet_count": 0,
        "egg_total": 0,
        "eggs": {},
        "updated_at": now(),
        "updated_text": date_time_text(),
        "source": "nomo_rejoin_removed_hatcher"
    }

    data, err = cloudflare_request(cfg, "POST", "/hatcher/update", payload)
    if err:
        return False, err

    if isinstance(data, dict) and data.get("ok") is False:
        return False, str(data.get("error") or "worker error")

    return True, data


def cloudflare_update_hatchers(cfg, updates, removes):
    sent = 0
    removed = 0

    for name, entry in updates or []:
        ok, msg = cloudflare_update_one_hatcher(cfg, name, entry)
        if not ok:
            return False, f"{name}: {msg}"
        sent += 1

    for item in removes or []:
        if isinstance(item, (list, tuple)) and item:
            name = item[0]
            status = item[1] if len(item) > 1 else "disabled"
        else:
            name = item
            status = "disabled"

        ok, msg = cloudflare_update_removed_hatcher(cfg, name, status)
        if not ok:
            return False, f"remove {name}: {msg}"
        removed += 1

    return True, f"updated cloudflare sent={sent} removed={removed}"


def backend_read_hatchers(cfg):
    if backend_provider(cfg) == "cloudflare":
        return cloudflare_read_hatchers(cfg)
    return jsonbin_read_bin(cfg)


def backend_health_check(cfg):
    if not cfg.get("jsonbin_hatchers_enabled", False):
        return False, "backend disabled"

    if backend_provider(cfg) == "cloudflare":
        data, err = cloudflare_request(cfg, "GET", "/health")
        if err:
            return False, err

        if isinstance(data, dict):
            kv = "yes" if data.get("kv_bound") else "no"
            sec = "yes" if data.get("secret_configured") else "no"
            return True, f"cloudflare ok version={data.get('version', '')} kv={kv} secret={sec}"

        return True, "cloudflare ok"

    data, err = jsonbin_read_bin(cfg)
    if err:
        return False, err

    hatchers = data.get("hatchers", {}) if isinstance(data, dict) else {}
    return True, f"jsonbin ok hatchers={len(hatchers) if isinstance(hatchers, dict) else 0}"

def norm_egg_name(name):
    return str(name or "").strip().lower()


def hatcher_required_eggs_ok(hcfg, state):
    required = hcfg.get("required_eggs", {})
    if not isinstance(required, dict) or not required:
        return False, [], []

    eggs = state.get("eggs", {}) if isinstance(state, dict) else {}
    if not isinstance(eggs, dict):
        eggs = {}

    egg_lookup = {norm_egg_name(k): int(v or 0) for k, v in eggs.items()}
    matched = []
    missing = []

    for egg_name, need in required.items():
        try:
            need_i = int(need)
        except Exception:
            need_i = 1
        need_i = max(1, need_i)
        have_i = int(egg_lookup.get(norm_egg_name(egg_name), 0) or 0)

        row = {"name": str(egg_name), "have": have_i, "need": need_i}
        if have_i >= need_i:
            matched.append(row)
        else:
            missing.append(row)

    return len(missing) == 0, matched, missing


def hatcher_ready_eval(hcfg, state):
    """Simple hatcher eval."""
    pets = int(state.get("pet_count", 0)) if state else 0
    ready_at = int(hcfg.get("ready_pet_count", 200))
    pet_ok = bool(state) and pets >= ready_at

    return {
        "ready": bool(state),
        "mode": "report_only",
        "pets": pets,
        "ready_at": ready_at,
        "pet_ok": pet_ok,
        "egg_ok": False,
        "ready_by_egg": False,
        "ready_reason": "online" if state else "no_state",
        "matched_eggs": [],
        "missing_eggs": [],
    }


def hatcher_entry(hcfg, state, err):
    evald = hatcher_ready_eval(hcfg, state)
    pets = evald["pets"]
    ready_at = evald["ready_at"]

    server_link = str(hcfg.get("server_link", "") or "").strip()

    if not hcfg.get("enabled", True):
        status = "disabled"
    elif not server_link or server_link.startswith("YOUR_") or server_link.startswith("PUT_"):
        status = "no_server"
    elif state:
        status = "online"
    else:
        status = "no_state"

    entry = {
        "enabled": bool(hcfg.get("enabled", True)),
        "package": hcfg.get("package", ""),
        "name": hcfg.get("hatcher_name", ""),
        "status": status,
        "server_link": server_link,
        "pet_count": pets,
        "ready_pet_count": ready_at,
        "ready_mode": "report_only",
        "enable_egg_ready": False,
        "ready_reason": evald["ready_reason"],
        "ready_by_pet": bool(evald["pet_ok"]),
        "ready_by_egg": False,
        "required_eggs": {},
        "matched_eggs": [],
        "missing_eggs": [],
        "updated_at": now(),
        "updated_text": date_time_text(),
        "source": "nomo_rejoin_hatcher_report_only"
    }

    if state:
        entry["username"] = state.get("username")
        entry["place_id"] = state.get("place_id")
        entry["job_id"] = state.get("job_id", "")
        entry["private_server_id"] = state.get("private_server_id", "")
        entry["private_server_known"] = state.get("private_server_known", False)
        entry["is_private_server"] = state.get("is_private_server", None)
        entry["server_type"] = state.get("server_type", "")
        entry["state_age"] = state.get("age")
        entry["state_ts"] = state.get("ts")
        entry["egg_total"] = int(state.get("egg_total", 0) or 0)
        entry["eggs"] = state.get("eggs", {}) if isinstance(state.get("eggs", {}), dict) else {}
    else:
        entry["error"] = str(err)
        entry["egg_total"] = 0
        entry["eggs"] = {}

    return entry


def hatcher_signature(hcfg, entry):
    """Low-request signature for hatcher report-only mode."""
    try:
        pets = int(entry.get("pet_count", 0) or 0)
    except Exception:
        pets = 0

    try:
        market_threshold = int(hcfg.get("upload_pet_threshold", hcfg.get("jsonbin_min_hatcher_pets", 100)) or 100)
    except Exception:
        market_threshold = 100

    sig = {
        "enabled": bool(entry.get("enabled", True)),
        "package": entry.get("package"),
        "name": entry.get("name"),
        "status": entry.get("status"),
        "server_link": entry.get("server_link"),
        "market_pet_bucket": "usable" if pets >= market_threshold else "low",
        "market_pet_threshold": market_threshold,
    }
    return json.dumps(sig, sort_keys=True, separators=(",", ":"))


def load_hatcher_runtime():
    rt = load_json(HATCHER_RUNTIME_FILE, {})
    if not isinstance(rt.get("last_update_ts"), dict):
        rt["last_update_ts"] = {}
    if not isinstance(rt.get("last_signature"), dict):
        old_sig = rt.get("last_signature", "") if isinstance(rt.get("last_signature"), str) else ""
        rt["last_signature"] = {"__old__": old_sig} if old_sig else {}
    if not isinstance(rt.get("last_status"), dict):
        rt["last_status"] = {}
    if not isinstance(rt.get("last_reason"), dict):
        rt["last_reason"] = {}
    return rt


def save_hatcher_runtime(rt):
    save_json(HATCHER_RUNTIME_FILE, rt)


def hatcher_should_update(hcfg, profile_name, entry, force=False):
    if force:
        return True, "forced"

    if not hcfg.get("update_only_on_status_change", True):
        return True, "heartbeat"

    rt = load_hatcher_runtime()
    sig = hatcher_signature(hcfg, entry)
    last_sig = str(rt.get("last_signature", {}).get(profile_name, ""))
    last_ts = int(rt.get("last_update_ts", {}).get(profile_name, 0) or 0)
    force_every = int(hcfg.get("force_update_every_seconds", 3600) or 3600)

    if sig != last_sig:
        return True, "status changed"

    if now() - last_ts >= force_every:
        return True, "forced heartbeat"

    left = max(0, force_every - (now() - last_ts))
    return False, f"skip unchanged, next forced in {format_age(left)}"


def update_hatcher_runtime(profile_name, hcfg, entry):
    rt = load_hatcher_runtime()
    rt["last_update_ts"][profile_name] = now()
    rt["last_signature"][profile_name] = hatcher_signature(hcfg, entry)
    rt["last_status"][profile_name] = entry.get("status")
    rt["last_reason"][profile_name] = entry.get("ready_reason")
    save_hatcher_runtime(rt)


def hatcher_report_once(hcfg, force=True, state_cache=None):
    hatcher_apply_main_backend_if_missing(hcfg, None, save=True)

    if not hcfg.get("enabled", True):
        return False, "hatching mode disabled"

    if hatcher_backend_missing(hcfg):
        return False, "Backend missing: set Global backend config"

    profiles = hatcher_profiles(hcfg, enabled_only=False)
    if not profiles:
        return False, "no hatcher profiles"

    updates = []
    removes = []
    skips = []

    for prof in profiles:
        eff = hatcher_effective(hcfg, prof)
        name = str(eff.get("hatcher_name", "hatcher")).strip() or "hatcher"
        pkg = str(eff.get("package", "") or "")
        cached = state_cache.get(pkg) if isinstance(state_cache, dict) else None
        if isinstance(cached, tuple) and len(cached) == 2:
            state, state_err = cached
        else:
            state, state_err = hatcher_read_state(eff)
        entry = hatcher_entry(eff, state, state_err)
        status = str(entry.get("status", "")).lower()

        # Do not publish template/incomplete hatcher slots.
        if status in ["disabled", "no_server", "no_state"]:
            removes.append((name, status))
            skips.append(f"{name}:{status}")
            continue

        should, why = hatcher_should_update(hcfg, name, entry, force=force)
        if should:
            updates.append((name, eff, entry, why))
        else:
            skips.append(f"{name}:{entry.get('status')}")

    if not updates and not removes:
        return True, "no update: " + (", ".join(skips[:4]) or "unchanged")

    provider = backend_provider(hcfg)

    if provider == "cloudflare":
        update_pairs = [(name, entry) for name, eff, entry, why in updates]
        remove_names = [name for name, status in removes]
        if not update_pairs and not remove_names:
            return True, "no upload: incomplete templates skipped (" + (", ".join(skips[:4]) or "none") + ")"
        ok, msg = cloudflare_update_hatchers(hcfg, update_pairs, remove_names)
        if not ok:
            return False, f"cloudflare update: {msg}"

        for name, eff, entry, why in updates:
            update_hatcher_runtime(name, hcfg, entry)

        parts = []
        for name, eff, entry, why in updates[:4]:
            parts.append(f"{name} {entry.get('status')} pets={entry.get('pet_count')} eggs={entry.get('egg_total')}")
        if len(updates) > 4:
            parts.append(f"+{len(updates)-4}")
        if remove_names:
            parts.append("removed " + ",".join(remove_names[:4]))
        return True, f"cloudflare updated {len(updates)} removed {len(remove_names)} ({', '.join(parts)})"

    # JSONBin fallback/path: read-modify-write full record.
    record, read_err = jsonbin_read_bin(hcfg)
    if read_err:
        return False, f"jsonbin read: {read_err}"

    if not isinstance(record, dict):
        record = {}

    if "hatchers" not in record or not isinstance(record.get("hatchers"), dict):
        record["hatchers"] = {}

    removed_names = []
    for name, status in removes:
        if name in record["hatchers"]:
            del record["hatchers"][name]
            removed_names.append(name)

    for name, eff, entry, why in updates:
        record["hatchers"][name] = entry

    if not updates and not removed_names:
        return True, "no upload: incomplete templates skipped (" + (", ".join(skips[:4]) or "none") + ")"

    record["updated_at"] = now()
    record["updated_text"] = date_time_text()

    ok, msg = jsonbin_update_bin(hcfg, record)
    if not ok:
        return False, f"jsonbin update: {msg}"

    for name, eff, entry, why in updates:
        update_hatcher_runtime(name, hcfg, entry)

    parts = []
    for name, eff, entry, why in updates[:4]:
        parts.append(f"{name} {entry.get('status')} pets={entry.get('pet_count')} eggs={entry.get('egg_total')}")
    if len(updates) > 4:
        parts.append(f"+{len(updates)-4}")
    if removed_names:
        parts.append("removed " + ",".join(removed_names[:4]))

    return True, f"jsonbin updated {len(updates)} removed {len(removed_names)} ({', '.join(parts)})"

def hatcher_test_upload_screen(main_cfg=None):
    hcfg = load_hatcher_config()
    copied_backend = hatcher_apply_main_backend_if_missing(hcfg, main_cfg, save=True)
    clear()
    banner("HATCHER FORCE UPLOAD TEST", main_cfg)
    print(f"Config : {HATCHER_CONFIG_FILE}")
    print(f"Provider: {backend_provider(hcfg)}")
    if backend_provider(hcfg) == "cloudflare":
        print(f"Worker : {hcfg.get('cloudflare_worker_url')}")
        print(f"Secret : {mask_secret(hcfg.get('cloudflare_secret'))}")
    else:
        print(f"Bin ID : {hcfg.get('jsonbin_bin_id')}")
        print(f"Key    : {mask_secret(hcfg.get('jsonbin_api_key'))}")
        print(f"Header : {hcfg.get('jsonbin_key_header')}")
    if copied_backend:
        print(col("Backend copied from market config automatically.", GREEN))
    print("")

    rows = []
    names = []
    for p in hatcher_profiles(hcfg, enabled_only=False):
        eff = hatcher_effective(hcfg, p)
        name = str(eff.get("hatcher_name", "hatcher")).strip() or "hatcher"
        names.append(name)
        state, err = hatcher_read_state(eff)
        if state:
            rows.append([
                (name, CYAN),
                (short_pkg(eff.get("package", "")), WHITE),
                ("YES" if eff.get("server_link") else "NO", GREEN if eff.get("server_link") else RED),
                (str(state.get("pet_count", 0)), GREEN, True),
                (str(state.get("egg_total", 0)), GREEN, True),
                (format_age(state.get("age", 0)), DIM),
            ])
        else:
            rows.append([
                (name, CYAN),
                (short_pkg(eff.get("package", "")), WHITE),
                ("YES" if eff.get("server_link") else "NO", GREEN if eff.get("server_link") else RED),
                ("-", RED),
                ("-", RED),
                (cut(str(err), 12), RED),
            ])

    draw_table(["Name", "Pkg", "Server", "Pet", "Egg", "Age/Err"], rows, [13, 8, 6, 5, 5, 12], main_cfg or {"ui_width": 80, "use_color": True}, border=CYAN)
    print("")
    print(col(f"Sending FORCE update to {backend_provider(hcfg)}...", YELLOW))

    ok, msg = hatcher_report_once(hcfg, force=True)
    print((col("UPLOAD OK", GREEN) if ok else col("UPLOAD ERR", RED)) + f": {msg}")

    if ok:
        print(col(f"Reading back {backend_provider(hcfg)} to verify...", YELLOW))
        record, err = backend_read_hatchers(hcfg)
        if err:
            print(col(f"READBACK ERR: {err}", RED))
        else:
            hatchers = record.get("hatchers", {}) if isinstance(record, dict) else {}
            found = []
            missing = []
            for name in names:
                if isinstance(hatchers, dict) and name in hatchers:
                    h = hatchers.get(name, {})
                    found.append(f"{name}:pets={h.get('pet_count')} eggs={h.get('egg_total')}")
                else:
                    missing.append(name)
            if found:
                print(col("FOUND: " + " | ".join(found[:4]), GREEN))
            if missing:
                print(col("MISSING: " + ", ".join(missing), RED))
            if not missing:
                print(col(f"Verified: {backend_provider(hcfg)} has the hatcher data.", GREEN))

    print("")
    if backend_provider(hcfg) == "jsonbin":
        print(col("If upload says OK but dashboard still looks old, refresh JSONBin page or open latest record.", DIM))
    else:
        print(col("If upload says OK, Market can now read /hatchers from the Worker.", DIM))
    pause()


def short_eggs_text(eggs, limit=3):
    if not isinstance(eggs, dict) or not eggs:
        return "{}"
    items = sorted(eggs.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))
    parts = [f"{k}:{v}" for k, v in items[:limit]]
    if len(items) > limit:
        parts.append(f"+{len(items)-limit}")
    return ", ".join(parts)


def hatcher_profile_status_row(hcfg, prof):
    eff = hatcher_effective(hcfg, prof)
    state, err = hatcher_read_state(eff)
    entry = hatcher_entry(eff, state, err)
    rt = load_hatcher_runtime()
    name = eff.get("hatcher_name", "hatcher")
    last_ts = int(rt.get("last_update_ts", {}).get(name, 0) or 0)
    last = format_age(now() - last_ts) if last_ts else "never"
    age = format_age(state.get("age")) if state else "no state"
    eggs = short_eggs_text(entry.get("eggs", {}), limit=2)
    status = entry.get("status", "?")
    status_color = GREEN if status == "ready" else (RED if status in ["disabled", "no_server"] else YELLOW)

    return [
        (name, CYAN),
        (cut(eff.get("package", ""), 12), WHITE),
        (str(entry.get("pet_count", 0)), GREEN if entry.get("ready_by_pet") else WHITE, True),
        (str(entry.get("egg_total", 0)), GREEN if entry.get("ready_by_egg") else WHITE, True),
        (cut(status, 10), status_color),
        (age, DIM),
        (last, DIM),
    ], eggs


def hatcher_status_screen(hcfg, last_msg=""):
    clear()
    banner("HATCHING MODE", None, CYAN)
    rt = load_hatcher_runtime()
    print("")
    print(f"Config : {col(str(HATCHER_CONFIG_FILE), DIM)}")
    print(f"Enable : {col(str(hcfg.get('enabled')), GREEN if hcfg.get('enabled') else RED)}")
    print(f"Check  : {hcfg.get('heartbeat_interval')}s | Force update: {hcfg.get('force_update_every_seconds')}s")
    if backend_provider(hcfg) == "cloudflare":
        print(f"Worker : {cut(hcfg.get('cloudflare_worker_url'), 50)}")
        print(f"Secret : {mask_secret(hcfg.get('cloudflare_secret'))}")
    else:
        print(f"Bin ID : {hcfg.get('jsonbin_bin_id')}")
        print(f"Key    : {mask_secret(hcfg.get('jsonbin_api_key'))}")
    print("")

    rows = []
    egg_notes = []
    for prof in hatcher_profiles(hcfg):
        row, eggs = hatcher_profile_status_row(hcfg, prof)
        rows.append(row)
        if eggs != "{}":
            egg_notes.append(f"{prof.get('hatcher_name')}: {eggs}")

    draw_table(["Name", "Package", "Pet", "Egg", "Status", "Age", "LastUp"], rows, [12, 12, 4, 4, 10, 8, 8], {"ui_width": 80, "use_color": True}, border=CYAN)

    if egg_notes:
        print(col("Eggs: " + " | ".join(egg_notes[:3]), DIM))

    if last_msg:
        print(f"Last : {last_msg}")

    print("")
    print(col("Type Q + ENTER to stop / return to Hatching Mode menu", DIM))


def hatcher_profile_to_tab(prof):
    return {
        "enabled": prof.get("enabled", True),
        "package": prof.get("package", ""),
        "user_name": prof.get("hatcher_name", prof.get("package", "hatcher")),
        "stat_file": prof.get("state_file", ""),
        "server_link": prof.get("server_link", ""),
        "restock_link": prof.get("server_link", ""),
    }


def hatcher_rejoin_status_screen(rows, hcfg, cfg, session_start, loops, last_msg=""):
    clear()
    w = term_width(cfg)
    banner("HATCHER MODE", cfg, CYAN)
    up = format_uptime(now() - session_start)
    print(f"  {col('UPTIME', DIM)} : {col(up, CYAN)}   {col('CHECKS', DIM)} : {loops}")
    if backend_provider(hcfg) == "cloudflare":
        print(f"  {col('BACKEND', DIM)}: cloudflare   {col('URL', DIM)}: {cut(hcfg.get('cloudflare_worker_url'), 36)}   {col('KEY', DIM)}: {mask_secret(hcfg.get('cloudflare_secret'))}")
    else:
        print(f"  {col('BACKEND', DIM)}: jsonbin   {col('BIN', DIM)}: {hcfg.get('jsonbin_bin_id')}   {col('KEY', DIM)}: {mask_secret(hcfg.get('jsonbin_api_key'))}")
    print(f"  {col('REPORT', DIM)} : status-change only={hcfg.get('update_only_on_status_change')} force={format_age(hcfg.get('force_update_every_seconds'))}")
    print("")

    widths = [2,  13,   10,   4,  4,  9,     6,  7,   18]
    table_overhead = (len(widths) + 1) + (2 * len(widths))
    spare = max(0, w - table_overhead - sum(widths))
    widths[-1] = max(widths[-1], min(40, widths[-1] + spare))

    table_rows = []
    for i, r in enumerate(rows, 1):
        status = r.get("status", "?")
        if status in ["Ready", "Ingame", "Online"]:
            st_code = GREEN
        elif status in ["Queued", "Loading", "Stale"]:
            st_code = YELLOW
        elif status in ["Offline", "No state", "No server", "Manual"]:
            st_code = RED
        else:
            st_code = WHITE

        table_rows.append([
            (str(i), CYAN),
            (r.get("user", ""), WHITE),
            (short_pkg(r.get("pkg", "")), WHITE),
            (str(r.get("pets", "-")), GREEN if r.get("ready_pet") else WHITE, True),
            (str(r.get("eggs", "-")), WHITE, True),
            (status, st_code),
            (format_age(r.get("age")), DIM),
            (str(r.get("session", "-")), CYAN),
            (cut(r.get("note", ""), widths[-1]), note_color(r.get("note", ""))),
        ])

    draw_table(["No", "Username", "Package", "Pet", "Egg", "Status", "Age", "Session", "Note"],
               table_rows, widths, cfg)

    render_activity_log(cfg, lines=int(cfg.get("activity_log_lines", 6) or 6))

    if last_msg:
        print("")
        print(f"  Last upload: {cut(last_msg, max(30, w - 15))}")

    print("")
    print(col("  Hatcher mode only keeps packages alive on their own server_link and uploads all pets/eggs.", DIM))
    print(col("  Type Q + ENTER to stop / return to Hatching Mode menu", DIM))


def start_hatcher_reporter(main_cfg=None):
    """Hatcher mode loop."""
    cfg = dict(load_config())
    if isinstance(main_cfg, dict):
        cfg.update(main_cfg)

    cfg["ignore_alive_stale_state"] = True
    cfg["no_force_stop_alive"] = True
    cfg["soft_hop_fallback_hard"] = False

    hcfg = load_hatcher_config()
    hcfg["hatcher_rejoin_alive_stale"] = False
    save_hatcher_config(hcfg)
    rt = load_runtime()
    session_start = now()
    loops = 0
    open_queue = []
    last_msg = "not uploaded yet"
    last_report_at = 0

    def current_tabs():
        cur_hcfg = load_hatcher_config()
        return cur_hcfg, [hatcher_profile_to_tab(p) for p in hatcher_profiles(cur_hcfg, enabled_only=True)]

    try:
        if resolve_usernames_auto(cfg, hcfg, rt, force=True, quiet=True):
            save_hatcher_config(hcfg)
    except Exception:
        pass

    hcfg, tabs = current_tabs()

    # Smart startup
    if cfg.get("open_all_on_start", True):
        for tab in tabs:
            raw_alive = package_alive(tab["package"], cfg)
            fresh_state = state_recent_enough_for_alive(tab, cfg, seconds=int(cfg.get("state_stale_seconds", 180) or 180))
            rt_tab = get_runtime_tab(rt, tab["package"])
            rt_tab["target"] = "hatcher"
            if fresh_state:
                rt_tab["note"] = "start fresh state"
                save_runtime(rt)
            elif raw_alive and cfg.get("start_reopen_alive_without_fresh_state", True):
                rt_tab["note"] = "start alive no-fresh -> soft"
                save_runtime(rt)
                queue_open(
                    open_queue, tab, "hatcher", "hatcher start alive no-fresh",
                    force=True,
                    skip_if_alive=False,
                    mode="soft"
                )
            elif not raw_alive:
                queue_open(open_queue, tab, "hatcher", "hatcher start", force=True, skip_if_alive=True, mode="hard")
            else:
                rt_tab["note"] = "start already open"
                save_runtime(rt)

        while open_queue:
            if stop_requested():
                save_runtime(rt)
                return
            process_open_queue(open_queue, cfg, rt, session_start, loops)
            if open_queue:
                if not wait_seconds(int(cfg.get("delay_between_open", 45)), rt):
                    return

    while True:
        if stop_requested():
            save_runtime(rt)
            return

        loops += 1
        queue_stuck_self_heal(open_queue, cfg, rt)
        try:
            if resolve_usernames_auto(cfg, hcfg, rt, force=False, quiet=True):
                save_hatcher_config(hcfg)
        except Exception:
            pass
        hcfg, tabs = current_tabs()
        rows = []
        report_state_cache = {}

        for tab in tabs:
            pkg = tab["package"]
            rt_tab = get_runtime_tab(rt, pkg)
            rt_tab["target"] = "hatcher"
            raw_alive = package_alive(pkg, cfg)
            state, err = read_state(tab)
            report_state_cache[pkg] = (state, err)
            alive = raw_alive or (state is not None and not state_disconnect_ui(state) and int(state.get("age", 999999) or 999999) <= int(cfg.get("state_stale_seconds", 180) or 180))
            note = "ok"
            pets = "-"
            eggs = "-"
            age = "-"
            ready_pet = False
            status = "Ingame" if alive else "Offline"
            display_user = tab.get("user_name", pkg)

            if state:
                detected_user = _usable_detected_username(state.get("username"))
                if detected_user:
                    display_user, _ = sync_detected_username_for_package(
                        hcfg, pkg, detected_user, tab=tab, source="state"
                    )
                rt_tab["hatcher_no_state_since"] = 0
                pets = int(state.get("pet_count", 0) or 0)
                eggs = int(state.get("egg_total", 0) or 0)
                age = int(state.get("age", 999999) or 999999)
                if age <= int(cfg.get("state_stale_seconds", 180)):
                    clear_manual_login_block(rt_tab)
                prof = None
                for p in hatcher_profiles(hcfg, enabled_only=True):
                    if p.get("package") == pkg:
                        prof = p
                        break
                ready_at = int((prof or {}).get("ready_pet_count", 200))
                ready_pet = pets >= ready_at
                status = "Online" if alive else "Offline"

                loading_grace = alive and in_post_open_grace(rt_tab, cfg) and state_is_old_after_open(state, rt_tab)

                # V3.76: 5m old-state hard rule
                old_enabled, old_sec, old_max, old_cd = hatcher_alive_old_state_hard_settings(hcfg, cfg)
                try:
                    old_after_open_grace = int(hcfg.get(
                        "hatcher_alive_old_state_after_open_grace_seconds",
                        cfg.get("hatcher_alive_old_state_after_open_grace_seconds", 180)
                    ) or 180)
                except Exception:
                    old_after_open_grace = 180
                if old_after_open_grace < 0:
                    old_after_open_grace = 0

                last_open_for_old = int(rt_tab.get("last_open", 0) or 0)
                old_open_age = (now() - last_open_for_old) if last_open_for_old > 0 else 999999
                in_old_open_grace = old_open_age < old_after_open_grace

                if alive and old_enabled and age > old_max:
                    note = f"invalid old state ignored {age}s"
                    status = "Online"
                elif alive and old_enabled and age >= old_sec and in_old_open_grace:
                    left = max(1, old_after_open_grace - old_open_age)
                    note = f"old-state open grace {left}s"
                    status = "Loading"
                elif alive and old_enabled and age >= old_sec:
                    hard_added, hard_note, hard_action = queue_hatcher_alive_old_state_hard(
                        open_queue, tab, rt_tab, hcfg, cfg, age, "hatcher alive old state hard"
                    )
                    if hard_action:
                        note = hard_note
                        status = "Queued" if hard_added else "Stale"
                    elif str(hard_note).startswith("invalid old state ignored"):
                        note = hard_note
                        status = "Online"
                    else:
                        note = hard_note
                        status = "Online"
                elif loading_grace:
                    note = "loading grace"
                    status = "Loading"
                else:
                    stale = age > int(cfg.get("state_stale_seconds", 180))
                    if alive and stale and cfg.get("ignore_alive_stale_state", True):
                        if hcfg.get("hatcher_rejoin_alive_stale", False) and age >= stale_reopen_age(cfg) and cfg.get("rejoin_if_crash", True):
                            added, _ = queue_open(open_queue, tab, "hatcher", "hatcher stale", mode="soft")
                            note = "stale queued" if added else "already queued"
                            status = "Queued" if added else "Stale"
                        else:
                            note = "alive old state"
                            status = "Online"
            else:
                note = f"state {err}"
                status = "No state" if alive else "Offline"
                if alive:
                    no_state_since = int(rt_tab.get("hatcher_no_state_since", 0) or 0)
                    if no_state_since <= 0:
                        rt_tab["hatcher_no_state_since"] = now()
                        no_state_since = rt_tab["hatcher_no_state_since"]
                    no_state_for = max(0, now() - int(no_state_since))
                    hard_added, hard_note, hard_action = queue_hatcher_alive_old_state_hard(
                        open_queue, tab, rt_tab, hcfg, cfg, no_state_for, "hatcher alive no state hard"
                    )
                    if hard_action:
                        note = hard_note
                        status = "Queued" if hard_added else "No state"
                    elif str(hard_note).startswith("invalid old state ignored"):
                        note = hard_note
                        status = "No state"
                else:
                    rt_tab["hatcher_no_state_since"] = 0

            if alive:
                rt_tab["dead_since"] = 0

            if not tab.get("server_link") or str(tab.get("server_link")).startswith("YOUR_"):
                status = "No server"
                note = "server_link missing"
            elif manual_login_blocked(rt_tab, cfg):
                status = "Manual"
                note = rt_tab.get("note") or "needs manual login"
            elif not alive and cfg.get("rejoin_if_crash", True):
                dead_since = int(rt_tab.get("dead_since", 0) or 0)
                if dead_since <= 0:
                    rt_tab["dead_since"] = now()
                    dead_since = rt_tab["dead_since"]

                dead_for = now() - int(dead_since)
                dead_confirm = int(hcfg.get("hatcher_dead_confirm_seconds", 30) or 30)

                if dead_for >= dead_confirm:
                    added, _ = queue_open(open_queue, tab, "hatcher", "hatcher crash", skip_if_alive=True)
                    note = "crash queued" if added else "already queued"
                    status = "Queued" if added else "Offline"
                else:
                    note = f"dead confirm {max(1, dead_confirm - dead_for)}s"
                    status = "Offline"

            due_refresh, refresh_left = periodic_hard_refresh_due(rt_tab, cfg)
            if due_refresh and not open_queue and not queue_has(open_queue, pkg):
                if (not manual_login_blocked(rt_tab, cfg)) or cfg.get("periodic_hard_refresh_include_manual", True):
                    added, _ = queue_open(
                        open_queue, tab, "hatcher", "periodic hard refresh",
                        force=True, mode="hard_force", bypass_manual=True
                    )
                    if added:
                        mark_periodic_hard_refresh(rt_tab)
                        status = "Queued"
                        note = "periodic hard queued"
                elif note in ["ok", ""]:
                    note = f"refresh in {format_age(refresh_left)}"

            update_clone_session(rt_tab, status, cfg)
            rows.append({
                "user": display_user,
                "pkg": pkg,
                "pets": pets,
                "eggs": eggs,
                "ready_pet": ready_pet,
                "status": status,
                "age": age,
                "session": format_session(rt_tab),
                "note": note,
            })

        # One atomic shared-runtime write after all package rows are updated.
        save_runtime(rt)

        # Backend/reporting cadence is independent from the 10-second local
        # health loop. Reuse this cycle's state reads when a report is due.
        report_interval = max(10, int(hcfg.get("heartbeat_interval", 60) or 60))
        if last_report_at <= 0 or now() - last_report_at >= report_interval:
            ok, msg = hatcher_report_once(
                hcfg, force=False, state_cache=report_state_cache
            )
            last_report_at = now()
            tag = "OK" if ok else "ERR"
            last_msg = f"[{date_time_text()}] {tag}: {msg}"
        hatcher_rejoin_status_screen(rows, hcfg, cfg, session_start, loops, last_msg)

        if open_queue and cfg.get("smart_open_queue", True):
            if not wait_seconds(2, rt):
                return
            process_open_queue(open_queue, cfg, rt, session_start, loops)
            continue

        if not wait_seconds(int(cfg.get("check_interval", 10)), rt):
            return


def hatcher_choose_state_file_for_profile(hcfg, idx):
    clear()
    banner("CHOOSE STATE FILE", None, CYAN)
    found = auto_find_state_files()

    if not found:
        print(col("No nomo_rejoiner/*.json (legacy gag_rejoiner also supported) files found.", RED))
        pause()
        return

    for i, p in enumerate(found[:40], 1):
        print(f"{i}. {p}")

    print("0. Back")
    drain_stdin()
    ch = input("\nChoose: ").strip()

    if not ch.isdigit() or ch == "0":
        return

    pick = int(ch) - 1
    if 0 <= pick < len(found[:40]):
        hcfg["hatchers"][idx]["state_file"] = found[pick]
        save_hatcher_config(hcfg)
        print(col("Saved state_file.", GREEN))
        time.sleep(1)


def hatcher_choose_state_file(hcfg):
    # Backward compatible: choose for first hatcher.
    if "hatchers" not in hcfg or not hcfg["hatchers"]:
        hcfg["hatchers"] = [dict(DEFAULT_HATCHER_PROFILE)]
    hatcher_choose_state_file_for_profile(hcfg, 0)


def hatcher_copy_jsonbin_from_market(hcfg, main_cfg):
    for k in [
        "backend_provider",
        "jsonbin_bin_id", "jsonbin_api_key", "jsonbin_key_header", "jsonbin_timeout_seconds",
        "cloudflare_worker_url", "cloudflare_secret", "cloudflare_timeout_seconds",
    ]:
        if k in main_cfg:
            hcfg[k] = main_cfg.get(k)
    if not hcfg.get("jsonbin_key_header"):
        hcfg["jsonbin_key_header"] = "X-Master-Key"
    save_hatcher_config(hcfg)


def hatcher_global_settings(main_cfg=None):
    """Hatcher-only settings/tools."""
    if main_cfg is None:
        main_cfg = load_config()

    def edit_any(container, save_fn, key, shown_current=None):
        current = container.get(key)
        shown = shown_current if shown_current is not None else current
        val = input(f"New value for {key} [{shown}] (blank ENTER toggles bool): ").strip()

        if isinstance(current, bool):
            if not val:
                container[key] = not current
            else:
                container[key] = val.lower() in ["1", "true", "yes", "y", "on"]
        elif key == "jsonbin_no_hatcher_action":
            if not val:
                cur = str(current or "stay_market").strip().lower()
                container[key] = "fallback_restock" if cur == "stay_market" else "stay_market"
            else:
                v = val.strip().lower().replace("-", "_").replace(" ", "_")
                if v in ["1", "true", "yes", "y", "on", "fallback", "restock", "fallback_restock"]:
                    container[key] = "fallback_restock"
                elif v in ["0", "false", "no", "n", "off", "stay", "market", "stay_market"]:
                    container[key] = "stay_market"
                else:
                    print("Use stay_market or fallback_restock.")
                    time.sleep(1)
                    return
        elif isinstance(current, int):
            if not val:
                return
            try:
                container[key] = int(val)
            except Exception:
                print("Need number.")
                time.sleep(1)
                return
        else:
            if not val:
                return
            container[key] = val

        save_fn(container)

    while True:
        main_cfg = load_config()
        hcfg = load_hatcher_config()
        hatcher_apply_main_backend_if_missing(hcfg, main_cfg, save=True)

        clear()
        banner("HATCHER-ONLY CONFIG / TOOLS", main_cfg)
        print(col("Global/shared settings moved to main menu option 11.", DIM))
        print(col("Username/server/force restart moved to main menu options 4/5/6.", DIM))
        print("")
        print(f"1. enabled              = {hcfg.get('enabled')}")
        print(f"2. update_on_change     = {hcfg.get('update_only_on_status_change')}")
        print(f"3. force_update_seconds = {hcfg.get('force_update_every_seconds')}")
        print(f"4. heartbeat_interval   = {hcfg.get('heartbeat_interval')}")
        print(f"5. upload_pet_threshold = {hcfg.get('upload_pet_threshold', 100)}")
        print(f"6. rejoin_alive_stale   = {hcfg.get('hatcher_rejoin_alive_stale')}")
        print(f"7. dead_confirm_sec     = {hcfg.get('hatcher_dead_confirm_seconds')}")
        print(f"8. detect_public_server = {hcfg.get('detect_public_server')}")
        print(f"9. rejoin_public_server = {hcfg.get('rejoin_public_server')}")
        print(f"10.public_rejoin_delay  = {hcfg.get('public_server_rejoin_after_seconds')}")
        print(f"11.detect_wrong_place   = {hcfg.get('detect_wrong_place')}")
        print(f"12.expected_place_id    = {hcfg.get('expected_place_id')}")
        print("")
        print(col("Shared backend currently from Global config:", CYAN))
        if backend_provider(main_cfg) == "cloudflare":
            print(f"   provider=cloudflare url={main_cfg.get('cloudflare_worker_url')} secret={mask_secret(main_cfg.get('cloudflare_secret'))}")
        else:
            print(f"   provider=jsonbin bin={main_cfg.get('jsonbin_bin_id')} key={mask_secret(main_cfg.get('jsonbin_api_key'))} header={main_cfg.get('jsonbin_key_header')}")
        print("")
        print("P. Edit hatcher packages / state files")
        print("T. Report once / test upload")
        print("R. Start reporter only")
        print("0. Save/back")
        print(col("Tip: on true/false settings, choose it and press ENTER to toggle.", DIM))

        drain_stdin()
        ch = input("\nChoose: ").strip().lower()

        if ch == "0":
            save_hatcher_config(hcfg)
            return

        if ch == "p":
            edit_hatcher_basic(main_cfg)
            continue

        if ch == "t":
            hatcher_test_upload_screen(main_cfg)
            continue

        if ch == "r":
            try:
                start_hatcher_reporter_only(main_cfg)
            finally:
                reset_terminal()
                drain_stdin()
            continue

        mapping = {
            "1": "enabled",
            "2": "update_only_on_status_change",
            "3": "force_update_every_seconds",
            "4": "heartbeat_interval",
            "5": "upload_pet_threshold",
            "6": "hatcher_rejoin_alive_stale",
            "7": "hatcher_dead_confirm_seconds",
            "8": "detect_public_server",
            "9": "rejoin_public_server",
            "10": "public_server_rejoin_after_seconds",
            "11": "detect_wrong_place",
            "12": "expected_place_id",
        }

        if ch in mapping:
            key = mapping[ch]
            edit_any(hcfg, save_hatcher_config, key)
            continue


def edit_hatcher_basic(main_cfg=None):
    """Market-style compact editor for package/name/state_file only."""
    hcfg = load_hatcher_config()

    while True:
        clear()
        banner("EDIT HATCHER PACKAGES", main_cfg)
        print(col("This is like Market package edit. Hatcher only keeps alive + uploads pet/egg state.", DIM))
        print("")

        profiles = hatcher_profiles(hcfg)
        rows = []
        for i, p in enumerate(profiles, 1):
            rows.append([
                (str(i), CYAN),
                ("ON" if p.get("enabled", True) else "OFF", GREEN if p.get("enabled", True) else RED),
                (short_pkg(p.get("package", "")), WHITE),
                (p.get("hatcher_name", ""), WHITE),
                (cut(p.get("state_file", ""), 38), DIM),
            ])
        draw_table(["No", "On", "Pkg", "Hatcher", "State file"], rows, [2, 3, 6, 13, 38], {"ui_width": 80, "use_color": True}, border=CYAN)

        print("")
        print("A. Add hatcher")
        print("D. Delete hatcher")
        print("F. Find/select state_file for a hatcher")
        print("0. Back")
        drain_stdin()
        ch = input("\nChoose hatcher number to edit basic fields: ").strip().lower()

        if ch == "0":
            save_hatcher_config(hcfg)
            return

        if ch == "a":
            hcfg.setdefault("hatchers", []).append(normalize_hatcher_profile({}, len(hcfg.get("hatchers", []))))
            save_hatcher_config(hcfg)
            hcfg = load_hatcher_config()
            continue

        if ch == "d":
            idxs = input("Delete number: ").strip()
            if idxs.isdigit():
                idx = int(idxs) - 1
                if 0 <= idx < len(hcfg.get("hatchers", [])):
                    removed = hcfg["hatchers"].pop(idx)
                    save_hatcher_config(hcfg)
                    print(col(f"Deleted {removed.get('hatcher_name')}", GREEN))
                    time.sleep(1)
            hcfg = load_hatcher_config()
            continue

        if ch == "f":
            idxs = input("Select number for state_file: ").strip()
            if idxs.isdigit():
                idx = int(idxs) - 1
                if 0 <= idx < len(hcfg.get("hatchers", [])):
                    hatcher_choose_state_file_for_profile(hcfg, idx)
            hcfg = load_hatcher_config()
            continue

        if not ch.isdigit():
            continue

        idx = int(ch) - 1
        if not (0 <= idx < len(hcfg.get("hatchers", []))):
            continue

        p = hcfg["hatchers"][idx]
        clear()
        banner("EDIT HATCHER BASIC", main_cfg)
        print(f"1. enabled      = {p.get('enabled', True)}")
        print(f"2. package      = {p.get('package', '')}")
        print(f"3. hatcher_name = {p.get('hatcher_name', '')}")
        print(f"4. state_file   = {p.get('state_file', '')}")
        print("0. Back")
        pick = input("\nChoose: ").strip().lower()
        mapping = {"1": "enabled", "2": "package", "3": "hatcher_name", "4": "state_file"}
        if pick == "0" or pick not in mapping:
            continue
        key = mapping[pick]
        cur = p.get(key)
        val = input(f"New value for {key} [{cur}] (blank ENTER toggles bool): ").strip()
        if isinstance(cur, bool):
            if not val:
                p[key] = not cur
            else:
                p[key] = val.lower() in ["1", "true", "yes", "y", "on"]
        else:
            if not val:
                continue
            p[key] = val
        hcfg["hatchers"][idx] = normalize_hatcher_profile(p, idx)
        save_hatcher_config(hcfg)
        hcfg = load_hatcher_config()






def hatcher_teleport_problem(tab, state, hcfg, cfg):
    """Return (problem_code, note) when a hatcher is not in its expected private server."""
    if not state:
        return None, ""

    server_link = str(tab.get("server_link", "") or "").strip()
    if not server_link or server_link.startswith("YOUR_") or server_link.startswith("PUT_"):
        return None, ""

    expected_pid = extract_place_id_from_link(server_link) or str(hcfg.get("expected_place_id", "") or "").strip()
    current_pid = state.get("place_id")
    if hcfg.get("detect_wrong_place", True) and expected_pid and current_pid:
        if str(expected_pid) != str(current_pid):
            return "wrong_place", f"wrong place {current_pid}"

    if hcfg.get("detect_public_server", True) and state.get("private_server_known"):
        if state.get("is_private_server") is False:
            return "public_server", "public server"

    return None, ""




def should_queue_hatcher_teleport_rejoin(rt_tab, hcfg, cfg, problem_code):
    if not problem_code:
        return False, ""
    if not hcfg.get("rejoin_public_server", True):
        return False, "detect only"

    delay = int(hcfg.get("public_server_rejoin_after_seconds", 20) or 20)
    key = "hatcher_teleport_since"
    last_problem = rt_tab.get("hatcher_teleport_problem", "")
    t = now()

    if last_problem != problem_code or not int(rt_tab.get(key, 0) or 0):
        rt_tab[key] = t
        rt_tab["hatcher_teleport_problem"] = problem_code
        return False, f"wait {delay}s"

    age = t - int(rt_tab.get(key, t) or t)
    if age < delay:
        return False, f"wait {max(0, delay-age)}s"

    if not can_open(rt_tab, cfg):
        return False, "cooldown"

    return True, "queue rejoin"

def start_hatcher_safe_rejoiner(main_cfg=None):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    print(col("[DEBUG] start_hatcher_safe_rejoiner() called", CYAN))
    cfg = dict(load_config())
    if isinstance(main_cfg, dict):
        cfg.update(main_cfg)

    # Force safe behavior for hatcher mode
    cfg["no_force_stop_alive"] = True
    cfg["alive_open_mode"] = "soft"
    cfg["soft_hop_fallback_hard"] = False
    cfg["scheduled_hop_enabled"] = False
    cfg["clear_cache_on_soft_hop"] = False
    cfg["smart_open_queue"] = True
    cfg["wait_fresh_after_open"] = True
    cfg["open_only_closed_on_start"] = True
    # CRITICAL: without this, open_target's master switch promotes EVERY soft
    # open into a hard kill+open, so hatcher would force-stop alive clones it was
    # only meant to soft-rejoin. Hatcher mode must keep soft opens soft.
    cfg["disable_soft_rejoin"] = False

    hcfg = load_hatcher_config()
    rt = load_runtime()
    session_start = now()
    loops = 0
    open_queue = []
    last_msg = "not uploaded yet"
    last_report_at = 0

    def current_tabs():
        cur_hcfg = load_hatcher_config()
        return cur_hcfg, [hatcher_profile_to_tab(p) for p in hatcher_profiles(cur_hcfg, enabled_only=True)]

    # AUTO USERNAME RESOLVE: line up config names with the accounts actually
    # logged in (cookie -> Roblox API, fallback freshest state file) so state
    # files match without the manual "get username" step.
    try:
        if resolve_usernames_auto(cfg, hcfg, rt, force=True, quiet=True):
            save_hatcher_config(hcfg)
    except Exception:
        pass

    hcfg, tabs = current_tabs()

    # Startup: only open GENUINELY CLOSED hatcher packages. An alive clone may be
    # mid Lua-startup writing its first state.json — reopening it (soft opens can
    # still become a hard kill+open) would kill the counter the user just
    # launched. So alive clones are left alone with a grace anchor, and the queue
    # is NOT drained here: the main loop drains it so the status table appears
    # immediately instead of hanging on the debug line until every clone opens.
    if cfg.get("open_all_on_start", True):
        for tab in tabs:
            raw_alive = package_alive(tab["package"], cfg)
            fresh_state = state_recent_enough_for_alive(tab, cfg, seconds=int(cfg.get("state_stale_seconds", 180) or 180))
            rt_tab = get_runtime_tab(rt, tab["package"])
            rt_tab["target"] = "hatcher"
            if fresh_state:
                rt_tab["note"] = "start fresh state"
            elif raw_alive:
                # Alive but no fresh state yet: give the Lua a SHORT window to
                # write it (not the full 6-min post-open grace, which looked
                # stuck). Anchor last_open back so only startup_grace remains.
                # We never force-stop a clone that is already up at startup.
                startup_grace = int(cfg.get("hatcher_startup_grace_seconds", 75) or 75)
                post_grace = int(cfg.get("post_open_grace_seconds", 360) or 360)
                rt_tab["last_open"] = now() - max(0, post_grace - startup_grace)
                rt_tab["hatcher_no_state_since"] = now()
                rt_tab["note"] = f"start alive -> grace {startup_grace}s"
            else:
                # Genuinely closed -> queue a normal open. Drained by main loop.
                queue_open(open_queue, tab, "hatcher", "hatcher start", force=True, skip_if_alive=True, mode="hard")
        save_runtime(rt)

    while True:
        if stop_requested():
            save_runtime(rt)
            return

        loops += 1
        queue_stuck_self_heal(open_queue, cfg, rt)
        poll_solver_jobs(cfg, rt, open_queue)
        # Periodic auto username re-resolve (rate-limited inside the function).
        try:
            if resolve_usernames_auto(cfg, hcfg, rt, force=False, quiet=True):
                save_hatcher_config(hcfg)
        except Exception:
            pass
        hcfg, tabs = current_tabs()
        rows = []
        report_state_cache = {}

        for tab in tabs:
            pkg = tab["package"]
            rt_tab = get_runtime_tab(rt, pkg)
            rt_tab["target"] = "hatcher"

            raw_alive = package_alive(pkg, cfg)
            state, err = read_state(tab)
            report_state_cache[pkg] = (state, err)
            alive = raw_alive or (state is not None and not state_disconnect_ui(state) and int(state.get("age", 999999) or 999999) <= int(cfg.get("state_stale_seconds", 180) or 180))
            prof = None
            for p in hatcher_profiles(hcfg, enabled_only=True):
                if p.get("package") == pkg:
                    prof = p
                    break
            health = evaluate_package_health(
                tab, cfg, rt_tab, mode="hatcher", hcfg=hcfg, prof=prof,
                raw_alive=alive, state=state, err=err
            )
            note = health.get("note") or "ok"
            pets = "-"
            eggs = "-"
            age = "-"
            ready_pet = False
            status = health.get("status") or ("Online" if alive else "Offline")
            display_user = tab.get("user_name", pkg)

            if state:
                detected_user = _usable_detected_username(state.get("username"))
                if detected_user:
                    display_user, _ = sync_detected_username_for_package(
                        hcfg, pkg, detected_user, tab=tab, source="state"
                    )

            if not tab.get("server_link") or str(tab.get("server_link")).startswith("YOUR_"):
                status = "No server"
                note = "server_link missing"
            elif state:
                pets = int(state.get("pet_count", 0) or 0)
                eggs = int(state.get("egg_total", 0) or 0)
                age = int(state.get("age", 999999) or 999999)

                ready_at = int((prof or {}).get("ready_pet_count", 200))
                ready_pet = pets >= ready_at
                if health.get("clean_fresh"):
                    clear_manual_login_block(rt_tab)
                    clear_captcha_ui_runtime(rt_tab)

                problem_code, problem_note = hatcher_teleport_problem(tab, state, hcfg, cfg)
                if problem_code:
                    should_q, wait_note = should_queue_hatcher_teleport_rejoin(rt_tab, hcfg, cfg, problem_code)
                    if should_q and cfg.get("rejoin_if_crash", True):
                        mode = "soft" if alive else "hard"
                        added, _ = queue_open(open_queue, tab, "hatcher", problem_note, force=True, mode=mode)
                        note = "private rejoin queued" if added else "already queued"
                        status = "Queued" if added else "Wrong server"
                    else:
                        status = "Wrong server"
                        note = f"{problem_note} {wait_note}".strip()
                else:
                    rt_tab["hatcher_teleport_since"] = 0
                    rt_tab["hatcher_teleport_problem"] = ""

                loading_grace = (not problem_code) and alive and in_post_open_grace(rt_tab, cfg) and state_is_old_after_open(state, rt_tab)

                # V3.76: 5m old-state hard rule
                old_enabled, old_sec, old_max, old_cd = hatcher_alive_old_state_hard_settings(hcfg, cfg)
                try:
                    old_after_open_grace = int(hcfg.get(
                        "hatcher_alive_old_state_after_open_grace_seconds",
                        cfg.get("hatcher_alive_old_state_after_open_grace_seconds", 180)
                    ) or 180)
                except Exception:
                    old_after_open_grace = 180
                if old_after_open_grace < 0:
                    old_after_open_grace = 0

                last_open_for_old = int(rt_tab.get("last_open", 0) or 0)
                old_open_age = (now() - last_open_for_old) if last_open_for_old > 0 else 999999
                in_old_open_grace = old_open_age < old_after_open_grace

                if problem_code:
                    pass
                elif alive and old_enabled and age > old_max:
                    status = "Online"
                    note = f"invalid old state ignored {age}s"
                elif alive and old_enabled and age >= old_sec and in_old_open_grace:
                    status = "Loading"
                    note = f"old-state open grace {max(1, old_after_open_grace - old_open_age)}s"
                elif alive and old_enabled and age >= old_sec:
                    hard_added, hard_note, hard_action = queue_hatcher_alive_old_state_hard(
                        open_queue, tab, rt_tab, hcfg, cfg, age, "hatcher alive old state hard"
                    )
                    if hard_action:
                        note = hard_note
                        status = "Queued" if hard_added else "Stale"
                    elif str(hard_note).startswith("invalid old state ignored"):
                        note = hard_note
                        status = "Online"
                    else:
                        note = hard_note
                        status = "Online"
                elif loading_grace:
                    status = "Loading"
                    note = "loading grace"
                else:
                    stale = age > int(cfg.get("state_stale_seconds", 180))
                    if alive and stale and cfg.get("ignore_alive_stale_state", True):
                        force_stale = stale_reopen_age(cfg)
                        disconnect_stale = should_force_disconnect_rejoin(alive, age, cfg)

                        if disconnect_stale and cfg.get("force_stop_alive_on_disconnect_popup", True) and cfg.get("rejoin_if_crash", True):
                            if cfg.get("smart_open_queue", True):
                                added, _ = queue_open(open_queue, tab, "hatcher", "disconnect popup/stale", force=True, mode="hard_force")
                                note = "disconnect queued" if added else "already queued"
                                status = "Queued" if added else "Stale"
                            else:
                                ok, msg = open_target(tab, rt_tab, cfg, "hatcher", "disconnect popup/stale", force=True, rt=rt, mode="hard_force")
                                note = "disconnect reopen" if ok else msg
                                status = "Loading" if ok else "Stale"
                        elif hcfg.get("hatcher_rejoin_alive_stale", False) and age >= force_stale and cfg.get("rejoin_if_crash", True):
                            if cfg.get("smart_open_queue", True):
                                added, _ = queue_open(open_queue, tab, "hatcher", "stale too long", force=True, mode="soft")
                                note = "stale queued" if added else "already queued"
                                status = "Queued" if added else "Stale"
                            else:
                                ok, msg = open_target(tab, rt_tab, cfg, "hatcher", "stale too long", force=True, rt=rt, mode="soft")
                                note = "stale reopen" if ok else msg
                                status = "Loading" if ok else "Stale"
                        else:
                            # FIX V3.78: use `or 180` instead of `or 120` to avoid 0->120 falsy fallback
                            # that made 277h state appear fresh
                            if (age >= int(cfg.get("hatcher_alive_old_state_hard_force_seconds", 180) or 180)
                                    and age <= int(cfg.get("hatcher_alive_old_state_max_valid_seconds", 86400) or 86400)
                                    and cfg.get("rejoin_if_crash", True)):
                                added, _ = queue_open(open_queue, tab, "hatcher",
                                                      f"alive old state {age}s", force=True, mode="hard_force")
                                note = f"old {age}s kill+open" if added else "already queued"
                                status = "Queued" if added else "Stale"
                            else:
                                status = "Online"
                                note = "alive old state"
                    else:
                        status = "Online" if alive else "Offline"
            else:
                note = f"state {err}"
                if alive:
                    no_state_since = int(rt_tab.get("hatcher_no_state_since", 0) or 0)
                    if no_state_since <= 0:
                        rt_tab["hatcher_no_state_since"] = now()
                        no_state_since = rt_tab["hatcher_no_state_since"]
                    no_state_for = max(0, now() - int(no_state_since))
                    hard_added, hard_note, hard_action = queue_hatcher_alive_old_state_hard(
                        open_queue, tab, rt_tab, hcfg, cfg, no_state_for, "hatcher alive no state hard"
                    )
                    if hard_action:
                        note = hard_note
                        status = "Queued" if hard_added else "No state"
                    elif str(hard_note).startswith("invalid old state ignored"):
                        note = hard_note
                        status = "No state"
                    elif rt_tab.get("last_open") and in_post_open_grace(rt_tab, cfg):
                        status = "Loading"
                        note = f"waiting for {expected_state_name(tab)}"
                    elif hcfg.get("hatcher_rejoin_alive_stale", False) and cfg.get("rejoin_if_crash", True):
                        added, _ = queue_open(open_queue, tab, "hatcher", "hatcher no state", mode="soft")
                        note = "no state queued" if added else "no state wait"
                        status = "Queued" if added else "No state"
                    else:
                        # In-game but no readable state past grace: almost always
                        # the Lua isn't running here, or its username != config.
                        status = "No state"
                        note = f"ingame,no {expected_state_name(tab)}? chk Lua"
                else:
                    rt_tab["hatcher_no_state_since"] = 0
                    status = "Offline"

            # V3.53: direct Lua-detected disconnect/kick popup
            if state and alive and state_disconnect_ui(state) and cfg.get("rejoin_if_crash", True):
                added, dnote = queue_disconnect_ui_rejoin(open_queue, tab, "hatcher", rt_tab, cfg)
                status = "Queued" if (added or dnote == "already queued") else "Kicked"
                note = f"{state_disconnect_note(state)} {dnote}".strip()

            if alive:
                rt_tab["dead_since"] = 0

            # Dead package handling
            if (
                tab.get("server_link")
                and not str(tab.get("server_link")).startswith("YOUR_")
                and not alive
                and cfg.get("rejoin_if_crash", True)
            ):
                if cfg.get("smart_open_queue", True):
                    added, _ = queue_open(open_queue, tab, "hatcher", "crash/dead", skip_if_alive=True)
                    note = "crash queued" if added else "already queued"
                    status = "Queued" if added else "Offline"
                else:
                    ok, msg = open_target(tab, rt_tab, cfg, "hatcher", "crash/dead", rt=rt)
                    note = "crash open" if ok else msg
                    status = "Loading" if ok else "Offline"

            # A Lua-reported CAPTCHA outranks stale/dead routing, but the
            # provider is never called from the middle-session dashboard. Queue
            # one package rejoin; wait-after-open owns the single provider call.
            if health.get("bad") == "challenge" or (state and state_login_challenge_detail(state)):
                cancel_queued_package(open_queue, pkg)
                added, _ = queue_open(
                    open_queue, tab, "hatcher", "join challenge pre-solver rejoin",
                    force=True, mode="hard_force", bypass_manual=True,
                    metadata={"bypass_recheck": True},
                )
                status = "Queued" if added else "Manual"
                note = "challenge rejoin queued; solver checks after open" if added else "challenge already queued"

            # V4.12: visible package-scoped verification UI outranks every
            # stale/dead/routing decision above. Remove only this package's queued
            # opens and solve/hold it without restarting the other clones.
            captcha_action = apply_visible_captcha_ui_action(
                open_queue, tab, "hatcher", rt_tab, cfg, rt, health
            )
            if captcha_action is not None:
                status, note, _ = captcha_action

            # V3.84: no pool-wide provider scan here. The old-state hard queue
            # gets its normal turn; its wait-after-open performs one CAPTCHA check.

            due_refresh, refresh_left = periodic_hard_refresh_due(rt_tab, cfg)
            if due_refresh and captcha_action is None and not open_queue and not queue_has(open_queue, pkg) and not solver_job_running(pkg):
                if ((not manual_login_blocked(rt_tab, cfg)) or cfg.get("periodic_hard_refresh_include_manual", True)) and not (state and state_login_challenge_detail(state)):
                    added, _ = queue_open(
                        open_queue, tab, "hatcher", "periodic hard refresh",
                        force=True, mode="hard_force", bypass_manual=True
                    )
                    if added:
                        mark_periodic_hard_refresh(rt_tab)
                        status = "Queued"
                        note = "periodic hard queued"
                elif note in ["ok", ""]:
                    note = f"refresh in {format_age(refresh_left)}"

            update_clone_session(rt_tab, status, cfg)
            rows.append({
                "user": display_user,
                "pkg": pkg,
                "pets": pets,
                "eggs": eggs,
                "ready_pet": ready_pet,
                "status": status,
                "age": age,
                "session": format_session(rt_tab),
                "note": note,
            })

        # One atomic shared-runtime write after all package rows are updated.
        save_runtime(rt)

        # Backend/reporting cadence is independent from the 10-second local
        # health loop. Reuse this cycle's state reads when a report is due.
        report_interval = max(10, int(hcfg.get("heartbeat_interval", 60) or 60))
        if last_report_at <= 0 or now() - last_report_at >= report_interval:
            ok, msg = hatcher_report_once(
                hcfg, force=False, state_cache=report_state_cache
            )
            last_report_at = now()
            tag = "OK" if ok else "ERR"
            last_msg = f"[{date_time_text()}] {tag}: {msg}"
        hatcher_rejoin_status_screen(rows, hcfg, cfg, session_start, loops, last_msg)
        print(col("  Hatcher: alive old state 5m..6h => hard-force affected tab; >6h ignored.", GREEN))

        if open_queue and cfg.get("smart_open_queue", True):
            if not wait_seconds(2, rt):
                return
            process_open_queue(open_queue, cfg, rt, session_start, loops)
            continue

        if not wait_seconds(int(cfg.get("check_interval", 10)), rt):
            return

def start_hatcher_reporter_only(main_cfg=None):
    """Reporter-only hatcher mode."""
    hcfg = load_hatcher_config()
    session_start = now()
    loops = 0
    last_msg = "not uploaded yet"

    while True:
        if stop_requested():
            return

        loops += 1
        hcfg = load_hatcher_config()

        ok, msg = hatcher_report_once(hcfg, force=False)
        tag = "OK" if ok else "ERR"
        last_msg = f"[{date_time_text()}] {tag}: {msg}"

        hatcher_status_screen(hcfg, last_msg=last_msg)
        print("")
        print(col("Reporter-only mode: no am start, no force-stop, no soft hop, no Roblox touch.", GREEN))
        print(col(f"Uptime: {format_uptime(now() - session_start)} | Checks: {loops}", DIM))
        print(col("Type Q + ENTER to stop / return to Hatching Mode menu", DIM))

        if not wait_seconds(int(hcfg.get("heartbeat_interval", 60)), {}):
            return


# Backward-compatible old names now point to compact editor.
hatcher_profile_editor = edit_hatcher_basic
edit_hatcher_profile = None

HATCHER_MENU_ITEMS = [
    ("1",  "Enable HATCHER mode"),
    ("2",  "Package manager (shared)"),
    ("11", "Hatcher-only config / tools"),
    ("0",  "Back"),
]


def open_all_hatcher_tabs_once(main_cfg=None, selected_packages=None):
    cfg = dict(load_config())
    if isinstance(main_cfg, dict):
        cfg.update(main_cfg)
    hcfg = load_hatcher_config()
    rt = load_runtime()
    wanted = set(selected_packages or [])
    tabs = [hatcher_profile_to_tab(p) for p in hatcher_profiles(hcfg, enabled_only=True)
            if not wanted or p.get("package") in wanted]
    total = len(tabs)

    for idx, tab in enumerate(tabs, 1):
        if stop_requested():
            save_runtime(rt)
            break

        rt_tab = get_runtime_tab(rt, tab["package"])
        opening_screen(tab, "hatcher", cfg, idx, total, mode="hard")
        print(col("Manual option 6: force-stop first, then open. Auto hatcher rejoin safety is unchanged.", YELLOW))
        print(col("Type Q + ENTER to cancel after this open / during delay.", DIM))
        manual_force_restart_tab(tab, rt_tab, cfg, "hatcher", "manual hatcher force restart", rt=rt)
        save_runtime(rt)

        if idx < total:
            if not wait_seconds(int(cfg.get("delay_between_open", 45)), rt):
                break

    print(col("\nDone opening hatcher tabs.", GREEN))
    pause()


def hatching_mode_menu(main_cfg):
    while True:
        main_cfg = load_config()
        hcfg = load_hatcher_config()
        clear()
        print_banner(main_cfg)
        print(col(">>> HATCHER MODE <<<".center(term_width(main_cfg)), DIM))
        print("")
        rows = []
        for num, label in HATCHER_MENU_ITEMS:
            rows.append((num, label, RED if num == "0" else CYAN, WHITE))
        draw_boxed_menu(rows, main_cfg)
        print("")
        tab_rows = []
        for i, p in enumerate(hatcher_profiles(hcfg, enabled_only=False), 1):
            on = p.get("enabled", True)
            tab_rows.append([
                (str(i), CYAN),
                ("ON" if on else "OFF", GREEN if on else RED),
                (short_pkg(p.get("package", "")), WHITE),
                (p.get("hatcher_name", ""), WHITE),
                (short_link(p.get("server_link", "")), DIM),
            ])
        if tab_rows:
            draw_table(["No", "On", "Pkg", "Hatcher", "Server"], tab_rows,
                       [2, 3, 8, 14, 24], main_cfg)
        print("")
        choice = input("[Hatcher] Option: ").strip()
        if choice == "1":
            main_cfg = set_active_rejoin_mode("hatcher", main_cfg)
            print(col("HATCHER mode enabled. MARKET mode disabled.", GREEN))
            pause()
        elif choice == "2":
            select_package_menu(main_cfg)
        elif choice == "11":
            hatcher_global_settings(main_cfg)
        elif choice == "0":
            return

def market_mode_menu(cfg):
    while True:
        clear()
        print_banner(cfg)
        print(col(">>> MARKET MODE <<<".center(term_width(cfg)), DIM))
        print("")
        rows = []
        for num, label in MARKET_MENU_ITEMS:
            nc = RED if num == "0" else CYAN
            rows.append((num, label, nc, WHITE))
        draw_boxed_menu(rows, cfg)

        print("")
        print(col("Market packages:", BOLD))
        tab_rows = []
        for i, t in enumerate(cfg["tabs"], 1):
            on = t.get("enabled", True)
            tab_rows.append([
                (str(i), CYAN),
                ("ON" if on else "OFF", GREEN if on else RED),
                (short_pkg(t.get("package", "")), WHITE),
                (t.get("user_name", ""), WHITE),
                (short_link(t.get("restock_link", cfg.get("restock_link", ""))), DIM),
            ])
        draw_table(["No", "On", "Pkg", "Username", "Restock"], tab_rows, [2, 3, 6, 12, 22], cfg)
        print("")
        print(col(f"Active mode: {active_mode_label(cfg)}", GREEN if active_rejoin_mode(cfg) == "market" else YELLOW))
        print(col("Option 1 enables MARKET and disables HATCHER. Use top menu option 1 to start.", DIM))
        print(col("Use 11=market config. Main menu: 4=username, 5=server, 6=manual hard restart, 11=global config.", DIM))
        print("")

        drain_stdin()
        choice = input("[Market] Option: ").strip()

        if choice == "1":
            cfg = set_active_rejoin_mode("market", cfg)
            print(col("MARKET mode enabled. HATCHER mode disabled.", GREEN))
            print(col("Back to top menu and press option 1 to start rejoin.", DIM))
            pause()
            cfg = load_config()
        elif choice == "2":
            select_package_menu(cfg)
            cfg = load_config()
        elif choice == "11":
            market_only_settings(cfg)
            cfg = load_config()
        elif choice == "0":
            return


# ============================================================
# COOKIE EXPORT (Option 7)
# ============================================================

def get_cookie_from_package(package):
    """Extract .ROBLOSECURITY from package's WebView cookie DB."""
    db_src = f"/data/data/{package}/app_webview/Default/Cookies"
    safe_pkg = re.sub(r"[^A-Za-z0-9_.-]", "_", package)
    db_dst = f"/sdcard/Download/tmp_cookie_{safe_pkg}_{os.getpid()}.db"
    try:
        ret = subprocess.run(
            ["su", "-c", f"cp {db_src} {db_dst}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).returncode
    except Exception:
        return None
    if ret != 0 or not os.path.exists(db_dst):
        return None
    try:
        conn = sqlite3.connect(db_dst)
        c = conn.cursor()
        c.execute("SELECT value FROM cookies WHERE host_key LIKE '%.roblox.com' AND name='.ROBLOSECURITY'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        if os.path.exists(db_dst):
            os.remove(db_dst)

def get_installed_packages():
    """Return installed Roblox / Noka clone packages in natural order."""
    try:
        out = subprocess.check_output(
            "su -c 'pm list packages'", shell=True,
            stdin=subprocess.DEVNULL, timeout=15
        ).decode(errors="ignore").splitlines()
        packages = []
        seen = set()
        for line in out:
            pkg = line.replace("package:", "", 1).strip()
            low = pkg.lower()
            if not pkg or pkg in seen:
                continue
            if ".noka" in low or pkg == "com.roblox" or low.startswith("com.roblox."):
                seen.add(pkg)
                packages.append(pkg)
        return sorted(packages, key=natural_package_key)
    except Exception:
        return []


def _new_tab_for_package(pkg, position=0):
    idx = max(0, int(position or 0))
    if idx < len(DEFAULT_TABS):
        tab = json.loads(json.dumps(DEFAULT_TABS[idx]))
    else:
        tab = json.loads(json.dumps(DEFAULT_TABS[-1]))
        clone_no = idx + 1
        tab["user_name"] = pkg
        tab["stat_file"] = f"/storage/emulated/0/RobloxClone{clone_no:03d}/Arceus X/Workspace/nomo_rejoiner/state.json"
    tab["package"] = pkg
    tab["enabled"] = True
    if not tab.get("user_name") or str(tab.get("user_name")).startswith(("nomomarket", "nomostock")):
        tab["user_name"] = pkg
    return tab


def package_registry_entries(cfg, include_discovered=True, configured_only=False):
    """Build the shared package table used by every package-aware menu."""
    installed = set(get_installed_packages())
    entries = []
    seen = set()
    for tab in cfg.get("tabs", []):
        pkg = str(tab.get("package") or "").strip()
        if not pkg or pkg in seen:
            continue
        seen.add(pkg)
        entries.append({
            "package": pkg,
            "username": str(tab.get("user_name") or pkg),
            "enabled": bool(tab.get("enabled", True)),
            "installed": pkg in installed,
            "configured": True,
            "tab": tab,
        })
    if include_discovered and not configured_only:
        for pkg in sorted(installed, key=natural_package_key):
            if pkg in seen:
                continue
            entries.append({
                "package": pkg,
                "username": pkg,
                "enabled": False,
                "installed": True,
                "configured": False,
                "tab": None,
            })
    return entries


def _parse_package_selection(raw, entries):
    raw = clean_terminal_input(raw)
    low = raw.lower()
    if low in {"a", "all"}:
        return [e["package"] for e in entries]
    if low in {"e", "enabled"}:
        return [e["package"] for e in entries if e.get("enabled")]
    chosen = []
    by_pkg = {e["package"].lower(): e["package"] for e in entries}
    by_short = {}
    for e in entries:
        by_short.setdefault(short_pkg(e["package"]).lower(), e["package"])
    for token in re.split(r"[\s,]+", raw):
        token = token.strip()
        if not token:
            continue
        if re.fullmatch(r"\d+-\d+", token):
            a, b = [int(x) for x in token.split("-", 1)]
            step = 1 if b >= a else -1
            for num in range(a, b + step, step):
                if 1 <= num <= len(entries):
                    pkg = entries[num - 1]["package"]
                    if pkg not in chosen:
                        chosen.append(pkg)
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(entries):
                pkg = entries[idx]["package"]
                if pkg not in chosen:
                    chosen.append(pkg)
            continue
        pkg = by_pkg.get(token.lower()) or by_short.get(token.lower())
        if pkg and pkg not in chosen:
            chosen.append(pkg)
    return chosen


def choose_packages_common(cfg, title="SELECT PACKAGES", multi=True, enabled_only=False,
                           installed_only=False, include_discovered=True,
                           configured_only=False, allow_all=True):
    """Shared package picker. Returns package names; [] means back/cancel."""
    entries = package_registry_entries(cfg, include_discovered=include_discovered,
                                       configured_only=configured_only)
    if enabled_only:
        entries = [e for e in entries if e.get("enabled")]
    if installed_only:
        entries = [e for e in entries if e.get("installed")]
    if not entries:
        print(col("No matching packages found.", RED))
        return []

    clear()
    banner(title, cfg)
    rows = []
    for i, e in enumerate(entries, 1):
        rows.append([
            (str(i), CYAN),
            ("ON" if e.get("enabled") else ("NEW" if not e.get("configured") else "OFF"),
             GREEN if e.get("enabled") else (YELLOW if not e.get("configured") else RED)),
            ("YES" if e.get("installed") else "NO", GREEN if e.get("installed") else YELLOW),
            (short_pkg(e.get("package", "")), WHITE),
            (e.get("username", ""), WHITE),
        ])
    draw_table(["No", "Use", "Inst", "Package", "Username"], rows,
               [3, 4, 4, 12, 18], cfg)
    print("")
    if multi:
        print(col("Enter numbers/packages, comma lists, or ranges (example: 1,3-5).", DIM))
        if allow_all:
            print(col("A = all shown   E = enabled only   0 = back", DIM))
    else:
        print(col("Choose one number/package. 0 = back", DIM))
    drain_stdin()
    while True:
        raw = clean_terminal_input(input("\nChoose: "))
        if raw.lower() in {"0", "q", "quit", "b", "back", ""}:
            return []
        selected = _parse_package_selection(raw, entries)
        if not multi and selected:
            selected = selected[:1]
        if selected:
            return selected
        print(col("Invalid or empty selection. Use 1, 1,3-5, A/ALL, E/ENABLED, or 0.", RED))


def sync_installed_packages_into_config(cfg):
    installed = get_installed_packages()
    existing = {str(t.get("package") or ""): t for t in cfg.get("tabs", []) if t.get("package")}
    added = []
    tabs = list(cfg.get("tabs", []))
    for pkg in installed:
        if pkg in existing:
            continue
        tab = _new_tab_for_package(pkg, len(tabs))
        tabs.append(tab)
        existing[pkg] = tab
        added.append(pkg)
    cfg["tabs"] = tabs
    if added:
        hcfg = load_hatcher_config()
        sync_hatcher_profiles_with_tabs(cfg, hcfg)
        save_config(cfg)
    return added


def set_enabled_package_set(cfg, selected):
    wanted = set(selected or [])
    for tab in cfg.get("tabs", []):
        tab["enabled"] = tab.get("package") in wanted
    save_config(cfg)
    hcfg = load_hatcher_config()
    sync_hatcher_profiles_with_tabs(cfg, hcfg)


def refresh_usernames_for_packages(cfg, packages):
    hcfg = load_hatcher_config()
    profile_map = {p.get("package"): p for p in hatcher_profiles(hcfg, enabled_only=False)}
    changed = False
    for pkg in packages:
        tab = next((t for t in cfg.get("tabs", []) if t.get("package") == pkg), None)
        if tab is None:
            continue
        username, uid = get_username_from_package_api(pkg)
        source = "cookie/API"
        if not username:
            state, _ = read_state(tab)
            username = _usable_detected_username((state or {}).get("username"))
            source = "state"
        if username:
            tab["user_name"] = username
            prof = profile_map.get(pkg)
            if prof is not None:
                prof["hatcher_name"] = username
            print(col(f"{short_pkg(pkg)} -> {username} [{source}]", GREEN if source == "cookie/API" else YELLOW))
            changed = True
        else:
            print(col(f"{short_pkg(pkg)} -> unresolved", RED))
    if changed:
        save_config(cfg)
        save_hatcher_config(hcfg)
    return changed

def compute_age(created_str):
    """Return days since account creation, or 'N/A' if invalid."""
    if not created_str or created_str == "N/A":
        return "N/A"
    try:
        from datetime import datetime
        created_date = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
        age = (datetime.now().astimezone() - created_date).days
        return str(age)
    except:
        return "N/A"

PET_COUNTER_AUTOEXEC_TEMPLATE = r'''-- NOMO Pet Counter loader (network fallback)
getgenv().NOMO_PET_COUNTER = getgenv().NOMO_PET_COUNTER or {}
getgenv().NOMO_PET_COUNTER.WriteFolder = "nomo_rejoiner"
-- Legacy raw builds still read this table name; keep it as an alias.
getgenv().NOMO_GAG_COUNTER = getgenv().NOMO_PET_COUNTER
local src = game:HttpGet("https://pastebin.com/raw/PbgMUPUG")
src = src:gsub("game%.PrivateServerId", "nil")
src = src:gsub("game%.PrivateServerOwnerId", "nil")
local fn, err = loadstring(src)
if not fn then
    warn("[NOMO PET COUNTER] compile failed: " .. tostring(err))
    return
end
fn()
'''

MARKET_LOADER_AUTOEXEC_TEMPLATE = r'''-- NOMO Market / GAG loader template
if game.PlaceId == 126884695634066 then
    -- Grow a Garden
    print("grow a garden")
    loadstring(game:HttpGet("https://pastebin.com/raw/PJYuhuuk"))()
elseif game.PlaceId == 129954712878723 then
    -- Trade World
    print("trade world")
    loadstring(game:HttpGet("https://pastebin.com/raw/DNeJi4nC"))()
else
    return
end
'''

BUNDLED_PET_COUNTER_FILE = BASE_DIR / "nomo_pet_counter.lua"


def pet_counter_autoexec_source():
    """Prefer the bundled renamed counter; retain the raw loader only as fallback."""
    try:
        if BUNDLED_PET_COUNTER_FILE.exists():
            source = BUNDLED_PET_COUNTER_FILE.read_text(encoding="utf-8")
            if "NOMO PET COUNTER" in source and "nomo_rejoiner" in source:
                return source
    except Exception:
        pass
    return PET_COUNTER_AUTOEXEC_TEMPLATE

AUTOEXEC_DEFAULT_CUSTOM_FILE = "nomo_autoexec.lua"
AUTOEXEC_PET_COUNTER_FILE = "nomo_pet_counter.lua"
AUTOEXEC_MARKET_LOADER_FILE = "nomo_market_loader.lua"
_AUTOEXEC_ALLOWED_SUFFIXES = {".lua", ".luau", ".txt"}


def autoexec_dir_from_state_path(state_path):
    """Resolve the one real executor AutoExec directory from a clone state path.

    Arceus X stores startup scripts beside Workspace in ``Arceus X/Autoexec``.
    Delta stores them in ``Delta/Autoexecute``.  The old
    ``Arceus X/Workspace/autoexec`` path never existed and is intentionally not
    returned.
    """
    raw = str(state_path or "").strip().replace("\\", "/")
    if not raw:
        return None

    lower = raw.lower()
    arceus_marker = "/arceus x/workspace/"
    if arceus_marker in lower:
        idx = lower.index(arceus_marker)
        return Path(raw[:idx]) / "Arceus X" / "Autoexec"

    delta_marker = "/delta/workspace/"
    if delta_marker in lower:
        idx = lower.index(delta_marker)
        return Path(raw[:idx]) / "Delta" / "Autoexecute"

    # Fallback for unusual state filenames while still requiring a known
    # executor folder.  Never guess a Workspace/autoexec directory.
    p = Path(raw)
    parts = list(p.parts)
    for i, part in enumerate(parts):
        pl = str(part).lower()
        if pl == "arceus x":
            return Path(*parts[: i + 1]) / "Autoexec"
        if pl == "delta":
            return Path(*parts[: i + 1]) / "Autoexecute"
    return None


def autoexec_base_from_state_path(state_path):
    """Backward-compatible helper returning the executor root folder."""
    folder = autoexec_dir_from_state_path(state_path)
    return folder.parent if folder is not None else None


def autoexec_dirs_for_tab(tab, mode="both"):
    # ``mode`` is kept only so older call sites/configs remain compatible.
    # Every mode resolves to exactly one correct executor directory.
    explicit = str(tab.get("autoexec_path") or "").strip()
    folder = Path(explicit) if explicit else autoexec_dir_from_state_path(
        tab.get("stat_file") or tab.get("state_file") or ""
    )
    if folder is None:
        return []
    label = "Delta/Autoexecute" if folder.name.lower() == "autoexecute" else "Arceus X/Autoexec"
    return [(label, folder)]


def autoexec_paths_for_tab(tab, mode="both", filename=AUTOEXEC_DEFAULT_CUSTOM_FILE):
    return [folder / filename for _, folder in autoexec_dirs_for_tab(tab, mode)]


def autoexec_tabs(cfg):
    tabs = []
    seen = set()
    for tab in cfg.get("tabs", []):
        pkg = tab.get("package")
        if pkg and pkg not in seen:
            tabs.append(tab)
            seen.add(pkg)
    try:
        hcfg = load_hatcher_config()
        for prof in hatcher_profiles(hcfg, enabled_only=False):
            pkg = prof.get("package")
            if pkg and pkg not in seen:
                tabs.append(hatcher_profile_to_tab(prof))
                seen.add(pkg)
    except Exception:
        pass
    return tabs


def _autoexec_selected_tabs(cfg, title):
    tabs = autoexec_tabs(cfg)
    if not tabs:
        print(col("No packages found in config.", RED))
        pause()
        return []
    selected_pkgs = choose_packages_common(
        cfg,
        title,
        multi=True,
        include_discovered=False,
        configured_only=True,
    )
    if not selected_pkgs:
        return []
    selected_map = {t.get("package"): t for t in tabs}
    selected = [selected_map[p] for p in selected_pkgs if p in selected_map]
    if not selected:
        print(col("Selected packages have no configured AutoExec paths.", RED))
        pause()
    return selected


def _print_autoexec_full_paths(selected_tabs, cfg=None, heading="Resolved AutoExec path(s)"):
    print("")
    print(col(heading + ":", BOLD))
    shown = 0
    for tab in selected_tabs or []:
        pkg = str(tab.get("package") or "?")
        dirs = autoexec_dirs_for_tab(tab, "1")
        if not dirs:
            print(col(f"  {pkg} -> UNRESOLVED (check state_file)", RED))
            continue
        for _label, folder in dirs:
            print(f"  {col(pkg, CYAN)} -> {folder}")
            shown += 1
    return shown


def _choose_autoexec_location(cfg, allow_both=True, selected_tabs=None):
    """Show the exact paths and select the single valid AutoExec location.

    ``allow_both`` is retained for compatibility; V4.03 deliberately has no
    second Workspace/autoexec destination.
    """
    shown = _print_autoexec_full_paths(selected_tabs or [], cfg)
    if selected_tabs and shown == 0:
        print(col("No valid AutoExec path could be resolved.", RED))
        pause()
        return None
    return "1"


def _safe_autoexec_filename(raw, default=AUTOEXEC_DEFAULT_CUSTOM_FILE):
    name = clean_terminal_input(raw) or default
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    if not name:
        name = default
    suffix = Path(name).suffix.lower()
    if suffix not in _AUTOEXEC_ALLOWED_SUFFIXES:
        name += ".lua"
    return name[:120]


def _format_bytes(size):
    try:
        size = int(size)
    except Exception:
        return "?"
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    return f"{size / (1024 * 1024):.1f}M"


def _collect_autoexec_files(selected_tabs, path_mode="both"):
    records = []
    seen = set()
    empty_dirs = []
    for tab in selected_tabs:
        pkg = str(tab.get("package") or "")
        dirs = autoexec_dirs_for_tab(tab, path_mode)
        if not dirs:
            empty_dirs.append((pkg, "cannot derive AutoExec folder"))
            continue
        for label, folder in dirs:
            folder_key = (pkg, str(folder))
            try:
                files = [p for p in folder.iterdir() if p.is_file()] if folder.exists() else []
            except Exception as exc:
                empty_dirs.append((pkg, f"{label}: {exc}"))
                continue
            files = sorted(files, key=lambda p: p.name.lower())
            if not files:
                empty_dirs.append((pkg, f"{label}: empty/missing"))
            for path in files:
                key = (pkg, str(path))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = path.stat().st_size
                except Exception:
                    size = -1
                records.append({
                    "package": pkg,
                    "location": label,
                    "path": path,
                    "size": size,
                })
    return records, empty_dirs


def _parse_index_selection(raw, count, allow_all=True):
    raw = clean_terminal_input(raw)
    low = raw.lower()
    if allow_all and low in {"a", "all"}:
        return list(range(count))
    chosen = []
    for token in re.split(r"[\s,]+", raw):
        token = token.strip()
        if not token:
            continue
        if re.fullmatch(r"\d+-\d+", token):
            a, b = [int(x) for x in token.split("-", 1)]
            step = 1 if b >= a else -1
            for num in range(a, b + step, step):
                idx = num - 1
                if 0 <= idx < count and idx not in chosen:
                    chosen.append(idx)
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < count and idx not in chosen:
                chosen.append(idx)
    return chosen


def _print_autoexec_file_table(records, cfg):
    rows = []
    for i, rec in enumerate(records, 1):
        rows.append([
            (str(i), CYAN),
            (short_pkg(rec["package"]), WHITE),
            (rec["location"], DIM),
            (rec["path"].name, WHITE),
            (_format_bytes(rec["size"]), DIM, True),
        ])
    draw_table(["No", "Package", "Location", "File", "Size"], rows,
               [3, 10, 18, 28, 7], cfg)


def _choose_autoexec_records(records, action="Choose file(s)", multi=True):
    print("")
    print(col(action, BOLD))
    print(col("Use numbers/ranges (1,3-5), A=all, 0=back.", DIM))
    while True:
        raw = clean_terminal_input(input("Choose: "))
        if raw.lower() in {"", "0", "q", "quit", "b", "back"}:
            return []
        idxs = _parse_index_selection(raw, len(records), allow_all=True)
        if idxs:
            if not multi:
                idxs = idxs[:1]
            return [records[i] for i in idxs]
        print(col("Invalid file selection.", RED))


def _read_autoexec_text(path, max_chars=40000):
    try:
        data = path.read_bytes()
    except Exception as exc:
        return None, f"read failed: {exc}", False
    truncated = len(data) > max_chars
    data = data[:max_chars]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    return text, "", truncated


def autoexec_view_files(cfg):
    selected = _autoexec_selected_tabs(cfg, "AUTOEXEC: VIEW CONTENTS")
    if not selected:
        return
    path_mode = _choose_autoexec_location(cfg, allow_both=True, selected_tabs=selected)
    if path_mode is None:
        return
    records, empty_dirs = _collect_autoexec_files(selected, path_mode)
    clear()
    banner("AUTOEXEC FILES", cfg)
    if not records:
        print(col("No AutoExec files found in the selected folder(s).", YELLOW))
        for pkg, note in empty_dirs:
            print(col(f"{short_pkg(pkg)}: {note}", DIM))
        pause()
        return
    _print_autoexec_file_table(records, cfg)
    chosen = _choose_autoexec_records(records, "View which file(s)?", multi=True)
    if not chosen:
        return
    for rec in chosen:
        clear()
        banner(f"{short_pkg(rec['package'])}: {rec['path'].name}", cfg)
        print(col(str(rec["path"]), DIM))
        print(col("─" * term_width(cfg), DIM))
        content, err, truncated = _read_autoexec_text(rec["path"])
        if err:
            print(col(err, RED))
        else:
            lines = content.splitlines()
            if not lines:
                print(col("<empty file>", YELLOW))
            else:
                for no, line_text in enumerate(lines, 1):
                    print(f"{col(str(no).rjust(4), DIM)}  {line_text}")
            if truncated:
                print(col("\n<preview truncated at 40 KB>", YELLOW))
        pause()


def _write_autoexec_script(selected, path_mode, filename, script, cfg, replace_prompt=True):
    destinations = []
    for tab in selected:
        paths = autoexec_paths_for_tab(tab, path_mode, filename=filename)
        if not paths:
            print(col(f"{tab.get('package')}: cannot derive AutoExec path", RED))
            continue
        for path in paths:
            destinations.append((tab.get("package"), path))
    if not destinations:
        pause()
        return 0

    existing = [(pkg, path) for pkg, path in destinations if path.exists()]
    print("")
    print(col("Destinations:", BOLD))
    for pkg, path in destinations:
        state = "REPLACE" if path.exists() else "CREATE"
        state_color = YELLOW if path.exists() else GREEN
        print(f"  {col(short_pkg(pkg), CYAN)}  {col(state, state_color)}  {path}")
    if existing and replace_prompt:
        ans = clean_terminal_input(input("\nReplace existing selected file(s)? [Y/n]: ")).lower()
        if ans in {"n", "no"}:
            print(col("Cancelled; nothing changed.", YELLOW))
            pause()
            return 0

    wrote = 0
    unchanged = 0
    for pkg, path in destinations:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = None
            if path.exists():
                try:
                    old = path.read_text(encoding="utf-8")
                except Exception:
                    old = None
            if old == script:
                print(col(f"Unchanged: {path}", DIM))
                unchanged += 1
                continue
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(script, encoding="utf-8")
            os.replace(str(tmp), str(path))
            print(col(f"Saved: {path}", GREEN))
            wrote += 1
        except Exception as exc:
            print(col(f"Failed: {path} -> {exc}", RED))
    print(col(f"\nDone. Wrote {wrote}; unchanged {unchanged}.", GREEN if wrote or unchanged else YELLOW))
    pause()
    return wrote


def autoexec_install_pet_counter(cfg):
    selected = _autoexec_selected_tabs(cfg, "AUTOEXEC: ADD PET COUNTER")
    if not selected:
        return
    path_mode = _choose_autoexec_location(cfg, allow_both=True, selected_tabs=selected)
    if path_mode is None:
        return
    clear()
    banner("PET COUNTER TEMPLATE", cfg)
    print(col("File: " + AUTOEXEC_PET_COUNTER_FILE, BOLD))
    counter_source = pet_counter_autoexec_source()
    print(counter_source.rstrip())
    print("")
    print(col("This creates a separate file and does not overwrite nomo_autoexec.lua.", DIM))
    _write_autoexec_script(
        selected,
        path_mode,
        AUTOEXEC_PET_COUNTER_FILE,
        counter_source,
        cfg,
        replace_prompt=False,
    )


def autoexec_install_market_loader(cfg):
    selected = _autoexec_selected_tabs(cfg, "AUTOEXEC: ADD MARKET / GAG LOADER")
    if not selected:
        return
    path_mode = _choose_autoexec_location(cfg, allow_both=True, selected_tabs=selected)
    if path_mode is None:
        return
    clear()
    banner("MARKET / GAG LOADER TEMPLATE", cfg)
    print(col("File: " + AUTOEXEC_MARKET_LOADER_FILE, BOLD))
    print(MARKET_LOADER_AUTOEXEC_TEMPLATE.rstrip())
    print("")
    print(col("Grow a Garden and Trade World use separate fixed loaders.", DIM))
    print(col("This creates a separate file and does not overwrite the Pet Counter or nomo_autoexec.lua.", DIM))
    _write_autoexec_script(
        selected,
        path_mode,
        AUTOEXEC_MARKET_LOADER_FILE,
        MARKET_LOADER_AUTOEXEC_TEMPLATE,
        cfg,
        replace_prompt=False,
    )


def autoexec_write_custom(cfg):
    selected = _autoexec_selected_tabs(cfg, "AUTOEXEC: WRITE CUSTOM LUA")
    if not selected:
        return
    path_mode = _choose_autoexec_location(cfg, allow_both=True, selected_tabs=selected)
    if path_mode is None:
        return
    raw_name = input(f"Filename [{AUTOEXEC_DEFAULT_CUSTOM_FILE}]: ")
    filename = _safe_autoexec_filename(raw_name, AUTOEXEC_DEFAULT_CUSTOM_FILE)
    print("")
    print(col(f"Paste Lua for {filename}. Finish with a line containing only STOP.", YELLOW))
    print(col("Type CANCEL on its own line to abort.", DIM))
    lines = []
    while True:
        line_text = input()
        marker = clean_terminal_input(line_text).upper()
        if marker == "STOP":
            break
        if marker == "CANCEL":
            lines = []
            break
        lines.append(line_text)
    if not lines:
        print(col("No script saved.", YELLOW))
        pause()
        return
    script = "\n".join(lines).rstrip() + "\n"
    _write_autoexec_script(selected, path_mode, filename, script, cfg, replace_prompt=True)


def autoexec_delete_files(cfg):
    selected = _autoexec_selected_tabs(cfg, "AUTOEXEC: DELETE FILES")
    if not selected:
        return
    path_mode = _choose_autoexec_location(cfg, allow_both=True, selected_tabs=selected)
    if path_mode is None:
        return
    records, empty_dirs = _collect_autoexec_files(selected, path_mode)
    clear()
    banner("DELETE AUTOEXEC FILES", cfg)
    if not records:
        print(col("No AutoExec files found.", YELLOW))
        pause()
        return
    _print_autoexec_file_table(records, cfg)
    chosen = _choose_autoexec_records(records, "Delete which file(s)?", multi=True)
    if not chosen:
        return
    print("")
    print(col("Selected for deletion:", RED))
    for rec in chosen:
        print(f"  {short_pkg(rec['package'])}: {rec['path']}")
    confirm = clean_terminal_input(input("\nType DELETE to permanently remove only these files: "))
    if confirm.upper() != "DELETE":
        print(col("Cancelled; nothing deleted.", YELLOW))
        pause()
        return
    deleted = 0
    for rec in chosen:
        try:
            rec["path"].unlink()
            print(col(f"Deleted: {rec['path']}", GREEN))
            deleted += 1
        except FileNotFoundError:
            print(col(f"Already missing: {rec['path']}", YELLOW))
        except Exception as exc:
            print(col(f"Failed: {rec['path']} -> {exc}", RED))
    print(col(f"\nDeleted {deleted} file(s).", GREEN if deleted else YELLOW))
    pause()



def autoexec_copy_files(cfg):
    """Copy selected AutoExec scripts from one package to other packages."""
    tabs = autoexec_tabs(cfg)
    tab_map = {str(t.get("package") or ""): t for t in tabs if t.get("package")}
    if not tab_map:
        print(col("No configured packages have AutoExec paths.", RED))
        pause()
        return

    source_pkgs = choose_packages_common(
        cfg,
        "AUTOEXEC COPY: SOURCE PACKAGE",
        multi=False,
        include_discovered=False,
        configured_only=True,
    )
    if not source_pkgs:
        return
    source_pkg = source_pkgs[0]
    source_tab = tab_map.get(source_pkg)
    if source_tab is None:
        print(col("Source package has no configured AutoExec path.", RED))
        pause()
        return

    clear()
    banner(f"COPY FROM {short_pkg(source_pkg)}", cfg)
    print(col("Choose the source folder containing the files to copy.", DIM))
    source_mode = _choose_autoexec_location(cfg, allow_both=False, selected_tabs=[source_tab])
    if source_mode is None:
        return

    records, empty_dirs = _collect_autoexec_files([source_tab], source_mode)
    records = [
        rec for rec in records
        if rec["path"].suffix.lower() in _AUTOEXEC_ALLOWED_SUFFIXES
    ]
    clear()
    banner("AUTOEXEC COPY: SELECT FILES", cfg)
    if not records:
        print(col("No Lua/script files found in the selected source folder.", YELLOW))
        for pkg, note in empty_dirs:
            print(col(f"{short_pkg(pkg)}: {note}", DIM))
        pause()
        return

    _print_autoexec_file_table(records, cfg)
    chosen = _choose_autoexec_records(records, "Copy which file(s)?", multi=True)
    if not chosen:
        return

    destination_pkgs = choose_packages_common(
        cfg,
        "AUTOEXEC COPY: DESTINATIONS",
        multi=True,
        include_discovered=False,
        configured_only=True,
    )
    if not destination_pkgs:
        return

    removed_source = source_pkg in destination_pkgs
    destination_pkgs = [pkg for pkg in destination_pkgs if pkg != source_pkg]
    if removed_source:
        print(col(f"Source {short_pkg(source_pkg)} excluded from destinations.", DIM))
    destination_tabs = [tab_map[pkg] for pkg in destination_pkgs if pkg in tab_map]
    if not destination_tabs:
        print(col("Choose at least one destination package other than the source.", RED))
        pause()
        return

    clear()
    banner("AUTOEXEC COPY: DESTINATION LOCATION", cfg)
    print(col("Choose where the selected files should be copied.", DIM))
    destination_mode = _choose_autoexec_location(cfg, allow_both=True, selected_tabs=destination_tabs)
    if destination_mode is None:
        return

    print("")
    print("Existing-file behavior:")
    print("1. Overwrite same filenames")
    print("2. Skip existing files")
    print("0. Back")
    while True:
        conflict = read_menu_choice("Select: ", valid={"0", "1", "2"})
        if conflict is None:
            print(col("Invalid choice. Use 1, 2, or 0.", RED))
            continue
        break
    if conflict == "0":
        return
    overwrite = conflict == "1"

    clear()
    banner("CONFIRM AUTOEXEC COPY", cfg)
    print(col(f"Source: {source_pkg}", BOLD))
    print(col("Files:", BOLD))
    for rec in chosen:
        print(f"  - {rec['path'].name} ({_format_bytes(rec['size'])})")
    print(col("Destinations:", BOLD))
    for tab in destination_tabs:
        for label, folder in autoexec_dirs_for_tab(tab, destination_mode):
            print(f"  - {tab.get('package')}  [{label}]  {folder}")
    print(col(f"Conflict mode: {'OVERWRITE' if overwrite else 'SKIP EXISTING'}", YELLOW))
    confirm = clean_terminal_input(input("\nCopy now? [Y/n]: ")).lower()
    if confirm in {"n", "no"}:
        print(col("Cancelled; nothing copied.", YELLOW))
        pause()
        return

    totals = {"copied": 0, "skipped": 0, "failed": 0}
    package_results = {}

    for tab in destination_tabs:
        pkg = str(tab.get("package") or "")
        result = package_results.setdefault(pkg, {"copied": 0, "skipped": 0, "failed": 0, "notes": []})
        dirs = autoexec_dirs_for_tab(tab, destination_mode)
        if not dirs:
            result["failed"] += len(chosen)
            totals["failed"] += len(chosen)
            result["notes"].append("cannot derive destination AutoExec folder")
            continue

        for label, folder in dirs:
            for rec in chosen:
                source_path = rec["path"]
                target = folder / source_path.name
                try:
                    if target.exists() and not overwrite:
                        result["skipped"] += 1
                        totals["skipped"] += 1
                        result["notes"].append(f"skip {label}/{target.name}")
                        continue

                    data = source_path.read_bytes()
                    folder.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(target.name + ".nomo-copy.tmp")
                    tmp.write_bytes(data)
                    os.replace(str(tmp), str(target))
                    result["copied"] += 1
                    totals["copied"] += 1
                    result["notes"].append(f"copied {label}/{target.name}")
                except Exception as exc:
                    result["failed"] += 1
                    totals["failed"] += 1
                    result["notes"].append(f"FAILED {label}/{target.name}: {exc}")
                    try:
                        tmp = target.with_name(target.name + ".nomo-copy.tmp")
                        if tmp.exists():
                            tmp.unlink()
                    except Exception:
                        pass

    clear()
    banner("AUTOEXEC COPY RESULTS", cfg)
    for pkg, result in package_results.items():
        color = GREEN if result["failed"] == 0 else YELLOW
        print(col(
            f"{short_pkg(pkg)}: copied {result['copied']}, skipped {result['skipped']}, failed {result['failed']}",
            color,
        ))
        for note in result["notes"]:
            print(col(f"  {note}", RED if note.startswith("FAILED") else DIM))
    print("")
    overall_color = GREEN if totals["failed"] == 0 else YELLOW
    print(col(
        f"Done. Copied {totals['copied']}; skipped {totals['skipped']}; failed {totals['failed']}.",
        overall_color,
    ))
    pause()


def _autoexec_sha256(path):
    """Return a SHA-256 digest for a local AutoExec file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _autoexec_safe_move(source_path, target_path, overwrite=False):
    """Move one file without deleting the source until the target is verified.

    Returns ``(status, note)`` where status is moved, skipped, unchanged, or failed.
    """
    source_path = Path(source_path)
    target_path = Path(target_path)
    try:
        try:
            if source_path.resolve() == target_path.resolve():
                return "unchanged", "source and destination are identical"
        except Exception:
            if str(source_path) == str(target_path):
                return "unchanged", "source and destination are identical"

        if not source_path.exists() or not source_path.is_file():
            return "failed", "source file is missing"
        if target_path.exists() and not overwrite:
            return "skipped", "destination already exists"

        data_hash = _autoexec_sha256(source_path)
        source_size = source_path.stat().st_size
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(
            f".{target_path.name}.nomo-move-{os.getpid()}-{int(time.time() * 1000)}.tmp"
        )

        try:
            with open(source_path, "rb") as src, open(temp_path, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                try:
                    os.fsync(dst.fileno())
                except Exception:
                    pass

            if temp_path.stat().st_size != source_size:
                raise RuntimeError("temporary copy size mismatch")
            if _autoexec_sha256(temp_path) != data_hash:
                raise RuntimeError("temporary copy checksum mismatch")

            os.replace(str(temp_path), str(target_path))
            if not target_path.exists() or target_path.stat().st_size != source_size:
                raise RuntimeError("destination verification failed")
            if _autoexec_sha256(target_path) != data_hash:
                raise RuntimeError("destination checksum mismatch")

            source_path.unlink()
            return "moved", "verified and source removed"
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
    except Exception as exc:
        return "failed", str(exc)


def autoexec_rename_move_files(cfg):
    """Rename one AutoExec script or move selected scripts to one package."""
    tabs = autoexec_tabs(cfg)
    tab_map = {str(t.get("package") or ""): t for t in tabs if t.get("package")}
    if not tab_map:
        print(col("No configured packages have AutoExec paths.", RED))
        pause()
        return

    source_pkgs = choose_packages_common(
        cfg,
        "AUTOEXEC RENAME/MOVE: SOURCE PACKAGE",
        multi=False,
        include_discovered=False,
        configured_only=True,
    )
    if not source_pkgs:
        return
    source_pkg = source_pkgs[0]
    source_tab = tab_map.get(source_pkg)
    if source_tab is None:
        print(col("Source package has no configured AutoExec path.", RED))
        pause()
        return

    clear()
    banner(f"RENAME/MOVE FROM {short_pkg(source_pkg)}", cfg)
    source_mode = _choose_autoexec_location(cfg, allow_both=False, selected_tabs=[source_tab])
    if source_mode is None:
        return

    records, empty_dirs = _collect_autoexec_files([source_tab], source_mode)
    records = [
        rec for rec in records
        if rec["path"].suffix.lower() in _AUTOEXEC_ALLOWED_SUFFIXES
    ]
    clear()
    banner("AUTOEXEC RENAME / MOVE", cfg)
    if not records:
        print(col("No Lua/script files found in the selected source folder.", YELLOW))
        for pkg, note in empty_dirs:
            print(col(f"{short_pkg(pkg)}: {note}", DIM))
        pause()
        return

    _print_autoexec_file_table(records, cfg)
    chosen = _choose_autoexec_records(records, "Choose file(s) to rename/move", multi=True)
    if not chosen:
        return

    print("")
    print("1. Rename in the same package")
    print("2. Move to another package")
    print("0. Back")
    valid_actions = {"0", "2"}
    if len(chosen) == 1:
        valid_actions.add("1")
    else:
        print(col("Rename is available only when one file is selected.", DIM))
    while True:
        action = read_menu_choice("Select: ", valid=valid_actions)
        if action is not None:
            break
        allowed = "1, 2, or 0" if len(chosen) == 1 else "2 or 0"
        print(col(f"Invalid choice. Use {allowed}.", RED))
    if action == "0":
        return

    destination_tab = source_tab
    new_name = None
    operation_label = "RENAME"

    if action == "1":
        source_record = chosen[0]
        raw_name = input(f"New filename [{source_record['path'].name}]: ")
        new_name = _safe_autoexec_filename(raw_name, source_record["path"].name)
        if new_name == source_record["path"].name:
            print(col("Filename is unchanged; nothing to do.", YELLOW))
            pause()
            return
    else:
        destination_pkgs = choose_packages_common(
            cfg,
            "AUTOEXEC MOVE: DESTINATION PACKAGE",
            multi=False,
            include_discovered=False,
            configured_only=True,
        )
        if not destination_pkgs:
            return
        destination_pkg = destination_pkgs[0]
        if destination_pkg == source_pkg:
            print(col("Choose a different package for Move. Use Rename for the same package.", RED))
            pause()
            return
        destination_tab = tab_map.get(destination_pkg)
        if destination_tab is None:
            print(col("Destination package has no configured AutoExec path.", RED))
            pause()
            return
        operation_label = "MOVE"
        if len(chosen) == 1:
            raw_name = input(f"Destination filename [{chosen[0]['path'].name}]: ")
            new_name = _safe_autoexec_filename(raw_name, chosen[0]["path"].name)

    clear()
    banner(f"AUTOEXEC {operation_label}: DESTINATION", cfg)
    destination_mode = _choose_autoexec_location(
        cfg, allow_both=False, selected_tabs=[destination_tab]
    )
    if destination_mode is None:
        return
    destination_dirs = autoexec_dirs_for_tab(destination_tab, destination_mode)
    if not destination_dirs:
        print(col("Destination AutoExec path could not be resolved.", RED))
        pause()
        return
    destination_folder = destination_dirs[0][1]

    targets = []
    for rec in chosen:
        filename = new_name if len(chosen) == 1 and new_name else rec["path"].name
        targets.append((rec, destination_folder / filename))

    conflicts = [(rec, target) for rec, target in targets if target.exists()]
    overwrite = False
    if conflicts:
        print("")
        print(col("Existing destination file(s):", YELLOW))
        for rec, target in conflicts:
            try:
                same = rec["path"].resolve() == target.resolve()
            except Exception:
                same = str(rec["path"]) == str(target)
            if not same:
                print(f"  - {target}")
        print("")
        print("1. Overwrite existing destination file(s)")
        print("2. Skip existing destination file(s)")
        print("0. Cancel")
        conflict = read_menu_choice("Select: ", valid={"0", "1", "2"})
        if conflict in {None, "0"}:
            print(col("Cancelled; nothing changed.", YELLOW))
            pause()
            return
        overwrite = conflict == "1"

    clear()
    banner(f"CONFIRM AUTOEXEC {operation_label}", cfg)
    print(col(f"Source package: {source_pkg}", BOLD))
    print(col(f"Destination package: {destination_tab.get('package')}", BOLD))
    print("")
    for rec, target in targets:
        print(f"  {rec['path']}\n    -> {target}")
    print("")
    if overwrite:
        print(col("Existing target files will be overwritten.", YELLOW))
    confirm = clean_terminal_input(input(f"Proceed with {operation_label.lower()}? [Y/n]: ")).lower()
    if confirm in {"n", "no"}:
        print(col("Cancelled; nothing changed.", YELLOW))
        pause()
        return

    counts = {"moved": 0, "skipped": 0, "unchanged": 0, "failed": 0}
    results = []
    for rec, target in targets:
        status, note = _autoexec_safe_move(rec["path"], target, overwrite=overwrite)
        counts[status] = counts.get(status, 0) + 1
        results.append((rec["path"], target, status, note))

    clear()
    banner(f"AUTOEXEC {operation_label} RESULTS", cfg)
    for source_path, target, status, note in results:
        color = GREEN if status == "moved" else (YELLOW if status in {"skipped", "unchanged"} else RED)
        print(col(f"{status.upper()}: {source_path.name}", color))
        print(col(f"  {source_path}", DIM))
        print(col(f"  -> {target}", DIM))
        if note:
            print(col(f"  {note}", DIM if status != "failed" else RED))
    print("")
    summary_color = GREEN if counts["failed"] == 0 else YELLOW
    print(col(
        "Done. "
        f"Moved {counts['moved']}; skipped {counts['skipped']}; "
        f"unchanged {counts['unchanged']}; failed {counts['failed']}.",
        summary_color,
    ))
    pause()

def autoexec_menu(cfg):
    while True:
        cfg = load_config()
        clear()
        banner("AUTOEXEC MANAGER", cfg)
        print(col("Inspect and manage scripts per package. Nothing runs in a background loop.", DIM))
        print("")
        rows = [
            ("1", "View files and contents", CYAN, WHITE),
            ("2", "Add/update Pet Counter template", GREEN, WHITE),
            ("3", "Write/replace custom Lua file", CYAN, WHITE),
            ("4", "Delete selected AutoExec files", RED, WHITE),
            ("5", "Copy Lua between packages", MAGENTA, WHITE),
            ("6", "Rename/move AutoExec files", CYAN, WHITE),
            ("7", "Add/update Market loader template", GREEN, WHITE),
            ("0", "Back", RED, WHITE),
        ]
        draw_boxed_menu(rows, cfg)
        print("")
        print(col(f"Pet Counter source: {'bundled local file' if BUNDLED_PET_COUNTER_FILE.exists() else 'network fallback'}", DIM))
        choice = read_menu_choice("\nOption: ", valid={"0", "1", "2", "3", "4", "5", "6", "7"})
        if choice is None:
            print(col("Invalid option. Use one of the numbers shown.", RED))
            time.sleep(1)
            continue
        if choice == "0":
            return
        if choice == "1":
            autoexec_view_files(cfg)
        elif choice == "2":
            autoexec_install_pet_counter(cfg)
        elif choice == "3":
            autoexec_write_custom(cfg)
        elif choice == "4":
            autoexec_delete_files(cfg)
        elif choice == "5":
            autoexec_copy_files(cfg)
        elif choice == "6":
            autoexec_rename_move_files(cfg)
        elif choice == "7":
            autoexec_install_market_loader(cfg)


def export_cookies(selected_packages=None):
    packages = get_installed_packages()
    if selected_packages is not None:
        wanted = set(selected_packages)
        packages = [p for p in packages if p in wanted]
    if not packages:
        print("[COOKIE] No packages found.")
        pause()
        return

    cache = {}
    export_lines = []
    auth_lines = []
    cookies_only = []

    for pkg in packages:
        print(f"Extracting from {pkg}...")
        cookie = None
        db_src = f"/data/data/{pkg}/app_webview/Default/Cookies"
        db_dst = "/sdcard/Download/tmp_cookie.db"

        try:
            p = subprocess.run(
                ["su", "-c", f"cp {db_src} {db_dst}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if p.returncode == 0 and os.path.exists(db_dst):
                conn = sqlite3.connect(db_dst, timeout=1)
                c = conn.cursor()
                c.execute(
                    "SELECT value FROM cookies WHERE host_key LIKE '%.roblox.com' AND name='.ROBLOSECURITY'"
                )
                row = c.fetchone()
                conn.close()
                if row:
                    cookie = row[0]
        except subprocess.TimeoutExpired:
            print("  -> su cp timed out")
        except Exception as e:
            print(f"  -> Error: {e}")
        finally:
            if os.path.exists(db_dst):
                os.remove(db_dst)

        cookie_str = cookie if cookie else "_WARNING"

        username = pkg
        cfg = load_config()
        for tab in cfg.get("tabs", []):
            if tab.get("package") == pkg:
                state_path = tab.get("stat_file", "")
                if state_path and os.path.exists(state_path):
                    try:
                        with open(state_path) as f:
                            state = json.load(f)
                            if state.get("username"):
                                username = state.get("username")
                    except:
                        pass
                break

        export_lines.append(f"{pkg} {cookie_str}")
        auth_lines.append(f"{username}:{cookie_str}")
        cookies_only.append(cookie_str)

        cache[pkg] = {"cookie": cookie, "username": username, "updated": now()}
        print(f"  -> {'OK' if cookie else 'MISSING'}")

    # Save cache JSON
    with open(COOKIE_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

    # Write files
    export_file = BASE_DIR / "cookie_export.txt"
    auth_file = BASE_DIR / "cookie_auth.txt"
    only_file = BASE_DIR / "cookies_only.txt"
    with open(export_file, "w") as f:
        f.write("\n".join(export_lines))
    with open(auth_file, "w") as f:
        f.write("\n".join(auth_lines))
    with open(only_file, "w") as f:
        f.write("\n".join(cookies_only))

    print(f"\nExported {len(cache)} cookies.")
    print(f"   - Full cache: {COOKIE_CACHE}")
    print(f"   - Package cookie: {export_file}")
    print(f"   - Username:cookie: {auth_file}")
    print(f"   - Cookies only: {only_file}")
    pause()


def cookie_menu(cfg):
    while True:
        clear()
        banner("COOKIE EXPORT", cfg)
        print("1. Export to local files")
        print("2. Export to webhook (with package selection)")
        print("3. Set webhook URL")
        print("4. Test webhook")
        print("0. Back")
        print("")
        webhook = cfg.get("cookie_webhook_url", "").strip()
        shown = mask_secret(webhook) if webhook else "not set"
        print(f"Webhook URL: {shown}")
        drain_stdin()
        ch = input("\nChoose: ").strip()
        if ch == "0":
            return
        elif ch == "1":
            selected = select_packages_menu(cfg)
            if selected:
                export_cookies(selected)
        elif ch == "2":
            selected = select_packages_menu(cfg)
            if selected:
                export_cookies_webhook(cfg, selected)
        elif ch == "3":
            url = input("Paste webhook URL (Discord): ").strip()
            if url:
                cfg["cookie_webhook_url"] = url
                save_config(cfg)
                print(col("Saved.", GREEN))
                pause()
        elif ch == "4":
            test_webhook(cfg)
        else:
            print("Invalid.")
            time.sleep(1)

def test_webhook(cfg):
    webhook = cfg.get("cookie_webhook_url", "").strip()
    if not webhook:
        print(col("Webhook URL not set. Use option 3 first.", RED))
        pause()
        return

    payload = json.dumps({"content": "NOMO REJOIN - Webhook test successful!"}).encode("utf-8")
    try:
        req = urllib.request.Request(webhook, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "NOMO-Rejoin/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.getcode() in (200, 204):
                print(col("Test message sent successfully!", GREEN))
            else:
                print(col(f"Test failed: {resp.getcode()}", RED))
    except urllib.error.HTTPError as e:
        print(col(f"HTTP {e.code}: {e.reason}", RED))
        try:
            body = e.read().decode()
            print(col(f"Response: {body}", DIM))
        except:
            pass
    except Exception as e:
        print(col(f"Test failed: {e}", RED))
    pause()

def check_cookie_challenge(cookie):
    """Check cookie auth state via GET /v1/users/authenticated."""
    if not cookie:
        return "error"
    url = "https://users.roblox.com/v1/users/authenticated"
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "User-Agent": "NOMO-Rejoin/1.0",
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return "valid" if resp.status == 200 else "error"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "invalid"
        elif e.code == 403:
            if e.headers.get("rblx-challenge-id"):
                return "challenge"
            return "invalid"
        else:
            return "error"
    except Exception:
        return "error"

def get_xsrf_token(cookie):
    """Get XSRF token from Roblox auth endpoint."""
    url = "https://auth.roblox.com/v2/logout"
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return None
    except urllib.error.HTTPError as e:
        if e.code == 403:
            token = e.headers.get("x-csrf-token")
            if token:
                return token
        return None
    except Exception as e:
        print(f"Error getting XSRF token: {e}")
        return None


def get_universe_id_from_place(place_id):
    """Get universe ID from a place ID using the official endpoint (no auth required)."""
    url = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("universeId")
    except Exception as e:
        print(f"Error getting universe ID: {e}")
        return None

def roblox_http_error_text(e):
    try:
        body = e.read().decode("utf-8", errors="replace")
        return cut(body, 240)
    except Exception:
        return ""



def _roblox_json_request(url, cookie="", method="GET", payload=None, timeout=20, xsrf_token=""):
    """Return (json_data, error_text, response_headers) for a Roblox web API call."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    if cookie:
        headers["Cookie"] = f".ROBLOSECURITY={cookie}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if xsrf_token:
        headers["X-CSRF-TOKEN"] = xsrf_token

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            return data, "", dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        detail = roblox_http_error_text(e)
        return None, f"HTTP {e.code}" + (f" {detail}" if detail else ""), dict(e.headers.items())
    except Exception as e:
        return None, str(e), {}


def _first_value(obj, keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        val = obj.get(key)
        if val is not None and val != "":
            return val
    for nested_key in ("privateServer", "vipServer", "server", "subscription", "permissions"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                val = nested.get(key)
                if val is not None and val != "":
                    return val
    return None


def _normalize_private_server_item(item):
    """Normalize Roblox's old/new private-server response field variants."""
    if not isinstance(item, dict):
        return None
    server_id = _first_value(item, (
        "vipServerId", "privateServerId", "serverId", "id"
    ))
    link_code = _first_value(item, (
        "privateServerLinkCode", "linkCode", "joinCode", "privateServerCode"
    ))
    access_code = _first_value(item, (
        "accessCode", "reservedServerAccessCode"
    ))
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    owner_id = _first_value(item, ("ownerId", "ownerUserId", "userId"))
    if not owner_id:
        owner_id = _first_value(owner, ("id", "userId"))
    name = _first_value(item, ("name", "serverName")) or "NOMO Hatcher"
    active = _first_value(item, ("active", "isActive", "subscriptionActive"))
    if active is None:
        active = True
    elif isinstance(active, str):
        active = active.strip().lower() not in {"0", "false", "no", "off", "inactive", "cancelled", "canceled"}
    else:
        active = bool(active)
    return {
        "id": str(server_id) if server_id is not None else "",
        "name": str(name),
        "link_code": str(link_code) if link_code is not None else "",
        "access_code": str(access_code) if access_code is not None else "",
        "owner_id": str(owner_id) if owner_id is not None else "",
        "active": bool(active),
        "raw": item,
    }


def _private_server_items_from_response(data):
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = (
            data.get("data")
            or data.get("privateServers")
            or data.get("vipServers")
            or data.get("servers")
            or []
        )
    else:
        raw_items = []
    out = []
    for raw in raw_items:
        item = _normalize_private_server_item(raw)
        if item and (item.get("id") or item.get("link_code") or item.get("access_code")):
            out.append(item)
    return out


def fetch_private_server_metadata(cookie, server_id):
    if not server_id:
        return None
    url = f"https://games.roblox.com/v1/vip-servers/{urllib.parse.quote(str(server_id), safe='')}"
    data, err, _ = _roblox_json_request(url, cookie=cookie, timeout=15)
    if err or not data:
        return None
    return _normalize_private_server_item(data)


def _merge_private_server_item(base, extra):
    if not base:
        return extra
    if not extra:
        return base
    merged = dict(base)
    for key in ("id", "name", "link_code", "access_code", "owner_id"):
        if not merged.get(key) and extra.get(key):
            merged[key] = extra[key]
    if extra.get("active") is False:
        merged["active"] = False
    return merged


def fetch_owned_private_servers(cookie, place_id, universe_id, user_id=None):
    """Fetch the authenticated account's owned server, with endpoint fallbacks."""
    query = urllib.parse.urlencode({
        "universeId": str(universe_id),
        "placeId": str(place_id),
        "limit": 100,
        "sortOrder": "Asc",
    })
    endpoints = [
        f"https://games.roblox.com/v1/private-servers/my-private-servers?{query}",
        f"https://games.roblox.com/v1/vip-servers/my-private-servers?{query}",
        f"https://games.roblox.com/v1/games/{place_id}/private-servers?limit=100&sortOrder=Asc",
    ]

    seen = set()
    all_items = []
    errors = []
    for endpoint in endpoints:
        url = endpoint
        pages = 0
        while url and pages < 5:
            pages += 1
            data, err, _ = _roblox_json_request(url, cookie=cookie, timeout=20)
            if err:
                errors.append(err)
                break
            for item in _private_server_items_from_response(data):
                # The first two endpoints are explicitly "my" servers. The place
                # endpoint can contain invited servers, so filter by owner when it
                # exposes an owner id.
                if "games/" in endpoint and user_id and item.get("owner_id"):
                    if str(item.get("owner_id")) != str(user_id):
                        continue
                dedupe = item.get("id") or item.get("link_code") or item.get("access_code")
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                all_items.append(item)
            cursor = ""
            if isinstance(data, dict):
                cursor = str(data.get("nextPageCursor") or data.get("nextCursor") or "").strip()
            if not cursor:
                break
            sep = "&" if "?" in endpoint else "?"
            url = endpoint + sep + urllib.parse.urlencode({"cursor": cursor})

        if all_items:
            break

    # Prefer active servers and enrich missing join codes from metadata.
    enriched = []
    for item in all_items:
        if item.get("id") and not (item.get("link_code") or item.get("access_code")):
            item = _merge_private_server_item(item, fetch_private_server_metadata(cookie, item["id"]))
        enriched.append(item)
    enriched.sort(key=lambda x: (not bool(x.get("active", True)), not bool(x.get("link_code") or x.get("access_code"))))
    return enriched, "; ".join(dict.fromkeys(errors))


def get_private_server_product_info(universe_id):
    """Return public capability metadata as (enabled, price, raw, error).

    `enabled` is only a universe-level/public signal. It is NOT authoritative for
    account-specific entitlement (for example, a Roblox Plus account may still be
    offered a Create button). The create POST remains the source of truth.
    """
    url = f"https://games.roblox.com/v1/private-servers/enabled-in-universe/{universe_id}"
    data, err, _ = _roblox_json_request(url, timeout=15)
    if err or not isinstance(data, dict):
        return None, None, data or {}, err or "invalid response"

    enabled_raw = _first_value(data, ("privateServersEnabled", "enabled", "isEnabled"))
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    elif enabled_raw is None:
        enabled = None
    else:
        enabled = bool(enabled_raw)

    price_raw = _first_value(data, ("price", "privateServerPrice", "vipServerPrice", "expectedPrice"))
    if price_raw is None or price_raw == "":
        price = None
    else:
        try:
            price = max(0, int(price_raw))
        except Exception:
            price = None
    return enabled, price, data, ""


def _private_server_price_from_error(error_text):
    """Best-effort extraction of a current Robux price from a create error."""
    text = str(error_text or "")
    # Roblox errors are commonly embedded JSON. Cover current/legacy field names.
    patterns = (
        r'"(?:expectedPrice|currentPrice|purchasePrice|price|robuxPrice)"\s*:\s*(\d+)',
        r'(?:expected price|current price|price)\D{0,20}(\d+)',
    )
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            try:
                return max(0, int(m.group(1)))
            except Exception:
                pass
    return None


def _refresh_created_private_server(cookie, place_id, universe_id, user_id, created_item, attempts=4):
    """Re-fetch briefly when create response omits privateServerLinkCode."""
    if created_item and (created_item.get("link_code") or created_item.get("access_code")):
        return created_item
    created_id = str((created_item or {}).get("id") or "")
    created_name = str((created_item or {}).get("name") or "")
    for attempt in range(max(1, int(attempts))):
        if attempt:
            time.sleep(1.5)
        servers, _ = fetch_owned_private_servers(
            cookie, place_id, universe_id, user_id=user_id
        )
        for server in servers:
            if created_id and str(server.get("id") or "") == created_id:
                return _merge_private_server_item(created_item, server)
            if created_name and str(server.get("name") or "") == created_name:
                return _merge_private_server_item(created_item, server)
        # If there is only one usable owned server, it is almost certainly the
        # one just created (free servers are limited to one for this experience).
        usable = [s for s in servers if s.get("link_code") or s.get("access_code")]
        if len(usable) == 1:
            return _merge_private_server_item(created_item, usable[0])
    return created_item


def create_private_server(cookie, universe_id, name="NOMO Hatcher", expected_price=0):
    """Create one private server and return a normalized item."""
    xsrf_token = get_xsrf_token(cookie)
    if not xsrf_token:
        return None, "failed to get X-CSRF token"
    url = f"https://games.roblox.com/v1/games/vip-servers/{universe_id}"
    payload = {"name": str(name)[:50], "expectedPrice": int(expected_price or 0)}
    data, err, headers = _roblox_json_request(
        url,
        cookie=cookie,
        method="POST",
        payload=payload,
        timeout=25,
        xsrf_token=xsrf_token,
    )
    if err:
        # Roblox can rotate the token between the probe and purchase request.
        token2 = headers.get("x-csrf-token") or headers.get("X-CSRF-TOKEN")
        if token2 and token2 != xsrf_token:
            data, err, _ = _roblox_json_request(
                url,
                cookie=cookie,
                method="POST",
                payload=payload,
                timeout=25,
                xsrf_token=token2,
            )
    if err or not data:
        return None, err or "empty create response"
    item = _normalize_private_server_item(data)
    if item and item.get("id") and not (item.get("link_code") or item.get("access_code")):
        item = _merge_private_server_item(item, fetch_private_server_metadata(cookie, item["id"]))
    return item, ""



def set_private_server_friends_allowed(cookie, server_id, enabled=True):
    """Enable/disable Friends Allowed on an owned private server.

    Returns (ok, error_text, response_data). The endpoint is cookie-authenticated
    and X-CSRF protected. We never remove existing users and never request a new
    join code here.
    """
    server_id = str(server_id or "").strip()
    if not server_id:
        return False, "missing private server id", {}

    xsrf_token = get_xsrf_token(cookie)
    if not xsrf_token:
        return False, "failed to get X-CSRF token", {}

    url = (
        "https://games.roblox.com/v1/vip-servers/"
        f"{urllib.parse.quote(server_id, safe='')}/permissions"
    )

    # Current documented permission shape includes all four fields. A minimal
    # fallback is kept because Roblox has used both strict and partial PATCH
    # validation across deployments.
    payloads = [
        {
            "newJoinCode": False,
            "friendsAllowed": bool(enabled),
            "usersToAdd": [],
            "usersToRemove": [],
        },
        {
            "friendsAllowed": bool(enabled),
            "usersToAdd": [],
            "usersToRemove": [],
        },
    ]

    last_err = "permission update failed"
    for payload in payloads:
        data, err, headers = _roblox_json_request(
            url,
            cookie=cookie,
            method="PATCH",
            payload=payload,
            timeout=20,
            xsrf_token=xsrf_token,
        )

        if err:
            # Roblox may rotate the token on the protected request itself.
            token2 = headers.get("x-csrf-token") or headers.get("X-CSRF-TOKEN")
            if token2 and token2 != xsrf_token:
                xsrf_token = token2
                data, err, _ = _roblox_json_request(
                    url,
                    cookie=cookie,
                    method="PATCH",
                    payload=payload,
                    timeout=20,
                    xsrf_token=xsrf_token,
                )

        if not err:
            return True, "", data if isinstance(data, dict) else {}

        last_err = err
        # Only try the compatibility shape for a request-body validation error.
        if not str(err).startswith("HTTP 400"):
            break

    return False, last_err, {}


def add_private_server_allowed_users(cookie, server_id, user_ids, friends_allowed=True, chunk_size=25):
    """Add specific Roblox user IDs without removing existing members.

    Returns (added_ids, failed_items). Roblox accepts additive usersToAdd PATCHes;
    mixed chunks fall back to one user at a time so one restricted account cannot
    block all other Market accounts.
    """
    server_id = str(server_id or "").strip()
    ids = []
    seen = set()
    for raw in user_ids or []:
        value = str(raw or "").strip()
        if value.isdigit() and value not in seen:
            seen.add(value)
            ids.append(value)
    if not server_id:
        return [], [{"user_id": "", "error": "missing private server id"}]
    if not ids:
        return [], []

    xsrf_token = get_xsrf_token(cookie)
    if not xsrf_token:
        return [], [{"user_id": "", "error": "failed to get X-CSRF token"}]
    url = (
        "https://games.roblox.com/v1/vip-servers/"
        f"{urllib.parse.quote(server_id, safe='')}/permissions"
    )

    def patch(one_chunk):
        nonlocal xsrf_token
        payload = {
            "newJoinCode": False,
            "friendsAllowed": bool(friends_allowed),
            "usersToAdd": [int(x) for x in one_chunk],
            "usersToRemove": [],
        }
        data, err, headers = _roblox_json_request(
            url, cookie=cookie, method="PATCH", payload=payload, timeout=25,
            xsrf_token=xsrf_token,
        )
        if err:
            token2 = headers.get("x-csrf-token") or headers.get("X-CSRF-TOKEN")
            if token2 and token2 != xsrf_token:
                xsrf_token = token2
                data, err, _ = _roblox_json_request(
                    url, cookie=cookie, method="PATCH", payload=payload, timeout=25,
                    xsrf_token=xsrf_token,
                )
        return not bool(err), str(err or ""), data if isinstance(data, dict) else {}

    added = []
    failed = []
    size = max(1, min(50, int(chunk_size or 25)))
    for start in range(0, len(ids), size):
        chunk = ids[start:start + size]
        ok, err, _ = patch(chunk)
        if ok:
            added.extend(chunk)
            continue
        # A single ineligible user can reject a whole chunk. Retry individually.
        for uid in chunk:
            ok1, err1, _ = patch([uid])
            if ok1:
                added.append(uid)
            else:
                failed.append({"user_id": uid, "error": err1 or err or "permission update failed"})
    return added, failed


def _private_server_item_from_profile(profile):
    """Recover a previously saved server ID/join code from one hatcher profile."""
    if not isinstance(profile, dict):
        return None

    server_id = str(profile.get("private_server_id") or "").strip()
    link_code = str(profile.get("private_server_link_code") or "").strip()
    access_code = str(profile.get("private_server_access_code") or "").strip()
    saved_link = str(profile.get("server_link") or "").strip()

    # Recover codes from both current and older saved link formats.
    if saved_link:
        if not link_code:
            m = re.search(r"(?:[?&]|\b)linkCode=([^&\s]+)", saved_link, flags=re.I)
            if m:
                link_code = urllib.parse.unquote(m.group(1))
        if not access_code:
            m = re.search(r"(?:[?&]|\b)accessCode=([^&\s]+)", saved_link, flags=re.I)
            if m:
                access_code = urllib.parse.unquote(m.group(1))

    if not (server_id or link_code or access_code):
        return None
    return {
        "id": server_id,
        "name": str(profile.get("hatcher_name") or "NOMO Hatcher"),
        "link_code": link_code,
        "access_code": access_code,
        "owner_id": "",
        "active": True,
        "raw": {},
    }


def build_private_server_link(place_id, item):
    """Build an Android Roblox deep link from an actual join/link code."""
    if not isinstance(item, dict):
        return ""
    code = str(item.get("link_code") or "").strip()
    if code:
        return (
            f"roblox://experiences/start?placeId={place_id}"
            f"&linkCode={urllib.parse.quote(code, safe='')}"
        )
    access = str(item.get("access_code") or "").strip()
    if access:
        return (
            f"roblox://experiences/start?placeId={place_id}"
            f"&accessCode={urllib.parse.quote(access, safe='')}"
        )
    return ""


def load_cookie_cache():
    """Load the cookie cache from JSON file."""
    if not COOKIE_CACHE.exists():
        return {}
    try:
        with open(COOKIE_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def _hatcher_profile_for_package(hcfg, cfg, pkg, username=""):
    profiles = hatcher_profiles(hcfg)
    for prof in profiles:
        if prof.get("package") == pkg:
            return prof

    # Create a missing profile without touching any other package.
    idx = len(profiles)
    prof = normalize_hatcher_profile({
        "enabled": True,
        "package": pkg,
        "hatcher_name": username or pkg,
        "server_link": "",
    }, idx)
    for tab in cfg.get("tabs", []):
        if tab.get("package") == pkg:
            prof["state_file"] = tab.get("stat_file") or tab.get("state_file") or prof.get("state_file", "")
            break
    hcfg.setdefault("hatchers", []).append(prof)
    return prof


def auto_fetch_private_servers(cfg, selected_packages=None, pause_at_end=True):
    """Fetch/create Hatcher servers and sync D1 Market access.

    selected_packages is used by the setup wizard so the user does not need to
    select the same packages twice. Normal Option 5 > 4 behavior is unchanged.
    """
    selected = list(selected_packages or [])
    if not selected:
        selected = choose_packages_common(cfg, "FETCH / CREATE HATCHER SERVERS", multi=True,
                                          installed_only=True, include_discovered=True)
    if not selected:
        return
    print(col("Runs once from this menu only. It is not part of the rejoin loop.", DIM))
    print(col("Each package uses only its own local cookie.", DIM))
    print(col("Registered D1 Market user IDs are added to server permissions once.", DIM))
    print("")

    default_place = "126884695634066"
    entered = input(f"Hatcher place ID [{default_place}]: ").strip()
    place_id = entered or default_place
    if not place_id.isdigit():
        print(col("Place ID must be numeric.", RED))
        pause()
        return

    print(f"Resolving universe for place {place_id}...")
    universe_id = get_universe_id_from_place(place_id)
    if not universe_id:
        print(col("Could not resolve universe ID.", RED))
        pause()
        return

    enabled, price, product_raw, product_err = get_private_server_product_info(universe_id)

    # V3.92: This unauthenticated endpoint is advisory only. Roblox can expose
    # account-specific creation (notably Roblox Plus/free entitlement) even when
    # the public universe response says false. Always fetch existing servers and,
    # when requested, let the authenticated create POST be the source of truth.
    if enabled is False:
        print(col(
            "Public API reports creation unavailable, but account-specific Create/Plus may still work.",
            YELLOW,
        ))
    elif enabled is None:
        print(col(f"Could not verify public private-server settings: {product_err}", YELLOW))
    create_missing = input("Create a server when the account has none? [Y/n]: ").strip().lower() not in {"n", "no"}

    creation_text = (
        f"public enabled, {price if price is not None else 'price unknown'}"
        if enabled is True
        else ("public false (account create will still be tested)" if enabled is False else "public status unknown")
    )
    print(f"Universe: {universe_id} | creation check: {creation_text}")

    hcfg = load_hatcher_config()
    cache = load_cookie_cache()

    # Prefer Hatcher backend credentials, with Market config as fallback.
    registry_cfg = dict(cfg)
    for key in ("backend_provider", "cloudflare_worker_url", "cloudflare_secret", "cloudflare_timeout_seconds"):
        value = hcfg.get(key)
        if value not in (None, "") and not is_placeholder_value(value):
            registry_cfg[key] = value
    market_accounts = []
    market_registry_err = ""
    if backend_provider(registry_cfg) == "cloudflare":
        market_accounts, market_registry_err = cloudflare_read_accounts(registry_cfg, role="market")
        if market_registry_err:
            print(col(f"D1 Market registry unavailable: {market_registry_err}", YELLOW))
        else:
            print(col(f"D1 Market accounts ready for permission sync: {len(market_accounts)}", GREEN if market_accounts else YELLOW))
    else:
        market_registry_err = "Cloudflare backend not enabled"
        print(col("D1 Market registry not configured; Friends Allowed only.", YELLOW))

    changed = False
    results = []

    for pkg in selected:
        print(col(f"\n[{pkg}]", CYAN))
        # Prefer fresh extraction from that exact clone. Cache is only fallback.
        cookie = get_cookie_from_package(pkg)
        if not cookie:
            cookie = str((cache.get(pkg) or {}).get("cookie") or "").strip()
            if cookie:
                print(col("Using cached package cookie (direct extraction unavailable).", YELLOW))
        if not cookie:
            print(col("No cookie found; skipped.", RED))
            results.append((pkg, "NO COOKIE"))
            continue

        username, user_id = get_username_from_cookie(cookie, timeout=12)
        if not username:
            state = check_cookie_challenge(cookie)
            print(col(f"Cookie is not usable ({state}); skipped.", RED))
            results.append((pkg, f"COOKIE {state.upper()}"))
            continue
        print(f"Account: {username} ({user_id})")

        profile = _hatcher_profile_for_package(hcfg, cfg, pkg, username=username)
        saved_item = _private_server_item_from_profile(profile)

        servers, fetch_err = fetch_owned_private_servers(cookie, place_id, universe_id, user_id=user_id)
        usable = next((s for s in servers if s.get("active", True) and (s.get("link_code") or s.get("access_code"))), None)
        existing = usable or next((s for s in servers if s.get("active", True)), None)
        created = False

        # Roblox often lists an owned server but omits the owner accessCode on
        # later reads. Merge the previously saved local code instead of treating
        # the account as server-less and attempting an impossible second create.
        if existing and not usable:
            if existing.get("id"):
                existing = _merge_private_server_item(
                    existing, fetch_private_server_metadata(cookie, existing.get("id"))
                )
            if saved_item:
                same_server = (
                    not existing.get("id")
                    or not saved_item.get("id")
                    or str(existing.get("id")) == str(saved_item.get("id"))
                )
                if same_server:
                    existing = _merge_private_server_item(existing, saved_item)
            usable = existing
            if usable.get("link_code") or usable.get("access_code"):
                print(col("Owned server found; Roblox omitted its join code, reusing the saved local code.", YELLOW))
            else:
                print(col("Owned server found without a join code; duplicate creation will be skipped.", YELLOW))

        # Some Roblox list endpoints omit a just-created/legacy owned server. A
        # verified or previously saved profile still proves one exists, so do not
        # attempt another create and hit the one-server limit.
        if not existing and saved_item:
            verified = None
            if saved_item.get("id"):
                verified = fetch_private_server_metadata(cookie, saved_item.get("id"))
            existing = _merge_private_server_item(verified, saved_item) if verified else saved_item
            usable = existing
            print(col("Using the server already saved for this package; duplicate creation skipped.", YELLOW))

        if not existing and create_missing:
            server_name = f"NOMO {username}"[:50]

            # First try expectedPrice=0. This is safe: a paid purchase cannot go
            # through at the wrong expected price, while free/Roblox Plus account
            # entitlement can succeed even when the public enabled endpoint says
            # false. No Robux-spending retry happens without explicit confirmation.
            print(col("No owned or saved server found; testing account free/Plus creation...", DIM))
            usable, create_err = create_private_server(
                cookie,
                universe_id,
                name=server_name,
                expected_price=0,
            )

            if not usable:
                paid_price = _private_server_price_from_error(create_err)
                if paid_price is None and price is not None and int(price) > 0:
                    paid_price = int(price)

                if paid_price is not None and paid_price > 0:
                    print(col(
                        f"Free/Plus creation was not available. Recurring price: {paid_price} Robux.",
                        YELLOW,
                    ))
                    confirm = input(
                        f"Type BUY {paid_price} to create it for {username}, or ENTER to skip: "
                    ).strip()
                    if confirm != f"BUY {paid_price}":
                        print(col("Paid creation skipped.", YELLOW))
                        results.append((pkg, "PAID SKIPPED"))
                        continue
                    usable, create_err = create_private_server(
                        cookie,
                        universe_id,
                        name=server_name,
                        expected_price=paid_price,
                    )

            if not usable:
                print(col(f"Create API rejected this account: {create_err}", RED))
                results.append((pkg, "CREATE FAILED"))
                continue

            usable = _refresh_created_private_server(
                cookie,
                place_id,
                universe_id,
                user_id,
                usable,
            )
            existing = usable
            created = True

        if not existing:
            detail = fetch_err or "no owned or saved private server"
            print(col(f"No server found: {detail}", YELLOW))
            results.append((pkg, "NOT FOUND"))
            continue

        usable = usable or existing
        link = build_private_server_link(place_id, usable)

        # One-time permission sync can still run when Roblox lists an existing
        # server without returning its join code. Specific Market users are
        # additive only; existing members are never removed.
        friends_ok, friends_err, _ = set_private_server_friends_allowed(
            cookie, usable.get("id", ""), enabled=True
        )
        if friends_ok:
            print(col("Friends Allowed: ENABLED", GREEN))
        else:
            print(col(f"Friends Allowed update failed: {friends_err}", YELLOW))

        market_ids = [a.get("user_id") for a in market_accounts if str(a.get("user_id") or "") != str(user_id or "")]
        allowed_added = []
        allowed_failed = []
        if market_ids and usable.get("id"):
            allowed_added, allowed_failed = add_private_server_allowed_users(
                cookie, usable.get("id", ""), market_ids, friends_allowed=True
            )
            if allowed_added:
                print(col(f"Specific Market access: ADDED/CONFIRMED {len(allowed_added)}", GREEN))
            if allowed_failed:
                print(col(f"Specific Market access failed for {len(allowed_failed)} account(s).", YELLOW))
                for item in allowed_failed[:5]:
                    print(col(f"  {item.get('user_id')}: {cut(item.get('error', ''), 100)}", DIM))
        elif market_registry_err:
            print(col("Specific Market access skipped: D1 registry unavailable.", YELLOW))
        elif not market_ids:
            print(col("Specific Market access skipped: no registered Market accounts.", YELLOW))

        profile["hatcher_name"] = username
        if link:
            profile["server_link"] = android_safe_roblox_link(link, cfg)
        profile["private_server_id"] = usable.get("id", "") or profile.get("private_server_id", "")
        profile["private_server_link_code"] = usable.get("link_code", "") or profile.get("private_server_link_code", "")
        profile["private_server_access_code"] = usable.get("access_code", "") or profile.get("private_server_access_code", "")
        profile["private_server_friends_allowed"] = bool(friends_ok)
        profile["private_server_permissions_error"] = "" if friends_ok else str(friends_err or "")[:300]
        profile["private_server_permissions_synced_at"] = now()
        profile["private_server_market_users_synced"] = len(allowed_added)
        profile["private_server_market_users_failed"] = allowed_failed[:20]
        profile["private_server_market_registry_error"] = str(market_registry_err or "")[:300]
        profile["private_server_synced_at"] = now()
        changed = True

        base_action = "CREATED" if created else "FOUND"
        if link:
            base_action += " + SAVED"
            access_suffix = f" + ALLOWED {len(allowed_added)}" if allowed_added else ""
            action = (f"{base_action} + FRIENDS{access_suffix}" if friends_ok
                      else f"{base_action} / FRIENDS FAILED{access_suffix}")
            print(col(f"{action}: {short_link(profile['server_link'])}", GREEN if friends_ok and not allowed_failed else YELLOW))
        else:
            action = f"{base_action} + FRIENDS / NO JOIN CODE" if friends_ok else f"{base_action} / NO JOIN CODE / FRIENDS FAILED"
            print(col("Existing server kept; Roblox returned no join code. No duplicate create was attempted.", YELLOW))
        results.append((pkg, action))

    if changed:
        save_hatcher_config(hcfg)

    print("\nResult:")
    for pkg, result in results:
        color = YELLOW if "FRIENDS FAILED" in result or "SKIPPED" in result or result == "NOT FOUND" else (GREEN if "SAVED" in result else RED)
        print(f"  {short_pkg(pkg):<10} {col(result, color)}")
    print(col("\nDone. These links will be used on the next Hatcher rejoin; nothing was added to a loop.", GREEN if changed else YELLOW))
    if pause_at_end:
        pause()
    return {"changed": changed, "results": results}


# ============================================================
# V3.82 BACKGROUND CAPTCHA SOLVER JOBS
# ============================================================

_SOLVER_JOBS = {}
_SOLVER_LOCK = threading.RLock()


def solver_job_running(package):
    with _SOLVER_LOCK:
        job = _SOLVER_JOBS.get(str(package or ""))
        return bool(job and not job.get("done"))


def solver_job_note(package):
    with _SOLVER_LOCK:
        job = _SOLVER_JOBS.get(str(package or ""))
        if not job:
            return "solver idle"
        if job.get("done"):
            return "solver finishing"
        elapsed = max(0, now() - int(job.get("started_at", now()) or now()))
        timeout = int(job.get("timeout", 180) or 180)
        if job.get("phase") == "probe":
            return f"checking captcha {elapsed}/{timeout}s"
        return f"solver running {elapsed}/{timeout}s"


def solver_job_completion_state(package):
    with _SOLVER_LOCK:
        job = _SOLVER_JOBS.get(str(package or ""))
        if not job or not job.get("done"):
            return ""
        return "no_challenge" if job.get("no_challenge") else "challenge_result"


def cancel_queued_package(open_queue, package):
    """Remove pending opens for one challenged package only."""
    package = str(package or "")
    before = len(open_queue or [])
    if open_queue is not None:
        open_queue[:] = [
            item for item in open_queue
            if str((item.get("tab") or {}).get("package", "")) != package
        ]
    return before - len(open_queue or [])


def solver_place_id_for_tab(tab, cfg):
    place_id = str(cfg.get("solver_place_id", "126884695634066") or "126884695634066")
    for key in ("server_link", "restock_link"):
        link = str((tab or {}).get(key, "") or "")
        if link:
            pid = extract_place_id_from_link(link)
            if pid:
                return str(pid)
    target = str((tab or {}).get("target", "") or "")
    if target == "market":
        pid = extract_place_id_from_link(cfg.get("market_link", ""))
        if pid:
            return str(pid)
    return place_id


def solver_response_status(data):
    """Return the provider's normalized status without exposing token/cookie."""
    if not isinstance(data, dict):
        return ""
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    return str(
        data.get("status") or nested.get("status") or result.get("status") or ""
    ).strip().upper()


def _solver_probe_worker(package, tab, cookie, cfg_snapshot, place_id):
    details = []
    locally_detected = False
    provider_cookie = cookie
    cookie_precheck_ok = False
    try:
        if cookie and cfg_snapshot.get("login_challenge_api_detection_enabled", True):
            api_hit, api_detail = roblox_cookie_detection(cookie)
            details.append(api_detail)
            locally_detected = locally_detected or (api_hit is True)
            # False means authenticated/valid; True means Roblox itself returned
            # a challenge header. Both prove the cookie is usable. None means
            # invalid OR the validation request was inconclusive, so do not send.
            cookie_precheck_ok = api_hit in (True, False)
            if not cookie_precheck_ok:
                provider_cookie = ""
        elif cookie:
            # V3.84 defaults to strict precheck. Only allow bypass if the user
            # explicitly disables the safety knob.
            cookie_precheck_ok = not cfg_snapshot.get("solver_require_cookie_precheck", True)
            if not cookie_precheck_ok:
                details.append("api precheck required")
                provider_cookie = ""
        else:
            details.append("api skipped no cookie")
            provider_cookie = ""

        if cfg_snapshot.get("login_challenge_ui_detection_enabled", True):
            ui_hit, ui_detail = ui_login_challenge_detection(tab, cfg_snapshot, force=False, allow_unscoped=False)
            details.append(ui_detail)
            locally_detected = locally_detected or (ui_hit is True)

        provider_probe = bool(cfg_snapshot.get("solver_provider_probe_on_no_state", True))
        should_call_provider = locally_detected or provider_probe

        if not should_call_provider:
            ok = False
            response = {"success": False, "status": "NO_CHALLENGE", "detail": "; ".join(details)}
            no_challenge = True
        elif not cfg_snapshot.get("solver_enabled", False):
            ok = False
            no_challenge = not locally_detected
            response = {
                "success": False,
                "error": "challenge detected; solver disabled" if locally_detected else "provider probe skipped; solver disabled",
                "detail": "; ".join(details),
            }
        elif not provider_cookie:
            ok = False
            no_challenge = False
            response = {
                "success": False,
                "status": "INVALID_COOKIES" if cookie and not cookie_precheck_ok else "NO_COOKIE",
                "error": "cookie invalid or validation unavailable; not sent" if cookie else "no package cookie; not sent",
                "detail": "; ".join(details),
            }
        else:
            ok, response = solve_captcha(provider_cookie, cfg_snapshot, place_id)
            kind = solver_response_kind(response)
            no_challenge = kind == "no_challenge"
            ok = kind == "solved"
            if isinstance(response, dict):
                response.setdefault("detection", "; ".join(details))

        with _SOLVER_LOCK:
            job = _SOLVER_JOBS.get(package)
            if job:
                job["detected"] = bool(locally_detected)
                job["cookie_precheck_ok"] = bool(cookie_precheck_ok)
                job["provider_probed"] = bool(should_call_provider and provider_cookie and cfg_snapshot.get("solver_enabled", False))
                job["no_challenge"] = bool(no_challenge)
                job["ok"] = bool(ok)
                job["response"] = response
                job["provider_status"] = solver_response_status(response)
                job["done"] = True
                job["finished_at"] = now()
    except Exception as e:
        with _SOLVER_LOCK:
            job = _SOLVER_JOBS.get(package)
            if job:
                job["detected"] = bool(locally_detected)
                job["cookie_precheck_ok"] = bool(cookie_precheck_ok)
                job["ok"] = False
                job["response"] = {"success": False, "error": str(e), "detection": "; ".join(details)}
                job["provider_status"] = ""
                job["done"] = True
                job["finished_at"] = now()


def start_challenge_probe_job(tab, cfg, rt, rt_tab, probe_token=0, reason="wait-after-open no fresh state"):
    """Start one background provider check for one actual open generation.

    V3.84 deliberately does not periodically scan every stale package. The token
    is normally rt_tab.last_open, so the same rejoin can submit at most once.
    """
    pkg = str((tab or {}).get("package", "") or "")
    if not pkg or not cfg.get("login_challenge_detection_enabled", True):
        return False, "challenge probe disabled"

    token = int(probe_token or 0)
    with _SOLVER_LOCK:
        existing = _SOLVER_JOBS.get(pkg)
        if existing:
            return True, solver_job_note(pkg)

    if cfg.get("solver_probe_once_per_open", True) and token:
        previous_token = int(rt_tab.get("solver_probe_token", 0) or 0)
        if previous_token == token:
            return False, "captcha already checked this rejoin"

    min_submit = max(600, int(cfg.get("solver_min_resubmit_seconds", 600) or 600))
    last_submit = int(rt_tab.get("solver_provider_last_submit_at", 0) or 0)
    if last_submit > 0 and now() - last_submit < min_submit:
        left = max(1, min_submit - (now() - last_submit))
        # Mark this open generation as checked so the wait loop does not keep
        # asking every few seconds during the same rejoin.
        if token:
            rt_tab["solver_probe_token"] = token
        return False, f"provider cooldown {format_age(left)}"

    cookie = cached_cookie_for_package(pkg) or ensure_cookie_for_package(pkg, cfg)
    place_id = solver_place_id_for_tab(tab, cfg)
    timeout = max(20, int(cfg.get("solver_timeout_seconds", 180) or 180))
    job = {
        "package": pkg,
        "tab": dict(tab or {}),
        "target": str(rt_tab.get("target", "") or ("hatcher" if (tab or {}).get("server_link") else "market")),
        "reason": str(reason or "wait-after-open no fresh state"),
        "phase": "probe",
        "probe_token": token,
        "started_at": now(),
        "timeout": timeout,
        "done": False,
        "ok": False,
        "no_challenge": False,
        "response": None,
    }
    with _SOLVER_LOCK:
        _SOLVER_JOBS[pkg] = job

    rt_tab["solver_probe_token"] = token
    rt_tab["solver_probe_started_at"] = job["started_at"]
    rt_tab["solver_state"] = "checking"
    rt_tab["note"] = "checking captcha"
    save_runtime(rt)
    log_activity(f"checking login/captcha once: {cut(job['reason'], 65)}", pkg, CYAN)

    cfg_snapshot = dict(cfg)
    thread = threading.Thread(
        target=_solver_probe_worker,
        args=(pkg, dict(tab or {}), cookie, cfg_snapshot, place_id),
        name=f"nomo-captcha-probe-{pkg}",
        daemon=True,
    )
    thread.start()
    return True, solver_job_note(pkg)


def maybe_start_blocked_join_probe(tab, cfg, rt, rt_tab, state, alive, open_queue=None, mode="hatcher"):
    """Compatibility stub.

    V3.83 called the provider for every alive stale package from the dashboard.
    V3.84 intentionally disables that behavior. Provider checks happen only in
    wait_until_fresh_after_open(), once after NOMO actually opens/rejoins a clone.
    """
    return False, ""


def _solver_worker(package, cookie, cfg_snapshot, place_id):
    ok = False
    no_challenge = False
    response = {"success": False, "error": "solver worker stopped"}
    try:
        _ignored, response = solve_captcha(cookie, cfg_snapshot, place_id)
        kind = solver_response_kind(response)
        ok = kind == "solved"
        no_challenge = kind == "no_challenge"
    except Exception as e:
        response = {"success": False, "error": str(e)}
    with _SOLVER_LOCK:
        job = _SOLVER_JOBS.get(package)
        if job:
            job["ok"] = bool(ok)
            job["no_challenge"] = bool(no_challenge)
            job["response"] = response
            job["done"] = True
            job["finished_at"] = now()


def start_solver_job(tab, cfg, rt, rt_tab, reason, force=False):
    pkg = str((tab or {}).get("package", "") or "")
    if not pkg:
        return False, "solver: missing package"
    if not cfg.get("solver_enabled", False):
        return False, "solver disabled"
    if not str(cfg.get("solver_endpoint", "") or "").strip():
        return False, "solver endpoint missing"
    if not str(cfg.get("solver_api_key", "") or "").strip():
        return False, "solver API key missing"

    with _SOLVER_LOCK:
        existing = _SOLVER_JOBS.get(pkg)
        if existing and not existing.get("done"):
            return True, solver_job_note(pkg)
        if existing and existing.get("done"):
            return True, "solver finishing"

    cooldown = max(0, int(cfg.get("solver_retry_cooldown_seconds", 300) or 300))
    last_attempt = int(rt_tab.get("solver_last_attempt", 0) or 0)
    if not force and last_attempt > 0 and now() - last_attempt < cooldown:
        left = max(1, cooldown - (now() - last_attempt))
        return False, f"solver retry in {format_age(left)}"

    cookie = cached_cookie_for_package(pkg) or ensure_cookie_for_package(pkg, cfg)
    if not cookie:
        return False, "solver: no package cookie"
    api_hit, api_detail = roblox_cookie_detection(cookie)
    if cfg.get("solver_require_cookie_precheck", True) and api_hit not in (True, False):
        if "invalid/expired" in str(api_detail).lower():
            return False, "solver: package cookie invalid/expired"
        return False, "solver: cookie precheck unavailable; not sent"

    place_id = solver_place_id_for_tab(tab, cfg)
    timeout = max(15, int(cfg.get("solver_timeout_seconds", 180) or 180))
    target = str(rt_tab.get("target", "") or ("hatcher" if (tab or {}).get("server_link") else "market"))
    cfg_snapshot = {
        "solver_enabled": True,
        "solver_endpoint": str(cfg.get("solver_endpoint", "") or ""),
        "solver_api_key": str(cfg.get("solver_api_key", "") or ""),
        "solver_place_id": str(place_id),
        "solver_timeout_seconds": timeout,
        "solver_provider_probe_on_no_state": bool(cfg.get("solver_provider_probe_on_no_state", True)),
    }
    job = {
        "package": pkg,
        "tab": dict(tab or {}),
        "target": target,
        "reason": str(reason or "challenge"),
        "phase": "solve",
        "started_at": now(),
        "timeout": timeout,
        "done": False,
        "ok": False,
        "response": None,
    }
    with _SOLVER_LOCK:
        _SOLVER_JOBS[pkg] = job

    rt_tab["solver_state"] = "running"
    rt_tab["solver_started_at"] = job["started_at"]
    rt_tab["solver_last_attempt"] = job["started_at"]
    rt_tab["solver_reason"] = job["reason"]
    rt_tab["note"] = "solver starting"
    save_runtime(rt)
    log_activity(f"solver started: {cut(job['reason'], 60)}", pkg, CYAN)

    thread = threading.Thread(
        target=_solver_worker,
        args=(pkg, cookie, cfg_snapshot, place_id),
        name=f"nomo-solver-{pkg}",
        daemon=True,
    )
    thread.start()
    return True, "solver running 0/{}s".format(timeout)


def _solver_error_text(response):
    if isinstance(response, dict):
        value = response.get("error") or response.get("message") or response.get("status") or response
    else:
        value = response
    return cut(str(value or "unknown solver error"), 180)


def handle_detected_solver_challenge(tab, cfg, rt, rt_tab, reason):
    """Start/describe a solver job, or isolate the package if unavailable."""
    pkg = str((tab or {}).get("package", "") or "")
    started, note = start_solver_job(tab, cfg, rt, rt_tab, reason)
    if started:
        rt_tab["note"] = note
        save_runtime(rt)
        return "Solving", note

    # Disabled, missing credentials/cookie, or cooldown after a failed attempt.
    rt_tab["manual_login_needed"] = True
    rt_tab["manual_login_reason"] = "captcha/login challenge"
    rt_tab["manual_login_detail"] = str(reason or note)
    if not int(rt_tab.get("manual_login_detected_at", 0) or 0):
        rt_tab["manual_login_detected_at"] = now()
    rt_tab["note"] = note
    set_hold(pkg, note)
    save_runtime(rt)
    return "Manual", note


def poll_solver_jobs(cfg, rt, open_queue):
    """Apply completed jobs on the main thread; never expose cookies/tokens."""
    completed = []
    with _SOLVER_LOCK:
        for pkg, job in list(_SOLVER_JOBS.items()):
            if job.get("done"):
                completed.append((pkg, dict(job)))
                del _SOLVER_JOBS[pkg]

    changed = False
    min_submit = max(600, int(cfg.get("solver_min_resubmit_seconds", 600) or 600))
    for pkg, job in completed:
        rt_tab = get_runtime_tab(rt, pkg)
        tab = dict(job.get("tab") or {"package": pkg})
        target = str(job.get("target", "market") or "market")
        response = job.get("response")
        status_code = str(job.get("provider_status") or solver_response_status(response) or "").upper()

        if job.get("provider_probed"):
            rt_tab["solver_provider_last_submit_at"] = int(job.get("started_at", now()) or now())
            rt_tab["solver_provider_last_status"] = status_code or "UNKNOWN"
            rt_tab["solver_provider_next_submit_at"] = now() + min_submit

        no_captcha_result = (
            status_code in {"NO_CAPTCHA", "NO_CHALLENGE", "NOT_REQUIRED", "CLEAR", "CLEAN"}
            or bool(job.get("no_challenge"))
        )
        solved_result = (
            status_code in {"CAPTCHA_SUCCESS", "SUCCESS", "SOLVED", "COMPLETED", "OK"}
            or bool(job.get("ok"))
        )

        # V4.12: the provider can return NO_CAPTCHA even while Roblox visibly
        # shows "Verifying you're not a bot". Recheck a fresh package-scoped UI
        # snapshot before clearing anything. A still-visible challenge is a false
        # negative: keep this package open/held and retry only after cooldown.
        if no_captcha_result:
            visible_ui = android_login_challenge_ui_detail(pkg, cfg, force=True)
            if visible_ui:
                retry_after = max(
                    600,
                    int(cfg.get("captcha_ui_retry_seconds", 600) or 600),
                    int(cfg.get("solver_retry_cooldown_seconds", 600) or 600),
                )
                cancel_queued_package(open_queue, pkg)
                rt_tab["captcha_ui_visible"] = True
                rt_tab["captcha_ui_false_negative"] = True
                rt_tab["captcha_ui_last_seen_at"] = now()
                rt_tab["captcha_ui_retry_at"] = now() + retry_after
                rt_tab["solver_state"] = "false_negative"
                rt_tab["solver_last_error"] = "provider NO_CAPTCHA while verification UI remains visible"
                rt_tab["manual_login_needed"] = False
                rt_tab["manual_login_reason"] = ""
                rt_tab["manual_login_detail"] = ""
                rt_tab["note"] = f"NO_CAPTCHA false negative; retry in {format_age(retry_after)}"
                set_hold(pkg, "verification UI still visible after provider NO_CAPTCHA")
                log_activity(
                    f"solver NO_CAPTCHA false negative; verification UI still visible; retry in {format_age(retry_after)}",
                    pkg, YELLOW,
                )
                changed = True
                continue

        # V4.09: keep NO_CAPTCHA separate from CAPTCHA_SUCCESS. The provider
        # returning NO_CAPTCHA means there was nothing to solve, so a package that
        # has just been opened must not be PID-stopped and opened again. This was
        # the MARKET timeout double-restart shown as:
        #   open ok -> solver NO_CAPTCHA -> queued hard force
        # CAPTCHA_SUCCESS still gets one recovery rejoin because reconnecting can
        # be required after the challenge is actually solved.
        if no_captcha_result or solved_result:
            result_label = "NO_CAPTCHA" if no_captcha_result else "CAPTCHA_SUCCESS"
            detail = _solver_probe_detail(response)

            clear_hold(pkg)
            clear_manual_login_block(rt_tab)
            clear_captcha_ui_runtime(rt_tab)
            rt_tab["solver_busy_retry_pending"] = False
            rt_tab["solver_busy_retry_at"] = 0
            rt_tab["solver_state"] = "success" if solved_result else "clear"
            rt_tab["solver_last_success"] = now()
            rt_tab["solver_last_error"] = ""
            rt_tab["solver_last_probe"] = detail

            # Remove only stale/duplicate queue entries for this package. Never
            # disturb another clone's queued recovery.
            cancel_queued_package(open_queue, pkg)

            should_rejoin = bool(cfg.get("solver_rejoin_on_success", True))
            if no_captcha_result:
                should_rejoin = bool(cfg.get("solver_rejoin_on_no_captcha", False))

            if should_rejoin:
                rt_tab["note"] = f"{result_label} - rejoin queued"
                activity = f"solver {result_label}; rejoin queued"
                if detail:
                    activity += " - " + cut(detail, 70)
                log_activity(activity, pkg, GREEN)

                added, _ = queue_open(
                    open_queue, tab, target, f"solver {result_label.lower()} rejoin",
                    force=True, mode="hard_force", bypass_manual=True,
                    metadata={
                        "solver_recovery": True,
                        "solver_result": result_label,
                        "skip_solver_probe": True,
                        "bypass_recheck": True,
                    },
                )
                if not added:
                    rt_tab["note"] = f"{result_label} - already queued"
            else:
                rt_tab["note"] = f"{result_label} - current open kept; waiting state"
                activity = f"solver {result_label}; current open kept (no extra restart)"
                if detail:
                    activity += " - " + cut(detail, 70)
                log_activity(activity, pkg, GREEN)

            changed = True
            continue

        err = _solver_error_text(response)
        detail = _solver_probe_detail(response)
        rt_tab["solver_state"] = "failed"
        rt_tab["solver_last_error"] = err

        if status_code in {"SERVER_BUSY", "BUSY", "RATE_LIMITED", "TOO_MANY_REQUESTS"}:
            retry_after = max(600, int(cfg.get("solver_min_resubmit_seconds", 600) or 600))
            rt_tab["solver_busy_retry_pending"] = True
            rt_tab["solver_busy_retry_at"] = now() + retry_after
            rt_tab["note"] = f"SERVER_BUSY; retry rejoin in {format_age(retry_after)}"
            log_activity("solver SERVER_BUSY; retry rejoin scheduled in 10m", pkg, YELLOW)
        elif status_code in {"INVALID_COOKIES", "INVALID_COOKIE", "UNAUTHORIZED", "AUTH_FAILED"}:
            rt_tab["solver_busy_retry_pending"] = False
            rt_tab["solver_busy_retry_at"] = 0
            rt_tab["manual_login_needed"] = True
            rt_tab["manual_login_reason"] = "invalid package cookie"
            rt_tab["manual_login_detail"] = err
            rt_tab["manual_login_detected_at"] = now()
            rt_tab["note"] = "INVALID_COOKIES - refresh account cookie"
            set_hold(pkg, "solver rejected invalid/expired package cookie")
            log_activity("solver INVALID_COOKIES; not retrying", pkg, RED)
        elif str(job.get("phase", "")) == "probe" and not job.get("detected"):
            # Provider/network error during a one-shot post-open probe is not
            # proof of CAPTCHA and must not place the account on manual hold.
            rt_tab["note"] = f"captcha check failed; retry after 10m/rejoin: {cut(err, 55)}"
            log_activity(
                f"captcha probe error: {cut(err, 85)}"
                + ((" | " + cut(detail, 55)) if detail else ""),
                pkg, YELLOW,
            )
        else:
            rt_tab["manual_login_needed"] = True
            rt_tab["manual_login_reason"] = "solver failed"
            rt_tab["manual_login_detail"] = err
            rt_tab["manual_login_detected_at"] = now()
            rt_tab["note"] = f"solver failed: {cut(err, 80)}"
            set_hold(pkg, f"solver failed: {err}")
            send_login_challenge_alert(cfg, tab, rt_tab, rt_tab["note"])
            log_activity(f"solver failed: {cut(err, 100)}", pkg, RED)
        changed = True

    if changed:
        save_runtime(rt)
    return changed


def _solver_probe_detail(data):
    if not isinstance(data, dict):
        return ""
    return cut(str(data.get("detection") or data.get("detail") or ""), 180)


def solver_response_kind(data):
    """Return solved / no_challenge / pending / failed for provider JSON."""
    if not isinstance(data, dict):
        return "failed"
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    status = str(data.get("status") or nested.get("status") or result.get("status") or "").strip().upper()

    no_challenge_statuses = {
        "NO_CAPTCHA", "NO_CHALLENGE", "NOT_REQUIRED", "CLEAR", "CLEAN"
    }
    solved_statuses = {
        "CAPTCHA_SUCCESS", "SUCCESS", "SOLVED", "COMPLETED", "OK"
    }
    pending_statuses = {
        "PENDING", "PROCESSING", "QUEUED", "IN_PROGRESS", "WORKING"
    }
    if status in no_challenge_statuses:
        return "no_challenge"
    if status in solved_statuses:
        return "solved"
    if status in pending_statuses:
        return "pending"

    success = data.get("success")
    if success is None:
        success = nested.get("success", result.get("success"))
    if success is True:
        return "solved"
    return "failed"


def solver_response_is_success(data):
    return solver_response_kind(data) == "solved"


def normalized_solver_endpoint(endpoint):
    endpoint = str(endpoint or "").strip().rstrip("/")
    if not endpoint:
        return ""
    # Bare scheme+host keeps compatibility with Winter's historical endpoint.
    after_scheme = endpoint.split("://", 1)[-1]
    if "/" not in after_scheme:
        return endpoint + "/api/captcha/solve"
    return endpoint


def solve_captcha(cookie, cfg, place_id=None):
    """Submit one package cookie to the configured third-party solver."""
    if not cfg.get("solver_enabled", False):
        return False, {"success": False, "error": "solver disabled"}
    endpoint = normalized_solver_endpoint(cfg.get("solver_endpoint", ""))
    api_key = str(cfg.get("solver_api_key", "") or "").strip()
    if not endpoint:
        return False, {"success": False, "error": "solver endpoint not set"}
    if not api_key:
        return False, {"success": False, "error": "solver API key not set"}
    if not cookie:
        return False, {"success": False, "error": "cookie missing"}

    if place_id is None:
        place_id = cfg.get("solver_place_id", "126884695634066")
    try:
        place_id_value = int(str(place_id))
    except Exception:
        return False, {"success": False, "error": f"invalid place ID: {place_id}"}

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"NOMO-Rejoin/{__version__}",
    }
    payload = json.dumps({"cookie": cookie, "placeId": place_id_value}).encode("utf-8")
    timeout = max(15, int(cfg.get("solver_timeout_seconds", 180) or 180))

    try:
        req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {"success": False, "error": f"non-JSON response: {cut(raw, 180)}"}
            return solver_response_is_success(data), data
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return False, {"success": False, "error": f"HTTP {e.code}: {cut(body, 240)}"}
    except Exception as e:
        return False, {"success": False, "error": str(e)}

def get_roblox_user_info(cookie):
    """Fetch username, userID, created date from Roblox API using cookie."""
    if not cookie:
        return {"valid": False, "status": "No cookie"}

    url_auth = "https://users.roblox.com/v1/users/authenticated"
    headers = {"Cookie": f".ROBLOSECURITY={cookie}"}
    try:
        req = urllib.request.Request(url_auth, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.getcode() == 200:
                data = json.loads(resp.read().decode())
                user_id = data.get("id")
                username = data.get("name", "")
                created = data.get("created", "")

                if not created and user_id:
                    url_profile = f"https://users.roblox.com/v1/users/{user_id}"
                    req2 = urllib.request.Request(url_profile)
                    with urllib.request.urlopen(req2, timeout=10) as resp2:
                        if resp2.getcode() == 200:
                            profile = json.loads(resp2.read().decode())
                            created = profile.get("created", "")
                return {
                    "username": username,
                    "userID": user_id,
                    "created": created,
                    "valid": True,
                    "status": "Valid"
                }
            else:
                return {"valid": False, "status": f"HTTP {resp.getcode()}"}
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return {"valid": False, "status": "Challenge/Invalid"}
        if e.code == 401:
            return {"valid": False, "status": "Invalid/Expired"}
        return {"valid": False, "status": f"HTTP {e.code}"}
    except json.JSONDecodeError as e:
        return {"valid": False, "status": f"JSON Error: {str(e)}"}
    except Exception as e:
        return {"valid": False, "status": f"Error: {str(e)}"}
    

def export_cookies_webhook(cfg, selected_packages=None):
    import urllib.request
    import urllib.error
    import random
    import string

    webhook = cfg.get("cookie_webhook_url", "").strip()
    if not webhook:
        print(col("Webhook URL not set. Please paste it now:", YELLOW))
        url = input("URL: ").strip()
        if not url:
            print("Aborted.")
            pause()
            return
        cfg["cookie_webhook_url"] = url
        save_config(cfg)
        webhook = url

    all_packages = get_installed_packages()
    if not all_packages:
        print("[COOKIE] No packages found.")
        pause()
        return

    if selected_packages:
        packages = [p for p in all_packages if p in selected_packages]
        if not packages:
            print("No matching packages.")
            pause()
            return
    else:
        packages = all_packages

    account_data = []

    for pkg in packages:
        print(f"  -> Extracting from {pkg}...")
        cookie = None
        db_src = f"/data/data/{pkg}/app_webview/Default/Cookies"
        db_dst = "/sdcard/Download/tmp_cookie.db"
        try:
            p = subprocess.run(
                ["su", "-c", f"cp {db_src} {db_dst}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if p.returncode == 0 and os.path.exists(db_dst):
                conn = sqlite3.connect(db_dst, timeout=1)
                c = conn.cursor()
                c.execute(
                    "SELECT value FROM cookies WHERE host_key LIKE '%.roblox.com' AND name='.ROBLOSECURITY'"
                )
                row = c.fetchone()
                conn.close()
                if row:
                    cookie = row[0]
        except Exception as e:
            print(f"    Error: {e}")
            cookie = None
        finally:
            if os.path.exists(db_dst):
                os.remove(db_dst)

        info = None
        if cookie:
            info = get_roblox_user_info(cookie)
        else:
            info = {"valid": False, "status": "No cookie"}

        username = pkg
        cfg_local = load_config()
        for tab in cfg_local.get("tabs", []):
            if tab.get("package") == pkg:
                state_path = tab.get("stat_file", "")
                if state_path and os.path.exists(state_path):
                    try:
                        with open(state_path) as f:
                            state = json.load(f)
                            if state.get("username"):
                                username = state.get("username")
                    except:
                        pass
                break

        if info and info.get("valid") and info.get("username"):
            username = info.get("username", username)

        created = info.get("created", "N/A") if info else "N/A"
        age = compute_age(created) if created != "N/A" else "N/A"

        account_data.append({
            "package": pkg,
            "cookie": cookie,
            "username": username,
            "userID": info.get("userID", "N/A") if info else "N/A",
            "created": created[:10] if created != "N/A" else "N/A",
            "age": age,
            "status": info.get("status", "Unknown") if info else "Unknown",
            "valid": info.get("valid", False) if info else False,
        })
        print(f"    {username}: {account_data[-1]['status']} (cookie len: {len(cookie) if cookie else 0})")

    # Build a pretty message
    message_lines = ["**NOMO REJOIN - Cookie Export**", ""]
    for acc in account_data:
        message_lines.append(f"**Cookie - {acc['username']}**")
        message_lines.append("")
        message_lines.append(f"Username: {acc['username']}")
        message_lines.append(f"ID: {acc['userID']}")
        message_lines.append(f"Created: {acc['created']}")
        message_lines.append(f"Age: {acc['age']} days")
        message_lines.append(f"Status: {acc['status']}")
        message_lines.append("")
        message_lines.append("---")
        message_lines.append("")

    message = "\n".join(message_lines)

    # ---- Build combined file with ONLY cookies ----
    combined_lines = []
    for acc in account_data:
        cookie_str = acc["cookie"] if acc["cookie"] else "_WARNING"
        combined_lines.append(cookie_str)
    combined_content = "\n".join(combined_lines)
    combined_filename = "all_cookies.txt"

    # ---- Prepare multipart ----
    boundary = '----WebKitFormBoundary' + ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    body_parts = []

    # 1. payload_json (the message)
    json_part = json.dumps({"content": message})
    body_parts.append(f'--{boundary}'.encode())
    body_parts.append(b'Content-Disposition: form-data; name="payload_json"')
    body_parts.append(b'')
    body_parts.append(json_part.encode('utf-8'))

    # 2. Single combined file with only cookies
    body_parts.append(f'--{boundary}'.encode())
    body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{combined_filename}"'.encode())
    body_parts.append(b'Content-Type: text/plain')
    body_parts.append(b'')
    body_parts.append(combined_content.encode('utf-8'))

    body_parts.append(f'--{boundary}--'.encode())
    body_parts.append(b'')

    body = b'\r\n'.join(body_parts)

    # Send multipart
    try:
        req = urllib.request.Request(webhook, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("User-Agent", "NOMO-Rejoin/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.getcode() in (200, 204):
                print(col("Webhook sent successfully with combined cookie file.", GREEN))
                pause()
                return
            else:
                print(col(f"Multipart error: {resp.getcode()}, falling back to single files...", YELLOW))
    except Exception as e:
        print(col(f"Multipart failed: {e}, falling back to single files...", YELLOW))

    # ---- Fallback: send summary message + combined file separately ----
    try:
        summary_payload = json.dumps({"content": message}).encode('utf-8')
        req = urllib.request.Request(webhook, data=summary_payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "NOMO-Rejoin/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.getcode() in (200, 204):
                print(col("Summary message sent.", GREEN))
            else:
                print(col(f"Summary failed: {resp.getcode()}", YELLOW))
    except Exception as e:
        print(col(f"Summary failed: {e}", YELLOW))

    # Send combined file
    boundary2 = '----WebKitFormBoundary' + ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    parts2 = []
    json_part2 = json.dumps({"content": "Combined cookie file attached."})
    parts2.append(f'--{boundary2}'.encode())
    parts2.append(b'Content-Disposition: form-data; name="payload_json"')
    parts2.append(b'')
    parts2.append(json_part2.encode('utf-8'))
    parts2.append(f'--{boundary2}'.encode())
    parts2.append(f'Content-Disposition: form-data; name="file"; filename="{combined_filename}"'.encode())
    parts2.append(b'Content-Type: text/plain')
    parts2.append(b'')
    parts2.append(combined_content.encode('utf-8'))
    parts2.append(f'--{boundary2}--'.encode())
    parts2.append(b'')
    body2 = b'\r\n'.join(parts2)

    try:
        req = urllib.request.Request(webhook, data=body2, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary2}")
        req.add_header("User-Agent", "NOMO-Rejoin/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.getcode() in (200, 204):
                print(col(f"Sent {combined_filename}", GREEN))
            else:
                print(col(f"Failed to send {combined_filename}: {resp.getcode()}", RED))
    except Exception as e:
        print(col(f"Failed to send {combined_filename}: {e}", RED))

    pause()

def choose_packages(cfg):
    return choose_packages_common(cfg, "SELECT PACKAGES", multi=True,
                                  installed_only=True, include_discovered=True)

def normalize_cookie_input(raw):
    """Keep real Roblox cookies intact while stripping file/header noise."""
    raw = str(raw or "").strip().strip('"').strip("'").strip()
    if not raw:
        return raw

    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    for line in reversed(lines):
        low = line.lower()
        if low.startswith(".roblosecurity="):
            return line.split("=", 1)[1].strip()
        if line.startswith(("_|WARNING:", "_|WARNING", "_WARNING", "__|WARNING")):
            return line

    if raw.lower().startswith(".roblosecurity="):
        return raw.split("=", 1)[1].strip()
    return raw


def parse_cookie_line(line):
    """Parse a single line into (username, password, cookie). Returns (None, None, None) if invalid."""
    line = normalize_cookie_input(line)
    if not line:
        return None, None, None

    roblox_markers = ("_|WARNING:", "_|WARNING", "_WARNING", "__|WARNING")
    if line.startswith(roblox_markers):
        return None, None, line

    for marker in roblox_markers:
        marker_idx = line.find(marker)
        if marker_idx > 0:
            prefix = line[:marker_idx].rstrip(":").strip()
            cookie = line[marker_idx:].strip()
            prefix_parts = prefix.split(":", 1)
            if len(prefix_parts) == 2:
                return prefix_parts[0].strip(), prefix_parts[1].strip(), cookie
            return prefix.strip() or None, None, cookie

    parts = line.split(':', 2)
    if len(parts) == 3:
        return parts[0].strip(), parts[1].strip(), normalize_cookie_input(parts[2])
    elif len(parts) == 2:
        return parts[0].strip(), None, normalize_cookie_input(parts[1])
    else:
        return None, None, line


def clean_cookie(raw):
    """Remove common warning prefixes from a cookie string."""
    return normalize_cookie_input(raw)

# Add this function near inject_cookie():
def inject_and_restart(package, cookie, cfg):
    """Inject cookie, force-stop, verify, then reopen."""
    # Step 1: Force-stop the app to release DB lock
    force_stop_package(package, cfg)
    time.sleep(1)

    # Step 2: Actually inject the cookie
    ok, msg = inject_cookie(package, cookie)
    if not ok:
        return False, msg

    # Step 3: Clear WebView cache
    cache_path = f"/data/data/{package}/app_webview/Cache"
    clear_cmd = f"rm -rf {shlex.quote(cache_path)}/*"
    try:
        subprocess.run(
            "su -c " + shlex.quote(clear_cmd),
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            text=True,
        )
    except Exception:
        pass

    # Step 4: Force-stop again so app re-reads the fresh cookie on next launch
    force_stop_package(package, cfg)
    time.sleep(1)

    return True, msg + " (app stopped; ready to reopen)"



    
def inject_cookie(package, cookie):
    """Write .ROBLOSECURITY into every WebView cookie DB found for a package."""
    if not cookie or not package:
        return False, "missing cookie or package"

    safe_pkg = re.sub(r"[^A-Za-z0-9_.-]", "_", package)

    # Helper to run a shell command with timeout and no stdin
    def run_su_cmd(cmd, timeout=5):
        full_cmd = f"su -c {shlex.quote(cmd)}"
        try:
            p = subprocess.run(
                full_cmd,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                text=True
            )
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout after {timeout}s"
        except Exception as e:
            return -2, "", str(e)

    def discover_cookie_dbs():
        candidates = [
            f"/data/data/{package}/app_webview/Default/Cookies",
            f"/data/user/0/{package}/app_webview/Default/Cookies",
            f"/data/data/{package}/app_webview/Cookies",
            f"/data/user/0/{package}/app_webview/Cookies",
        ]

        roots = [
            f"/data/data/{package}/app_webview",
            f"/data/user/0/{package}/app_webview",
            f"/data/data/{package}",
            f"/data/user/0/{package}",
        ]
        find_cmd = "for d in " + " ".join(shlex.quote(x) for x in roots) + "; do " \
                   "[ -d \"$d\" ] && find \"$d\" -type f -name Cookies 2>/dev/null; " \
                   "done"
        ret, out, err = run_su_cmd(find_cmd, timeout=12)
        if ret == 0 and out:
            candidates.extend(x.strip() for x in out.splitlines() if x.strip())

        seen = set()
        found = []
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            ret, out, err = run_su_cmd(f"test -s {shlex.quote(path)}", timeout=4)
            if ret == 0:
                found.append(path)
        return found

    def inject_one_db(db_src, db_idx):
        db_dst = f"/sdcard/Download/tmp_cookie_{safe_pkg}_{os.getpid()}_{db_idx}.db"

        # ---- Copy DB with retry ----
        success = False
        owner = ""
        ret, out, err = run_su_cmd(f"stat -c %u:%g {shlex.quote(db_src)}", timeout=5)
        if ret == 0:
            owner = out.strip()

        for attempt in range(3):
            ret, out, err = run_su_cmd(f"cp {shlex.quote(db_src)} {shlex.quote(db_dst)}", timeout=5)
            if ret == 0 and os.path.exists(db_dst) and os.path.getsize(db_dst) > 0:
                success = True
                break
            time.sleep(1)
        if not success:
            return False, f"{db_src}: failed to copy database"

        try:
            conn = sqlite3.connect(db_dst, timeout=2)
            c = conn.cursor()

            # Get schema
            c.execute("PRAGMA table_info(cookies)")
            columns_info = c.fetchall()
            columns = [row[1] for row in columns_info]
            col_types = {row[1]: row[2] for row in columns_info}
            col_notnull = {row[1]: row[3] for row in columns_info}

            # Build a row with default values for all columns
            row_data = {}
            for col in columns:
                col_type = col_types.get(col, "TEXT").upper()
                if col_notnull.get(col, 0) == 1:
                    if "BLOB" in col_type:
                        row_data[col] = b""
                    elif "INT" in col_type or "REAL" in col_type:
                        row_data[col] = 0
                    else:
                        row_data[col] = ""
                else:
                    row_data[col] = None

            # Override with our known values
            row_data["host_key"] = ".roblox.com"
            row_data["name"] = ".ROBLOSECURITY"
            row_data["value"] = cookie
            row_data["path"] = "/"
            if "encrypted_value" in columns:
                row_data["encrypted_value"] = b""
            if "top_frame_site_key" in columns:
                row_data["top_frame_site_key"] = ""
            if "partition_key" in columns:
                row_data["partition_key"] = ""

            chrome_now = int((time.time() + 11644473600) * 1000000)
            chrome_expires = chrome_now + int(10 * 365 * 24 * 60 * 60 * 1000000)

            for ts_col in ["expires_utc", "creation_utc", "creation_act", "last_access_utc", "last_access_act"]:
                if ts_col in columns:
                    row_data[ts_col] = chrome_expires if ts_col == "expires_utc" else chrome_now

            optional_values = {
                "secure": 1,
                "httponly": 1,
                "is_secure": 1,
                "is_httponly": 1,
                "has_expires": 1,
                "is_persistent": 1,
                "priority": 1,
                "samesite": -1,
                "source_scheme": 2,
                "source_port": 443,
                "is_same_party": 0,
                "same_party": 0,
                "is_partitioned": 0,
            }
            for opt_col, opt_val in optional_values.items():
                if opt_col in columns:
                    row_data[opt_col] = opt_val

            # Remove old Roblox login cookies first
            c.execute("DELETE FROM cookies WHERE name = ?", (".ROBLOSECURITY",))

            placeholders = ",".join(["?" for _ in columns])
            col_names = ",".join(columns)
            c.execute(f"INSERT OR REPLACE INTO cookies ({col_names}) VALUES ({placeholders})", [row_data.get(col) for col in columns])

            conn.commit()
            conn.close()

            # ---- Copy back with retry ----
            success = False
            for attempt in range(3):
                ret, out, err = run_su_cmd(f"cp {shlex.quote(db_dst)} {shlex.quote(db_src)}", timeout=5)
                if ret == 0:
                    success = True
                    break
                time.sleep(1)
            if not success:
                return False, f"{db_src}: failed to copy back database"

            if owner:
                run_su_cmd(f"chown {shlex.quote(owner)} {shlex.quote(db_src)}", timeout=5)
            run_su_cmd(f"chmod 600 {shlex.quote(db_src)}", timeout=5)

            # ---- Verification (read back) ----
            ret, out, err = run_su_cmd(f"cp {shlex.quote(db_src)} {shlex.quote(db_dst)}", timeout=5)
            if ret == 0 and os.path.exists(db_dst):
                conn2 = sqlite3.connect(db_dst, timeout=2)
                c2 = conn2.cursor()
                c2.execute("SELECT value FROM cookies WHERE name='.ROBLOSECURITY' ORDER BY rowid DESC LIMIT 1")
                row2 = c2.fetchone()
                conn2.close()
                if row2 and row2[0] == cookie:
                    return True, db_src
                return False, f"{db_src}: verification failed"

            return True, f"{db_src}: injected (verification skipped)"

        except Exception as e:
            return False, f"{db_src}: DB error: {e}"
        finally:
            if os.path.exists(db_dst):
                try: os.remove(db_dst)
                except: pass

    db_paths = discover_cookie_dbs()
    if not db_paths:
        return False, "no WebView Cookies database found for package"

    ok_paths = []
    errors = []
    for i, db_src in enumerate(db_paths, 1):
        ok, msg = inject_one_db(db_src, i)
        if ok:
            ok_paths.append(msg)
        else:
            errors.append(msg)

    if ok_paths:
        detail = ", ".join(ok_paths[:3])
        extra = "" if len(ok_paths) <= 3 else f" (+{len(ok_paths) - 3} more)"
        return True, f"cookie injected into {len(ok_paths)} DB(s): {detail}{extra}"

    return False, "; ".join(errors[:3]) or "cookie injection failed"

def old_inject_cookie_unused(package, cookie):
    # ---- Copy DB with retry ----
    success = False
    owner = ""
    ret, out, err = run_su_cmd(f"stat -c %u:%g {shlex.quote(db_src)}", timeout=5)
    if ret == 0:
        owner = out.strip()

    for attempt in range(3):
        ret, out, err = run_su_cmd(f"cp {shlex.quote(db_src)} {shlex.quote(db_dst)}", timeout=5)
        if ret == 0 and os.path.exists(db_dst) and os.path.getsize(db_dst) > 0:
            success = True
            break
        time.sleep(1)
    if not success:
        return False, "failed to copy database (may not exist or locked)"

    try:
        conn = sqlite3.connect(db_dst, timeout=2)
        c = conn.cursor()

        c.execute("PRAGMA table_info(cookies)")
        columns_info = c.fetchall()
        columns = [row[1] for row in columns_info]
        col_types = {row[1]: row[2] for row in columns_info}
        col_notnull = {row[1]: row[3] for row in columns_info}

        row_data = {}
        for col in columns:
            col_type = col_types.get(col, "TEXT").upper()
            if col_notnull.get(col, 0) == 1:
                if "INT" in col_type or "REAL" in col_type:
                    row_data[col] = 0
                else:
                    row_data[col] = ""
            else:
                row_data[col] = None

        row_data["host_key"] = ".roblox.com"
        row_data["name"] = ".ROBLOSECURITY"
        row_data["value"] = cookie
        row_data["path"] = "/"

        chrome_now = int((time.time() + 11644473600) * 1000000)
        chrome_expires = chrome_now + int(10 * 365 * 24 * 60 * 60 * 1000000)

        for ts_col in ["expires_utc", "creation_utc", "creation_act", "last_access_utc", "last_access_act"]:
            if ts_col in columns:
                row_data[ts_col] = chrome_expires if ts_col == "expires_utc" else chrome_now

        optional_values = {
            "secure": 1,
            "httponly": 1,
            "is_secure": 1,
            "is_httponly": 1,
            "has_expires": 1,
            "is_persistent": 1,
            "priority": 1,
            "samesite": -1,
            "source_scheme": 2,
            "source_port": 443,
            "is_same_party": 0,
            "same_party": 0,
        }
        for opt_col, opt_val in optional_values.items():
            if opt_col in columns:
                row_data[opt_col] = opt_val

        c.execute("DELETE FROM cookies WHERE host_key LIKE ? AND name = ?", ("%.roblox.com", ".ROBLOSECURITY"))

        placeholders = ",".join(["?" for _ in columns])
        col_names = ",".join(columns)
        c.execute(f"INSERT OR REPLACE INTO cookies ({col_names}) VALUES ({placeholders})", [row_data.get(col) for col in columns])

        conn.commit()
        conn.close()

        success = False
        for attempt in range(3):
            ret, out, err = run_su_cmd(f"cp {shlex.quote(db_dst)} {shlex.quote(db_src)}", timeout=5)
            if ret == 0:
                success = True
                break
            time.sleep(1)
        if not success:
            return False, "failed to copy back database"

        if owner:
            run_su_cmd(f"chown {shlex.quote(owner)} {shlex.quote(db_src)}", timeout=5)

        try:
            ret, out, err = run_su_cmd(f"cp {shlex.quote(db_src)} {shlex.quote(db_dst)}", timeout=5)
            if ret == 0 and os.path.exists(db_dst):
                conn2 = sqlite3.connect(db_dst, timeout=2)
                c2 = conn2.cursor()
                c2.execute("SELECT value FROM cookies WHERE host_key LIKE '%.roblox.com' AND name='.ROBLOSECURITY' ORDER BY rowid DESC LIMIT 1")
                row2 = c2.fetchone()
                conn2.close()
                if row2 and row2[0] == cookie:
                    return True, "cookie injected and verified"
                else:
                    return False, "verification failed: cookie mismatch"
        except Exception as e:
            pass
        finally:
            if os.path.exists(db_dst):
                try: os.remove(db_dst)
                except: pass

        return True, "cookie injected (verification skipped)"

    except Exception as e:
        return False, f"DB error: {e}"
    finally:
        if os.path.exists(db_dst):
            try: os.remove(db_dst)
            except: pass


def import_cookie_menu(cfg):
    while True:
        clear()
        banner("LOGIN VIA COOKIE", cfg)
        print("1. Login cookie for one package")
        print("2. Login cookies for all packages (from file)")
        print("0. Back")
        drain_stdin()
        ch = input("\nEnter your choice (1/2/0): ").strip()

        if ch == "0":
            return

        elif ch == "1":
            selected = choose_packages_common(cfg, "LOGIN COOKIE: SELECT PACKAGE", multi=False,
                                              enabled_only=True, include_discovered=False,
                                              configured_only=True)
            if not selected:
                continue
            pkg = selected[0]
            print(col(f"[+] Selected: {pkg}", GREEN))

            cookie_input = input("Enter your cookie (username:pass:cookie or username:cookie or cookie only): ").strip()
            if not cookie_input:
                print("No cookie provided.")
                pause()
                continue

            cookie_input = clean_cookie(cookie_input)

            username, password, cookie = parse_cookie_line(cookie_input)
            if not cookie:
                print(col("Could not parse cookie.", RED))
                pause()
                continue
            if len(cookie) < 500 or not cookie.startswith(("_|WARNING", "_WARNING", "__|WARNING")):
                print(col("Parsed cookie does not look like a full Roblox .ROBLOSECURITY cookie.", YELLOW))

            ok, msg = inject_and_restart(pkg, cookie, cfg)
            if ok:
                print(col(f"Cookie injected into {pkg}", GREEN))
                print(col(f"    {msg}", DIM))
                cache = {}
                if COOKIE_CACHE.exists():
                    try:
                        with open(COOKIE_CACHE) as f:
                            cache = json.load(f)
                    except:
                        pass
                if pkg not in cache:
                    cache[pkg] = {}
                cache[pkg]["cookie"] = cookie
                cache[pkg]["username"] = username or cache[pkg].get("username", pkg)
                cache[pkg]["last_import"] = time.time()
                with open(COOKIE_CACHE, "w") as f:
                    json.dump(cache, f, indent=2)
                if password:
                    cache = {}
                    if COOKIE_CACHE.exists():
                        try:
                            with open(COOKIE_CACHE) as f:
                                cache = json.load(f)
                        except:
                            pass
                    if pkg not in cache:
                        cache[pkg] = {}
                    cache[pkg]["password"] = password
                    cache[pkg]["username"] = username or cache[pkg].get("username", pkg)
                    cache[pkg]["last_import"] = time.time()
                    with open(COOKIE_CACHE, "w") as f:
                        json.dump(cache, f, indent=2)
                    print(col("Password stored locally.", GREEN))
            else:
                print(col(f"Injection failed: {msg}", RED))
            pause()

        elif ch == "2":
            print("\n[+] Supported formats:")
            print("    username:password:_WARNING... (user:pass:cookie)")
            print("    username:_WARNING... (user:cookie)")
            print("    _WARNING... (cookie only)")

            packages = [t.get("package") for t in cfg.get("tabs", []) if t.get("enabled", True)]
            if not packages:
                print(col("No enabled packages found.", RED))
                pause()
                continue

            print(f"\n[+] Cookie order must match the package order in config ({len(packages)} package(s)):")
            for i, pkg in enumerate(packages, 1):
                uname = pkg
                for tab in cfg.get("tabs", []):
                    if tab.get("package") == pkg:
                        uname = tab.get("user_name", pkg)
                        break
                print(f"    [{i}] {pkg} ({uname})")

            file_path = input("\n[?] Enter cookie file path: ").strip()
            if not file_path:
                print("No path provided.")
                pause()
                continue

            path = Path(file_path)
            if not path.exists():
                print(col(f"File not found: {path}", RED))
                pause()
                continue

            try:
                lines = path.read_text().strip().splitlines()
                if not lines:
                    print("File is empty.")
                    pause()
                    continue
            except Exception as e:
                print(col(f"Error reading file: {e}", RED))
                pause()
                continue

            imported = 0
            failed = 0
            pkg_queue = list(packages)
            pkg_idx = 0
            user_to_pkg = {}
            for tab in cfg.get("tabs", []):
                pkg = tab.get("package")
                uname = tab.get("user_name", "")
                if uname:
                    user_to_pkg[uname] = pkg
                user_to_pkg[pkg] = pkg

            cache = {}
            if COOKIE_CACHE.exists():
                try:
                    with open(COOKIE_CACHE) as f:
                        cache = json.load(f)
                except:
                    pass

            for line in lines:
                line = clean_cookie(line)
                if not line:
                    continue

                username, password, cookie = parse_cookie_line(line)
                if not cookie or cookie == "_WARNING":
                    print(col(f"Skipping invalid line: {line[:30]}...", YELLOW))
                    failed += 1
                    continue
                if len(cookie) < 500 or not cookie.startswith(("_|WARNING", "_WARNING", "__|WARNING")):
                    print(col(f"parsed cookie looks incomplete", YELLOW))

                pkg = None
                if username:
                    if username in user_to_pkg:
                        pkg = user_to_pkg[username]
                    else:
                        for u, p in user_to_pkg.items():
                            if u.lower() == username.lower():
                                pkg = p
                                break
                    if not pkg:
                        print(col(f"No package found for username: {username}", RED))
                        failed += 1
                        continue
                else:
                    if pkg_idx < len(pkg_queue):
                        pkg = pkg_queue[pkg_idx]
                        pkg_idx += 1
                    else:
                        print(col(f"No more packages for raw cookie: {line[:30]}...", YELLOW))
                        failed += 1
                        continue

                ok, msg = inject_and_restart(pkg, cookie, cfg)
                if ok:
                    imported += 1
                    if pkg not in cache:
                        cache[pkg] = {}
                    cache[pkg]["cookie"] = cookie
                    if password:
                        cache[pkg]["password"] = password
                    cache[pkg]["username"] = username or cache[pkg].get("username", pkg)
                    cache[pkg]["last_import"] = time.time()
                    print(col(f"  {pkg}: injected", GREEN))
                    print(col(f"    {msg}", DIM))
                else:
                    failed += 1
                    print(col(f"  {pkg}: {msg}", RED))

            if cache:
                try:
                    with open(COOKIE_CACHE, "w") as f:
                        json.dump(cache, f, indent=2)
                except Exception as e:
                    print(col(f"Could not save cache: {e}", YELLOW))

            print(col(f"\nDone. Imported: {imported}, Failed: {failed}", GREEN if imported else RED))
            pause()

        else:
            print("Invalid choice.")
            time.sleep(1)


def parse_solver_url(full_url):
    """Extract API key while preserving an exact custom solver path."""
    full_url = str(full_url or "").strip()
    if not full_url:
        return "", ""

    base = full_url
    api_key = ""
    if "?" in full_url:
        base, query = full_url.split("?", 1)
        kept = []
        for param in query.split("&"):
            if "=" in param and param.split("=", 1)[0].lower() == "apikey":
                api_key = param.split("=", 1)[1]
            elif param:
                kept.append(param)
        if kept:
            base += "?" + "&".join(kept)

    return base.rstrip("/"), api_key

def ensure_cookie_for_package(pkg, cfg=None):
    """Ensure cookie is in cache; if not, extract it now."""
    cache = load_cookie_cache()
    if pkg in cache and cache[pkg].get("cookie"):
        return cache[pkg]["cookie"]
    cookie = get_cookie_from_package(pkg)
    if cookie:
        if pkg not in cache:
            cache[pkg] = {}
        cache[pkg]["cookie"] = cookie
        cache[pkg]["updated"] = now()
        if cfg:
            for tab in cfg.get("tabs", []):
                if tab.get("package") == pkg:
                    state_path = tab.get("stat_file", "")
                    if state_path and os.path.exists(state_path):
                        try:
                            with open(state_path) as f:
                                state = json.load(f)
                                if state.get("username"):
                                    cache[pkg]["username"] = state.get("username")
                        except:
                            pass
                    break
        with open(COOKIE_CACHE, "w") as f:
            json.dump(cache, f, indent=2)
        return cookie
    return None

def recovery_menu(cfg):
    while True:
        clear()
        banner("RECOVERY TOOLS", cfg)
        print("1. Clear ALL holds (captcha_hold.json)")
        print("2. Clear ALL manual login flags (runtime.json)")
        print("3. Show current hold status")
        print("4. Clear hold for one package")
        print("5. Clear manual flag for one package")
        print("6. Test login/CAPTCHA detection for one package")
        print("0. Back")
        drain_stdin()
        ch = input("\nChoose: ").strip()
        if ch == "0":
            reset_terminal()
            drain_stdin()
            return
        elif ch == "1":
            clear_all_holds()
            print(col("All holds cleared from captcha_hold.json.", GREEN))
            rt = load_runtime()
            for pkg, rt_tab in list(rt.items()):
                if isinstance(rt_tab, dict) and rt_tab.get("manual_login_needed"):
                    clear_manual_login_block(rt_tab)
            save_runtime(rt)
            print(col("All manual flags cleared from runtime.json.", GREEN))
            pause()
        elif ch == "2":
            rt = load_runtime()
            cleared = 0
            for pkg, rt_tab in list(rt.items()):
                if isinstance(rt_tab, dict) and rt_tab.get("manual_login_needed"):
                    clear_manual_login_block(rt_tab)
                    cleared += 1
            save_runtime(rt)
            print(col(f"Cleared {cleared} manual flags from runtime.json.", GREEN))
            pause()
        elif ch == "3":
            hold_file = BASE_DIR / "captcha_hold.json"
            if not hold_file.exists():
                print(col("No holds file found. All packages are free.", GREEN))
                pause()
                continue
            try:
                with open(hold_file) as f:
                    holds = json.load(f)
                if not holds:
                    print(col("No holds. All packages are free.", GREEN))
                else:
                    print(col("Packages currently on hold:", YELLOW))
                    for pkg, data in holds.items():
                        reason = data.get("reason", "unknown")
                        ts = data.get("timestamp", 0)
                        age = format_age(now() - ts) if ts else "?"
                        print(f"  {pkg}: {reason} (since {age})")
            except Exception as e:
                print(col(f"Error reading holds: {e}", RED))
            pause()
        elif ch == "4":
            selected = choose_packages_common(cfg, "CLEAR HOLD: SELECT PACKAGE", multi=False,
                                              include_discovered=True, installed_only=False)
            if selected:
                pkg = selected[0]
                clear_hold(pkg)
                print(col(f"Hold cleared for {pkg}", GREEN))
                pause()
        elif ch == "5":
            selected = choose_packages_common(cfg, "CLEAR MANUAL FLAG: SELECT PACKAGE", multi=False,
                                              include_discovered=True, installed_only=False)
            if selected:
                pkg = selected[0]
                rt = load_runtime()
                rt_tab = get_runtime_tab(rt, pkg)
                if clear_manual_login_block(rt_tab):
                    save_runtime(rt)
                    print(col(f"Manual flag cleared for {pkg}", GREEN))
                else:
                    print(col(f"No manual flag for {pkg}", YELLOW))
                pause()
        elif ch == "6":
            test_login_challenge_detection_menu(cfg)
        else:
            print("Invalid choice.")
            time.sleep(1)

def solver_menu(cfg):
    while True:
        clear()
        banner("CAPTCHA SOLVER", cfg)
        print(f"1. Enable/disable: {cfg.get('solver_enabled', False)}")
        print(f"2. Endpoint + API key (paste full URL): {cfg.get('solver_endpoint', 'https://solver.wintercode.dev')}")
        print(f"   API key: {mask_secret(cfg.get('solver_api_key', ''))}")
        print(f"3. Default place ID: {cfg.get('solver_place_id', '126884695634066')}")
        print(f"4. Timeout / min provider interval / probe delay: {cfg.get('solver_timeout_seconds', 180)}s / {cfg.get('solver_min_resubmit_seconds', 600)}s / {cfg.get('solver_probe_after_seconds', 45)}s")
        print(f"   Once per actual rejoin: {cfg.get('solver_probe_once_per_open', True)}")
        print(f"   Cookie precheck required: {cfg.get('solver_require_cookie_precheck', True)}")
        print("5. Test solver with a package (uses cached cookie)")
        print("6. Test solver with a sample cookie (paste manually)")
        print("0. Back")
        drain_stdin()
        ch = input("\nChoose: ").strip()
        if ch == "0":
            save_config(cfg)
            reset_terminal()
            drain_stdin()
            return
        elif ch == "1":
            cfg["solver_enabled"] = not cfg.get("solver_enabled", False)
            save_config(cfg)
            print(col(f"Solver {'enabled' if cfg['solver_enabled'] else 'disabled'}", GREEN if cfg['solver_enabled'] else RED))
            pause()
        elif ch == "2":
            url = input("Paste solver URL (e.g., https://solver.wintercode.dev/api/captcha/solve?apikey=YOUR_KEY): ").strip()
            if url:
                base, key = parse_solver_url(url)
                if base:
                    cfg["solver_endpoint"] = base
                    print(col(f"Base URL set to: {base}", GREEN))
                if key:
                    cfg["solver_api_key"] = key
                    print(col(f"API key set: {mask_secret(key)}", GREEN))
                elif not key and "?apikey=" not in url and "?apiKey=" not in url:
                    key2 = input("No API key found in URL. Paste API key separately: ").strip()
                    if key2:
                        cfg["solver_api_key"] = key2
                        print(col("API key saved.", GREEN))
                save_config(cfg)
            pause()
        elif ch == "3":
            pid = input("Default place ID (numeric): ").strip()
            if pid.isdigit():
                cfg["solver_place_id"] = pid
                save_config(cfg)
                print(col(f"Place ID set to {pid}", GREEN))
            else:
                print(col("Invalid place ID (must be numeric).", RED))
            pause()
        elif ch == "4":
            try:
                timeout = int(input(f"Request timeout seconds [{cfg.get('solver_timeout_seconds', 180)}]: ").strip() or cfg.get("solver_timeout_seconds", 180))
                cooldown = int(input(f"Minimum provider interval seconds [{cfg.get('solver_min_resubmit_seconds', 600)}]: ").strip() or cfg.get("solver_min_resubmit_seconds", 600))
                probe = int(input(f"Post-rejoin no-fresh probe delay seconds [{cfg.get('solver_probe_after_seconds', 45)}]: ").strip() or cfg.get("solver_probe_after_seconds", 45))
                cfg["solver_timeout_seconds"] = max(15, timeout)
                cfg["solver_min_resubmit_seconds"] = max(600, cooldown)
                cfg["solver_retry_cooldown_seconds"] = max(600, cooldown)
                cfg["solver_probe_after_seconds"] = max(10, probe)
                save_config(cfg)
                print(col("Solver timing saved.", GREEN))
            except Exception:
                print(col("Invalid number.", RED))
            pause()
        elif ch == "5":
            selected = choose_packages_common(
                cfg, "SOLVER TEST: SELECT PACKAGE", multi=False,
                installed_only=True, include_discovered=True
            )
            if not selected:
                continue
            pkg = selected[0]
            cookie = get_cookie_from_package(pkg)
            if not cookie:
                print(col(f"No cookie found for {pkg}. Run Option 7 -> 1 first.", RED))
                pause()
                continue

            place_id = cfg.get("solver_place_id", "126884695634066")
            for tab in cfg.get("tabs", []):
                if tab.get("package") == pkg:
                    server_link = tab.get("server_link", "")
                    if server_link:
                        pid = extract_place_id_from_link(server_link)
                        if pid:
                            place_id = pid
                    break

            print(col(f"Testing solver for {pkg}...", YELLOW))
            try:
                ok, resp = solve_captcha(cookie, cfg, place_id)
                if ok:
                    print(col("Solver success", GREEN))
                    if is_on_hold(pkg):
                        clear_hold(pkg)
                        print(col("Hold cleared from captcha_hold.json.", GREEN))
                    rt = load_runtime()
                    rt_tab = get_runtime_tab(rt, pkg)
                    clear_manual_login_block(rt_tab)
                    save_runtime(rt)
                    print(col("Manual login flag cleared from runtime.json.", GREEN))
                    status = check_cookie_challenge(cookie)
                    if status == "valid":
                        print(col("Cookie is now valid.", GREEN))
                    elif status == "challenge":
                        print(col("Cookie still challenged - solver may need more time.", YELLOW))
                    else:
                        print(col(f"Cookie status: {status} - likely expired.", RED))
                else:
                    error = resp.get("error", resp) if isinstance(resp, dict) else resp
                    print(col(f"Solver failed: {error}", RED))
            except Exception as e:
                print(col(f"Error: {e}", RED))
            pause()

        elif ch == "6":
            cookie = input("Paste a cookie to test: ").strip()
            if cookie:
                print(col(f"Testing solver (timeout {cfg.get('solver_timeout_seconds', 180)}s)...", YELLOW))
                ok, resp = solve_captcha(cookie, cfg)
                if ok:
                    print(col("Solver success", GREEN))
                    status = check_cookie_challenge(cookie)
                    if status == "valid":
                        print(col("Cookie is now valid.", GREEN))
                    elif status == "challenge":
                        print(col("Cookie still challenged - solver may need more time or token must be submitted.", YELLOW))
                    else:
                        print(col(f"Cookie status: {status} - likely expired.", RED))
                else:
                    error = resp.get('error', resp) if isinstance(resp, dict) else resp
                    print(col(f"Solver failed: {error}", RED))
            else:
                print(col("No cookie provided.", RED))
            pause()
        else:
            print("Invalid choice.")
            time.sleep(1)


def _setup_yes_no(prompt, default=True):
    # All setup recommendations use default=True; pressing ENTER means YES.
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = clean_terminal_input(input(prompt + suffix)).lower()
    if not raw:
        return default
    return raw in {"y", "yes", "1", "true", "on"}


def _setup_secret(prompt="NOMO secret"): 
    try:
        value = getpass.getpass(prompt + ": ")
    except Exception:
        value = input(prompt + ": ")
    return str(value or "").strip()


def _setup_configure_cloudflare(cfg):
    """Configure the shared Cloudflare/D1 credentials and return (cfg, hcfg, ok)."""
    cfg = load_config()
    hcfg = load_hatcher_config()
    existing_url = cloudflare_base_url(cfg) or cloudflare_base_url(hcfg)
    existing_secret = str(cfg.get("cloudflare_secret") or hcfg.get("cloudflare_secret") or "").strip()

    clear()
    banner("SETUP: CLOUDFLARE / D1", cfg)
    if existing_url and existing_secret and not is_placeholder_value(existing_url) and not is_placeholder_value(existing_secret):
        print(f"Worker: {existing_url}")
        print(f"Secret: {mask_secret(existing_secret)}")
        if _setup_yes_no("Reuse these existing backend credentials?", True):
            worker_url, secret = existing_url, existing_secret
        else:
            worker_url = clean_terminal_input(input("Worker URL: ")).rstrip("/")
            secret = _setup_secret()
    else:
        print(col("No complete Cloudflare/D1 config was found.", YELLOW))
        worker_url = clean_terminal_input(input("Worker URL: ")).rstrip("/")
        secret = _setup_secret()

    if not worker_url or is_placeholder_value(worker_url) or not secret or is_placeholder_value(secret):
        print(col("Backend setup skipped: URL or secret is missing.", RED))
        return cfg, hcfg, False

    for target in (cfg, hcfg):
        target["backend_provider"] = "cloudflare"
        target["jsonbin_hatchers_enabled"] = True
        target["cloudflare_worker_url"] = worker_url
        target["cloudflare_secret"] = secret
    hcfg["enabled"] = True
    save_config(cfg)
    save_hatcher_config(hcfg)

    ok, msg = backend_health_check(cfg)
    if ok:
        print(col("D1 backend health: OK", GREEN))
    else:
        print(col(f"Backend saved, but health check failed: {msg}", YELLOW))
    return cfg, hcfg, bool(ok)


def _setup_install_counter_for_packages(cfg, packages, path_mode):
    tabs_by_pkg = {str(t.get("package") or ""): t for t in autoexec_tabs(cfg)}
    selected_tabs = [tabs_by_pkg[p] for p in packages if p in tabs_by_pkg]
    results = []
    for tab in selected_tabs:
        pkg = str(tab.get("package") or "")
        paths = autoexec_paths_for_tab(tab, path_mode, filename=AUTOEXEC_PET_COUNTER_FILE)
        if not paths:
            results.append((pkg, False, "no AutoExec path"))
            continue
        ok_count = 0
        errors = []
        for path in paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(pet_counter_autoexec_source(), encoding="utf-8")
                os.replace(str(tmp), str(path))
                ok_count += 1
            except Exception as exc:
                errors.append(str(exc))
        results.append((pkg, ok_count == len(paths), "; ".join(errors) if errors else f"saved {ok_count}"))
    return results


def _setup_register_market_accounts(cfg, packages):
    cache = load_cookie_cache()
    tabs_by_pkg = {str(t.get("package") or ""): t for t in cfg.get("tabs", [])}
    accounts = []
    results = []
    for pkg in packages:
        cookie = get_cookie_from_package(pkg) or str((cache.get(pkg) or {}).get("cookie") or "").strip()
        if not cookie:
            results.append((pkg, False, "no cookie"))
            continue
        username, user_id = get_username_from_cookie(cookie, timeout=12)
        if not username or not str(user_id or "").isdigit():
            results.append((pkg, False, "cookie could not resolve account"))
            continue
        tab = tabs_by_pkg.get(pkg)
        if tab is not None:
            tab["user_name"] = username
        accounts.append({
            "username": username,
            "user_id": str(user_id),
            "role": "market",
            "package_name": pkg,
            "source_name": "nomo-rejoin-v4.04-setup",
        })
        results.append((pkg, True, f"{username} ({user_id})"))
    if not accounts:
        return False, "no accounts ready", results
    ok, msg, data = cloudflare_register_accounts(cfg, accounts)
    if ok:
        save_config(cfg)
        return True, f"registered {int(data.get('registered_count', len(accounts)) or len(accounts))}", results
    return False, msg, results


def new_redfinger_setup_wizard(cfg=None):
    """Guided one-time setup for a fresh Redfinger. Does not start the rejoin loop."""
    cfg = load_config() if cfg is None else cfg
    clear()
    banner("NEW REDFINGER SETUP WIZARD", cfg)
    print(col("This changes local NOMO configuration only and never starts rejoining by itself.", DIM))
    print("")
    print("1. MARKET Redfinger")
    print("2. HATCHER Redfinger")
    print("0. Cancel")
    mode_choice = read_menu_choice("Select role: ", {"0", "1", "2"})
    if mode_choice in {None, "0"}:
        return
    mode = "market" if mode_choice == "1" else "hatcher"

    clear()
    banner("SETUP: PACKAGES", cfg)
    added = sync_installed_packages_into_config(cfg)
    if added:
        print(col("Detected and added: " + ", ".join(added), GREEN))
    cfg = load_config()
    selected = choose_packages_common(
        cfg,
        f"SETUP {mode.upper()} ACTIVE PACKAGES",
        multi=True,
        include_discovered=True,
        installed_only=True,
    )
    if not selected:
        print(col("Setup cancelled: no packages selected.", RED))
        pause()
        return
    configured = {str(t.get("package") or "") for t in cfg.get("tabs", [])}
    for pkg in selected:
        if pkg not in configured:
            cfg.setdefault("tabs", []).append(_new_tab_for_package(pkg, len(cfg.get("tabs", []))))
            configured.add(pkg)
    set_enabled_package_set(cfg, selected)
    cfg = load_config()
    cfg = set_active_rejoin_mode(mode, cfg)
    hcfg = load_hatcher_config()
    sync_hatcher_profiles_with_tabs(cfg, hcfg)
    save_hatcher_config(hcfg)

    clear()
    banner("SETUP: USERNAMES", cfg)
    refresh_usernames_for_packages(cfg, selected)
    cfg = load_config()

    cfg, hcfg, backend_ok = _setup_configure_cloudflare(cfg)

    counter_results = []
    if _setup_yes_no("Install/update the NOMO Pet Counter AutoExec for selected packages?", True):
        tabs_by_pkg = {str(t.get("package") or ""): t for t in autoexec_tabs(cfg)}
        selected_tabs = [tabs_by_pkg[p] for p in selected if p in tabs_by_pkg]
        _print_autoexec_full_paths(selected_tabs, cfg, "Pet Counter destination path(s)")
        counter_results = _setup_install_counter_for_packages(cfg, selected, "1")

    mode_ok = False
    mode_msg = "skipped"
    server_result = None
    first_upload = None
    if mode == "market":
        if backend_ok and _setup_yes_no("Register these Market accounts to D1 now?", True):
            mode_ok, mode_msg, reg_results = _setup_register_market_accounts(cfg, selected)
        else:
            reg_results = []
    else:
        reg_results = []
        if _setup_yes_no("Fetch/create Hatcher private servers and sync Market access now?", True):
            server_result = auto_fetch_private_servers(cfg, selected_packages=selected, pause_at_end=False)
            mode_ok = bool((server_result or {}).get("changed"))
            mode_msg = "private-server setup completed" if mode_ok else "private-server setup made no changes"
        if backend_ok and _setup_yes_no("Force the first Hatcher D1 report now?", True):
            hcfg = load_hatcher_config()
            first_upload = hatcher_report_once(hcfg, force=True)

    clear()
    banner("SETUP COMPLETE", cfg)
    print(f"Role       : {col(mode.upper(), GREEN)}")
    print(f"Packages   : {', '.join(short_pkg(p) for p in selected)}")
    print(f"Backend    : {col('READY' if backend_ok else 'SAVED / CHECK NEEDED', GREEN if backend_ok else YELLOW)}")
    print("")
    print(col("Usernames:", BOLD))
    tabs = {str(t.get("package") or ""): t for t in load_config().get("tabs", [])}
    for pkg in selected:
        print(f"  {short_pkg(pkg):<10} {tabs.get(pkg, {}).get('user_name', '-')}")
    if counter_results:
        print("")
        print(col("Pet Counter:", BOLD))
        for pkg, ok, note in counter_results:
            print(f"  {short_pkg(pkg):<10} {col('OK' if ok else 'FAILED', GREEN if ok else RED)}  {note}")
    if mode == "market":
        print("")
        print(f"D1 Market registration: {col(mode_msg, GREEN if mode_ok else YELLOW)}")
        for pkg, ok, note in reg_results:
            print(f"  {short_pkg(pkg):<10} {col('OK' if ok else 'SKIP', GREEN if ok else YELLOW)}  {note}")
    else:
        print("")
        print(f"Private servers: {col(mode_msg, GREEN if mode_ok else YELLOW)}")
        if first_upload is not None:
            ok, msg = first_upload
            print(f"First D1 report: {col('OK' if ok else 'NOT SENT', GREEN if ok else YELLOW)}  {msg}")
    print("")
    print(col("Next: restart/re-execute the executor if the Pet Counter was newly installed, then use Option 1.", DIM))
    pause()


def mode_menu(cfg):
    while True:
        clear()
        banner("MODE SELECTION", cfg)
        print(f"Current mode: {col(active_mode_label(cfg), GREEN)}")
        print("")
        print("1. Enable MARKET mode")
        print("2. Enable HATCHER mode")
        print("3. Market config (pet routing / restock settings)")
        print("4. Hatcher config (reporting / backend settings)")
        print("0. Back")
        drain_stdin()
        ch = input("\nChoose: ").strip()
        if ch == "0":
            reset_terminal()
            drain_stdin()
            return
        elif ch == "1":
            cfg = set_active_rejoin_mode("market", cfg)
            print(col("MARKET mode enabled. HATCHER mode disabled.", GREEN))
            pause()
        elif ch == "2":
            cfg = set_active_rejoin_mode("hatcher", cfg)
            print(col("HATCHER mode enabled. MARKET mode disabled.", GREEN))
            pause()
        elif ch == "3":
            market_only_settings(cfg)
            cfg = load_config()
        elif ch == "4":
            hatcher_global_settings(cfg)
            cfg = load_config()
        else:
            print("Invalid choice.")
            time.sleep(1)


# ============================================================
# V4.05 SAFE DIAGNOSTICS EXPORT
# ============================================================

_DIAG_SECRET_KEY_PARTS = (
    "cookie", "secret", "password", "passwd", "authorization",
    "api_key", "apikey", "access_token", "refresh_token", "webhook",
)
_DIAG_PRIVATE_CODE_KEYS = {
    "accesscode", "access_code", "linkcode", "link_code",
    "private_server_access_code", "private_server_link_code",
    "privateserveraccesscode", "privateserverlinkcode",
}
_DIAG_ROBLOX_COOKIE_RE = re.compile(
    r"(?:_\|WARNING:-DO-NOT-SHARE-THIS\.[^\s\"'<>]+|\.ROBLOSECURITY\s*[=:]\s*[^\s\"'<>]+)",
    re.IGNORECASE,
)
_DIAG_AUTH_RE = re.compile(
    r"(?im)^\s*(authorization|x-master-key|x-api-key)\s*[:=]\s*.+$"
)
_DIAG_QUERY_CODE_RE = re.compile(
    r"(?i)([?&](?:accessCode|privateServerLinkCode|linkCode|shareCode|code)=)[^&#\s]+"
)


def _diag_redact_text(value):
    """Redact credentials/codes from free-form text before exporting it."""
    text = str(value or "")
    text = _DIAG_ROBLOX_COOKIE_RE.sub("[REDACTED ROBLOX COOKIE]", text)
    text = _DIAG_AUTH_RE.sub(lambda m: f"{m.group(1)}: [REDACTED]", text)
    text = _DIAG_QUERY_CODE_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


def _diag_key_is_secret(key):
    low = str(key or "").strip().lower().replace("-", "_")
    if low in _DIAG_PRIVATE_CODE_KEYS:
        return True
    return any(part in low for part in _DIAG_SECRET_KEY_PARTS)


def _diag_redact_obj(value, key_hint=""):
    """Recursively produce a JSON-safe, redacted copy."""
    if _diag_key_is_secret(key_hint):
        if value in (None, "", [], {}):
            return value
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(k): _diag_redact_obj(v, str(k))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_diag_redact_obj(v, key_hint) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        return _diag_redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _diag_redact_text(repr(value))


def _diag_json_text(value):
    return json.dumps(_diag_redact_obj(value), indent=2, ensure_ascii=False, sort_keys=True)


def _diag_local_command(cmd, timeout=12):
    """Run a Termux-local command without forcing it through su."""
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return {"code": int(p.returncode), "output": _diag_redact_text((p.stdout or "").strip())}
    except subprocess.TimeoutExpired:
        return {"code": 124, "output": f"timeout after {timeout}s"}
    except Exception as e:
        return {"code": -1, "output": _diag_redact_text(str(e))}


def _diag_android_command(cmd, cfg, timeout=12):
    code, out = shell_timeout(cmd, cfg, capture=True, timeout=timeout)
    return {"code": int(code), "output": _diag_redact_text(out)}


def _diag_file_info(path):
    p = Path(path) if path else None
    result = {"path": str(p) if p else "", "exists": bool(p and p.exists())}
    if not p or not p.exists():
        return result
    try:
        st = p.stat()
        result.update({
            "size_bytes": int(st.st_size),
            "modified_unix": int(st.st_mtime),
            "modified_local": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        result["stat_error"] = str(e)
    return result


def _diag_script_info():
    try:
        script_path = Path(__file__).resolve()
    except Exception:
        script_path = Path(__file__)
    info = _diag_file_info(script_path)
    try:
        h = hashlib.sha256()
        with open(script_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        info["sha256"] = h.hexdigest()
    except Exception as e:
        info["sha256_error"] = str(e)
    info["nomo_version"] = __version__
    return info


def _diag_package_record(entry, cfg):
    pkg = str(entry.get("package") or "")
    tab = entry.get("tab") or next(
        (t for t in cfg.get("tabs", []) if t.get("package") == pkg),
        {"package": pkg, "user_name": entry.get("username", pkg), "stat_file": ""},
    )
    try:
        pids = package_pids(pkg, cfg)
    except Exception as e:
        pids = []
        pid_error = str(e)
    else:
        pid_error = ""

    try:
        state_path = resolve_state_path(tab)
        state, state_err = read_state(tab)
    except Exception as e:
        state_path, state, state_err = None, None, str(e)

    autoexec = []
    try:
        autoexec = [
            {"label": label, **_diag_file_info(folder)}
            for label, folder in autoexec_dirs_for_tab(tab, "1")
        ]
    except Exception as e:
        autoexec = [{"error": str(e)}]

    resolve_cmd = f"cmd package resolve-activity --brief {shlex.quote(pkg)} 2>/dev/null | tail -n 1"
    resolved = _diag_android_command(resolve_cmd, cfg, timeout=8) if pkg else {"code": -1, "output": "no package"}

    state_summary = None
    if isinstance(state, dict):
        keep = (
            "username", "pet_count", "egg_total", "age", "ts", "write_seq",
            "script_uptime", "place_id", "job_id", "private_server_id",
            "private_server_owner_id", "private_server_known", "is_private_server",
            "server_type", "counter_version", "disconnected",
            "disconnect_observed_ts", "disconnect_reason", "disconnect_title",
            "disconnect_text", "disconnect_code",
        )
        state_summary = {k: state.get(k) for k in keep if k in state}

    return _diag_redact_obj({
        "package": pkg,
        "username": entry.get("username", tab.get("user_name", pkg)),
        "enabled": bool(entry.get("enabled", tab.get("enabled", False))),
        "configured": bool(entry.get("configured", bool(entry.get("tab")))),
        "installed": bool(entry.get("installed", False)),
        "alive": bool(pids),
        "pids": pids,
        "pid_error": pid_error,
        "resolved_activity": resolved,
        "configured_state_file": str(tab.get("stat_file") or tab.get("state_file") or ""),
        "resolved_state_file": _diag_file_info(state_path),
        "state_read_error": state_err,
        "state": state_summary,
        "autoexec_paths": autoexec,
    })


def _diag_activity_tail(max_lines=300):
    path = BASE_DIR / "activity.log"
    if not path.exists():
        return "activity.log does not exist\n"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        text = "".join(lines[-max(1, int(max_lines)):])
        # Keep the ZIP small if one line unexpectedly contains a huge payload.
        return _diag_redact_text(text[-500000:])
    except Exception as e:
        return f"Could not read activity.log: {_diag_redact_text(e)}\n"


def _diag_summary_text(report):
    lines = [
        f"NOMO REJOIN DIAGNOSTICS {report.get('nomo_version', '')}",
        f"Created: {report.get('created_local', '')}",
        f"Mode: {report.get('active_mode', '')}",
        f"Base directory: {report.get('base_dir', '')}",
        "",
        "BACKEND",
        f"  OK: {report.get('backend', {}).get('ok')}",
        f"  Status: {report.get('backend', {}).get('message', '')}",
        "",
        "PACKAGES",
    ]
    for item in report.get("packages", []):
        state = item.get("state") or {}
        age = state.get("age", "-")
        lines.append(
            f"  {item.get('package','?')}: installed={item.get('installed')} "
            f"enabled={item.get('enabled')} alive={item.get('alive')} "
            f"pids={item.get('pids', [])} state_age={age} "
            f"counter={state.get('counter_version', '-') or '-'}"
        )
        lines.append(f"    state: {item.get('resolved_state_file', {}).get('path', '')}")
        paths = item.get("autoexec_paths") or []
        if paths:
            for p in paths:
                lines.append(f"    autoexec: {p.get('path', p.get('error', ''))}")
        else:
            lines.append("    autoexec: unresolved")
    issues = report.get("issues", [])
    lines.extend(["", "DETECTED ISSUES"])
    if issues:
        lines.extend(f"  - {x}" for x in issues)
    else:
        lines.append("  None detected by the exporter.")
    lines.extend([
        "",
        "PRIVACY",
        "  Credentials, cookies, webhook URLs, authorization values, and private-server codes",
        "  are redacted. Review files before sharing outside your own troubleshooting chat.",
        "",
    ])
    return _diag_redact_text("\n".join(lines))


def build_diagnostics_report(cfg):
    """Collect a redacted diagnostics snapshot. This never modifies rejoin state."""
    hcfg = load_hatcher_config()
    rt_market = load_runtime()
    rt_hatcher = load_hatcher_runtime()

    try:
        backend_ok, backend_msg = backend_health_check(cfg)
    except Exception as e:
        backend_ok, backend_msg = False, f"health check exception: {e}"

    entries = package_registry_entries(cfg, include_discovered=True)
    packages = []
    for entry in entries:
        try:
            packages.append(_diag_package_record(entry, cfg))
        except Exception as e:
            packages.append(_diag_redact_obj({
                "package": entry.get("package", "?"),
                "collection_error": str(e),
            }))

    try:
        usage = shutil.disk_usage(BASE_DIR)
        storage = {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        }
    except Exception as e:
        storage = {"error": str(e)}

    system_info = {
        "python_version": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "uname": list(platform.uname()),
        "cwd": os.getcwd(),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "gid": os.getgid() if hasattr(os, "getgid") else None,
        "term": os.environ.get("TERM", ""),
        "prefix": os.environ.get("PREFIX", ""),
        "home": os.environ.get("HOME", ""),
        "storage": storage,
        "commands": {
            "id": _diag_local_command("id"),
            "python_version": _diag_local_command("python --version 2>&1"),
            "termux_info": _diag_local_command("command -v termux-info >/dev/null 2>&1 && termux-info || echo 'termux-info unavailable'", 20),
            "android_props": _diag_android_command(
                "printf 'model='; getprop ro.product.model; "
                "printf 'android='; getprop ro.build.version.release; "
                "printf 'sdk='; getprop ro.build.version.sdk; "
                "printf 'abi='; getprop ro.product.cpu.abi; "
                "printf 'manufacturer='; getprop ro.product.manufacturer",
                cfg,
                timeout=10,
            ),
            "su_id": _diag_android_command("id", cfg, timeout=8),
        },
    }

    issues = []
    if not backend_ok:
        issues.append(f"Backend health: {backend_msg}")
    enabled_pkgs = [x for x in packages if x.get("enabled")]
    if not enabled_pkgs:
        issues.append("No enabled packages are configured.")
    for item in enabled_pkgs:
        pkg = item.get("package", "?")
        if not item.get("installed"):
            issues.append(f"{pkg}: configured but Android package was not found.")
        if not item.get("alive"):
            issues.append(f"{pkg}: no exact package PID is running.")
        state = item.get("state") or {}
        state_file = item.get("resolved_state_file") or {}
        if not state_file.get("exists"):
            issues.append(f"{pkg}: state file is missing.")
        else:
            age = state.get("age")
            try:
                if int(age) > int(cfg.get("state_stale_seconds", 180)):
                    issues.append(f"{pkg}: state is stale ({format_age(age)} old).")
            except Exception:
                pass
        paths = item.get("autoexec_paths") or []
        if not paths:
            issues.append(f"{pkg}: AutoExec path could not be resolved.")
        else:
            for p in paths:
                if p.get("error"):
                    issues.append(f"{pkg}: AutoExec resolve error: {p.get('error')}")

    report = {
        "nomo_version": __version__,
        "created_unix": now(),
        "created_local": date_time_text(),
        "active_mode": active_rejoin_mode(cfg),
        "base_dir": str(BASE_DIR),
        "script": _diag_script_info(),
        "backend": {
            "provider": backend_provider(cfg),
            "enabled": bool(cfg.get("jsonbin_hatchers_enabled", False)),
            "ok": bool(backend_ok),
            "message": _diag_redact_text(backend_msg),
        },
        "system": system_info,
        "packages": packages,
        "issues": issues,
        "files": {
            "nomo_json": _diag_file_info(NOMO_FILE),
            "activity_log": _diag_file_info(BASE_DIR / "activity.log"),
            "bundled_counter": _diag_file_info(BUNDLED_PET_COUNTER_FILE),
        },
        "config_redacted": _diag_redact_obj(cfg),
        "hatcher_config_redacted": _diag_redact_obj(hcfg),
        "runtime_market_redacted": _diag_redact_obj(rt_market),
        "runtime_hatcher_redacted": _diag_redact_obj(rt_hatcher),
    }
    return _diag_redact_obj(report)


def export_diagnostics_zip(cfg):
    """Write a safe troubleshooting ZIP and show its full Android path."""
    clear()
    banner("EXPORT DIAGNOSTICS", cfg)
    print(col("Collecting package, state, backend, AutoExec, and device information...", CYAN))
    print(col("Cookies, API keys, secrets, webhooks, and private-server codes are removed.", DIM))
    try:
        report = build_diagnostics_report(cfg)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = BASE_DIR / "diagnostics"
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / f"NOMO_DIAGNOSTICS_{__version__}_{stamp}.zip"

        files = {
            "README.txt": (
                "NOMO Rejoin diagnostics export.\n"
                "All known credentials and private-server access/link codes were redacted.\n"
                "Review before sharing outside your own troubleshooting chat.\n"
            ),
            "summary.txt": _diag_summary_text(report),
            "report.json": json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
            "config_redacted.json": _diag_json_text(report.get("config_redacted", {})),
            "hatcher_config_redacted.json": _diag_json_text(report.get("hatcher_config_redacted", {})),
            "runtime_market_redacted.json": _diag_json_text(report.get("runtime_market_redacted", {})),
            "runtime_hatcher_redacted.json": _diag_json_text(report.get("runtime_hatcher_redacted", {})),
            "packages_state_paths.json": _diag_json_text(report.get("packages", [])),
            "activity_tail.log": _diag_activity_tail(300),
        }
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for name, text in files.items():
                zf.writestr(name, _diag_redact_text(text))

        size = zip_path.stat().st_size
        print("")
        print(col("Diagnostics ZIP created.", GREEN))
        print(col("Full path:", BOLD))
        print(str(zip_path))
        print(f"Size: {size / 1024:.1f} KB")
        print(f"Detected issues: {len(report.get('issues', []))}")
        log_activity(f"diagnostics exported -> {zip_path.name}", "", GREEN)
    except Exception as e:
        print("")
        print(col(f"Diagnostics export failed: {_diag_redact_text(e)}", RED))
    pause()

# ============================================================
# BUILT-IN UPDATE MANAGER
# ============================================================

_UPDATE_VERSION_RE = re.compile(
    r'(?m)^__version__\s*=\s*["\'](V\d+(?:\.\d+)+)["\']'
)


def nomo_update_source_url(cfg=None):
    value = ""
    if isinstance(cfg, dict):
        value = str(cfg.get("update_source_url", "") or "").strip()
    return value or NOMO_UPDATE_URL


def _nomo_version_from_text(text):
    match = _UPDATE_VERSION_RE.search(str(text or ""))
    return match.group(1) if match else ""


def _nomo_version_tuple(value):
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in nums) if nums else (0,)


def _nomo_fetch_remote_source(cfg):
    """Download the configured GitHub Raw source without modifying local files."""
    url = nomo_update_source_url(cfg)
    timeout = max(10, int(cfg.get("update_timeout_seconds", 45) or 45))
    separator = "&" if "?" in url else "?"
    request_url = f"{url}{separator}nomo_cache_bust={int(time.time())}"
    req = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": f"NOMO-Rejoin/{__version__}",
            "Accept": "text/plain, application/octet-stream;q=0.9, */*;q=0.1",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type", "") or "").lower()
            raw = response.read(NOMO_UPDATE_MAX_BYTES + 1)
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {getattr(e, 'reason', e)}") from e
    except Exception as e:
        raise RuntimeError(f"download failed: {e}") from e

    if status < 200 or status >= 300:
        raise RuntimeError(f"HTTP status {status}")
    if len(raw) > NOMO_UPDATE_MAX_BYTES:
        raise RuntimeError(f"remote file is larger than {NOMO_UPDATE_MAX_BYTES // 1024 // 1024} MB")
    if len(raw) < 10_000:
        raise RuntimeError(f"remote file is suspiciously small ({len(raw)} bytes)")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise RuntimeError("remote file is not valid UTF-8 Python source") from e

    beginning = text.lstrip()[:300].lower()
    if "text/html" in content_type or beginning.startswith("<!doctype html") or beginning.startswith("<html"):
        raise RuntimeError("remote server returned HTML instead of Python")
    if "# nomo rejoin" not in text[:3000].lower():
        raise RuntimeError("remote file is missing the NOMO REJOIN header")
    if "def main(" not in text or "if __name__" not in text:
        raise RuntimeError("remote file is missing required NOMO entry points")

    remote_version = _nomo_version_from_text(text)
    if not remote_version:
        raise RuntimeError("remote file has no readable __version__")
    return text, remote_version, url


def _nomo_compile_source_file(path):
    """Run Python's real py_compile module and remove its temporary pycache."""
    path = Path(path)
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )
    pycache = path.parent / "__pycache__"
    try:
        if pycache.exists():
            shutil.rmtree(pycache)
    except Exception:
        pass
    if result.returncode != 0:
        detail = (result.stdout or "py_compile failed").strip()
        raise RuntimeError(cut(detail, 500))
    return True


def _nomo_unique_backup_path(version, reason="backup"):
    NOMO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe_version = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(version or "unknown"))
    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(reason or "backup"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = NOMO_BACKUP_DIR / f"nomo_rejoin_{safe_version}_{safe_reason}_{stamp}.py"
    index = 2
    while candidate.exists():
        candidate = NOMO_BACKUP_DIR / f"nomo_rejoin_{safe_version}_{safe_reason}_{stamp}_{index}.py"
        index += 1
    return candidate


def _nomo_backup_current(reason="pre_update"):
    source = NOMO_APP_FILE if NOMO_APP_FILE.exists() else Path(__file__).resolve()
    if not source.exists():
        return None
    try:
        current_text = source.read_text(encoding="utf-8", errors="replace")
    except Exception:
        current_text = ""
    version = _nomo_version_from_text(current_text) or __version__
    backup = _nomo_unique_backup_path(version, reason)
    shutil.copy2(source, backup)
    return backup


def _nomo_write_update_atomically(source_text):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = BASE_DIR / ".nomo_rejoin.update.tmp.py"
    try:
        temp_path.write_text(source_text, encoding="utf-8")
        os.chmod(temp_path, 0o755)
        _nomo_compile_source_file(temp_path)
        os.replace(str(temp_path), str(NOMO_APP_FILE))
        os.chmod(NOMO_APP_FILE, 0o755)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass


def nomo_check_update(cfg, *, print_result=True):
    source, remote_version, url = _nomo_fetch_remote_source(cfg)
    local_version = __version__
    comparison = (
        1 if _nomo_version_tuple(remote_version) > _nomo_version_tuple(local_version)
        else -1 if _nomo_version_tuple(remote_version) < _nomo_version_tuple(local_version)
        else 0
    )
    if print_result:
        print(f"Local:  {local_version}")
        print(f"Remote: {remote_version}")
        print(f"Source: {url}")
        if comparison > 0:
            print(col("Update available.", GREEN))
        elif comparison == 0:
            print(col("Already on the latest version.", GREEN))
        else:
            print(col("Remote source is older than this local build.", YELLOW))
    return {
        "source": source,
        "local_version": local_version,
        "remote_version": remote_version,
        "url": url,
        "comparison": comparison,
    }


def nomo_install_latest_update(cfg, *, force=False, restart=False, interactive=True):
    """Install the GitHub Raw source after full validation and a timestamped backup."""
    print(col("Downloading and validating NOMO Rejoin...", CYAN))
    info = nomo_check_update(cfg, print_result=True)
    comparison = info["comparison"]
    remote_version = info["remote_version"]

    if comparison < 0 and not force:
        print(col("Blocked: the remote build is older. Use a backup rollback instead.", RED))
        return False
    if comparison == 0 and not force:
        if not interactive:
            print(col("Nothing installed; local and remote versions match.", YELLOW))
            return True
        if not _setup_yes_no("Reinstall the same version?", default=False):
            print(col("Update cancelled.", YELLOW))
            return False

    backup = _nomo_backup_current("pre_update")
    _nomo_write_update_atomically(info["source"])

    print(col(f"Installed NOMO Rejoin {remote_version}.", GREEN))
    print(f"Target: {NOMO_APP_FILE}")
    if backup:
        print(f"Backup: {backup}")
    log_activity(f"updated NOMO -> {remote_version}", "", GREEN)

    if restart:
        do_restart = True
        if interactive:
            do_restart = _setup_yes_no("Restart NOMO now?", default=True)
        if do_restart:
            print(col("Restarting NOMO...", CYAN))
            reset_terminal()
            os.execv(sys.executable, [sys.executable, str(NOMO_APP_FILE)])
    return True


def _nomo_list_backups():
    if not NOMO_BACKUP_DIR.exists():
        return []
    backups = [p for p in NOMO_BACKUP_DIR.glob("nomo_rejoin_*.py") if p.is_file()]
    return sorted(backups, key=lambda p: p.stat().st_mtime, reverse=True)


def _nomo_backup_description(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        version = _nomo_version_from_text(text) or "unknown"
        size = path.stat().st_size / 1024
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return version, size, when
    except Exception:
        return "unknown", 0.0, "unknown"


def nomo_print_backups(cfg):
    backups = _nomo_list_backups()
    if not backups:
        print(col("No NOMO Python backups found.", YELLOW))
        print(f"Folder: {NOMO_BACKUP_DIR}")
        return []
    rows = []
    for index, backup in enumerate(backups, 1):
        version, size, when = _nomo_backup_description(backup)
        rows.append([
            (str(index), CYAN),
            (version, WHITE),
            (when, WHITE),
            (f"{size:.1f} KB", WHITE, True),
            (backup.name, DIM),
        ])
    draw_table(["No", "Version", "Created", "Size", "File"], rows,
               [3, 8, 19, 9, 42], cfg)
    print(f"Folder: {NOMO_BACKUP_DIR}")
    return backups


def nomo_restore_backup(backup, cfg, *, restart=False, interactive=True):
    backup = Path(backup)
    if not backup.exists() or not backup.is_file():
        raise RuntimeError("selected backup does not exist")
    _nomo_compile_source_file(backup)
    text = backup.read_text(encoding="utf-8")
    version = _nomo_version_from_text(text)
    if not version or "# NOMO REJOIN" not in text[:3000]:
        raise RuntimeError("selected file is not a valid NOMO Rejoin backup")

    current_backup = _nomo_backup_current("pre_rollback")
    temp_path = BASE_DIR / ".nomo_rejoin.rollback.tmp.py"
    try:
        shutil.copy2(backup, temp_path)
        _nomo_compile_source_file(temp_path)
        os.replace(str(temp_path), str(NOMO_APP_FILE))
        os.chmod(NOMO_APP_FILE, 0o755)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

    print(col(f"Rolled back to {version}.", GREEN))
    print(f"Restored: {backup}")
    if current_backup:
        print(f"Current build preserved as: {current_backup}")
    log_activity(f"rolled back NOMO -> {version}", "", YELLOW)

    if restart:
        do_restart = True
        if interactive:
            do_restart = _setup_yes_no("Restart NOMO now?", default=True)
        if do_restart:
            print(col("Restarting NOMO...", CYAN))
            reset_terminal()
            os.execv(sys.executable, [sys.executable, str(NOMO_APP_FILE)])
    return True


def nomo_repair_launchers():
    """Install tiny local-only nomo/gag launchers; update logic stays in Python."""
    prefix = Path(os.environ.get("PREFIX") or "/data/data/com.termux/files/usr")
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    nomo_cmd = bin_dir / "nomo"
    gag_cmd = bin_dir / "gag"
    script = f'''#!/data/data/com.termux/files/usr/bin/bash
APP={shlex.quote(str(NOMO_APP_FILE))}
PYTHON={shlex.quote(str(prefix / "bin" / "python"))}
if [ ! -f "$APP" ]; then
  echo "NOMO Rejoin is missing: $APP"
  exit 1
fi
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python)"
fi
exec "$PYTHON" "$APP" "$@"
'''
    temp = bin_dir / ".nomo.launcher.tmp"
    temp.write_text(script, encoding="utf-8")
    os.chmod(temp, 0o755)
    os.replace(str(temp), str(nomo_cmd))
    try:
        if gag_cmd.exists() or gag_cmd.is_symlink():
            gag_cmd.unlink()
        gag_cmd.symlink_to(nomo_cmd)
    except Exception:
        shutil.copy2(nomo_cmd, gag_cmd)
        os.chmod(gag_cmd, 0o755)
    return nomo_cmd, gag_cmd


def nomo_auto_repair_launchers_once(cfg):
    """Replace the old shell updater once V4.06 is running from the canonical file."""
    if cfg.get("_v406_launcher_repaired_once"):
        return False
    try:
        if not NOMO_APP_FILE.exists():
            return False
        nomo_repair_launchers()
        cfg["_v406_launcher_repaired_once"] = True
        save_config(cfg)
        return True
    except Exception:
        return False


def nomo_update_manager(cfg):
    while True:
        cfg = load_config()
        clear()
        banner("NOMO UPDATE MANAGER", cfg)
        print(f"Local version: {__version__}")
        print(f"App file:      {NOMO_APP_FILE}")
        print("")
        rows = [
            ("1", "Check for update", CYAN, WHITE),
            ("2", "Install latest update", CYAN, WHITE),
            ("3", "Show local / remote version", CYAN, WHITE),
            ("4", "View update source", CYAN, WHITE),
            ("5", "List backups", CYAN, WHITE),
            ("6", "Roll back selected backup", CYAN, WHITE),
            ("7", "Repair nomo / gag commands", CYAN, WHITE),
            ("0", "Back", RED, WHITE),
        ]
        draw_boxed_menu(rows, cfg)
        choice = read_menu_choice("\nChoose: ", {"0", "1", "2", "3", "4", "5", "6", "7"})
        if choice == "0":
            return
        if choice is None:
            print(col("Invalid choice.", RED))
            time.sleep(1)
            continue

        clear()
        banner("NOMO UPDATE MANAGER", cfg)
        try:
            if choice in {"1", "3"}:
                nomo_check_update(cfg, print_result=True)
                pause()
            elif choice == "2":
                nomo_install_latest_update(cfg, restart=True, interactive=True)
                pause()
            elif choice == "4":
                print(col("GitHub Raw update source:", BOLD))
                print(nomo_update_source_url(cfg))
                print("")
                print(col("Local target:", BOLD))
                print(NOMO_APP_FILE)
                print("")
                print("Updates happen only when you choose Install or run `nomo update`.")
                pause()
            elif choice == "5":
                nomo_print_backups(cfg)
                pause()
            elif choice == "6":
                backups = nomo_print_backups(cfg)
                if not backups:
                    pause()
                    continue
                raw = clean_terminal_input(input("\nBackup number (0 cancels): "))
                if raw in {"", "0"}:
                    continue
                try:
                    index = int(raw) - 1
                except Exception:
                    index = -1
                if not (0 <= index < len(backups)):
                    print(col("Invalid backup number.", RED))
                    pause()
                    continue
                selected = backups[index]
                version, _, _ = _nomo_backup_description(selected)
                print(f"Selected: {selected.name} ({version})")
                if _setup_yes_no("Restore this backup?", default=False):
                    nomo_restore_backup(selected, cfg, restart=True, interactive=True)
                else:
                    print(col("Rollback cancelled.", YELLOW))
                pause()
            elif choice == "7":
                nomo_cmd, gag_cmd = nomo_repair_launchers()
                print(col("Launchers repaired.", GREEN))
                print(f"nomo: {nomo_cmd}")
                print(f"gag:  {gag_cmd}")
                print("")
                print("Supported commands:")
                print("  nomo")
                print("  nomo update")
                print("  nomo version")
                print("  nomo rollback")
                print("  nomo source")
                pause()
        except Exception as e:
            print(col(f"Update manager error: {e}", RED))
            pause()


def handle_nomo_cli_command():
    """Handle launcher subcommands. Return True when a command was consumed."""
    if len(sys.argv) < 2:
        return False
    command = clean_terminal_input(sys.argv[1]).lower()
    if command not in {"update", "install", "version", "rollback", "source", "repo", "repair"}:
        print("Usage: nomo [update|version|rollback|source|repair]")
        return True

    cfg = load_config()
    try:
        if command in {"update", "install"}:
            force = any(str(x).lower() in {"--force", "-f"} for x in sys.argv[2:])
            nomo_install_latest_update(cfg, force=force, restart=False, interactive=False)
        elif command == "version":
            nomo_check_update(cfg, print_result=True)
        elif command in {"source", "repo"}:
            print(nomo_update_source_url(cfg))
        elif command == "repair":
            nomo_cmd, gag_cmd = nomo_repair_launchers()
            print(f"Repaired: {nomo_cmd}")
            print(f"Alias:    {gag_cmd}")
        elif command == "rollback":
            backups = _nomo_list_backups()
            if not backups:
                print(f"No backups found in {NOMO_BACKUP_DIR}")
                return True
            nomo_restore_backup(backups[0], cfg, restart=False, interactive=False)
        return True
    except Exception as e:
        print(f"NOMO command failed: {e}")
        return True


def select_package_menu(cfg):
    while True:
        cfg = load_config()
        clear()
        banner("PACKAGE MANAGER", cfg)
        entries = package_registry_entries(cfg, include_discovered=True)
        rows = []
        for i, e in enumerate(entries, 1):
            rows.append([
                (str(i), CYAN),
                ("ON" if e.get("enabled") else ("NEW" if not e.get("configured") else "OFF"),
                 GREEN if e.get("enabled") else (YELLOW if not e.get("configured") else RED)),
                ("YES" if e.get("installed") else "NO", GREEN if e.get("installed") else YELLOW),
                (short_pkg(e.get("package", "")), WHITE),
                (e.get("username", ""), WHITE),
            ])
        if rows:
            draw_table(["No", "Use", "Inst", "Package", "Username"], rows,
                       [3, 4, 4, 12, 18], cfg)
        else:
            print(col("No packages configured or detected.", YELLOW))
        print("")
        print("1. Sync installed packages into NOMO")
        print("2. Set exact active package set")
        print("3. Toggle enable/disable package(s)")
        print("4. Add custom package name(s)")
        print("5. Remove package(s) from NOMO config")
        print("6. Refresh selected usernames (cookie -> state)")
        print("0. Back")
        drain_stdin()
        ch = read_menu_choice("\nChoose: ", {"0", "1", "2", "3", "4", "5", "6", "q", "b", "back"})
        if ch in {"0", "q", "b", "back"}:
            return
        if ch is None:
            print(col("Invalid choice. Use 0-6.", RED))
            time.sleep(1)
            continue
        if ch == "1":
            added = sync_installed_packages_into_config(cfg)
            if added:
                print(col("Added: " + ", ".join(added), GREEN))
            else:
                print(col("No new installed packages found.", YELLOW))
            pause()
        elif ch == "2":
            selected = choose_packages_common(cfg, "SET ACTIVE PACKAGES", multi=True,
                                              include_discovered=True, installed_only=False)
            if selected:
                # Add any selected discovered package before enabling it.
                configured = {t.get("package") for t in cfg.get("tabs", [])}
                for pkg in selected:
                    if pkg not in configured:
                        cfg.setdefault("tabs", []).append(_new_tab_for_package(pkg, len(cfg.get("tabs", []))))
                set_enabled_package_set(cfg, selected)
                print(col(f"Active package set saved ({len(selected)}).", GREEN))
                pause()
        elif ch == "3":
            selected = choose_packages_common(cfg, "TOGGLE PACKAGES", multi=True,
                                              include_discovered=False, configured_only=True)
            if selected:
                wanted = set(selected)
                for tab in cfg.get("tabs", []):
                    if tab.get("package") in wanted:
                        tab["enabled"] = not tab.get("enabled", True)
                save_config(cfg)
                hcfg = load_hatcher_config()
                sync_hatcher_profiles_with_tabs(cfg, hcfg)
                print(col("Package enable states updated.", GREEN))
                pause()
        elif ch == "4":
            tmpl = add_custom_package_names(cfg)
            if tmpl:
                existing = {t.get("package") for t in cfg.get("tabs", [])}
                added = []
                for pkg in tmpl.get("packages", []):
                    if pkg not in existing:
                        cfg.setdefault("tabs", []).append(_new_tab_for_package(pkg, len(cfg.get("tabs", []))))
                        existing.add(pkg)
                        added.append(pkg)
                save_config(cfg)
                hcfg = load_hatcher_config()
                sync_hatcher_profiles_with_tabs(cfg, hcfg)
                print(col("Added: " + (", ".join(added) if added else "none (already configured)"), GREEN if added else YELLOW))
                pause()
        elif ch == "5":
            selected = choose_packages_common(cfg, "REMOVE FROM NOMO CONFIG", multi=True,
                                              include_discovered=False, configured_only=True)
            if selected:
                print(col("This does not uninstall Android apps.", YELLOW))
                confirm = input(f"Remove {len(selected)} package(s) from NOMO config? [y/N]: ").strip().lower()
                if confirm == "y":
                    wanted = set(selected)
                    cfg["tabs"] = [t for t in cfg.get("tabs", []) if t.get("package") not in wanted]
                    save_config(cfg)
                    hcfg = load_hatcher_config()
                    sync_hatcher_profiles_with_tabs(cfg, hcfg)
                    print(col("Removed from NOMO config.", GREEN))
                    pause()
        elif ch == "6":
            selected = choose_packages_common(cfg, "REFRESH USERNAMES", multi=True,
                                              include_discovered=False, configured_only=True)
            if selected:
                refresh_usernames_for_packages(cfg, selected)
                pause()
        else:
            print(col("Invalid choice.", RED))
            time.sleep(1)

def main():
    cfg = load_config()
    nomo_auto_repair_launchers_once(cfg)

    while True:
        reset_terminal()
        drain_stdin()

        normalize_active_mode_flags(cfg)

        main_menu(cfg)
        choice = read_menu_choice("[?] Option: ", {str(x[0]) for x in MAIN_MENU_ITEMS})
        if choice is None:
            print(col("Invalid option. Use one of the numbers shown in the menu.", RED))
            time.sleep(1)
            continue

        if choice == "1":
            mode = active_rejoin_mode(cfg)
            try:
                if mode == "hatcher":
                    start_hatcher_safe_rejoiner(cfg)
                else:
                    start_rejoin(cfg)
            finally:
                reset_terminal()
                drain_stdin()
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "2":
            mode_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "3":
            select_package_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "4":
            get_all_usernames_via_api(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "5":
            set_global_private_server_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "6":
            try:
                force_restart_active_tabs_once(cfg)
            finally:
                reset_terminal()
                drain_stdin()
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "7":
            cookie_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "8":
            import_cookie_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "9":
            recovery_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "10":
            solver_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "11":
            config_settings(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "12":
            autoexec_menu(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "13":
            new_redfinger_setup_wizard(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "14":
            export_diagnostics_zip(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "15":
            nomo_update_manager(cfg)
            cfg = load_config()
            normalize_active_mode_flags(cfg)

        elif choice == "0":
            print("Bye.")
            return

        else:
            print(col("Invalid option. Use one of the numbers shown in the menu.", RED))
            time.sleep(1)


if __name__ == "__main__":
    try:
        if not handle_nomo_cli_command():
            main()
    except KeyboardInterrupt:
        request_stop()
        reset_terminal()
        print("\n[!] NOMO REJOIN stopped. Back to Termux.")
        sys.exit(130)
    finally:
        reset_terminal()