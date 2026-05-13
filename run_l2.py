"""Standalone L2 Manager runner.

Decomposes a high-level intent into per-agent sub-intents and writes
them to SHARED_STATE_DIR/subintents.json.  Stays alive to watch for
intent changes (via INTENT env var reload or shared-state file).
"""
import asyncio
import os
import sys
import signal

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from shared.state import SharedState
from agents.l2_manager_agent import L2ManagerAgent


async def main():
    intent = os.environ.get('INTENT', 'Maximize throughput')
    llm_type = os.environ.get('LLM_TYPE', 'vllm')

    state = SharedState()
    state.set_intent('l2_manager', intent)
    state.set_status('l2_manager', 'running')

    agent = L2ManagerAgent(state, llm_type=llm_type)

    print(f"[L2 Runner] Intent: {intent}")
    print(f"[L2 Runner] LLM: {llm_type}")
    print(f"[L2 Runner] Shared state dir: {os.environ.get('SHARED_STATE_DIR', '(default)')}")

    # Graceful shutdown
    def _shutdown():
        agent.running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await agent.start()


if __name__ == "__main__":
    asyncio.run(main())
