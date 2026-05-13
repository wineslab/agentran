"""Standalone Scheduler Agent runner.

Reads its intent from SHARED_STATE_DIR/subintents.json (written by the
L2 Manager) or falls back to the SCHEDULER_INTENT env var.  Watches the
file for updates so the L2 Manager can change the intent at runtime.
"""
import asyncio
import json
import os
import sys
import signal

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from shared.state import SharedState
from shared.config import SCHEDULER_AGENT_CONFIG
from agents.scheduling_agent import SchedulerControlAgent

SHARED_STATE_DIR = os.environ.get('SHARED_STATE_DIR', os.path.dirname(os.path.abspath(__file__)))
SUBINTENTS_PATH = os.path.join(SHARED_STATE_DIR, 'subintents.json')
POLL_INTERVAL = 5  # seconds between intent-file checks


def read_intent_from_file() -> str | None:
    """Read scheduler intent from the shared subintents file."""
    try:
        with open(SUBINTENTS_PATH, 'r') as f:
            data = json.load(f)
        return data.get('scheduler')
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


async def wait_for_intent(timeout: float = 300) -> str:
    """Block until the subintents file appears or fall back to env var."""
    fallback = os.environ.get('SCHEDULER_INTENT', '')
    elapsed = 0
    while elapsed < timeout:
        intent = read_intent_from_file()
        if intent:
            return intent
        if elapsed % 10 == 0:
            print(f"[Scheduler Runner] Waiting for {SUBINTENTS_PATH} ...")
        await asyncio.sleep(2)
        elapsed += 2

    if fallback:
        print(f"[Scheduler Runner] Timeout waiting for file, using SCHEDULER_INTENT env var")
        return fallback
    raise RuntimeError(f"No scheduler intent found after {timeout}s (no file and no SCHEDULER_INTENT env var)")


async def intent_watcher(state: SharedState):
    """Periodically reload intent from file so L2 Manager changes propagate."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        new_intent = read_intent_from_file()
        if new_intent and new_intent != state.get_intent('scheduler'):
            print(f"[Scheduler Runner] Intent updated from file: {new_intent[:80]}...")
            state.set_intent('scheduler', new_intent)


async def main():
    llm_type = os.environ.get('LLM_TYPE', 'vllm')

    print(f"[Scheduler Runner] LLM: {llm_type}")
    print(f"[Scheduler Runner] Config: min_limit={SCHEDULER_AGENT_CONFIG['min_limit']}, "
          f"max_limit={SCHEDULER_AGENT_CONFIG['max_limit']}, "
          f"interval={SCHEDULER_AGENT_CONFIG['min_decision_interval']}s")

    # Wait for L2 Manager to write the intent
    intent = await wait_for_intent()
    print(f"[Scheduler Runner] Intent: {intent}")

    state = SharedState()
    state.set_intent('scheduler', intent)
    state.set_status('scheduler', 'running')

    agent = SchedulerControlAgent(state, llm_type=llm_type)

    # Graceful shutdown
    def _shutdown():
        agent.running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    # Run agent + intent watcher concurrently
    await asyncio.gather(
        agent.start(),
        intent_watcher(state),
    )


if __name__ == "__main__":
    asyncio.run(main())
