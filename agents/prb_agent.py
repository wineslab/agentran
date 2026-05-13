"""PRB Blocking Agent — skeleton / template.

This agent is NOT wired into the Docker Compose deployment.  It is kept as
a reference showing how to:
  1. Read per-UE RSRP and allocation data from InfluxDB.
  2. Issue per-UE PRB blocking/unblocking commands via the MCP tool interface.

The agent manages per-UE PRB masks to preemptively avoid causing interference
to neighboring cells whose frequency bands partially overlap with ours.

To make it operational, implement ``make_decision()`` with your own policy
logic and add the service to ``deploy/docker-compose.yml``.
"""

import asyncio
import os
import json
from datetime import datetime
from agents.base_agent import BaseAgent
from fastmcp import Client
from shared.config import PRB_AGENT_CONFIG, INFLUX_CONFIG
from shared.influx_client import InfluxClient
from shared.influx_aggregations import MetricAggregator


class PRBBlockingAgent(BaseAgent):
    def __init__(self, shared_state, llm_type='fine-tuned'):
        super().__init__('prb', shared_state)
        self.client = Client("mcp_local_UL.py")
        self.llm_type = llm_type
        self.last_decision_time = 0
        self.config = PRB_AGENT_CONFIG

        self.influx = InfluxClient(**INFLUX_CONFIG)
        self.agg = MetricAggregator(self.influx)

        # Track current blocking state: {rnti: {cell_key: True/False}}
        self.current_blocks = {}
        # Cache rnti -> uid mapping
        self._rnti_to_uid = {}

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def work_loop(self):
        async with self.client:
            await self.client.ping()

            # Discover overlap PRB ranges from InfluxDB (falls back to config defaults)
            await self._discover_overlap_ranges()

            while self.running:
                try:
                    if self.state.get_status(self.agent_type) == 'paused':
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # ── 1. Rate-limit decisions ──────────────────────
                    now = asyncio.get_event_loop().time()
                    if now - self.last_decision_time < self.config['min_decision_interval']:
                        await asyncio.sleep(self.config['min_decision_interval'] - (now - self.last_decision_time))
                        continue

                    # ── 2. Get intent from L2 Manager ────────────────
                    intent = self.state.get_intent(self.agent_type)
                    if not intent:
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # ── 3. Pull per-UE state from InfluxDB ───────────
                    state = await self.agg.query_prb_agent_state(
                        window_seconds=self.config['rsrp_window_seconds'],
                        resolution_ms=self.config['rsrp_resolution_ms'],
                    )
                    if not state or not state.get('ues'):
                        await asyncio.sleep(self.config['state_check_interval'])
                        continue

                    # Prune stale / reconnected UEs
                    self._prune_stale_ues(state)

                    # ── 4. Make & apply decision ─────────────────────
                    policy = await self.make_decision(state, intent)
                    if policy is not None:
                        await self._apply_policy(policy, state)
                        self.last_decision_time = now

                except Exception as e:
                    print(f"[PRB Agent Error] {e}")
                    import traceback
                    traceback.print_exc()

                await asyncio.sleep(self.config['state_check_interval'])

    # ------------------------------------------------------------------
    # Decision — implement your policy here
    # ------------------------------------------------------------------

    async def make_decision(self, state, intent):
        """Return a policy dict or None.

        ``state`` is the output of ``MetricAggregator.query_prb_agent_state()``
        containing per-UE RSRP, SNR targets, allocated PRBs, and per-UE
        serving-cell RSRP timeseries.

        Expected return format::

            {
                "05c4": {"cell2": "block", "cell3": "clear"},
                "1a3f": {"cell2": "clear"},
            }

        Keys are lowercase RNTI hex strings; values map neighbor cell keys
        (from ``config['neighbor_prb_map']``) to ``"block"`` or ``"clear"``.

        The neighbor-to-PRB overlap mapping is in ``self.config['neighbor_prb_map']``::

            {
                'cell2': {'start_prb': 0, 'num_prbs': 10, 'label': 'neighbor low edge'},
                'cell3': {'start_prb': 41, 'num_prbs': 10, 'label': 'neighbor high edge'},
            }

        Available per-UE data in ``state['ues'][rnti]``:
          - ``serving_rsrp``: latest RSRP on the serving cell (dBm)
          - ``target_snrx10``: current SNR target (dB × 10)
          - ``bwp_size``, ``allocated_rb_start``, ``allocated_rbs``

        Per-UE RSRP timeseries in ``state['per_ue_rsrp'][rnti]``:
          - ``ch0``, ``ch1``: list of ``{'time': ..., 'power': dBm}`` dicts
        """
        # TODO: implement your decision logic here
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _discover_overlap_ranges(self):
        """Query InfluxDB for actual overlap PRB ranges from the RSRP monitor."""
        try:
            ranges = await self.agg.query_overlap_prb_ranges(lookback_seconds=30)
            if ranges:
                self.config['neighbor_prb_map'].update(ranges)
                for cell_key, mapping in ranges.items():
                    end = mapping['start_prb'] + mapping['num_prbs'] - 1
                    print(f"[PRB Agent] Discovered {cell_key} overlap: PRBs [{mapping['start_prb']}-{end}]")
            else:
                print("[PRB Agent] No edge data in InfluxDB, using default overlap config")
        except Exception as e:
            print(f"[PRB Agent] Failed to discover overlap ranges: {e}, using defaults")

    def _prune_stale_ues(self, state):
        """Remove stale UEs and reset state when uid changes (reconnect)."""
        active = set(state.get('ues', {}).keys())
        for stale in set(self.current_blocks.keys()) - active:
            del self.current_blocks[stale]
            self._rnti_to_uid.pop(stale, None)

        for rnti in active:
            new_uid = state['ues'][rnti].get('uid')
            old_uid = self._rnti_to_uid.get(rnti)
            if old_uid is not None and new_uid != old_uid:
                print(f"[PRB Agent] UE {rnti} uid changed {old_uid} -> {new_uid}, resetting")
                self.current_blocks.pop(rnti, None)
                self._rnti_to_uid.pop(rnti, None)

    async def _apply_policy(self, policy, state):
        """Apply blocking decisions via MCP tools."""
        valid_ues = set(state['ues'].keys())
        neighbor_map = self.config['neighbor_prb_map']

        for rnti, channels in policy.items():
            rnti = rnti.lower()
            if rnti not in valid_ues:
                continue

            uid = state['ues'][rnti].get('uid')
            if uid is None:
                continue
            self._rnti_to_uid[rnti] = uid

            if rnti not in self.current_blocks:
                self.current_blocks[rnti] = {}

            for cell_key, action in channels.items():
                if cell_key not in neighbor_map:
                    continue
                mapping = neighbor_map[cell_key]
                was_blocked = self.current_blocks[rnti].get(cell_key, False)

                if action == "block" and not was_blocked:
                    try:
                        await self.client.call_tool("prbmask_ue_block", {
                            "uid": uid,
                            "start_prb": mapping['start_prb'],
                            "num_prbs": mapping['num_prbs'],
                        })
                        self.current_blocks[rnti][cell_key] = True
                        print(f"[PRB Agent] BLOCK {cell_key} PRBs [{mapping['start_prb']}-{mapping['start_prb']+mapping['num_prbs']-1}] for {rnti}")
                    except Exception as e:
                        print(f"[PRB Agent Error] block for {rnti}: {e}")

                elif action == "clear" and was_blocked:
                    try:
                        await self.client.call_tool("prbmask_ue_unblock", {
                            "uid": uid,
                            "start_prb": mapping['start_prb'],
                            "num_prbs": mapping['num_prbs'],
                        })
                        self.current_blocks[rnti][cell_key] = False
                        print(f"[PRB Agent] CLEAR {cell_key} PRBs for {rnti}")
                    except Exception as e:
                        print(f"[PRB Agent Error] unblock for {rnti}: {e}")

    async def handle_command(self, command):
        if command['type'] == 'clear_all_blocks':
            for rnti, blocks in self.current_blocks.items():
                for cell_key, is_blocked in blocks.items():
                    if is_blocked:
                        uid = self._rnti_to_uid.get(rnti)
                        if uid is not None:
                            try:
                                await self.client.call_tool("prbmask_ue_clear", {"uid": uid})
                            except Exception:
                                pass
            self.current_blocks.clear()
            print("[PRB Agent] All blocks cleared")
        else:
            await super().handle_command(command)
