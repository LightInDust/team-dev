"""Tests for progress_monitor.ProgressMonitor."""

import unittest

from progress_monitor import ProgressMonitor


def tool_start(name="bash", call_id="c1"):
    return {"type": "tool_execution_start", "toolCallId": call_id,
            "toolName": name}


def tool_end(name="bash", call_id="c1", is_error=False):
    return {"type": "tool_execution_end", "toolCallId": call_id,
            "toolName": name, "isError": is_error}


def message_update():
    return {"type": "message_update"}


def agent_end(terminal):
    return {"type": "agent_end", "isTerminal": terminal}


class ProgressMonitorTest(unittest.TestCase):
    def test_constructor_validation(self):
        for kwargs in (
            {"warn_seconds": 0},
            {"stall_seconds": 0},
            {"first_write_seconds": -5},
            {"warn_seconds": 180, "stall_seconds": 180},
            {"warn_seconds": 300, "stall_seconds": 180},
        ):
            with self.assertRaises(ValueError, msg=kwargs):
                ProgressMonitor(0.0, **kwargs)
        # Sanity: valid construction works.
        ProgressMonitor(0.0, warn_seconds=120, stall_seconds=180,
                        first_write_seconds=240)

    def test_thinking_stream_cannot_hide_stall(self):
        # Only message_updates (thinking) arrive; no tool progress at all.
        p = ProgressMonitor(0.0)
        now = 0.0
        for _ in range(100):
            now += 1.0
            p.event(message_update(), now)
        self.assertEqual(p.snapshot(now)["phase"], "streaming")
        # Warn first at warn_seconds...
        self.assertEqual(p.check(120.0), "warn")
        # ...then stall at stall_seconds. The thinking stream did not hide it.
        self.assertEqual(p.check(180.0), "stall")

    def test_active_bash_not_stalled(self):
        p = ProgressMonitor(0.0)
        p.event(tool_start("bash"), 10.0)
        # Huge backend: long elapsed AND long idle since the tool start.
        self.assertIsNone(p.check(10000.0))
        snap = p.snapshot(10000.0)
        self.assertEqual(snap["phase"], "tool")
        self.assertEqual(snap["active_tools"], ["bash"])
        self.assertIsNone(snap["first_write_at"])

    def test_tool_end_resets_idle(self):
        p = ProgressMonitor(0.0)
        p.event(tool_start("bash", "c1"), 10.0)
        p.event(tool_end("bash", "c1"), 100.0)
        self.assertEqual(p.snapshot(100.0)["idle_tool_seconds"], 0.0)
        self.assertIsNone(p.check(100.0))
        # Idle only counts from the reset point.
        self.assertEqual(p.check(220.0), "warn")  # 120s after the end
        self.assertEqual(p.check(280.0), "stall")  # 180s after the end

    def test_first_write_tracked_only_success(self):
        p = ProgressMonitor(0.0)
        # A failed write must not set first_write_at.
        p.event(tool_start("write", "c1"), 10.0)
        p.event(tool_end("write", "c1", is_error=True), 10.0)
        self.assertIsNone(p.snapshot(10.0)["first_write_at"])
        # A successful write then sets it (as elapsed time).
        p.event(tool_start("write", "c2"), 50.0)
        p.event(tool_end("write", "c2"), 50.0)
        snap = p.snapshot(50.0)
        self.assertEqual(snap["first_write_at"], 50.0)
        self.assertEqual(p.snapshot(100.0)["first_write_at"], 50.0)

    def test_read_churn_hits_first_write_deadline(self):
        # Continuous read activity keeps idle low, but absence of any
        # successful write still stalls at the first-write deadline.
        p = ProgressMonitor(0.0)
        now = 0.0
        while now < 300.0:
            now += 10.0
            p.event(tool_start("read", "c"), now - 1.0)
            p.event(tool_end("read", "c"), now)
        self.assertEqual(p.snapshot(now)["idle_tool_seconds"], 0.0)
        self.assertIsNone(p.check(200.0))  # below first-write deadline (240)
        result = p.check(240.0)  # exactly at the deadline
        # Cannot rely on warn (idle < warn_seconds here); stall fires.
        self.assertEqual(result, "stall")

    def test_nonterminal_agent_end_not_complete(self):
        p = ProgressMonitor(0.0)
        p.event(agent_end(False), 10.0)
        self.assertEqual(p.snapshot(10.0)["phase"], "starting")
        self.assertEqual(p.check(300.0), "stall")
        # A terminal agent_end marks completed; check then always returns None.
        p.event(message_update(), 310.0)
        p.event(tool_start("bash"), 320.0)
        p.event(agent_end(True), 320.0)
        self.assertEqual(p.snapshot(320.0)["phase"], "completed")
        self.assertIsNone(p.check(99999.0))

    def test_warning_once(self):
        p = ProgressMonitor(0.0)
        p.event(tool_start("bash", "c1"), 0.0)
        p.event(tool_end("bash", "c1"), 0.0)
        self.assertEqual(p.check(120.0), "warn")
        self.assertTrue(p.snapshot(120.0)["warned"])
        # Still idle, still past warn but not yet stall: no second warning.
        self.assertIsNone(p.check(150.0))
        self.assertIsNone(p.check(179.0))
        # Eventually stalls (warn does not reset the timer, so the same idle
        # budget carries through to stall).
        self.assertEqual(p.check(180.0), "stall")

    def test_stall_takes_precedence_over_warn(self):
        p = ProgressMonitor(0.0)
        p.event(tool_start("bash", "c1"), 0.0)
        p.event(tool_end("bash", "c1"), 0.0)
        # Both idle >= warn_seconds and idle >= stall_seconds hold; stall wins.
        self.assertEqual(p.check(190.0), "stall")
        self.assertFalse(p.warned)  # warn never issued because stall won

    def test_message_update_only_sets_thinking_no_tool(self):
        p = ProgressMonitor(0.0)
        p.event(message_update(), 5.0)
        self.assertEqual(p.snapshot(5.0)["phase"], "streaming")
        # A message_update does NOT reset the last_tool_progress timer: idle
        # still accumulates from start.
        self.assertEqual(p.snapshot(5.0)["idle_tool_seconds"], 5.0)
        self.assertEqual(p.snapshot(5.0)["last_event_age_seconds"], 0.0)

    def test_active_tool_beats_thinking(self):
        p = ProgressMonitor(0.0)
        p.event(message_update(), 1.0)
        p.event(tool_start("bash"), 2.0)
        self.assertEqual(p.snapshot(2.0)["phase"], "tool")
        self.assertEqual(p.snapshot(2.0)["active_tools"], ["bash"])


if __name__ == "__main__":
    unittest.main()
