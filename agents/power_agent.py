"""Power Control Agent — skeleton / template.

This agent is NOT wired into the Docker Compose deployment.  It is kept as
a reference showing how to:
  1. Read per-UE metrics from InfluxDB (throughput, BLER, RSRP).
  2. Issue per-UE power-control commands via the MCP tool interface.

To make it operational, implement ``make_decision()`` with your own policy
logic (rule-based, LLM-driven, RL, etc.) and add the service to
``deploy/docker-compose.yml``.
"""

import asyncio
from datetime import datetime
from agents.base_agent import BaseAgent
from fastmcp import Client
from shared.config import POWER_AGENT_CONFIG, INFLUX_CONFIG
from shared.influx_client import InfluxClient
from shared.influx_aggregations import MetricAggregator
from shared.decision_logger import log_decision


class PowerControlAgent(BaseAgent):
    def __init__(self, shared_state, llm_type='claude'):
        super().__init__('power', shared_state)
        self.client = Client("mcp_local_UL.py")
        self.llm_type = llm_type
        self.last_decision_time = 0
        self.config = POWER_AGENT_CONFIG
        self.initial_setup_done = False

        # InfluxDB for metrics reads and decision logging
        self.influx = InfluxClient(**INFLUX_CONFIG)
        self.agg = MetricAggregator(self.influx)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def work_loop(self):
        async with self.client:
            await self.client.ping()

            while self.running:
                try:
                    if self.state.get_status(self.agent_type) == 'paused':
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # ── 1. Pull network state from InfluxDB ──────────
                    network_state = await self.agg.query_ue_summary()
                    if not network_state or network_state.get('status') != 'success':
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # ── 2. Initial setup: default power for all UEs ──
                    if not self.initial_setup_done:
                        await self._apply_defaults(network_state)
                        self.initial_setup_done = True
                        await asyncio.sleep(self.config['min_decision_interval'])
                        continue

                    # ── 3. Rate-limit decisions ──────────────────────
                    now = asyncio.get_event_loop().time()
                    if now - self.last_decision_time < self.config['min_decision_interval']:
                        await asyncio.sleep(self.config['min_decision_interval'] - (now - self.last_decision_time))
                        continue

                    # ── 4. Get intent from L2 Manager ────────────────
                    intent = self.state.get_intent(self.agent_type)
                    if not intent:
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # ── 5. Make & apply decision ─────────────────────
                    policy = await self.make_decision(network_state, intent)
                    if policy:
                        await self._apply_policy(policy, network_state)
                        self.last_decision_time = now

                except Exception as e:
                    print(f"[Power Agent Error] {e}")
                    import traceback
                    traceback.print_exc()

                await asyncio.sleep(self.config['state_check_interval'])

    # ------------------------------------------------------------------
    # Decision — implement your policy here
    # ------------------------------------------------------------------

    async def make_decision(self, network_state, intent):
        """Return a policy dict  {rnti: new_snr_target}  or None.

        ``network_state`` is the output of ``MetricAggregator.query_ue_summary()``
        and contains per-UE throughput, BLER, RSRP, fiveQI, etc.

        ``intent`` is the sub-intent string set by the L2 Manager.

        SNR target range: 250 (25 dB) – 350 (35 dB).  Higher = more power =
        better throughput but higher energy consumption.  Steps are clipped to
        ±20 units (2 dB) per decision in ``_apply_policy()``.

        Example — always keep default power (no-op):

            return None

        Example — set all UEs to maximum power:

            return {rnti: 350 for rnti in self._all_ues(network_state)}
        """
        # TODO: implement your decision logic here
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _all_ues(self, network_state):
        """Return set of all RNTI strings from the current network state."""
        analysis = network_state.get('analysis', {})
        ues = set()
        for rnti in analysis.get('mtc_ues', {}):
            ues.add(rnti.lower())
        for rnti in analysis.get('fwa_ues', {}):
            ues.add(rnti.lower())
        return ues

    def _resolve_uid(self, network_state, rnti):
        """Look up the integer uid for a given RNTI."""
        analysis = network_state.get('analysis', {})
        for ue_dict in [analysis.get('mtc_ues', {}), analysis.get('fwa_ues', {})]:
            for key, data in ue_dict.items():
                if key.lower() == rnti.lower():
                    return data.get('uid')
        return None

    def _get_last_snr(self, rnti):
        """Return the last SNR target we set for this UE."""
        history = self.state.get_history(f'power:{rnti}', limit=1)
        if history:
            return history[0].get('snr_target', self.config['default_power'])
        return self.config['default_power']

    async def _apply_defaults(self, network_state):
        """Set every UE to the default SNR target."""
        for rnti in self._all_ues(network_state):
            uid = self._resolve_uid(network_state, rnti)
            if uid is None:
                continue
            try:
                await self.client.call_tool("set_power_control", {
                    "uid": uid,
                    "snr_target": self.config['default_power'],
                    "reasoning": "Initial setup — default power",
                })
                self.state.push_history(f'power:{rnti}', {
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'snr_target': self.config['default_power'],
                    'type': 'initial_setup',
                })
                await log_decision(self.influx, 'power', {
                    'snr_target': self.config['default_power'],
                    'action': 'initial_setup',
                }, tags={'ue': rnti, 'uid': str(uid)})
            except Exception as e:
                print(f"[Power Agent Error] initial setup for {rnti}: {e}")
        self.last_decision_time = asyncio.get_event_loop().time()

    async def _apply_policy(self, policy, network_state):
        """Apply a {rnti: snr_target} policy, with step-size clipping."""
        valid_ues = self._all_ues(network_state)
        for rnti, new_snr in policy.items():
            if rnti.lower() not in valid_ues:
                continue
            uid = self._resolve_uid(network_state, rnti)
            if uid is None:
                continue

            last_snr = self._get_last_snr(rnti)
            # Clip step to ±20 (2 dB)
            step = new_snr - last_snr
            new_snr = last_snr + max(-20, min(20, step))
            new_snr = max(250, min(350, new_snr))

            if new_snr == last_snr:
                continue

            try:
                await self.client.call_tool("set_power_control", {
                    "uid": uid,
                    "snr_target": new_snr,
                    "reasoning": f"Policy decision: {last_snr} -> {new_snr}",
                })
                print(f"[Power Agent] {rnti} (uid {uid}): SNR {last_snr} -> {new_snr}")
                self.state.push_history(f'power:{rnti}', {
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'snr_target': new_snr,
                    'previous_snr_target': last_snr,
                    'type': 'decision',
                })
                await log_decision(self.influx, 'power', {
                    'snr_target': new_snr,
                    'previous_snr_target': last_snr,
                    'action': 'decision',
                }, tags={'ue': rnti, 'uid': str(uid)})
            except Exception as e:
                print(f"[Power Agent Error] applying for {rnti}: {e}")

    async def handle_command(self, command):
        if command['type'] == 'reset_defaults':
            self.initial_setup_done = False
        else:
            await super().handle_command(command)
