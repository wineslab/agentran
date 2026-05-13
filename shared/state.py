"""In-memory shared state.

All agents run in the same asyncio event loop (single process),
so a simple Python object with dicts is sufficient.
"""

import json
from collections import deque


class SharedState:
    """Shared state between agents."""

    def __init__(self):
        self.status = {}      # agent_type -> 'running' | 'paused'
        self.intents = {}     # agent_type -> intent string
        self.data = {}        # arbitrary key -> value
        self._history = {}    # key -> deque of dicts

    # --- Status ---

    def get_status(self, agent_type: str) -> str:
        return self.status.get(agent_type, 'running')

    def set_status(self, agent_type: str, status: str):
        self.status[agent_type] = status

    # --- Intents ---

    def get_intent(self, agent_type: str) -> str | None:
        return self.intents.get(agent_type)

    def set_intent(self, agent_type: str, intent: str):
        self.intents[agent_type] = intent

    # --- History (per-UE decision history, agent decision logs, etc.) ---

    def push_history(self, key: str, entry: dict, maxlen: int = 20):
        """Add entry to front of history list (most recent first)."""
        if key not in self._history:
            self._history[key] = deque(maxlen=maxlen)
        self._history[key].appendleft(entry)

    def get_history(self, key: str, limit: int = 10) -> list[dict]:
        """Get most recent entries from history."""
        if key not in self._history:
            return []
        h = self._history[key]
        return list(h)[:limit]

    def has_history(self, key: str) -> bool:
        return key in self._history and len(self._history[key]) > 0

    def clear_history_prefix(self, prefix: str):
        """Clear all history keys matching a prefix."""
        to_delete = [k for k in self._history if k.startswith(prefix)]
        for k in to_delete:
            del self._history[k]

    # --- Generic key-value data ---

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value
