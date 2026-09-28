import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import dsmn_core
import hooks


NOW = 1_700_000_000_000


class DecisionTests(unittest.TestCase):
    def state(self):
        return dsmn_core.empty_state(NOW)

    def test_manual_sets_deadline(self):
        state = dsmn_core.apply_manual(self.state(), 30, NOW)
        self.assertEqual(state["manualUntil"], NOW + 30 * 60 * 1000)
        decision = dsmn_core.evaluate(state, {"power": "ac", "batteryPercent": None, "route": True}, NOW + 1000)
        self.assertTrue(decision["prevent"])

    def test_extend_only_does_not_shorten(self):
        state = dsmn_core.apply_manual(self.state(), 90, NOW)
        state = dsmn_core.apply_manual(
            state, 20, NOW + 1000, extend_only=True, requester={"name": "Claude Code", "context": "app", "event": "PreToolUse"}
        )
        self.assertEqual(state["manualUntil"], NOW + 90 * 60 * 1000)
        self.assertEqual(state["lastRequester"]["name"], "Claude Code")
        self.assertEqual(state["lastRequester"]["context"], "app")

    def test_extend_only_lengthens_a_shorter_lease(self):
        state = dsmn_core.apply_manual(self.state(), 10, NOW)
        state = dsmn_core.apply_manual(state, 20, NOW + 1000, extend_only=True)
        self.assertEqual(state["manualUntil"], NOW + 1000 + 20 * 60 * 1000)

    def test_expiry(self):
        state = dsmn_core.apply_manual(self.state(), 1, NOW)
        decision = dsmn_core.evaluate(state, {"power": "ac", "route": True}, NOW + 61 * 1000)
        self.assertFalse(decision["active"])
        self.assertFalse(decision["prevent"])
        self.assertEqual(decision["stopReason"], "manual_expired")

    def test_battery_floor_only_on_battery(self):
        state = dsmn_core.apply_manual(self.state(), 30, NOW)
        on_ac = dsmn_core.evaluate(state, {"power": "ac", "batteryPercent": 5, "route": True}, NOW)
        on_battery = dsmn_core.evaluate(state, {"power": "battery", "batteryPercent": 20, "route": True}, NOW)
        above = dsmn_core.evaluate(state, {"power": "battery", "batteryPercent": 21, "route": True}, NOW)
        self.assertTrue(on_ac["prevent"])
        self.assertFalse(on_battery["prevent"])
        self.assertIn("battery_below_threshold", on_battery["safetyBlocks"])
        self.assertTrue(above["prevent"])

    def test_no_route_grace(self):
        state = dsmn_core.apply_manual(self.state(), 30, NOW)
        state = dsmn_core.note_route(state, False, NOW)
        early = dsmn_core.evaluate(state, {"power": "ac", "route": False}, NOW + 1000)
        late = dsmn_core.evaluate(
            state, {"power": "ac", "route": False}, NOW + dsmn_core.NO_ROUTE_GRACE_MS
        )
        self.assertTrue(early["prevent"])
        self.assertFalse(late["prevent"])
        self.assertIn("network_unavailable", late["safetyBlocks"])

    def test_unknown_route_does_not_start_an_outage(self):
        state = dsmn_core.apply_manual(self.state(), 30, NOW)
        state = dsmn_core.note_route(state, None, NOW)
        self.assertIsNone(state["noRouteSince"])
        decision = dsmn_core.evaluate(state, {"power": "ac", "route": None}, NOW + dsmn_core.NO_ROUTE_GRACE_MS)
        self.assertTrue(decision["prevent"])

    def test_unknown_route_keeps_a_recorded_outage(self):
        state = dsmn_core.apply_manual(self.state(), 30, NOW)
        state = dsmn_core.note_route(state, False, NOW)
        state = dsmn_core.note_route(state, None, NOW + 1000)
        self.assertEqual(state["noRouteSince"], NOW)

    def test_route_restore_clears_outage(self):
        state = dsmn_core.apply_manual(self.state(), 30, NOW)
        state = dsmn_core.note_route(state, False, NOW)
        state = dsmn_core.note_route(state, True, NOW + 1000)
        self.assertIsNone(state["noRouteSince"])

    def test_desktop_without_battery_is_not_blocked(self):
        state = dsmn_core.apply_manual(self.state(), 30, NOW)
        decision = dsmn_core.evaluate(state, {"power": "ac", "batteryPercent": None, "route": True}, NOW)
        self.assertTrue(decision["prevent"])

    def test_rejects_overlong_manual(self):
        with self.assertRaises(ValueError):
            dsmn_core.apply_manual(self.state(), dsmn_core.MAX_MINUTES + 1, NOW)

    def test_stop_clears_the_lease(self):
        state = dsmn_core.apply_stop(dsmn_core.apply_manual(self.state(), 30, NOW), NOW + 5)
        decision = dsmn_core.evaluate(state, {"power": "ac", "route": True}, NOW + 5)
        self.assertFalse(decision["prevent"])
        self.assertEqual(state["lastStopReason"], "user_stop")

    def test_countdown_and_activity(self):
        self.assertEqual(dsmn_core.panel_countdown(90), "01:30")
        self.assertEqual(dsmn_core.panel_countdown(3661), "1:01:01")
        self.assertEqual(dsmn_core.compact_countdown(90), "1m")
        self.assertEqual(dsmn_core.compact_countdown(5400), "1h30m")
        state = dsmn_core.apply_manual(
            self.state(), 30, NOW, requester={"name": "Codex", "context": "", "event": "turn"}
        )
        buckets = dsmn_core.activity_buckets(state, NOW)
        self.assertEqual(sum(buckets), 1)
        self.assertEqual(buckets[-1], 1)
        self.assertEqual(dsmn_core.requester_line(state), "Codex")


