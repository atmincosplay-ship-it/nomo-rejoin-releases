#!/usr/bin/env python3
# NOMO REJOIN V5.0.2 — PHASE 1 SHADOW TRANSITION GUARD
#
# SINGLE-FILE BUILD
# - Reads the current V4 unified nomo.json configuration.
# - Reads exact Delta Global <username>_state.json files.
# - Detects Main Garden -> Trade World event transitions.
# - Pauses planned rejoin/backend behavior during transition hold.
# - Requires 300 continuous healthy seconds in Main Garden before release.
# - Shadow only: no PID stop, no Roblox launch/route, no Cloudflare write.
# - Does not modify V4 config or replace the current V4 rejoiner.
#
# Main Garden: 126884695634066
# Trade World: 129954712878723

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


VERSION = "5.0.3-phase1-shadow-clean-ui"

V4_BASE_DIR = Path("/storage/emulated/0/Download/nomo_rejoin")
V4_NOMO_FILE = V4_BASE_DIR / "nomo.json"

V5_BASE_DIR = Path("/storage/emulated/0/Download/nomo_rejoin_v5")
V5_SETTINGS_FILE = V5_BASE_DIR / "settings.json"
V5_RUNTIME_FILE = V5_BASE_DIR / "shadow_runtime.json"
V5_LOG_FILE = V5_BASE_DIR / "shadow.log"

DELTA_WORKSPACE = Path("/storage/emulated/0/Delta/Workspace")
DELTA_STATE_DIR = DELTA_WORKSPACE / "nomo_rejoiner"

MAIN_GARDEN_PLACE_ID = 126884695634066
TRADE_WORLD_PLACE_ID = 129954712878723

DEFAULT_SETTINGS = {
    "schemaVersion": 1,
    "controlMode": "shadow",
    "scanSeconds": 10,
    "stateFreshSeconds": 45,
    "stateStaleRecoverySeconds": 180,
    "placePolicy": "allowlist",
    "allowedPlaceIds": [
        MAIN_GARDEN_PLACE_ID,
        TRADE_WORLD_PLACE_ID,
    ],
    "transitionGuard": {
        "enabled": True,
        "entryPlaceIds": [TRADE_WORLD_PLACE_ID],
        "returnPlaceId": MAIN_GARDEN_PLACE_ID,
        "returnStableSeconds": 300,
        "pauseRejoin": True,
        "pauseBackend": True,
        "recoverDeadProcess": True,
        "recoverExplicitDisconnect": True,
    },
}


class TransitionPhase(str, Enum):
    NORMAL = "normal"
    TRANSITIONING = "transitioning"
    RETURNING = "returning"


class DecisionKind(str, Enum):
    HEALTHY = "healthy"
    WAIT_NO_STATE = "wait_no_state"
    RECOVER_DEAD = "recover_dead"
    RECOVER_DISCONNECT = "recover_disconnect"
    RECOVER_STALE = "recover_stale"
    ROUTE_EXPECTED = "route_expected"
    HOLD_TRANSITION = "hold_transition"
    BLOCKED_IDENTITY = "blocked_identity"


class BackendPlanKind(str, Enum):
    NONE = "none"
    MARK_TRANSITIONING = "mark_transitioning"
    PAUSE = "pause"
    RESUME_FORCE_REPORT = "resume_force_report"


@dataclass
class Account:
    package: str
    username: str
    mode: str
    state_file: Path
    server_link: str = ""
    enabled: bool = True
    expected_place_id: int | None = None
    identity_conflict: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StateSnapshot:
    path: Path
    exists: bool = False
    readable: bool = False
    username: str = ""
    timestamp: int = 0
    age_seconds: int | None = None
    write_seq: int = 0
    place_id: int | None = None
    job_id: str = ""
    disconnected: bool = False
    disconnect_reason: str = ""
    pet_count: int | None = None
    egg_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class ProcessSnapshot:
    package: str
    pids: list[int] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return bool(self.pids)


@dataclass
class TransitionResult:
    phase: TransitionPhase
    rejoin_paused: bool
    backend_paused: bool
    backend_plan: BackendPlanKind
    stable_seconds: int = 0
    note: str = ""
    runtime_changed: bool = False


@dataclass
class RejoinDecision:
    kind: DecisionKind
    would_act: bool
    reason: str


@dataclass
class AccountEvaluation:
    account: Account
    process: ProcessSnapshot
    state: StateSnapshot
    transition: TransitionResult
    decision: RejoinDecision


