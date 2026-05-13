"""Run the PRB blocking agent in isolation against vLLM.

Starts only the PRB agent with a hardcoded intent — no L2 manager,
no power agent, no scheduler agent.  Useful for evaluating the
prb_blocking_agent LoRA in a vacuum.

Usage:
    python test_prb_vllm.py              # vLLM fine-tuned model
    python test_prb_vllm.py --ollama     # use qwen3:32b via Ollama instead
"""

import argparse
import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from shared.state import SharedState
from shared.llm_client import LLMClient
from agents.prb_agent import PRBBlockingAgent
from shared.config import PRB_AGENT_CONFIG


async def main():
    parser = argparse.ArgumentParser(description="PRB Agent Solo Test (vLLM)")
    parser.add_argument("--ollama", action="store_true",
                        help="Use qwen3:32b via Ollama instead of vLLM fine-tuned model")
    parser.add_argument("--intent", type=str,
                        default="Preemptively block PRBs for UEs that risk causing interference to neighboring cells based on their RSRP patterns.",
                        help="Intent to set for the PRB agent")
    args = parser.parse_args()

    state = SharedState()

    # Set the PRB agent to running with the provided intent
    state.set_status('prb', 'running')
    state.set_intent('prb', args.intent)

    prb_agent = PRBBlockingAgent(state)

    if args.ollama:
        prb_agent.alt_client = LLMClient(
            api_url="http://YOUR_OLLAMA_URL",
            model="qwen2.5-coder:7b",
            timeout=60,
            max_retries=2,
        )

    active = prb_agent.alt_client if args.ollama else prb_agent.llm_client
    print("=== PRB Agent Solo Test ===")
    print(f"  Active:   {active.model} @ {active.api_url}")
    if args.ollama:
        print(f"  (using Ollama qwen3:32b instead of vLLM fine-tuned model)")
    print(f"  Interval: {PRB_AGENT_CONFIG['min_decision_interval']}s")
    print("Press Ctrl+C to stop\n")

    try:
        await prb_agent.start()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    asyncio.run(main())
