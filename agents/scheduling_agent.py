import asyncio
import os
import re
import json
import pathlib
from datetime import datetime
from agents.base_agent import BaseAgent
from fastmcp import Client
from shared.config import SCHEDULER_AGENT_CONFIG, INFLUX_CONFIG
from shared.llm_client import create_llm_client
from shared.influx_client import InfluxClient
from shared.influx_aggregations import MetricAggregator
from shared.decision_logger import log_decision


class SchedulerControlAgent(BaseAgent):
    def __init__(self, shared_state, llm_type='claude'):
        super().__init__('scheduler', shared_state)
        # Write gNB API URL to file so the MCP subprocess can read it
        # (fastmcp doesn't inherit parent env vars)
        gnb_url = os.environ.get("GNB_API_URL", "")
        if gnb_url:
            pathlib.Path(".gnb_api_url").write_text(gnb_url)
        self.client = Client("mcp_local_UL.py")
        self.llm_client = create_llm_client(llm_type, agent_type='scheduler')
        self.llm_type = llm_type
        self.last_decision_time = 0
        self.config = SCHEDULER_AGENT_CONFIG
        self.initial_setup_done = False
        self.current_fwa_limit = SCHEDULER_AGENT_CONFIG['default_fwa_limit']
        self.current_mtc_limit = SCHEDULER_AGENT_CONFIG['default_mtc_limit']

        # InfluxDB for metrics reads and decision logging
        self.influx = InfluxClient(**INFLUX_CONFIG)
        self.agg = MetricAggregator(self.influx)

    async def work_loop(self):
        async with self.client:
            await self.client.ping()
            print(f"[Scheduler Agent] Started with config: {self.config}")

            while self.running:
                try:
                    # Check if paused
                    if self.state.get_status(self.agent_type) == 'paused':
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # Get current network state from InfluxDB
                    network_state = await self.agg.query_ue_summary()
                    if not network_state or network_state.get('status') != 'success':
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # INITIAL SETUP: Apply default scheduler limits on first run
                    if not self.initial_setup_done:
                        await self.apply_initial_defaults()
                        self.initial_setup_done = True
                        await asyncio.sleep(self.config['min_decision_interval'])
                        continue

                    # Check minimum time between decisions
                    current_time = asyncio.get_event_loop().time()
                    time_since_last_decision = current_time - self.last_decision_time
                    if time_since_last_decision < self.config['min_decision_interval']:
                        wait_time = self.config['min_decision_interval'] - time_since_last_decision
                        await asyncio.sleep(wait_time)
                        continue

                    # Get current intent
                    intent = self.state.get_intent(self.agent_type)
                    if not intent:
                        print("[Scheduler Agent] No intent set, waiting...")
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # Make decision
                    decision = await self.make_decision(network_state, intent)
                    if decision:
                        await self.apply_decision(decision, network_state)
                        self.last_decision_time = current_time
                    else:
                        print("[Scheduler Agent] No decision made for this state")

                except Exception as e:
                    print(f"[Scheduler Agent] Error in work loop: {e}")
                    import traceback
                    traceback.print_exc()

                await asyncio.sleep(self.config['state_check_interval'])

    async def apply_initial_defaults(self):
        """Apply default scheduler limits on startup"""
        print(f"\n[Scheduler Agent] ========== INITIAL SETUP ==========")
        print(f"[Scheduler Agent] Setting default scheduler limits: eMBB={self.config['default_fwa_limit']}mbps, MTC={self.config['default_mtc_limit']}mbps")

        try:
            result = await self.client.call_tool("update_scheduler_config", {
                "fwa_value": self.config['default_fwa_limit'],
                "mtc_value": self.config['default_mtc_limit'],
                "reasoning": "Initial setup - setting default scheduler limits"
            })

            result_text = result.content[0].text if result.content else "No response"
            print(f"  Result: {result_text}")

            # Store in history as initial setup
            self.state.push_history('scheduler:decisions', {
                'fwa_limit': self.config['default_fwa_limit'],
                'mtc_limit': self.config['default_mtc_limit'],
                'result': result_text,
                'type': 'initial_setup'
            })

            # Update current limits
            self.current_fwa_limit = self.config['default_fwa_limit']
            self.current_mtc_limit = self.config['default_mtc_limit']

            # Store current limits in shared state (power agent reads these)
            self.state.set('scheduler:fwa_limit', self.current_fwa_limit)
            self.state.set('scheduler:mtc_limit', self.current_mtc_limit)

            # Log to InfluxDB + JSON
            await log_decision(self.influx, 'scheduler', {
                'fwa_limit': self.current_fwa_limit,
                'mtc_limit': self.current_mtc_limit,
                'action': 'initial_setup',
            })

        except Exception as e:
            print(f"Error setting initial scheduler limits: {e}")

        print(f"[Scheduler Agent] Initial setup complete")
        print(f"[Scheduler Agent] ===================================\n")
        self.last_decision_time = asyncio.get_event_loop().time()

    def extract_network_summary(self, network_state):
        """Extract summary metrics from network analysis result"""
        try:
            summary = network_state.get('summary', {})
            analysis = network_state.get('analysis', {})

            mtc_ues = analysis.get('mtc_ues', {})
            fwa_ues = analysis.get('fwa_ues', {})

            mtc_throughputs = [ue_data.get('throughput', 0) for ue_data in mtc_ues.values()]
            avg_mtc_throughput = sum(mtc_throughputs) / len(mtc_throughputs) if mtc_throughputs else 0
            min_mtc_throughput = min(mtc_throughputs) if mtc_throughputs else 0
            max_mtc_throughput = max(mtc_throughputs) if mtc_throughputs else 0

            fwa_throughputs = [ue_data.get('throughput', 0) for ue_data in fwa_ues.values()]
            avg_fwa_throughput = sum(fwa_throughputs) / len(fwa_throughputs) if fwa_throughputs else 0
            min_fwa_throughput = min(fwa_throughputs) if fwa_throughputs else 0
            max_fwa_throughput = max(fwa_throughputs) if fwa_throughputs else 0

            return {
                'mtc_count': summary.get('mtc_count', 0),
                'fwa_count': summary.get('fwa_count', 0),
                'fwa_activity': summary.get('fwa_activity_mbps', 0),
                'avg_mtc_throughput': round(avg_mtc_throughput, 1),
                'avg_fwa_throughput': round(avg_fwa_throughput, 1),
                'min_mtc_throughput': round(min_mtc_throughput, 1),
                'max_mtc_throughput': round(max_mtc_throughput, 1),
                'min_fwa_throughput': round(min_fwa_throughput, 1),
                'max_fwa_throughput': round(max_fwa_throughput, 1)
            }
        except Exception as e:
            print(f"Error extracting network summary: {e}")
            return {}

    def get_agent_history(self):
        """Get history from shared state"""
        return self.state.get_history('scheduler:decisions', limit=100)

    def get_current_limits(self):
        """Get current scheduler limits from shared state or use instance values"""
        fwa_limit = self.state.get('scheduler:fwa_limit')
        mtc_limit = self.state.get('scheduler:mtc_limit')

        if fwa_limit is not None:
            self.current_fwa_limit = int(fwa_limit)
        if mtc_limit is not None:
            self.current_mtc_limit = int(mtc_limit)

        return self.current_fwa_limit, self.current_mtc_limit

    def get_history_for_prompt(self):
        """Format history for LLM prompt"""
        history = self.get_agent_history()

        # Get current limits
        fwa_limit, mtc_limit = self.get_current_limits()

        if not history:
            return f"No previous decisions recorded. Current limits: eMBB={fwa_limit}mbps, MTC={mtc_limit}mbps."

        # Filter out initial setup entries
        history = [h for h in history if h.get('type') != 'initial_setup']

        if not history:
            return f"No previous decisions recorded. Current limits: eMBB={fwa_limit}mbps, MTC={mtc_limit}mbps (defaults)."

        history_text = "=== RECENT SCHEDULER DECISIONS (most recent first) ===\n"

        # Show last 10 decisions
        for i, entry in enumerate(history[:10]):
            timestamp = entry.get('timestamp', 'unknown time')

            # Format timestamp to relative time
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                time_ago = datetime.now() - dt
                if time_ago.total_seconds() < 60:
                    time_str = f"{int(time_ago.total_seconds())}s ago"
                elif time_ago.total_seconds() < 3600:
                    time_str = f"{int(time_ago.total_seconds() / 60)}m ago"
                else:
                    time_str = f"{int(time_ago.total_seconds() / 3600)}h ago"
            except:
                time_str = timestamp

            # Get limit changes
            prev_fwa = entry.get('previous_fwa_limit', 'unknown')
            prev_mtc = entry.get('previous_mtc_limit', 'unknown')
            new_fwa = entry.get('fwa_limit')
            new_mtc = entry.get('mtc_limit')

            # Get throughput info from network summary
            summary = entry.get('network_summary', {})
            avg_fwa_throughput = summary.get('avg_fwa_throughput', 'N/A')
            avg_mtc_throughput = summary.get('avg_mtc_throughput', 'N/A')
            min_fwa_throughput = summary.get('min_fwa_throughput', 'N/A')
            max_fwa_throughput = summary.get('max_fwa_throughput', 'N/A')
            min_mtc_throughput = summary.get('min_mtc_throughput', 'N/A')
            max_mtc_throughput = summary.get('max_mtc_throughput', 'N/A')
            fwa_activity = summary.get('fwa_activity_mbps', 'N/A')
            fwa_count = summary.get('fwa_count', 0)
            mtc_count = summary.get('mtc_count', 0)

            history_text += f"[{time_str}] Changed limits: eMBB {prev_fwa}->{new_fwa}mbps, MTC {prev_mtc}->{new_mtc}mbps\n"

            # Format throughput achieved based on UE counts
            if fwa_count > 0:
                fwa_throughput_str = f"avg={avg_fwa_throughput} (min={min_fwa_throughput}, max={max_fwa_throughput})mbps, activity={fwa_activity}mbps"
            else:
                fwa_throughput_str = "no eMBB UEs"

            if mtc_count > 0:
                mtc_throughput_str = f"avg={avg_mtc_throughput} (min={min_mtc_throughput}, max={max_mtc_throughput})mbps"
            else:
                mtc_throughput_str = "no MTC UEs"

            history_text += f"  Throughput achieved: eMBB {fwa_throughput_str}, MTC {mtc_throughput_str}\n"

        history_text += f"\nCurrent limits: eMBB={fwa_limit}mbps, MTC={mtc_limit}mbps\n"

        return history_text

    def make_prompt(self, network_state, intent):
        """Create prompt for LLM"""
        history_section = self.get_history_for_prompt()

        # Format network state from structured data
        analysis = network_state.get('analysis', {})
        summary = network_state.get('summary', {})

        network_info = f"Network Summary:\n"
        network_info += f"- MTC UEs: {summary.get('mtc_count', 0)}\n"
        network_info += f"- eMBB UEs: {summary.get('fwa_count', 0)}\n"
        network_info += f"- eMBB Activity: {summary.get('fwa_activity_mbps', 0)} Mbps\n"
        network_info += f"- Average MTC Throughput: {summary.get('avg_mtc_throughput', 0)} Mbps\n"
        network_info += f"- Average eMBB Throughput: {summary.get('avg_fwa_throughput', 0)} Mbps\n\n"

        # Add UE details if needed
        if analysis.get('mtc_ues'):
            network_info += "MTC UEs:\n"
            for rnti, ue_data in analysis['mtc_ues'].items():
                network_info += f"  {rnti}: Throughput={ue_data['throughput']}Mbps\n"

        if analysis.get('fwa_ues'):
            network_info += "\neMBB UEs:\n"
            for rnti, ue_data in analysis['fwa_ues'].items():
                network_info += f"  {rnti}: Throughput={ue_data['throughput']}Mbps\n"

        prompt = f"""You are a 5G scheduling agent that sets throughput limits for UE classes (MTC and eMBB/FWA).

RULES:
- Output a JSON dict with integer 'embb' and 'mtc' keys between <policy></policy> tags.
- Values must be integers between {self.config['min_limit']} and {self.config['max_limit']} (Mbps).
- Follow the intent EXACTLY. If it says specific values, use those exact values.

INTENT: {intent}

CURRENT LIMITS: eMBB={self.current_fwa_limit}mbps, MTC={self.current_mtc_limit}mbps

{history_section}

NETWORK STATE:
{network_info}

Respond with ONLY the policy tags. Example: <policy>{{"embb": 3, "mtc": 3}}</policy>"""

        return prompt

    async def make_decision(self, network_state, intent):
        """Make scheduler decision using LLM"""
        prompt = self.make_prompt(network_state, intent)

        # Use the shared LLM client
        llm_response = await self.llm_client.get_completion(prompt)
        if not llm_response:
            return None

        # Extract policy from response
        policy_match = re.search(r'<policy>(.*?)</policy>', llm_response, re.DOTALL)
        if policy_match:
            try:
                policy = json.loads(policy_match.group(1).strip())
                if 'embb' in policy and 'mtc' in policy:
                    print(f"We have a policy: {policy}")
                    return policy
                else:
                    print("[Scheduler Agent] Policy missing 'embb' or 'mtc' keys")
                    return None
            except json.JSONDecodeError as e:
                print(f"Failed to parse policy JSON: {e}")
                return None

        return None

    async def apply_decision(self, policy, network_state):
        """Apply scheduler decisions"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            raw_fwa = policy['embb']
            raw_mtc = policy['mtc']

            # Handle nested dict responses like {"embb": {"limit": 19}}
            if isinstance(raw_fwa, dict):
                raw_fwa = raw_fwa.get('limit', raw_fwa.get('value', self.current_fwa_limit))
            if isinstance(raw_mtc, dict):
                raw_mtc = raw_mtc.get('limit', raw_mtc.get('value', self.current_mtc_limit))

            # Cast to int and clip to valid range
            new_fwa_limit = int(max(self.config['min_limit'],
                            min(self.config['max_limit'], float(raw_fwa))))
            new_mtc_limit = int(max(self.config['min_limit'],
                            min(self.config['max_limit'], float(raw_mtc))))

            # Get current limits
            current_fwa, current_mtc = self.get_current_limits()

            # Extract network summary for metrics
            network_summary = self.extract_network_summary(network_state)

            # Only update if there's a change
            if new_fwa_limit != current_fwa or new_mtc_limit != current_mtc:
                print(f"[Scheduler Agent] Updating scheduler config: eMBB {current_fwa}->{new_fwa_limit}mbps, MTC {current_mtc}->{new_mtc_limit}mbps")

                # Apply the new configuration via MCP
                reasoning = f"Adjusting limits based on network state: eMBB {current_fwa}->{new_fwa_limit}mbps, MTC {current_mtc}->{new_mtc_limit}mbps"
                result = await self.client.call_tool("update_scheduler_config", {
                    "fwa_value": new_fwa_limit,
                    "mtc_value": new_mtc_limit,
                    "reasoning": reasoning
                })

                result_text = result.content[0].text if result.content else "No response"
                print(f"  Result: {result_text}")

                # Store decision in history
                self.state.push_history('scheduler:decisions', {
                    'timestamp': timestamp,
                    'fwa_limit': new_fwa_limit,
                    'mtc_limit': new_mtc_limit,
                    'network_summary': network_summary,
                    'result': result_text,
                    'previous_fwa_limit': current_fwa,
                    'previous_mtc_limit': current_mtc
                })

                # Update current limits
                self.current_fwa_limit = new_fwa_limit
                self.current_mtc_limit = new_mtc_limit

                # Store current limits in shared state (power agent reads these)
                self.state.set('scheduler:fwa_limit', new_fwa_limit)
                self.state.set('scheduler:mtc_limit', new_mtc_limit)

                # Log to InfluxDB + JSON
                await log_decision(self.influx, 'scheduler', {
                    'fwa_limit': new_fwa_limit,
                    'mtc_limit': new_mtc_limit,
                    'previous_fwa_limit': current_fwa,
                    'previous_mtc_limit': current_mtc,
                    'action': 'decision',
                })
            else:
                print("[Scheduler Agent] No scheduler changes needed - limits remain the same")
                await log_decision(self.influx, 'scheduler', {
                    'fwa_limit': current_fwa,
                    'mtc_limit': current_mtc,
                    'action': 'no_change',
                })

        except Exception as e:
            print(f"Error applying scheduler config: {e}")

    async def handle_command(self, command):
        """Handle commands from management agent"""
        if command['type'] == 'reset_defaults':
            self.initial_setup_done = False
            print(f"[Scheduler Agent] Will reset to defaults on next cycle")
        else:
            # Call parent class handler for standard commands
            await super().handle_command(command)