def deep_merge_defaults(value: Any, defaults: Any) -> Any:
    if isinstance(defaults, dict):
        base = dict(value) if isinstance(value, dict) else {}
        for key, default_value in defaults.items():
            base[key] = deep_merge_defaults(base.get(key), default_value)
        return base
    if isinstance(defaults, list):
        return value if isinstance(value, list) else list(defaults)
    return defaults if value is None else value


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def _clean_username(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {
        "none",
        "unknown",
        "hatcher",
        "booster",
    }:
        return ""
    return text


def _safe_state_name(username: str) -> str:
    output = []
    for char in username:
        if char.isalnum() or char in {"_", "-", "."}:
            output.append(char)
        else:
            output.append("_")
    return "".join(output).strip("._") or "unknown"


def _to_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except Exception:
        return None


def _process_name_matches_package(name: str, package: str) -> bool:
    name = str(name or "").strip()
    package = str(package or "").strip()
    return bool(
        name
        and package
        and (name == package or name.startswith(package + ":"))
    )


class V4ConfigAdapter:
    """Read-only adapter over V4's unified nomo.json."""

    def __init__(self, path: Path = V4_NOMO_FILE) -> None:
        self.path = path

    def load_raw(self) -> dict[str, Any]:
        data = read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def accounts(self) -> list[Account]:
        raw = self.load_raw()
        market = (
            raw.get("market")
            if isinstance(raw.get("market"), dict)
            else {}
        )
        hatcher = (
            raw.get("hatcher")
            if isinstance(raw.get("hatcher"), dict)
            else {}
        )
        booster = (
            raw.get("booster")
            if isinstance(raw.get("booster"), dict)
            else {}
        )

        active_mode = str(
            market.get("active_rejoin_mode")
            or (
                "rejoin_only"
                if market.get("local_rejoin_only")
                else "market"
            )
        ).strip().lower()
        if active_mode not in {
            "market",
            "hatcher",
            "booster",
            "rejoin_only",
        }:
            active_mode = "market"

        hatcher_by_package = {
            str(item.get("package") or ""): item
            for item in hatcher.get("hatchers", [])
            if isinstance(item, dict) and item.get("package")
        }
        booster_by_package = {
            str(item.get("package") or ""): item
            for item in booster.get("boosters", [])
            if isinstance(item, dict) and item.get("package")
        }

        accounts: list[Account] = []
        for tab in market.get("tabs", []):
            if not isinstance(tab, dict):
                continue

            package = str(tab.get("package") or "").strip()
            if not package:
                continue

            h_profile = hatcher_by_package.get(package, {})
            b_profile = booster_by_package.get(package, {})

            username = _clean_username(tab.get("user_name"))
            if not username:
                username = _clean_username(
                    h_profile.get("hatcher_name")
                )
            if not username:
                username = _clean_username(
                    b_profile.get("booster_name")
                )
            if not username:
                username = package

            configured_path = str(
                tab.get("stat_file") or ""
            ).strip()
            state_file = (
                Path(configured_path)
                if configured_path
                else (
                    DELTA_STATE_DIR
                    / f"{_safe_state_name(username)}_state.json"
                )
            )

            server_link = str(
                tab.get("server_link") or ""
            ).strip()
            if active_mode == "hatcher":
                server_link = str(
                    h_profile.get("server_link") or server_link
                ).strip()
            elif active_mode == "booster":
                server_link = str(
                    b_profile.get("server_link") or server_link
                ).strip()

            expected_place = (
                TRADE_WORLD_PLACE_ID
                if active_mode == "market"
                else MAIN_GARDEN_PLACE_ID
            )

            accounts.append(
                Account(
                    package=package,
                    username=username,
                    mode=active_mode,
                    state_file=state_file,
                    server_link=server_link,
                    enabled=bool(tab.get("enabled", True)),
                    expected_place_id=expected_place,
                    identity_conflict=bool(
                        tab.get(
                            "delta_mapping_conflict",
                            False,
                        )
                    ),
                    metadata={
                        "executor_storage": tab.get(
                            "executor_storage",
                            market.get(
                                "executor_storage_mode",
                                "",
                            ),
                        ),
                        "source": "v4:nomo.json",
                    },
                )
            )

        return accounts


class IdentitySystem:
    """One exact package -> username -> state-file owner."""

    def normalize(
        self,
        accounts: list[Account],
    ) -> list[Account]:
        username_groups: dict[str, list[Account]] = defaultdict(list)
        package_groups: dict[str, list[Account]] = defaultdict(list)

        for account in accounts:
            username_groups[account.username.lower()].append(account)
            package_groups[account.package].append(account)

        for group in username_groups.values():
            if len(group) > 1:
                for account in group:
                    account.identity_conflict = True

        for group in package_groups.values():
            if len(group) > 1:
                for account in group:
                    account.identity_conflict = True

        return accounts


class ProcessSystem:
    """Read-only exact-package PID discovery."""

    def _commands(self) -> list[list[str]]:
        ps_command = (
            "ps -A -o PID,NAME,ARGS 2>/dev/null "
            "|| ps -A 2>/dev/null"
        )
        commands: list[list[str]] = []
        if shutil.which("su"):
            commands.append(["su", "-c", ps_command])
        commands.append(["sh", "-c", ps_command])
        return commands

    def snapshot_many(
        self,
        packages: Iterable[str],
    ) -> dict[str, ProcessSnapshot]:
        wanted = [
            str(package).strip()
            for package in packages
            if str(package).strip()
        ]
        result = {
            package: ProcessSnapshot(package=package)
            for package in wanted
        }
        output = ""

        for command in self._commands():
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
            except Exception:
                continue
            if completed.stdout.strip():
                output = completed.stdout
                break

        if not output:
            return result

        for line in output.splitlines():
            stripped = line.strip()
            if (
                not stripped
                or stripped.lower().startswith("pid")
            ):
                continue

            parts = stripped.split(None, 2)
            if len(parts) < 2 or not parts[0].isdigit():
                continue

            pid = int(parts[0])
            candidates = [parts[1]]
            if len(parts) >= 3:
                candidates.extend(parts[2].split())

            for package in wanted:
                if any(
                    _process_name_matches_package(
                        candidate,
                        package,
                    )
                    for candidate in candidates
                ):
                    if pid not in result[package].pids:
                        result[package].pids.append(pid)

        for snapshot in result.values():
            snapshot.pids.sort()
        return result


class StateSystem:
    """Exact-file reader; never selects another account's freshest JSON."""

    def __init__(self, fresh_seconds: int) -> None:
        self.fresh_seconds = max(1, int(fresh_seconds))

    def read(
        self,
        account: Account,
        now_ts: int | None = None,
    ) -> StateSnapshot:
        now_ts = (
            int(time.time())
            if now_ts is None
            else int(now_ts)
        )
        path = Path(account.state_file)
        snapshot = StateSnapshot(path=path)

        if not path.exists():
            snapshot.error = "missing"
            return snapshot

        snapshot.exists = True
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            snapshot.error = f"read error: {exc}"
            return snapshot

        if not isinstance(raw, dict):
            snapshot.error = "state is not an object"
            return snapshot

        snapshot.readable = True
        snapshot.raw = raw
        snapshot.username = str(
            raw.get("username") or ""
        ).strip()
        snapshot.timestamp = _to_int(
            raw.get("ts")
        ) or 0
        snapshot.age_seconds = (
            max(0, now_ts - snapshot.timestamp)
            if snapshot.timestamp > 0
            else None
        )
        snapshot.write_seq = _to_int(
            raw.get("write_seq")
        ) or 0
        snapshot.place_id = _to_int(
            raw.get(
                "place_id",
                raw.get("placeId"),
            )
        )
        snapshot.job_id = str(
            raw.get(
                "job_id",
                raw.get("jobId", ""),
            )
            or ""
        )
        snapshot.disconnected = bool(
            raw.get(
                "disconnected",
                raw.get(
                    "is_disconnected",
                    raw.get(
                        "disconnect_detected",
                        raw.get("kicked", False),
                    ),
                ),
            )
        )
        snapshot.disconnect_reason = str(
            raw.get(
                "disconnect_reason",
                raw.get(
                    "disconnectReason",
                    raw.get("disconnect_text", ""),
                ),
            )
            or ""
        )
        snapshot.pet_count = _to_int(
            raw.get("pet_count")
        )
        snapshot.egg_count = _to_int(
            raw.get("egg_total")
        )

        if (
            snapshot.username
            and snapshot.username.lower()
            != account.username.lower()
        ):
            snapshot.error = (
                f"identity mismatch: expected "
                f"{account.username}, found "
                f"{snapshot.username}"
            )
            snapshot.readable = False

        return snapshot

    def is_fresh(
        self,
        snapshot: StateSnapshot,
    ) -> bool:
        return bool(
            snapshot.readable
            and snapshot.age_seconds is not None
            and snapshot.age_seconds <= self.fresh_seconds
        )


class TransitionSystem:
    """Shared event-hop guard used by every future mode."""

    def __init__(self, settings: dict[str, Any]) -> None:
        guard = settings.get("transitionGuard", {})
        self.enabled = bool(
            guard.get("enabled", True)
        )
        self.entry_place_ids = {
            int(value)
            for value in guard.get(
                "entryPlaceIds",
                [],
            )
            if str(value).strip()
        }
        self.return_place_id = int(
            guard.get("returnPlaceId", 0) or 0
        )
        self.return_stable_seconds = max(
            1,
            int(
                guard.get(
                    "returnStableSeconds",
                    300,
                )
                or 300
            ),
        )
        self.pause_rejoin = bool(
            guard.get("pauseRejoin", True)
        )
        self.pause_backend = bool(
            guard.get("pauseBackend", True)
        )

    def _record(
        self,
        runtime: dict[str, Any],
        package: str,
    ) -> dict[str, Any]:
        transitions = runtime.setdefault(
            "transitions",
            {},
        )
        return transitions.setdefault(
            package,
            {
                "phase": TransitionPhase.NORMAL.value,
                "enteredAt": 0,
                "returnStartedAt": 0,
                "lastPlaceId": None,
                "lastJobId": "",
                "transitionNoticePlanned": False,
                "resumeReportPlanned": False,
            },
        )

    def evaluate(
        self,
        account: Account,
        process: ProcessSnapshot,
        state: StateSnapshot,
        state_is_fresh: bool,
        runtime: dict[str, Any],
        now_ts: int | None = None,
    ) -> TransitionResult:
        now_ts = (
            int(time.time())
            if now_ts is None
            else int(now_ts)
        )
        record = self._record(
            runtime,
            account.package,
        )
        before = dict(record)

        try:
            phase = TransitionPhase(
                record.get(
                    "phase",
                    TransitionPhase.NORMAL.value,
                )
            )
        except ValueError:
            phase = TransitionPhase.NORMAL

        place_id = state.place_id
        backend_plan = BackendPlanKind.NONE
        stable_seconds = 0
        note = ""

        if not self.enabled:
            phase = TransitionPhase.NORMAL
            record.update(
                {
                    "phase": phase.value,
                    "returnStartedAt": 0,
                    "transitionNoticePlanned": False,
                    "resumeReportPlanned": False,
                }
            )
            return TransitionResult(
                phase=phase,
                rejoin_paused=False,
                backend_paused=False,
                backend_plan=BackendPlanKind.NONE,
                note="transition guard disabled",
                runtime_changed=record != before,
            )

        if phase == TransitionPhase.NORMAL:
            if (
                state_is_fresh
                and place_id in self.entry_place_ids
            ):
                phase = TransitionPhase.TRANSITIONING
                record["enteredAt"] = now_ts
                record["returnStartedAt"] = 0
                if not record.get(
                    "transitionNoticePlanned",
                    False,
                ):
                    backend_plan = (
                        BackendPlanKind.MARK_TRANSITIONING
                    )
                    record[
                        "transitionNoticePlanned"
                    ] = True
                record["resumeReportPlanned"] = False
                note = "entered event transition hold"

        elif phase == TransitionPhase.TRANSITIONING:
            if (
                state_is_fresh
                and place_id == self.return_place_id
                and not state.disconnected
                and process.alive
            ):
                phase = TransitionPhase.RETURNING
                record["returnStartedAt"] = now_ts
                note = (
                    f"returned to main; stable 0/"
                    f"{self.return_stable_seconds}s"
                )
            else:
                note = "event transition hold active"

        elif phase == TransitionPhase.RETURNING:
            healthy_return = bool(
                process.alive
                and state_is_fresh
                and not state.disconnected
                and place_id == self.return_place_id
            )

            if not healthy_return:
                phase = TransitionPhase.TRANSITIONING
                record["returnStartedAt"] = 0
                note = (
                    "return confirmation reset; "
                    "hold continues"
                )
            else:
                started = int(
                    record.get(
                        "returnStartedAt",
                        0,
                    )
                    or 0
                )
                if started <= 0:
                    started = now_ts
                    record["returnStartedAt"] = started

                stable_seconds = max(
                    0,
                    now_ts - started,
                )
                if (
                    stable_seconds
                    >= self.return_stable_seconds
                ):
                    phase = TransitionPhase.NORMAL
                    record["enteredAt"] = 0
                    record["returnStartedAt"] = 0
                    record[
                        "transitionNoticePlanned"
                    ] = False
                    if not record.get(
                        "resumeReportPlanned",
                        False,
                    ):
                        backend_plan = (
                            BackendPlanKind.RESUME_FORCE_REPORT
                        )
                        record[
                            "resumeReportPlanned"
                        ] = True
                    note = (
                        "return stable; transition "
                        "hold released"
                    )
                else:
                    note = (
                        f"returning stable "
                        f"{stable_seconds}/"
                        f"{self.return_stable_seconds}s"
                    )

        if (
            phase
            in {
                TransitionPhase.TRANSITIONING,
                TransitionPhase.RETURNING,
            }
            and backend_plan
            == BackendPlanKind.NONE
        ):
            backend_plan = BackendPlanKind.PAUSE

        record["phase"] = phase.value
        record["lastPlaceId"] = place_id
        record["lastJobId"] = state.job_id
        record["updatedAt"] = now_ts

        rejoin_paused = bool(
            self.pause_rejoin
            and phase
            in {
                TransitionPhase.TRANSITIONING,
                TransitionPhase.RETURNING,
            }
        )
        backend_paused = bool(
            self.pause_backend
            and phase
            in {
                TransitionPhase.TRANSITIONING,
                TransitionPhase.RETURNING,
            }
        )

        return TransitionResult(
            phase=phase,
            rejoin_paused=rejoin_paused,
            backend_paused=backend_paused,
            backend_plan=backend_plan,
            stable_seconds=stable_seconds,
            note=note or phase.value,
            runtime_changed=record != before,
        )


class RejoinSystem:
    """Shared decision engine. Shadow mode returns plans only."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.stale_seconds = max(
            1,
            int(
                settings.get(
                    "stateStaleRecoverySeconds",
                    180,
                )
                or 180
            ),
        )
        self.place_policy = str(
            settings.get(
                "placePolicy",
                "allowlist",
            )
            or "allowlist"
        ).lower()
        self.allowed_places = {
            int(value)
            for value in settings.get(
                "allowedPlaceIds",
                [],
            )
            if str(value).strip()
        }

    def classify(
        self,
        account: Account,
        process: ProcessSnapshot,
        state: StateSnapshot,
        state_is_fresh: bool,
        transition: TransitionResult,
    ) -> RejoinDecision:
        if account.identity_conflict:
            return RejoinDecision(
                kind=DecisionKind.BLOCKED_IDENTITY,
                would_act=False,
                reason=(
                    "duplicate package/username "
                    "identity mapping"
                ),
            )

        if not process.alive:
            return RejoinDecision(
                kind=DecisionKind.RECOVER_DEAD,
                would_act=True,
                reason=(
                    "exact package process is dead"
                    if not transition.rejoin_paused
                    else (
                        "dead process recovery allowed "
                        "during transition hold"
                    )
                ),
            )

        if state.disconnected:
            return RejoinDecision(
                kind=DecisionKind.RECOVER_DISCONNECT,
                would_act=True,
                reason=(
                    state.disconnect_reason
                    or "explicit disconnect reported"
                ),
            )

        if transition.rejoin_paused:
            return RejoinDecision(
                kind=DecisionKind.HOLD_TRANSITION,
                would_act=False,
                reason=transition.note,
            )

        if not state.exists or not state.readable:
            return RejoinDecision(
                kind=DecisionKind.WAIT_NO_STATE,
                would_act=False,
                reason=state.error or "state missing",
            )

        if (
            state.age_seconds is not None
            and state.age_seconds
            >= self.stale_seconds
        ):
            return RejoinDecision(
                kind=DecisionKind.RECOVER_STALE,
                would_act=True,
                reason=(
                    f"state stale "
                    f"{state.age_seconds}s/"
                    f"{self.stale_seconds}s"
                ),
            )

        if self.place_policy == "strict":
            if (
                account.expected_place_id is not None
                and state.place_id is not None
                and state.place_id
                != account.expected_place_id
            ):
                return RejoinDecision(
                    kind=DecisionKind.ROUTE_EXPECTED,
                    would_act=True,
                    reason=(
                        f"place {state.place_id} != "
                        f"expected "
                        f"{account.expected_place_id}"
                    ),
                )

        elif self.place_policy == "allowlist":
            if (
                state.place_id is not None
                and self.allowed_places
                and state.place_id
                not in self.allowed_places
            ):
                return RejoinDecision(
                    kind=DecisionKind.ROUTE_EXPECTED,
                    would_act=True,
                    reason=(
                        f"place {state.place_id} "
                        "is outside allowlist"
                    ),
                )

        return RejoinDecision(
            kind=DecisionKind.HEALTHY,
            would_act=False,
            reason=(
                "fresh state"
                if state_is_fresh
                else (
                    "state present within "
                    "recovery threshold"
                )
            ),
        )


class BackendSystem:
    """Shadow Cloudflare planner; performs no HTTP request."""

    def describe(
        self,
        evaluation: AccountEvaluation,
    ) -> str:
        plan = evaluation.transition.backend_plan

        if plan == BackendPlanKind.MARK_TRANSITIONING:
            return (
                "WOULD SEND ONCE: transitioning, "
                "selectable=false"
            )
        if plan == BackendPlanKind.PAUSE:
            return (
                "PAUSED: no heartbeat/update/removal"
            )
        if plan == BackendPlanKind.RESUME_FORCE_REPORT:
            return (
                "WOULD SEND ONCE: fresh forced "
                "resume report"
            )
        return "normal/no backend transition action"


class NomoV5ShadowEngine:
    def __init__(self) -> None:
        self.settings = self._load_settings()
        self.runtime = self._load_runtime()
        self.adapter = V4ConfigAdapter()
        self.identity = IdentitySystem()
        self.process = ProcessSystem()
        self.state = StateSystem(
            int(
                self.settings.get(
                    "stateFreshSeconds",
                    45,
                )
            )
        )
        self.transition = TransitionSystem(
            self.settings
        )
        self.rejoin = RejoinSystem(
            self.settings
        )
        self.backend = BackendSystem()

    def _load_settings(self) -> dict[str, Any]:
        raw = read_json(V5_SETTINGS_FILE, {})
        merged = deep_merge_defaults(
            raw,
            DEFAULT_SETTINGS,
        )
        merged["controlMode"] = "shadow"
        atomic_write_json(
            V5_SETTINGS_FILE,
            merged,
        )
        return merged

    def _load_runtime(self) -> dict[str, Any]:
        raw = read_json(V5_RUNTIME_FILE, {})
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("schemaVersion", 1)
        raw.setdefault("transitions", {})
        raw.setdefault("lastScanAt", 0)
        return raw

    def save_runtime(self) -> None:
        atomic_write_json(
            V5_RUNTIME_FILE,
            self.runtime,
        )

    def scan_once(
        self,
        now_ts: int | None = None,
        process_overrides: (
            dict[str, list[int]] | None
        ) = None,
    ) -> list[AccountEvaluation]:
        now_ts = (
            int(time.time())
            if now_ts is None
            else int(now_ts)
        )
        accounts = self.identity.normalize(
            self.adapter.accounts()
        )
        packages = [
            account.package
            for account in accounts
        ]

        if process_overrides is None:
            process_map = (
                self.process.snapshot_many(packages)
            )
        else:
            process_map = {
                package: ProcessSnapshot(
                    package=package,
                    pids=list(
                        process_overrides.get(
                            package,
                            [],
                        )
                    ),
                )
                for package in packages
            }

        evaluations: list[AccountEvaluation] = []
        for account in accounts:
            process = process_map[account.package]
            state = self.state.read(
                account,
                now_ts=now_ts,
            )
            state_is_fresh = (
                self.state.is_fresh(state)
            )
            transition = self.transition.evaluate(
                account=account,
                process=process,
                state=state,
                state_is_fresh=state_is_fresh,
                runtime=self.runtime,
                now_ts=now_ts,
            )
            decision = self.rejoin.classify(
                account=account,
                process=process,
                state=state,
                state_is_fresh=state_is_fresh,
                transition=transition,
            )

            evaluation = AccountEvaluation(
                account=account,
                process=process,
                state=state,
                transition=transition,
                decision=decision,
            )
            evaluations.append(evaluation)

            append_log(
                V5_LOG_FILE,
                (
                    f"{account.package} "
                    f"user={account.username} "
                    f"place={state.place_id} "
                    f"job={state.job_id or '-'} "
                    f"phase={transition.phase.value} "
                    f"decision={decision.kind.value} "
                    f"would_act={decision.would_act} "
                    f"backend="
                    f"{transition.backend_plan.value}"
                ),
            )

        self.runtime["lastScanAt"] = now_ts
        self.save_runtime()
        return evaluations



def _trim(value: object, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _package_label(package: str) -> str:
    package = str(package or "")
    if package.startswith("premium.noka"):
        suffix = package[len("premium.noka"):]
        return suffix or "N"
    if package == "free.noka":
        return "F"
    if package == "com.roblox":
        return "R"
    return _trim(package, 3)


def _place_label(place_id: int | None) -> str:
    if place_id == MAIN_GARDEN_PLACE_ID:
        return "MAIN"
    if place_id == TRADE_WORLD_PLACE_ID:
        return "TRADE"
    if place_id is None:
        return "-"
    return "OTHER"


def _phase_label(
    evaluation: AccountEvaluation,
    return_target: int,
) -> str:
    transition = evaluation.transition
    if transition.phase == TransitionPhase.TRANSITIONING:
        return "HOLD"
    if transition.phase == TransitionPhase.RETURNING:
        return (
            f"BACK {transition.stable_seconds}/"
            f"{return_target}s"
        )
    return "NORMAL"


def _decision_label(
    evaluation: AccountEvaluation,
) -> str:
    decision = evaluation.decision
    state = evaluation.state

    if decision.kind == DecisionKind.HEALTHY:
        return "OK"
    if decision.kind == DecisionKind.HOLD_TRANSITION:
        return "PAUSED"
    if decision.kind == DecisionKind.RECOVER_DEAD:
        return "DEAD"
    if decision.kind == DecisionKind.RECOVER_DISCONNECT:
        return "DISCONNECT"
    if decision.kind == DecisionKind.RECOVER_STALE:
        age = (
            state.age_seconds
            if state.age_seconds is not None
            else 0
        )
        return f"STALE {age}s"
    if decision.kind == DecisionKind.WAIT_NO_STATE:
        return "NO STATE"
    if decision.kind == DecisionKind.ROUTE_EXPECTED:
        return "WRONG PLACE"
    if decision.kind == DecisionKind.BLOCKED_IDENTITY:
        return "CONFLICT"
    return decision.kind.value.upper()


def _backend_label(
    evaluation: AccountEvaluation,
) -> str:
    plan = evaluation.transition.backend_plan
    if plan == BackendPlanKind.MARK_TRANSITIONING:
        return "PAUSE ONCE"
    if plan == BackendPlanKind.PAUSE:
        return "PAUSED"
    if plan == BackendPlanKind.RESUME_FORCE_REPORT:
        return "RESUME ONCE"
    return "NORMAL"


def _terminal_width() -> int:
    try:
        return max(58, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


def clear_screen() -> None:
    print("\033[2J\033[H", end="")


def print_banner(compact: bool = True) -> None:
    width = min(_terminal_width(), 96)
    print("=" * width)
    print(f"NOMO V5 SHADOW  {VERSION}")
    print("Read-only: no restart, route, or Cloudflare write")
    print("=" * width)


def print_evaluations(
    engine: NomoV5ShadowEngine,
    evaluations: list[AccountEvaluation],
) -> None:
    width = _terminal_width()
    compact = width < 92
    print_banner(compact=compact)

    return_target = int(
        engine.settings["transitionGuard"]["returnStableSeconds"]
    )

    if not evaluations:
        print("No configured accounts found.")
        return

    if compact:
        print(
            f"{'ID':<3} {'USER':<13} {'PID':<4} "
            f"{'PLACE':<6} {'PHASE':<15} {'STATUS':<14}"
        )
        print("-" * min(width, 72))
        for item in evaluations:
            package = _package_label(item.account.package)
            username = _trim(item.account.username, 13)
            alive = "ON" if item.process.alive else "OFF"
            place = _place_label(item.state.place_id)
            phase = _trim(
                _phase_label(item, return_target),
                15,
            )
            status = _trim(
                _decision_label(item),
                14,
            )
            print(
                f"{package:<3} {username:<13} {alive:<4} "
                f"{place:<6} {phase:<15} {status:<14}"
            )
    else:
        print(
            f"{'ID':<3} {'PACKAGE':<17} {'USER':<16} "
            f"{'PID':<4} {'PLACE':<6} {'PHASE':<16} "
            f"{'STATUS':<15} {'CLOUDFLARE':<12}"
        )
        print("-" * min(width, 96))
        for item in evaluations:
            print(
                f"{_package_label(item.account.package):<3} "
                f"{_trim(item.account.package, 17):<17} "
                f"{_trim(item.account.username, 16):<16} "
                f"{('ON' if item.process.alive else 'OFF'):<4} "
                f"{_place_label(item.state.place_id):<6} "
                f"{_trim(_phase_label(item, return_target), 16):<16} "
                f"{_trim(_decision_label(item), 15):<15} "
                f"{_trim(_backend_label(item), 12):<12}"
            )

    hold_count = sum(
        1
        for item in evaluations
        if item.transition.rejoin_paused
    )
    would_recover = sum(
        1
        for item in evaluations
        if item.decision.would_act
    )
    healthy = sum(
        1
        for item in evaluations
        if item.decision.kind == DecisionKind.HEALTHY
    )

    print("")
    print(
        f"Healthy {healthy} | Hold {hold_count} | "
        f"Would recover {would_recover} | "
        f"Scan {engine.settings.get('scanSeconds', 10)}s"
    )
    print("Shadow only — no action is performed.")

def run_once() -> int:
    engine = NomoV5ShadowEngine()
    evaluations = engine.scan_once()
    print_evaluations(
        engine,
        evaluations,
    )
    return 0


def run_watch() -> int:
    engine = NomoV5ShadowEngine()
    interval = max(
        2,
        int(
            engine.settings.get(
                "scanSeconds",
                10,
            )
        ),
    )

    try:
        while True:
            clear_screen()
            evaluations = engine.scan_once()
            print_evaluations(
                engine,
                evaluations,
            )
            print(
                f"Next scan {interval}s | Ctrl+C stops dashboard"
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nShadow watcher stopped.")
        return 0


def show_mapping() -> int:
    engine = NomoV5ShadowEngine()
    accounts = engine.identity.normalize(
        engine.adapter.accounts()
    )
    print_banner()

    if not accounts:
        print("No accounts found in V4 nomo.json.")
        return 0

    for account in accounts:
        status = (
            "CONFLICT"
            if account.identity_conflict
            else "LOCKED"
        )
        print(
            f"{account.package:<20} -> "
            f"{account.username:<20} "
            f"[{status}]"
        )
        print(f"  {account.state_file}")
    return 0


def show_settings() -> int:
    engine = NomoV5ShadowEngine()
    print(
        json.dumps(
            engine.settings,
            indent=2,
        )
    )
    return 0


def reset_shadow_runtime() -> int:
    atomic_write_json(
        V5_RUNTIME_FILE,
        {
            "schemaVersion": 1,
            "transitions": {},
            "lastScanAt": 0,
        },
    )
    print(f"Reset: {V5_RUNTIME_FILE}")
    return 0


def self_test() -> int:
    temp_root = Path(
        tempfile.mkdtemp(
            prefix="nomo_v5_selftest_"
        )
    )
    try:
        state_path = (
            temp_root / "tester_state.json"
        )
        account = Account(
            package="premium.nokaA",
            username="tester",
            mode="hatcher",
            state_file=state_path,
            expected_place_id=(
                MAIN_GARDEN_PLACE_ID
            ),
        )
        process = ProcessSnapshot(
            package=account.package,
            pids=[1234],
        )
        runtime = {"transitions": {}}
        state_system = StateSystem(45)
        transition_system = TransitionSystem(
            DEFAULT_SETTINGS
        )

        def write_state(
            ts: int,
            place_id: int,
            job_id: str,
        ) -> StateSnapshot:
            state_path.write_text(
                json.dumps(
                    {
                        "username": "tester",
                        "ts": ts,
                        "write_seq": ts,
                        "place_id": place_id,
                        "job_id": job_id,
                        "pet_count": 10,
                        "egg_total": 2,
                    }
                ),
                encoding="utf-8",
            )
            return state_system.read(
                account,
                now_ts=ts,
            )

        state = write_state(
            1000,
            MAIN_GARDEN_PLACE_ID,
            "main-a",
        )
        result = transition_system.evaluate(
            account,
            process,
            state,
            True,
            runtime,
            now_ts=1000,
        )
        assert (
            result.phase
            == TransitionPhase.NORMAL
        )

        state = write_state(
            1010,
            TRADE_WORLD_PLACE_ID,
            "trade-a",
        )
        result = transition_system.evaluate(
            account,
            process,
            state,
            True,
            runtime,
            now_ts=1010,
        )
        assert (
            result.phase
            == TransitionPhase.TRANSITIONING
        )
        assert (
            result.backend_plan
            == BackendPlanKind.MARK_TRANSITIONING
        )

        state = write_state(
            1020,
            TRADE_WORLD_PLACE_ID,
            "trade-b",
        )
        result = transition_system.evaluate(
            account,
            process,
            state,
            True,
            runtime,
            now_ts=1020,
        )
        assert (
            result.backend_plan
            == BackendPlanKind.PAUSE
        )

        state = write_state(
            1030,
            MAIN_GARDEN_PLACE_ID,
            "main-b",
        )
        result = transition_system.evaluate(
            account,
            process,
            state,
            True,
            runtime,
            now_ts=1030,
        )
        assert (
            result.phase
            == TransitionPhase.RETURNING
        )

        state = write_state(
            1330,
            MAIN_GARDEN_PLACE_ID,
            "main-b",
        )
        result = transition_system.evaluate(
            account,
            process,
            state,
            True,
            runtime,
            now_ts=1330,
        )
        assert (
            result.phase
            == TransitionPhase.NORMAL
        )
        assert (
            result.backend_plan
            == BackendPlanKind.RESUME_FORCE_REPORT
        )

        print(
            "Self-test passed: transition hold "
            "and 300-second return release."
        )
        return 0
    finally:
        shutil.rmtree(
            temp_root,
            ignore_errors=True,
        )



def menu() -> int:
    while True:
        clear_screen()
        print_banner()
        print("1. One clean scan")
        print("2. Live clean dashboard")
        print("3. Show identity mapping")
        print("4. Show settings")
        print("5. Reset shadow runtime")
        print("6. Self-test")
        print("0. Exit")

        choice = input("\nChoose: ").strip()

        if choice == "1":
            clear_screen()
            run_once()
            time.sleep(3)
        elif choice == "2":
            return run_watch()
        elif choice == "3":
            clear_screen()
            show_mapping()
            time.sleep(4)
        elif choice == "4":
            clear_screen()
            show_settings()
            time.sleep(4)
        elif choice == "5":
            clear_screen()
            reset_shadow_runtime()
            time.sleep(2)
        elif choice == "6":
            clear_screen()
            self_test()
            time.sleep(2)
        elif choice == "0":
            return 0

def main(
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "NOMO V5 Phase 1 shadow "
            "transition guard"
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
    )
    parser.add_argument(
        "--mapping",
        action="store_true",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
    )
    parser.add_argument(
        "--reset-runtime",
        action="store_true",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
    )
    parser.add_argument(
        "--version",
        action="store_true",
    )
    args = parser.parse_args(argv)

    if args.once:
        return run_once()
    if args.watch:
        return run_watch()
    if args.mapping:
        return show_mapping()
    if args.settings:
        return show_settings()
    if args.reset_runtime:
        return reset_shadow_runtime()
    if args.self_test:
        return self_test()
    if args.version:
        print(VERSION)
        return 0
    return menu()


if __name__ == "__main__":
    raise SystemExit(main())