class HookTests(unittest.TestCase):
    def test_codex_false_becomes_true_and_true_stays_identical(self):
        original = "[features]\nhooks = false # keep\nmodel = \"x\"\n"
        updated, status = hooks.enable_codex_hooks(original)
        self.assertEqual(status, "updated")
        self.assertIn("hooks = true # keep", updated)
        self.assertIn('model = "x"', updated)
        again, status = hooks.enable_codex_hooks(updated)
        self.assertEqual(status, "already")
        self.assertEqual(again, updated)

    def test_codex_already_true_is_byte_identical(self):
        original = "# comment\n[features]\nhooks = true\n"
        updated, status = hooks.enable_codex_hooks(original)
        self.assertEqual(status, "already")
        self.assertIs(updated, original)

    def test_codex_inline_and_dotted_are_refused(self):
        self.assertEqual(hooks.enable_codex_hooks("features = { hooks = false }\n")[1], "refused")
        self.assertEqual(hooks.enable_codex_hooks("features.hooks = false\n")[1], "refused")

    def test_codex_missing_table_is_appended(self):
        updated, status = hooks.enable_codex_hooks("model = \"x\"\n")
        self.assertEqual(status, "updated")
        self.assertTrue(updated.startswith("model = \"x\"\n"))
        self.assertIn("[features]\nhooks = true\n", updated)

    def test_install_merges_and_uninstall_leaves_other_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_HOME"] = directory
            claude = Path(directory) / ".claude"
            claude.mkdir()
            settings = claude / "settings.json"
            settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
            report = hooks.install_one("claude", force=True)
            self.assertTrue(report["ok"], report)
            merged = json.loads(settings.read_text())
            self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["command"], "echo hi")
            self.assertIn("dsmn-agent-hook.sh", json.dumps(merged["hooks"]["PreToolUse"]))
            removed = hooks.uninstall_one("claude")
            self.assertEqual(removed["message"], "removed")
            after = json.loads(settings.read_text())
            self.assertNotIn("dsmn-agent-hook.sh", json.dumps(after))
            self.assertEqual(after["hooks"]["Stop"][0]["hooks"][0]["command"], "echo hi")

    def test_install_summary_is_one_sentence(self):
        text = hooks.install_summary(
            [
                {"target": "claude", "ok": True, "message": "installed"},
                {"target": "cursor", "ok": True, "message": "installed"},
                {"target": "codex", "ok": True, "message": "not installed"},
                {"target": "grok", "ok": False, "message": "refused"},
                {"target": "pi", "ok": True, "message": "installed"},
            ],
            "already linked",
        )
        self.assertIn("Installed for Claude, Cursor, and Pi.", text)
        self.assertIn("Codex is not on this machine.", text)
        self.assertIn("Grok could not be installed.", text)
        self.assertTrue(text.endswith("Already linked."))

    def test_install_hooks_names_every_present_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_HOME"] = directory
            for name in (".claude", ".cursor", ".codex", ".grok", ".pi"):
                (Path(directory) / name).mkdir()
            report = hooks.install_hooks("all")
            self.assertIn("summary", report)
            messages = {item["target"]: item["message"] for item in report["results"]}
            self.assertEqual(messages["claude"], "installed")
            self.assertEqual(messages["grok"], "installed")
            self.assertTrue((Path(directory) / ".grok" / "hooks" / "dsmn.json").is_file())
            self.assertIn("dsmn-agent-hook.sh", (Path(directory) / ".claude" / "settings.json").read_text())

    def test_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_HOME"] = directory
            claude = Path(directory) / ".claude"
            claude.mkdir()
            target = Path(directory) / "real.json"
            target.write_text("{}")
            link = claude / "settings.json"
            link.symlink_to(target)
            report = hooks.install_one("claude", force=True)
            self.assertFalse(report["ok"])
            self.assertEqual(target.read_text(), "{}")

    def test_install_keeps_private_mode_and_foreign_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_HOME"] = directory
            claude = Path(directory) / ".claude"
            claude.mkdir()
            settings = claude / "settings.json"
            settings.write_text(json.dumps({
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "caffeinate -i sleep 10"}]},
                        {"hooks": [{"type": "command", "command": "/opt/other/claude-hook.sh"}]},
                    ]
                }
            }))
            os.chmod(settings, 0o600)
            report = hooks.install_one("claude", force=True)
            self.assertTrue(report["ok"], report)
            text = settings.read_text()
            self.assertIn("caffeinate -i sleep 10", text)
            self.assertIn("/opt/other/claude-hook.sh", text)
            self.assertIn("dsmn-agent-hook.sh", text)
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(claude.glob("*.dsmn-tmp")))
            removed = hooks.uninstall_one("claude")
            self.assertEqual(removed["message"], "removed")
            after = settings.read_text()
            self.assertIn("caffeinate -i sleep 10", after)
            self.assertIn("/opt/other/claude-hook.sh", after)
            self.assertNotIn("dsmn-agent-hook.sh", after)
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)

    def test_install_does_not_truncate_an_existing_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_HOME"] = directory
            os.environ["DSMN_STATE_DIR"] = directory
            claude = Path(directory) / ".claude"
            claude.mkdir()
            settings = claude / "settings.json"
            settings.write_text("{}\n")
            decoy = claude / "settings.json.dsmn-tmp"
            decoy.write_text("keep this\n")
            os.chmod(decoy, 0o600)
            report = hooks.install_one("claude", force=True)
            self.assertTrue(report["ok"], report)
            self.assertEqual(decoy.read_text(), "keep this\n")
            self.assertIn("dsmn-agent-hook.sh", settings.read_text())
            state_decoy = Path(directory) / "state.json.tmp"
            state_decoy.write_text("keep state\n")
            dsmn_core.Store().update(lambda state: state, dsmn_core.now_ms())
            self.assertEqual(state_decoy.read_text(), "keep state\n")
            self.assertTrue((Path(directory) / "state.json").is_file())
            os.environ.pop("DSMN_STATE_DIR", None)

    def test_uninstall_keeps_a_command_that_only_mentions_our_words(self):
        impostor = "echo dsmn-agent-hook.sh DSMN_HOOK_AGENT='claude'"
        cursor_impostor = "echo dsmn-agent-hook.sh DSMN_HOOK_AGENT='cursor'"
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_HOME"] = directory
            home = Path(directory)
            claude = home / ".claude"
            claude.mkdir()
            settings = claude / "settings.json"
            settings.write_text(json.dumps({
                "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": impostor}]}]}
            }))
            hooks.install_one("claude", force=True)
            hooks.uninstall_one("claude")
            after = settings.read_text()
            self.assertIn(impostor, after)
            self.assertNotIn(hooks.hook_command("claude", "PreToolUse"), after)

            cursor = home / ".cursor"
            cursor.mkdir()
            cursor_file = cursor / "hooks.json"
            cursor_file.write_text(json.dumps({
                "version": 1,
                "hooks": {"preToolUse": [{"command": cursor_impostor}]},
            }))
            hooks.install_one("cursor", force=True)
            hooks.uninstall_one("cursor")
            cursor_after = cursor_file.read_text()
            self.assertIn(cursor_impostor, cursor_after)
            self.assertNotIn(hooks.hook_command("cursor", "preToolUse"), cursor_after)

            pi = home / ".pi" / "agent" / "extensions"
            pi.mkdir(parents=True)
            pi_file = pi / "dsmn.ts"
            pi_file.write_text("export const note = 'dsmn-agent-hook.sh'\n")
            removed = hooks.uninstall_one("pi")
            self.assertEqual(removed["message"], "absent")
            self.assertEqual(pi_file.read_text(), "export const note = 'dsmn-agent-hook.sh'\n")


class HoldIdentityTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("DSMN_STATE_DIR", None)

    def test_start_ticks_use_the_last_closing_paren(self):
        stat = "42 (weird) name) R 1 1 1 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 424242 100 0\n"
        self.assertEqual(dsmn_core.process_start_ticks(stat), 424242)
        self.assertIsNone(dsmn_core.process_start_ticks("no paren here"))

    def test_hold_command_is_dsmn_hold_only(self):
        self.assertTrue(dsmn_core.hold_command(["/usr/bin/python3", "/opt/bin/dsmn", "hold"]))
        self.assertTrue(dsmn_core.hold_command(["/opt/bin/dsmn", "hold"]))
        self.assertFalse(dsmn_core.hold_command(["/usr/bin/python3", "/opt/bin/dsmn", "status"]))
        self.assertFalse(dsmn_core.hold_command(["/usr/bin/sleep", "hold"]))
        self.assertFalse(dsmn_core.hold_command(["/opt/bin/dsmn-agent-hook.sh", "hold"]))

    def test_release_hold_requires_start_time_and_command(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_STATE_DIR"] = directory
            pid_file = dsmn_core.pid_path()
            original_kill = dsmn_core.os.kill
            original_start = dsmn_core.read_process_start_ticks
            original_argv = dsmn_core.read_cmdline
            sent = []

            def record(pid, sig):
                sent.append((pid, sig))

            dsmn_core.os.kill = record
            dsmn_core.read_process_start_ticks = lambda pid: 99 if pid == 4321 else None
            dsmn_core.read_cmdline = lambda pid: ["/usr/bin/python3", "/opt/bin/dsmn", "hold"] if pid == 4321 else None
            try:
                pid_file.write_text("4321 100\n")
                self.assertIsNone(dsmn_core.hold_pid())
                dsmn_core.release_hold()
                pid_file.write_text("4321\n")
                self.assertIsNone(dsmn_core.hold_pid())
                dsmn_core.release_hold()
                dsmn_core.read_cmdline = lambda pid: ["/usr/bin/sleep", "hold"]
                pid_file.write_text("4321 99\n")
                self.assertIsNone(dsmn_core.hold_pid())
                dsmn_core.release_hold()
                self.assertEqual(sent, [])
                dsmn_core.read_cmdline = lambda pid: ["/usr/bin/python3", "/opt/bin/dsmn", "hold"]
                self.assertEqual(dsmn_core.hold_pid(), 4321)
                dsmn_core.release_hold()
                self.assertEqual(sent, [(4321, signal.SIGTERM)])
            finally:
                dsmn_core.os.kill = original_kill
                dsmn_core.read_process_start_ticks = original_start
                dsmn_core.read_cmdline = original_argv


class RepairTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("DSMN_STATE_DIR", None)

    def test_repair_keeps_the_timer_and_does_not_signal_a_stranger(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DSMN_STATE_DIR"] = directory
            moment = dsmn_core.now_ms()
            state = dsmn_core.apply_manual(dsmn_core.empty_state(moment), 30, moment)
            dsmn_core.Store().update(lambda _current: state, moment)
            original = (
                dsmn_core.probe_snapshot,
                dsmn_core.subprocess.Popen,
                dsmn_core.os.kill,
                dsmn_core.list_inhibitors,
            )
            started = []
            killed = []
            dsmn_core.probe_snapshot = lambda: {"power": "ac", "batteryPercent": None, "route": True}
            dsmn_core.subprocess.Popen = lambda *args, **kwargs: started.append(args)
            dsmn_core.os.kill = lambda pid, sig: killed.append((pid, sig))
            dsmn_core.list_inhibitors = lambda: [
                {"what": "sleep", "who": "dsmn", "why": "not ours", "mode": "block", "uid": 1000, "pid": 99999}
            ]
            try:
                payload = dsmn_core.command_repair()
                saved = dsmn_core.Store().load(moment)
                self.assertEqual(saved["manualUntil"], state["manualUntil"])
                self.assertTrue(payload["prevent"])
                self.assertTrue(started)
                self.assertEqual(killed, [])
                dsmn_core.kill_dsmn_inhibitors()
                self.assertEqual(killed, [])
            finally:
                (
                    dsmn_core.probe_snapshot,
                    dsmn_core.subprocess.Popen,
                    dsmn_core.os.kill,
                    dsmn_core.list_inhibitors,
                ) = original


if __name__ == "__main__":
    unittest.main()
