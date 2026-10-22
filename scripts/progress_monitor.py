"""Pure-Python progress monitor for a coding-session event stream.

All time is supplied explicitly by the caller in monotonic seconds; the
class performs no wall-clock work and has no dependencies outside the
standard library.

Event vocabulary (dicts produced by the harness):
    {"type": "tool_execution_start", "toolCallId": ..., "toolName": ...}
    {"type": "tool_execution_end",   "toolCallId": ..., "toolName": ...,
     "isError": bool}
    {"type": "message_update", ...}
    {"type": "agent_end", "isTerminal": bool|None, ...}
"""


class ProgressMonitor:
    """Tracks agent activity and reports warn/stall state."""

    def __init__(self, started, warn_seconds=120, stall_seconds=180,
                 first_write_seconds=240):
        if warn_seconds <= 0 or stall_seconds <= 0 or first_write_seconds <= 0:
            raise ValueError("intervals must be positive")
        if warn_seconds >= stall_seconds:
            raise ValueError("warn_seconds must be less than stall_seconds")

        self.started = float(started)
        self.warn_seconds = warn_seconds
        self.stall_seconds = stall_seconds
        self.first_write_seconds = first_write_seconds

        # Internal absolute monotonic times.
        self.last_event = self.started
        self.last_tool_progress = self.started
        self.first_write_at = None  # absolute time of first successful write

        # State.
        self.completed = False
        self.warned = False
        self._thinking = False
        self._stream_kind = 'unknown'
        self._stream_chars = {}
        self._active = {}  # key -> tool name (insertion-ordered)
        self._tool_counts = {}  # name -> number of starts

    def _key(self, item):
        return item.get("toolCallId") or item.get("toolName") or "unknown"

    @staticmethod
    def _name(item):
        return item.get("toolName") or "unknown"

    def event(self, item, now):
        """Consume one event, updating progress bookkeeping at time `now`."""
        self.last_event = now
        typ = item.get("type")
        if typ == "tool_execution_start":
            key = self._key(item)
            name = self._name(item)
            self._active[key] = name
            self._tool_counts[name] = self._tool_counts.get(name, 0) + 1
            self.last_tool_progress = now
        elif typ == "tool_execution_end":
            key = self._key(item)
            self._active.pop(key, None)
            self.last_tool_progress = now
            name = self._name(item)
            if name in ("write", "edit") and not item.get("isError"):
                if self.first_write_at is None:
                    self.first_write_at = now
        elif typ == "agent_end":
            if item.get("isTerminal") is not False:
                self.completed = True
        elif typ == "message_update":
            kind = item.get('stream_kind', 'unknown')
            self._stream_kind = kind if kind in ('thinking', 'text', 'toolcall') else 'unknown'
            self._stream_chars[self._stream_kind] = self._stream_chars.get(self._stream_kind, 0) + item.get('delta_chars', 0)
            if not self._active:
                self._thinking = True

    def check(self, now):
        """Return "warn", "stall", or None."""
        if self.completed:
            return None
        if self._active:
            # Coordinator's hard total timeout governs while a tool runs.
            return None
        elapsed = now - self.started
        idle = now - self.last_tool_progress

        if idle >= self.stall_seconds:
            return "stall"
        if self.first_write_at is None and elapsed >= self.first_write_seconds:
            return "stall"
        if not self.warned and idle >= self.warn_seconds:
            self.warned = True
            return "warn"
        return None

    def _phase(self):
        if self.completed:
            return "completed"
        if self._active:
            return "tool"
        if self._thinking:
            return {'thinking': 'thinking', 'text': 'text', 'toolcall': 'tool_arguments'}.get(self._stream_kind, 'streaming')
        return "starting"

    def snapshot(self, now):
        """Return a descriptive snapshot at time `now`."""
        elapsed = now - self.started
        return {
            "phase": self._phase(),
            "elapsed_seconds": elapsed,
            "idle_tool_seconds": now - self.last_tool_progress,
            "active_tools": list(self._active.values()),
            "tool_counts": dict(self._tool_counts),
            "last_event_age_seconds": now - self.last_event,
            "first_write_at": (self.first_write_at - self.started)
                if self.first_write_at is not None else None,
            "warned": self.warned,
            "stream_chars": dict(self._stream_chars),
        }
