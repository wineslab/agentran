# agents/l2_manager_agent.py
import asyncio
import json
import re
from datetime import datetime
from agents.base_agent import BaseAgent
from shared.config import L2_MANAGER_CONFIG, INFLUX_CONFIG
from shared.llm_client import create_llm_client
from shared.influx_client import InfluxClient
from shared.decision_logger import log_intent, save_subintents


class L2ManagerAgent(BaseAgent):
    def __init__(self, shared_state, llm_type='claude', skip_init=False):
        super().__init__('l2_manager', shared_state)
        self.llm_client = create_llm_client(llm_type, agent_type='l2_manager')
        self.llm_type = llm_type
        self.config = L2_MANAGER_CONFIG
        self.last_update_time = 0
        self.high_level_intent = None
        self.skip_init = skip_init
        self.influx = InfluxClient(**INFLUX_CONFIG)

    async def work_loop(self):
        """Main work loop for L2 Manager"""
        # Get the high-level intent on startup
        self.high_level_intent = self.state.get_intent('l2_manager')
        if not self.high_level_intent:
            print("[L2 Manager] No high-level intent found. Setting default.")
            self.high_level_intent = self.config.get('default_intent',
                "Optimize the network for both MTC and eMBB users, ensuring MTC devices have sufficient bandwidth for their needs while maximizing eMBB throughput.")
            self.state.set_intent('l2_manager', self.high_level_intent)

        # Track whether decomposition has succeeded at least once
        self.decomposition_done = False

        # Initial intent breakdown (skip if sub-intents were pre-loaded)
        if self.skip_init:
            print("[L2 Manager] Skipping initial decomposition (sub-intents pre-loaded)")
            self.decomposition_done = True
        else:
            self.decomposition_done = await self.update_sub_intents()
            if not self.decomposition_done:
                print("[L2 Manager] Initial decomposition failed, will retry...")

        while self.running:
            try:
                # Check for high-level intent changes
                current_intent = self.state.get_intent('l2_manager')
                if current_intent != self.high_level_intent:
                    print(f"[L2 Manager] High-level intent changed. Updating sub-intents.")
                    self.high_level_intent = current_intent
                    self.decomposition_done = await self.update_sub_intents()
                    self.last_update_time = asyncio.get_event_loop().time()

                # Retry decomposition if it hasn't succeeded yet
                if not self.decomposition_done:
                    print("[L2 Manager] Retrying intent decomposition...")
                    self.decomposition_done = await self.update_sub_intents()
                    if self.decomposition_done:
                        print("[L2 Manager] Decomposition succeeded on retry")
                    else:
                        await asyncio.sleep(5)  # back off before next retry

                await asyncio.sleep(1)

            except Exception as e:
                print(f"[L2 Manager Error] Error in work loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(1)

    async def update_sub_intents(self):
        """Update the sub-intents for Power, Scheduler, and PRB agents.

        Returns True if decomposition succeeded, False otherwise.
        """
        prompt = f"""You need to break down the following user intent into three simple intents for three specialized agents:

User Intent: {self.high_level_intent}

The three agents are:
1. Power Control Agent: Controls the target SNR of the uplink. Higher power means more battery consumption but better channel quality.
2. Scheduler Agent: Controls the maximum throughput limit for each class of UE (MTC and eMBB/FWA).
3. PRB Blocking Agent: Controls per-UE PRB (Physical Resource Block) blocking to preemptively avoid causing interference to neighboring cells. It can block or unblock overlap PRB ranges for individual UEs based on their RSRP patterns.

Break down the user intent into three simple intents, one for each agent. The agents are aware of their own capabilities, so just focus on what the user wants without detailing specific actions.

Keep it simple and concise. All agents should be aware of the QoS requirements.

Provide your response in this format:
<power_intent>
[Intent for power control agent]
</power_intent>

<scheduler_intent>
[Intent for scheduler agent]
</scheduler_intent>

<prb_intent>
[Intent for PRB blocking agent]
</prb_intent>

If spectral efficiency or battery saving is required, you need to set it in the power control agent as the limits don't have an influence on those.
Low battery usage can be reached with low TX power
High spectral efficiency is reached via high tx power.
Make sure to give the agents per-class instruction so the information is not lost!
If the user does not mention interference management, the PRB blocking agent should default to preemptively blocking PRBs for UEs that risk causing interference to neighboring cells based on their RSRP patterns.
"""

        # Get LLM response
        try:
            llm_response = await self.llm_client.get_completion(prompt)

            if not llm_response:
                print("[L2 Manager] No response from LLM")
                return False

            # Parse the response
            power_match = re.search(r'<power_intent>(.*?)</power_intent>', llm_response, re.DOTALL)
            scheduler_match = re.search(r'<scheduler_intent>(.*?)</scheduler_intent>', llm_response, re.DOTALL)
            prb_match = re.search(r'<prb_intent>(.*?)</prb_intent>', llm_response, re.DOTALL)

            if power_match and scheduler_match:
                new_power_intent = power_match.group(1).strip()
                new_scheduler_intent = scheduler_match.group(1).strip()
                new_prb_intent = prb_match.group(1).strip() if prb_match else (
                    "Preemptively block PRBs for UEs that risk causing interference "
                    "to neighboring cells based on their RSRP patterns."
                )

                # Store intents directly in shared state
                self.state.set_intent('power', new_power_intent)
                self.state.set_intent('scheduler', new_scheduler_intent)
                self.state.set_intent('prb', new_prb_intent)

                # Log the update
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                print(f"\n[L2 Manager] Updated sub-intents:")
                print(f"  Power Intent: {new_power_intent}")
                print(f"  Scheduler Intent: {new_scheduler_intent}")
                print(f"  PRB Intent: {new_prb_intent}")

                # Store in history
                self.state.push_history('l2_manager:decisions', {
                    'timestamp': timestamp,
                    'high_level_intent': self.high_level_intent,
                    'power_intent': new_power_intent,
                    'scheduler_intent': new_scheduler_intent,
                    'prb_intent': new_prb_intent,
                })

                # Save sub-intents to JSON for Flask UI
                save_subintents({
                    'power': new_power_intent,
                    'scheduler': new_scheduler_intent,
                    'prb': new_prb_intent,
                })

                # Log intents to InfluxDB
                await log_intent(self.influx, 'power', new_power_intent, self.high_level_intent)
                await log_intent(self.influx, 'scheduler', new_scheduler_intent, self.high_level_intent)
                await log_intent(self.influx, 'prb', new_prb_intent, self.high_level_intent)

                return True

            else:
                print("[L2 Manager Error] Failed to parse LLM response for intents")
                print(f"[L2 Manager Error] Raw response: {llm_response[:500]}")
                return False

        except Exception as e:
            print(f"[L2 Manager Error] Error updating sub-intents: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def handle_command(self, command):
        """Handle commands from management agent"""
        if command['type'] == 'force_update':
            # Force an immediate update of sub-intents
            await self.update_sub_intents()
            self.last_update_time = asyncio.get_event_loop().time()
        else:
            await super().handle_command(command)
