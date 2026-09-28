"""Install dsmn's agent hooks without touching unrelated configuration."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from dsmn_core import cli_linked, replace_file

EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"]
CURSOR_EVENTS = ["sessionStart", "beforeSubmitPrompt", "preToolUse", "postToolUse"]
TARGETS = ["claude", "cursor", "codex", "grok", "pi"]


def home() -> Path:
    override = os.environ.get("DSMN_HOME")
    return Path(override) if override else Path.home()


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def hook_script() -> Path:
    return plugin_root() / "bin" / "dsmn-agent-hook.sh"


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def hook_command(agent: str, event: str) -> str:
    return (
        f"DSMN_HOOK_AGENT={shell_quote(agent)} "
        f"DSMN_HOOK_EVENT={shell_quote(event)} "
        f"{shell_quote(str(hook_script()))}"
    )


def _hook_group(agent: str, event: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": hook_command(agent, event),
                "timeout": 5,
            }
        ]
    }


def _command_is_ours(command: str, agent: str, event: str) -> bool:
    """The command dsmn writes for this agent and event. Nothing that merely contains it."""
    return command == hook_command(agent, event)


def _write_private(path: Path, payload: str) -> None:
    """Replace path without widening its permissions. New files stay private."""
    path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    replace_file(path, payload, mode)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise RuntimeError(f"{path} is a symlink; existing configuration was not written")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{path} is not valid JSON; existing configuration was not written") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} is not a JSON object; existing configuration was not written")
    return value


def _write_json(path: Path, value: dict) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    _write_private(path, payload)


def _merge_standard(root: dict, agent: str) -> bool:
    hooks = root.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise RuntimeError("unsupported hooks JSON structure; existing configuration was not written")
    changed = False
    for event in EVENTS:
        groups = hooks.get(event) or []
        if not isinstance(groups, list):
            raise RuntimeError("unsupported hooks JSON structure; existing configuration was not written")
        expected = _hook_group(agent, event)
        kept = []
        for group in groups:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            nested = group.get("hooks")
            if isinstance(nested, list):
                filtered = []
                removed = False
                for item in nested:
                    command = str(item.get("command") or "") if isinstance(item, dict) else ""
                    if _command_is_ours(command, agent, event):
                        removed = True
                        continue
                    filtered.append(item)
                if removed:
                    changed = True
                    if not filtered:
                        continue
                    group = dict(group)
                    group["hooks"] = filtered
            kept.append(group)
        if expected not in kept:
            kept.append(expected)
            changed = True
        hooks[event] = kept
    root["hooks"] = hooks
    return changed


def _strip_standard(root: dict, agent: str) -> bool:
    hooks = root.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            filtered = []
            for item in group["hooks"]:
                command = str(item.get("command") or "") if isinstance(item, dict) else ""
                if _command_is_ours(command, agent, event):
                    changed = True
                    continue
                filtered.append(item)
            if not filtered:
                changed = True
                continue
            if len(filtered) != len(group["hooks"]):
                group = dict(group)
                group["hooks"] = filtered
            kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
            changed = True
    if not hooks:
        root.pop("hooks", None)
    else:
        root["hooks"] = hooks
    return changed


def _cursor_merge(root: dict) -> bool:
    changed = False
    if root.get("version") != 1:
        root["version"] = 1
        changed = True
    hooks = root.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise RuntimeError("unsupported hooks JSON structure; existing configuration was not written")
    for event in CURSOR_EVENTS:
        entries = hooks.get(event) or []
        if not isinstance(entries, list):
            raise RuntimeError("unsupported hooks JSON structure; existing configuration was not written")
        command = hook_command("cursor", event)
        kept = []
        seen = False
        for entry in entries:
            current = str(entry.get("command") or "") if isinstance(entry, dict) else ""
            if current == command:
                if seen:
                    changed = True
                    continue
                seen = True
            kept.append(entry)
        if not seen:
            kept.append({"command": command, "timeout": 5})
            changed = True
        hooks[event] = kept
    root["hooks"] = hooks
    return changed


def _cursor_strip(root: dict) -> bool:
    hooks = root.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            command = str(entry.get("command") or "") if isinstance(entry, dict) else ""
            if command == hook_command("cursor", event):
                changed = True
                continue
            kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    root["hooks"] = hooks
    return changed


def enable_codex_hooks(text: str) -> tuple[str, str]:
    """Return (text, status). status is already, updated, or refused.

    Only a boolean `hooks` key inside a [features] table is edited. Dotted
    keys and inline tables are left untouched and reported as refused.
    """
    lines = text.splitlines(keepends=True)
    section = ""
    features_at = None
    hooks_at = None
    for index, line in enumerate(lines):
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        if body.startswith("features.hooks"):
            return text, "refused"
        if body.startswith("features=") or body.startswith("features ="):
            compact = body.replace(" ", "")
            if "hooks=true" in compact:
                return text, "already"
            return text, "refused"
        if body.startswith("[") and "]" in body:
            name = body[1:body.index("]")].strip().strip("\"'")
            section = name
            if name == "features" and features_at is None:
                features_at = index
            continue
        if section != "features":
            continue
        if body.startswith("hooks=") or body.startswith("hooks ="):
            value = body.split("=", 1)[1].strip()
            if value.startswith("true"):
                return text, "already"
            if value.startswith("false"):
                hooks_at = index
                continue
            return text, "refused"
    if hooks_at is not None:
        original = lines[hooks_at]
        lines[hooks_at] = original.replace("false", "true", 1)
        return "".join(lines), "updated"
    if features_at is not None:
        header = lines[features_at]
        if not header.endswith("\n"):
            lines[features_at] = header + "\n"
        lines.insert(features_at + 1, "hooks = true\n")
        return "".join(lines), "updated"
    suffix = "" if not text or text.endswith("\n") else "\n"
    addition = f"{suffix}[features]\nhooks = true\n" if text else "[features]\nhooks = true\n"
    return text + addition, "updated"


def _write_text(path: Path, text: str) -> None:
    _write_private(path, text)


def _agent_present(target: str) -> bool:
    directories = {
        "claude": home() / ".claude",
        "cursor": home() / ".cursor",
        "codex": home() / ".codex",
        "grok": home() / ".grok",
        "pi": home() / ".pi",
    }
    binaries = {
        "claude": "claude",
        "cursor": "cursor",
        "codex": "codex",
        "grok": "grok",
        "pi": "pi",
    }
    if directories[target].exists():
        return True
    return shutil.which(binaries[target]) is not None


def _paths(target: str) -> list[Path]:
    root = home()
    if target == "claude":
        return [root / ".claude" / "settings.json"]
    if target == "cursor":
        return [root / ".cursor" / "hooks.json"]
    if target == "codex":
        return [root / ".codex" / "config.toml", root / ".codex" / "hooks.json"]
    if target == "grok":
        return [root / ".grok" / "hooks" / "dsmn.json"]
    return [root / ".pi" / "agent" / "extensions" / "dsmn.ts"]


def _pi_source() -> str:
    path = json.dumps(str(hook_script()))
    return f"""import type {{ ExtensionAPI }} from "@earendil-works/pi-coding-agent";
