# main.py
import asyncio
import argparse
import signal
import sys
import os

from datetime import datetime

# Add the project root to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from shared.state import SharedState
from agents.power_agent import PowerControlAgent
from agents.scheduling_agent import SchedulerControlAgent
from agents.prb_agent import PRBBlockingAgent
from agents.l2_manager_agent import L2ManagerAgent
from shared.config import POWER_AGENT_CONFIG, SCHEDULER_AGENT_CONFIG, PRB_AGENT_CONFIG, L2_MANAGER_CONFIG


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-Agent Framework")
    parser.add_argument('--intent', type=str, default=None,
                        help="High-level intent for the L2 Manager")
    parser.add_argument('--l2-llm', type=str, default='claude',
                        choices=['claude', 'qwen', 'vllm', 'fine-tuned'],
                        help="LLM for L2 Manager agent (default: claude)")
    parser.add_argument('--power-llm', type=str, default='claude',
                        choices=['claude', 'qwen', 'vllm', 'fine-tuned'],
                        help="LLM for Power Control agent (default: claude)")
    parser.add_argument('--scheduler-llm', type=str, default='claude',
                        choices=['claude', 'qwen', 'vllm', 'fine-tuned'],
                        help="LLM for Scheduler agent (default: claude)")
    parser.add_argument('--prb-llm', type=str, default='fine-tuned',
                        choices=['claude', 'qwen', 'vllm', 'fine-tuned'],
                        help="LLM for PRB Blocking agent (default: fine-tuned)")
    parser.add_argument('--agents', type=str, default='all',
                        help="Comma-separated list of agents to run: l2,power,scheduler,prb (default: all)")
    parser.add_argument('--skip-l2-init', action='store_true',
                        help="Skip initial L2 intent decomposition (reuse existing sub-intents)")
    return parser.parse_args()


async def main():
    args = parse_args()

    state = SharedState()
    print("Shared state initialized")

    # Set initial high-level intent for L2 Manager
    state.set_status('l2_manager', 'running')
    default_intent = "Maximize the overall throughput of the system and do not throttle any user or try to save battery."
    intent = args.intent or default_intent
    state.set_intent('l2_manager', intent)

    # Set initial status for other agents (intents will be set by L2 Manager)
    state.set_status('power', 'running')
    state.set_status('scheduler', 'running')
    state.set_status('prb', 'running')

    # Load existing sub-intents if skipping L2 init, otherwise set defaults
    if args.skip_l2_init:
        try:
            import json
            subintents_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'subintents.json')
            with open(subintents_path, 'r') as f:
                saved = json.load(f)
            state.set_intent('power', saved.get('power', ''))
            state.set_intent('scheduler', saved.get('scheduler', ''))
            state.set_intent('prb', saved.get('prb', ''))
            print(f"  Loaded existing sub-intents from file (skipping L2 init)")
        except Exception as e:
            print(f"  Warning: Could not load sub-intents ({e}), L2 Manager will decompose")
            args.skip_l2_init = False
    else:
        # Set fallback intents so agents can start immediately even if L2 Manager
        # decomposition fails (e.g. LLM unreachable). L2 Manager will override
        # these once it successfully decomposes.
        state.set_intent('power',
            "Maximize spectral efficiency by using high transmission power for all UE classes.")
        state.set_intent('scheduler',
            "Allow maximum throughput for both eMBB and MTC UEs.")
        state.set_intent('prb',
            "Preemptively block PRBs for UEs that risk causing interference to neighboring cells based on their RSRP patterns.")

    print("Initial configuration set")
    print(f"  High-level intent: {intent}")
    print(f"  L2 Manager LLM: {args.l2_llm}")
    print(f"  Power Agent LLM: {args.power_llm}")
    print(f"  Scheduler Agent LLM: {args.scheduler_llm}")
    print(f"  PRB Agent LLM: {args.prb_llm}")
    print(f"  Default power: {POWER_AGENT_CONFIG['default_power']} (SNR {POWER_AGENT_CONFIG['default_power']/10}dB)")
    print(f"  Default scheduler limits: eMBB={SCHEDULER_AGENT_CONFIG['default_fwa_limit']}mbps, MTC={SCHEDULER_AGENT_CONFIG['default_mtc_limit']}mbps")
    print(f"  PRB blocking decision interval: {PRB_AGENT_CONFIG['min_decision_interval']}s")

    # Create agents with per-agent LLM selection
    agents = [
        L2ManagerAgent(state, llm_type=args.l2_llm, skip_init=args.skip_l2_init),
        PowerControlAgent(state, llm_type=args.power_llm),
        SchedulerControlAgent(state, llm_type=args.scheduler_llm),
        PRBBlockingAgent(state, llm_type=args.prb_llm),
    ]

    print("\nStarting services...")
    print(f"  - L2 Manager: decomposes intent for all agents ({args.l2_llm})")
    print(f"  - Power Agent: min interval {POWER_AGENT_CONFIG['min_decision_interval']}s ({args.power_llm})")
    print(f"  - Scheduler Agent: min interval {SCHEDULER_AGENT_CONFIG['min_decision_interval']}s ({args.scheduler_llm})")
    print(f"  - PRB Agent: min interval {PRB_AGENT_CONFIG['min_decision_interval']}s ({args.prb_llm})")
    print(f"  - Metrics: read from InfluxDB")
    print("\nPress Ctrl+C to stop\n")

    experiment_start_time = datetime.now().strftime('%Y%m%d_%H%M%S')
    print(f"Experiment {experiment_start_time} started")

    # Start all agents as tasks so we can cancel them
    tasks = []

    # Give L2 Manager a head start to set initial intents (Claude may be slower)
    l2_task = asyncio.create_task(agents[0].start())
    tasks.append(l2_task)
    if not args.skip_l2_init:
        await asyncio.sleep(5)
    else:
        await asyncio.sleep(0.5)  # Brief pause to let L2 Manager start its loop

    # Then start other agents
    for agent in agents[1:]:
        tasks.append(asyncio.create_task(agent.start()))

    # Handle shutdown signals — cancel all tasks
    shutdown_event = asyncio.Event()

    def _shutdown():
        print("\nShutting down agents...")
        for agent in agents:
            agent.running = False
        for task in tasks:
            task.cancel()
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    # Wait for all tasks (they'll end via cancellation or self.running = False)
    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    except Exception as e:
        print(f"\nShutting down due to error: {e}")

    print(f"Experiment {experiment_start_time} stopped")


if __name__ == "__main__":
    asyncio.run(main())
