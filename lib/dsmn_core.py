"""Lease rules for dsmn on Omarchy.

A lease is a deadline. Agent hooks and the panel renew it. Sleep is blocked
only while the deadline is in the future and no safety rule says otherwise.
The inhibitor is a process; when that process exits, the machine can sleep.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_MINUTES = 8 * 60
BATTERY_FLOOR = 20
NO_ROUTE_GRACE_MS = 15 * 60 * 1000
ACTIVITY_WINDOW_MS = 6 * 60 * 60 * 1000
ACTIVITY_BUCKETS = 12
RECENT_LIMIT = 5
TRIGGER_LIMIT = 400
HOLD_POLL_SECONDS = 5

PLUGIN_ID = "nomdelasociete.dsmn"
LID_DROPIN = Path("/etc/systemd/logind.conf.d/nomdelasociete-dsmn-lid.conf")
LID_DROPIN_BODY = """# Written by dsmn. Lid close honors sleep inhibitors, so a dsmn lease
# can keep a closed laptop awake. Remove this file to restore logind's default.
[Login]
LidSwitchIgnoreInhibited=no
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def state_dir() -> Path:
    override = os.environ.get("DSMN_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "dsmn"


def state_path() -> Path:
    return state_dir() / "state.json"


def pid_path() -> Path:
    return state_dir() / "hold.pid"


def hook_log_path() -> Path:
    override = os.environ.get("DSMN_HOOK_LOG")
    return Path(override) if override else state_dir() / "hook.log"


def empty_state(now: int | None = None) -> dict:
    moment = now_ms() if now is None else now
    return {
        "manualUntil": None,
        "lastRequester": None,
        "recentRequesters": [],
        "triggers": [],
        "noRouteSince": None,
        "lastStopReason": None,
        "lastActivationReason": None,
        "updatedAt": moment,
    }


def _normalize(raw: dict | None, now: int) -> dict:
    state = empty_state(now)
    if not isinstance(raw, dict):
        return state
    until = raw.get("manualUntil")
    state["manualUntil"] = int(until) if isinstance(until, (int, float)) else None
    requester = raw.get("lastRequester")
    state["lastRequester"] = requester if isinstance(requester, dict) else None
    recent = raw.get("recentRequesters")
    state["recentRequesters"] = recent if isinstance(recent, list) else []
    triggers = raw.get("triggers")
    state["triggers"] = triggers if isinstance(triggers, list) else []
    since = raw.get("noRouteSince")
    state["noRouteSince"] = int(since) if isinstance(since, (int, float)) else None
    stop = raw.get("lastStopReason")
    state["lastStopReason"] = stop if isinstance(stop, str) else None
    reason = raw.get("lastActivationReason")
    state["lastActivationReason"] = reason if isinstance(reason, str) else None
    updated = raw.get("updatedAt")
    state["updatedAt"] = int(updated) if isinstance(updated, (int, float)) else now
    return state


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or state_path()

    def load(self, now: int | None = None) -> dict:
        moment = now_ms() if now is None else now
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty_state(moment)
        return _normalize(raw, moment)

    def update(self, mutator, now: int | None = None) -> dict:
        moment = now_ms() if now is None else now
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        lock_path = self.path.with_suffix(".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self.load(moment)
            updated = mutator(state)
            if updated is None:
                updated = state
            payload = json.dumps(updated, indent=2, sort_keys=True) + "\n"
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            return updated


def lease_active(state: dict, now: int) -> bool:
    until = state.get("manualUntil")
    return isinstance(until, int) and until > now


def note_route(state: dict, route: bool | None, now: int) -> dict:
    """Unknown route data keeps a recorded outage and does not start one."""
    if not lease_active(state, now) or route is True:
        state["noRouteSince"] = None
    elif route is False and state.get("noRouteSince") is None:
        state["noRouteSince"] = now
    return state


def evaluate(state: dict, snapshot: dict, now: int) -> dict:
    blocks: list[str] = []
    active = lease_active(state, now)
    percent = snapshot.get("batteryPercent")
    if (
        active
        and snapshot.get("power") == "battery"
        and isinstance(percent, (int, float))
        and percent <= BATTERY_FLOOR
    ):
        blocks.append("battery_below_threshold")

    since = state.get("noRouteSince")
    if (
        active
        and snapshot.get("route") is False
        and isinstance(since, int)
        and now - since >= NO_ROUTE_GRACE_MS
    ):
        blocks.append("network_unavailable")

    prevent = active and not blocks
    if blocks:
        stop_reason = blocks[0]
    elif state.get("manualUntil") is not None and not active:
        stop_reason = "manual_expired"
    elif not active:
        stop_reason = state.get("lastStopReason") or "not_armed"
    else:
        stop_reason = None
    return {
        "active": active,
        "prevent": prevent,
        "safetyBlocks": blocks,
        "stopReason": stop_reason,
    }


def _record_requester(state: dict, requester: dict | None, now: int) -> None:
    if not requester or not requester.get("name"):
        return
    entry = {
        "name": str(requester.get("name") or ""),
        "context": str(requester.get("context") or ""),
        "event": str(requester.get("event") or ""),
        "at": now,
    }
    state["lastRequester"] = entry
    recent = [entry]
    for item in state.get("recentRequesters") or []:
        if not isinstance(item, dict):
            continue
        if item.get("name") == entry["name"]:
            continue
        recent.append(item)
        if len(recent) >= RECENT_LIMIT:
            break
    state["recentRequesters"] = recent
    triggers = [item for item in (state.get("triggers") or []) if isinstance(item, dict)]
    triggers.append({"at": now, "name": entry["name"]})
    cutoff = now - (8 * 60 * 60 * 1000)
    triggers = [item for item in triggers if isinstance(item.get("at"), int) and item["at"] >= cutoff]
    state["triggers"] = triggers[-TRIGGER_LIMIT:]


def apply_manual(
    state: dict,
    minutes: int,
    now: int,
    extend_only: bool = False,
    requester: dict | None = None,
) -> dict:
    if minutes < 1 or minutes > MAX_MINUTES:
        raise ValueError(f"minutes must be from 1 to {MAX_MINUTES}")
    requested = now + minutes * 60 * 1000
    until = state.get("manualUntil")
    if extend_only and isinstance(until, int) and until > requested:
        _record_requester(state, requester, now)
        state["updatedAt"] = now
        return state
    if not lease_active(state, now):
        state["noRouteSince"] = None
    state["manualUntil"] = requested
    state["lastActivationReason"] = "manual_lease" if extend_only else "manual_timer"
    state["lastStopReason"] = None
    if requester and requester.get("name"):
        _record_requester(state, requester, now)
    else:
        state["lastRequester"] = None
    state["updatedAt"] = now
    return state


def apply_stop(state: dict, now: int, reason: str = "user_stop") -> dict:
    state["manualUntil"] = None
    state["noRouteSince"] = None
    state["lastStopReason"] = reason
    state["updatedAt"] = now
    return state


def panel_countdown(remaining_seconds: int) -> str:
    seconds = max(0, int(remaining_seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def compact_countdown(remaining_seconds: int) -> str:
    seconds = max(0, int(remaining_seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    leftover = minutes % 60
    return f"{hours}h" if leftover == 0 else f"{hours}h{leftover}m"


def activity_buckets(state: dict, now: int) -> list[int]:
    buckets = [0] * ACTIVITY_BUCKETS
    width = ACTIVITY_WINDOW_MS // ACTIVITY_BUCKETS
    start = now - ACTIVITY_WINDOW_MS
    for item in state.get("triggers") or []:
        if not isinstance(item, dict):
            continue
        at = item.get("at")
        if not isinstance(at, int) or at < start or at > now:
            continue
        index = min(ACTIVITY_BUCKETS - 1, (at - start) // width)
        buckets[index] += 1
    return buckets


def requester_line(state: dict) -> str:
    requester = state.get("lastRequester") or {}
    if not isinstance(requester, dict) or not requester.get("name"):
        return ""
    context = str(requester.get("context") or "").strip()
    if context:
        return f"{requester['name']} · {context}"
    return str(requester["name"])


def probe_power() -> str:
    batteries = sorted(Path("/sys/class/power_supply").glob("BAT*"))
    if not batteries:
        return "ac"
    discharging = False
    for battery in batteries:
        try:
            status = (battery / "status").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if status == "discharging":
            discharging = True
    return "battery" if discharging else "ac"


def probe_battery_percent() -> int | None:
    values = []
    for battery in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        for name in ("capacity",):
            try:
                values.append(int((battery / name).read_text(encoding="utf-8").strip()))
                break
            except (OSError, ValueError):
                continue
    if not values:
        return None
    return max(0, min(100, min(values)))


def probe_route() -> bool | None:
    try:
        result = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def probe_snapshot() -> dict:
    return {
        "power": probe_power(),
        "batteryPercent": probe_battery_percent(),
        "route": probe_route(),
    }


def lid_present() -> bool:
    return any(Path("/proc/acpi/button/lid").glob("*/state"))


def lid_dropin_installed() -> bool:
    return LID_DROPIN.is_file()


def list_inhibitors() -> list[dict]:
    try:
        result = subprocess.run(
            [
                "busctl",
                "--json=short",
                "call",
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "ListInhibitors",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    rows = payload.get("data") or []
    if rows and isinstance(rows[0], list):
        rows = rows[0]
    inhibitors = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        inhibitors.append(
            {
                "what": str(row[0]),
                "who": str(row[1]),
                "why": str(row[2]),
                "mode": str(row[3]),
                "uid": int(row[4]),
                "pid": int(row[5]),
            }
        )
    return inhibitors


def dsmn_inhibitors(inhibitors: list[dict] | None = None) -> list[dict]:
    rows = inhibitors if inhibitors is not None else list_inhibitors()
    return [row for row in rows if row.get("who") == "dsmn"]


def process_start_ticks(stat_text: str) -> int | None:
    """starttime from /proc/pid/stat. comm may contain spaces and parentheses."""
    end = stat_text.rfind(")")
    if end < 0:
        return None
    fields = stat_text[end + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def read_process_start_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return process_start_ticks(text)


def hold_command(argv: list[str]) -> bool:
    """The saved holder is the `dsmn hold` process, not whichever pid was reused."""
    if len(argv) < 2 or argv[-1] != "hold":
        return False
    return any(Path(part).name == "dsmn" for part in argv[:-1])


def read_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _hold_record() -> tuple[int, int] | None:
    try:
        parts = pid_path().read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(parts) != 2:
        return None
    try:
        pid, started = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if pid <= 0:
        return None
    return pid, started


def hold_pid() -> int | None:
    record = _hold_record()
    if record is None:
        return None
    pid, started = record
    if read_process_start_ticks(pid) != started:
        return None
    argv = read_cmdline(pid)
    if argv is None or not hold_command(argv):
        return None
    return pid


def inhibitor_held() -> bool:
    if hold_pid() is not None:
        return True
    return any(row.get("mode") == "block" for row in dsmn_inhibitors())


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _bin() -> Path:
    return _plugin_root() / "bin" / "dsmn"


def ensure_hold(state: dict, decision: dict) -> None:
    if not decision.get("prevent"):
        return
    if hold_pid() is not None:
        return
    subprocess.Popen(
        [sys.executable, str(_bin()), "watch"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(_plugin_root()),
    )


def release_hold() -> None:
    pid = hold_pid()
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def kill_dsmn_inhibitors() -> None:
    """Signal only the verified holder. A logind who of dsmn is just a label."""
    release_hold()


def tick(store: Store | None = None, now: int | None = None, snapshot: dict | None = None) -> dict:
    keeper = store or Store()
    moment = now_ms() if now is None else now
    facts = snapshot if snapshot is not None else probe_snapshot()

    def mutate(state: dict) -> dict:
        note_route(state, facts.get("route"), moment)
        state["updatedAt"] = moment
        return state

    state = keeper.update(mutate, moment)
    decision = evaluate(state, facts, moment)
    return {"state": state, "snapshot": facts, "decision": decision}


def status_payload(bundle: dict | None = None, now: int | None = None) -> dict:
    moment = now_ms() if now is None else now
    if bundle is None:
        state = Store().load(moment)
        facts = probe_snapshot()
        note_route(state, facts.get("route"), moment)
        decision = evaluate(state, facts, moment)
        bundle = {"state": state, "snapshot": facts, "decision": decision}
    state = bundle["state"]
    facts = bundle["snapshot"]
    decision = bundle["decision"]
    until = state.get("manualUntil")
    remaining = 0
    if isinstance(until, int) and until > moment:
        remaining = max(0, (until - moment) // 1000)
    inhibitors = list_inhibitors()
    ours = dsmn_inhibitors(inhibitors)
    held = hold_pid() is not None or any(row.get("mode") == "block" for row in ours)
    warning = ""
    if decision["safetyBlocks"]:
        if "battery_below_threshold" in decision["safetyBlocks"]:
            warning = f"Battery is at or below {BATTERY_FLOOR}%. Sleep is allowed."
        elif "network_unavailable" in decision["safetyBlocks"]:
            warning = "No network route for 15 minutes. Sleep is allowed."
    elif decision["active"] and not held:
        warning = "The lease is on, and the sleep lock is not held yet."
    title = "Awake" if decision["prevent"] else "Sleep allowed"
    line = requester_line(state)
    payload = {
        "active": decision["active"],
        "prevent": decision["prevent"],
        "inhibitorHeld": held,
        "remainingSeconds": remaining,
        "countdown": panel_countdown(remaining) if decision["active"] else "",
        "compact": compact_countdown(remaining) if decision["active"] else "",
        "manualUntil": until if isinstance(until, int) else None,
        "title": title,
        "requesterLine": line,
        "prompt": line or "Start a timer, or let your agents take over.",
        "warning": warning,
        "safetyBlocks": decision["safetyBlocks"],
        "stopReason": decision["stopReason"],
        "activity": activity_buckets(state, moment),
        "batteryPercent": facts.get("batteryPercent"),
        "onBattery": facts.get("power") == "battery",
        "route": facts.get("route"),
        "lidPresent": lid_present(),
        "lidDropIn": lid_dropin_installed(),
        "inhibitors": [
            {"who": row["who"], "what": row["what"], "why": row["why"], "mode": row["mode"]}
            for row in inhibitors
        ],
        "ours": [
            {"who": row["who"], "what": row["what"], "why": row["why"], "mode": row["mode"]}
            for row in ours
        ],
        "hooks": {},
        "cliLinked": False,
    }
    try:
        from hooks import cli_linked as hooks_cli_linked
        from hooks import hooks_status

        payload["hooks"] = hooks_status()
        payload["cliLinked"] = hooks_cli_linked()
    except Exception:
        pass
    return payload


def doctor_text(payload: dict | None = None) -> str:
    report = payload or status_payload()
    lines = [
        f"Status: {'active' if report['prevent'] else 'inactive'}",
        f"Power lock: {'on' if report['inhibitorHeld'] else 'off'}",
        f"Countdown: {report['countdown'] or '—'}",
        f"Requester: {report['requesterLine'] or '—'}",
        f"Battery: {report['batteryPercent'] if report['batteryPercent'] is not None else '—'}"
        f"{' (on battery)' if report['onBattery'] else ''}",
        f"Route: {_route_label(report['route'])}",
        f"Lid: {'present' if report['lidPresent'] else 'none'}",
        f"Lid inhibitors honored: {'yes' if report['lidDropIn'] else 'no'}",
    ]
    if report["warning"]:
        lines.append(f"Warning: {report['warning']}")
    if report["ours"]:
        lines.append("dsmn inhibitors:")
        for row in report["ours"]:
            lines.append(f"  {row['mode']} {row['what']} — {row['why']}")
    else:
        lines.append("dsmn inhibitors: none")
    others = [row for row in report["inhibitors"] if row["who"] != "dsmn"]
    if others:
        lines.append("Other inhibitors:")
        for row in others:
            lines.append(f"  {row['who']} {row['mode']} {row['what']} — {row['why']}")
    return "\n".join(lines) + "\n"


def _route_label(route: bool | None) -> str:
    if route is True:
        return "yes"
    if route is False:
        return "no"
    return "unknown"


def command_manual(argv: list[str]) -> int:
    minutes = None
    extend_only = False
    requester = {"name": "", "context": "", "event": ""}
    as_json = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--minutes" and index + 1 < len(argv):
            minutes = int(argv[index + 1])
            index += 2
            continue
        if arg == "--extend-only":
            extend_only = True
            index += 1
            continue
        if arg == "--requester" and index + 1 < len(argv):
            requester["name"] = argv[index + 1]
            index += 2
            continue
        if arg == "--context" and index + 1 < len(argv):
            requester["context"] = argv[index + 1]
            index += 2
            continue
        if arg == "--event" and index + 1 < len(argv):
            requester["event"] = argv[index + 1]
            index += 2
            continue
        if arg == "--json":
            as_json = True
            index += 1
            continue
        raise SystemExit(f"unknown argument: {arg}")
    if minutes is None:
        raise SystemExit("manual requires --minutes")
    moment = now_ms()

    def mutate(state: dict) -> dict:
        return apply_manual(state, minutes, moment, extend_only, requester)

    try:
        Store().update(mutate, moment)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    bundle = tick()
    if bundle["decision"]["prevent"]:
        ensure_hold(bundle["state"], bundle["decision"])
    payload = status_payload(bundle, moment)
    if as_json:
        print(json.dumps(payload))
    else:
        print(doctor_text(payload), end="")
    return 0


def command_stop(reason: str = "user_stop") -> int:
    moment = now_ms()
    Store().update(lambda state: apply_stop(state, moment, reason), moment)
    release_hold()
    payload = status_payload(tick(), moment)
    return payload


def command_repair() -> dict:
    """Keep the running timer and start the holder again when it should be awake."""
    moment = now_ms()
    bundle = tick()
    if bundle["decision"]["prevent"]:
        ensure_hold(bundle["state"], bundle["decision"])
    return status_payload(bundle, moment)


def watch_main() -> int:
    lock_path = state_dir() / "hold.lock"
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    os.execvp(
        "systemd-inhibit",
        [
            "systemd-inhibit",
            "--what=sleep:idle:handle-lid-switch",
            "--who=dsmn",
            "--why=dsmn lease",
            "--mode=block",
            sys.executable,
            str(_bin()),
            "hold",
        ],
    )
    return 1


def hold_main() -> int:
    lock_path = state_dir() / "hold.lock"
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    started = read_process_start_ticks(os.getpid())
    if started is None:
        return 1
    record = f"{os.getpid()} {started}"
    path = pid_path()
    path.write_text(record + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        while True:
            bundle = tick()
            if not bundle["decision"]["prevent"]:
                return 0
            time.sleep(HOLD_POLL_SECONDS)
    finally:
        try:
            if path.read_text(encoding="utf-8").strip() == record:
                path.unlink()
        except OSError:
            pass


def cli_linked() -> bool:
    link = Path.home() / ".local" / "bin" / "dsmn"
    try:
        return link.is_symlink() and link.resolve() == _bin().resolve()
    except OSError:
        return False


def link_cli() -> str:
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, mode=0o755, exist_ok=True)
    link = bin_dir / "dsmn"
    target = _bin().resolve()
    if link.is_symlink() and link.resolve() == target:
        return "already linked"
    if link.exists() or link.is_symlink():
        raise SystemExit(f"{link} already exists and is not this dsmn")
    link.symlink_to(target)
    return "linked"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_help())
        return 0
    command = args[0]
    rest = args[1:]
    if command == "status":
        payload = status_payload()
        if "--json" in rest:
            print(json.dumps(payload))
        else:
            print(doctor_text(payload), end="")
        return 0
    if command == "manual":
        return command_manual(rest)
    if command == "stop":
        payload = command_stop("user_stop")
        print(json.dumps(payload) if "--json" in rest else doctor_text(payload), end="" if "--json" not in rest else "\n")
        return 0
    if command == "repair":
        payload = command_repair()
        print(json.dumps(payload) if "--json" in rest else doctor_text(payload), end="" if "--json" not in rest else "\n")
        return 0
    if command == "doctor":
        from hooks import doctor_extra

        print(doctor_text(), end="")
        print(doctor_extra(), end="")
        return 0
    if command == "ensure":
        bundle = tick()
        if bundle["decision"]["prevent"]:
            ensure_hold(bundle["state"], bundle["decision"])
        else:
            release_hold()
        return 0
    if command == "watch":
        return watch_main()
    if command == "hold":
        return hold_main()
    if command == "link-cli":
        print(link_cli())
        return 0
    if command == "install-hooks":
        from hooks import install_hooks

        target = rest[0] if rest and not rest[0].startswith("--") else "all"
        print(json.dumps(install_hooks(target)))
        return 0
    if command == "uninstall-hooks":
        from hooks import uninstall_hooks

        target = rest[0] if rest and not rest[0].startswith("--") else "all"
        print(json.dumps(uninstall_hooks(target)))
        return 0
    if command == "hooks-status":
        from hooks import hooks_status

        print(json.dumps(hooks_status()))
        return 0
    if command == "lid-script":
        print(str(_plugin_root() / "bin" / "dsmn-lid-enable"))
        return 0
    raise SystemExit(f"unknown command: {command}")


def _help() -> str:
    return """dsmn — keep this Omarchy machine awake for a lease

  dsmn status [--json]
  dsmn manual --minutes <n> [--extend-only] [--requester <name>] [--context <text>] [--event <name>] [--json]
  dsmn stop [--json]
  dsmn repair [--json]
  dsmn doctor
  dsmn install-hooks [all|claude|cursor|codex|grok|pi]
  dsmn uninstall-hooks [all|claude|cursor|codex|grok|pi]
  dsmn link-cli

Sleep returns when the timer ends. Agent activity can extend it.
"""