import {{ spawn }} from "node:child_process";

const hookPath = {path};

function trigger(eventName: string) {{
  try {{
    const child = spawn(hookPath, [], {{
      detached: true,
      stdio: "ignore",
      env: {{
        ...process.env,
        DSMN_HOOK_AGENT: "pi",
        DSMN_HOOK_EVENT: eventName,
      }},
    }});
    child.on("error", () => {{}});
    child.unref();
  }} catch {{
    // Keep Pi fail-open if dsmn is unavailable.
  }}
}}

export default function (pi: ExtensionAPI) {{
  pi.on("session_start", async () => trigger("session_start"));
  pi.on("input", async () => trigger("input"));
  pi.on("turn_start", async () => trigger("turn_start"));
  pi.on("tool_call", async () => trigger("tool_call"));
  pi.on("tool_result", async () => trigger("tool_result"));
  pi.on("turn_end", async () => trigger("turn_end"));
}}
"""


def install_one(target: str, force: bool = False) -> dict:
    if target not in TARGETS:
        return {"target": target, "ok": False, "message": "unknown agent"}
    if not force and not _agent_present(target):
        return {"target": target, "ok": True, "message": "not installed"}
    try:
        if target == "codex":
            return _install_codex()
        if target == "pi":
            path = _paths("pi")[0]
            if path.is_symlink():
                raise RuntimeError(f"{path} is a symlink; existing configuration was not written")
            if path.is_file():
                current = path.read_text(encoding="utf-8")
                if current not in ("", _pi_source()):
                    return {
                        "target": target,
                        "ok": False,
                        "message": "the Pi extension is not dsmn's file; it was not written",
                    }
            _write_text(path, _pi_source())
            os.chmod(hook_script(), 0o755)
            return {"target": target, "ok": True, "message": "installed", "path": str(path)}
        path = _paths(target)[0]
        root = _read_json(path)
        if target == "cursor":
            _cursor_merge(root)
        else:
            _merge_standard(root, target)
        _write_json(path, root)
        os.chmod(hook_script(), 0o755)
        return {"target": target, "ok": True, "message": "installed", "path": str(path)}
    except (OSError, RuntimeError) as error:
        return {"target": target, "ok": False, "message": str(error)}


def _install_codex() -> dict:
    toml_path, json_path = _paths("codex")
    if toml_path.is_symlink() or json_path.is_symlink():
        raise RuntimeError("codex configuration is a symlink; existing configuration was not written")
    original = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    updated, status = enable_codex_hooks(original)
    if status == "refused":
        raise RuntimeError(
            "codex features.hooks is not a boolean key inside [features]; enable it manually"
        )
    if status == "updated":
        _write_text(toml_path, updated)
    root = _read_json(json_path)
    _merge_standard(root, "codex")
    try:
        _write_json(json_path, root)
    except (OSError, RuntimeError) as error:
        prefix = "config.toml was updated, but hooks.json was not. " if status == "updated" else ""
        raise RuntimeError(prefix + str(error)) from error
    os.chmod(hook_script(), 0o755)
    return {"target": "codex", "ok": True, "message": "installed", "path": str(json_path)}


def uninstall_one(target: str) -> dict:
    if target not in TARGETS:
        return {"target": target, "ok": False, "message": "unknown agent"}
    try:
        if target == "pi":
            path = _paths("pi")[0]
            if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == _pi_source():
                path.unlink()
                return {"target": target, "ok": True, "message": "removed"}
            return {"target": target, "ok": True, "message": "absent"}
        if target == "codex":
            path = _paths("codex")[1]
        else:
            path = _paths(target)[0]
        if not path.exists():
            return {"target": target, "ok": True, "message": "absent"}
        root = _read_json(path)
        if target == "cursor":
            changed = _cursor_strip(root)
        else:
            changed = _strip_standard(root, target)
        if target == "grok" and not root.get("hooks") and not root and not path.is_symlink():
            path.unlink()
            return {"target": target, "ok": True, "message": "removed"}
        if changed:
            _write_json(path, root)
            return {"target": target, "ok": True, "message": "removed"}
        return {"target": target, "ok": True, "message": "absent"}
    except (OSError, RuntimeError) as error:
        return {"target": target, "ok": False, "message": str(error)}


def _selected(target: str) -> list[str]:
    if target == "all":
        return list(TARGETS)
    if target not in TARGETS:
        raise SystemExit(f"unknown agent: {target}")
    return [target]


def _join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]


def install_summary(results: list[dict], link_message: str = "") -> str:
    labels = {
        "claude": "Claude",
        "cursor": "Cursor",
        "codex": "Codex",
        "grok": "Grok",
        "pi": "Pi",
    }
    installed = []
    absent = []
    failed = []
    for item in results:
        name = labels.get(item.get("target"), item.get("target") or "Agent")
        message = item.get("message") or ""
        if not item.get("ok"):
            failed.append(f"{name} could not be installed. {message}".strip())
        elif message == "not installed":
            absent.append(name)
        elif message in ("installed", "already installed"):
            installed.append(name)
        else:
            failed.append(f"{name}: {message}")
    sentences = []
    if installed:
        sentences.append(f"Installed for {_join_names(installed)}.")
    if len(absent) == 1:
        sentences.append(f"{absent[0]} is not on this machine.")
    elif absent:
        sentences.append(f"{_join_names(absent)} are not on this machine.")
    sentences.extend(failed)
    if link_message:
        sentence = link_message[:1].upper() + link_message[1:]
        if not sentence.endswith("."):
            sentence += "."
        sentences.append(sentence)
    return " ".join(sentences) or "Nothing to install."


def install_hooks(target: str = "all") -> dict:
    chosen = _selected(target)
    force = target != "all"
    results = [install_one(name, force=force) for name in chosen]
    link_message = ""
    if not os.environ.get("DSMN_HOME"):
        try:
            import dsmn_core

            link_message = dsmn_core.link_cli()
        except SystemExit as error:
            link_message = str(error)
    return {
        "results": results,
        "cliLinked": cli_linked(),
        "summary": install_summary(results, link_message),
    }


def uninstall_hooks(target: str = "all") -> dict:
    return {"results": [uninstall_one(name) for name in _selected(target)]}


def _current(target: str) -> str:
    if not _agent_present(target):
        return "not installed"
    try:
        if target == "pi":
            path = _paths("pi")[0]
            if not path.is_file():
                return "missing"
            return "current" if path.read_text(encoding="utf-8") == _pi_source() else "outdated"
        if target == "codex":
            toml_path, json_path = _paths("codex")
            text = toml_path.read_text(encoding="utf-8") if toml_path.is_file() else ""
            _, status = enable_codex_hooks(text)
            enabled = status == "already"
            root = _read_json(json_path) if json_path.is_file() else {}
            hooks = root.get("hooks") if isinstance(root.get("hooks"), dict) else {}
            commands_ok = all(
                any(
                    isinstance(group, dict)
                    and group.get("hooks") == _hook_group("codex", event)["hooks"]
                    for group in (hooks.get(event) or [])
                    if isinstance(group, dict)
                )
                for event in EVENTS
            )
            if enabled and commands_ok:
                return "current"
            return "missing"
        path = _paths(target)[0]
        if not path.is_file():
            return "missing"
        root = _read_json(path)
        if target == "cursor":
            hooks = root.get("hooks") if isinstance(root.get("hooks"), dict) else {}
            ok = all(
                any(
                    isinstance(entry, dict) and entry.get("command") == hook_command("cursor", event)
                    for entry in (hooks.get(event) or [])
                )
                for event in CURSOR_EVENTS
            )
        else:
            hooks = root.get("hooks") if isinstance(root.get("hooks"), dict) else {}
            ok = all(
                any(
                    isinstance(group, dict) and group.get("hooks") == _hook_group(target, event)["hooks"]
                    for group in (hooks.get(event) or [])
                )
                for event in EVENTS
            )
        if ok:
            return "current"
        return "missing"
    except (OSError, RuntimeError):
        return "unreadable"


def hooks_status() -> dict:
    return {target: _current(target) for target in TARGETS}


def doctor_extra() -> str:
    status = hooks_status()
    parts = [f"{name}: {value}" for name, value in status.items()]
    linked = "yes" if cli_linked() else "no"
    return "Hooks: " + ", ".join(parts) + f"\nCLI linked: {linked}\n"
