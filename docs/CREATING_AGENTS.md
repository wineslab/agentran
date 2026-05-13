# Creating a new agent

This guide walks through adding a new control agent end-to-end. We use a
hypothetical **BeamAgent** that picks beamforming weights as a running example.

The codebase has four agents today (L2 Manager, Power, Scheduler, PRB). Read
[`agents/scheduling_agent.py`](../agents/scheduling_agent.py) alongside this
guide — it is the most representative.

---

## 1. Anatomy of an agent

Every agent extends [`BaseAgent`](../agents/base_agent.py):

```python
class BaseAgent(ABC):
    def __init__(self, agent_type, shared_state):
        self.agent_type = agent_type   # 'power', 'scheduler', 'prb', ...
        self.state = shared_state      # SharedState instance
        self.running = True

    async def start(self):
        await self.work_loop()

    async def handle_command(self, command):
        # 'set_intent' / 'pause' / 'resume' / 'clear_history'
        ...

    @abstractmethod
    async def work_loop(self):
        pass
```

Concrete agents implement `work_loop` and follow this shape:

```python
async def work_loop(self):
    async with self.client:                      # FastMCP client (gNB control)
        await self.client.ping()
        while self.running:
            try:
                if self.state.get_status(self.agent_type) == 'paused':
                    await asyncio.sleep(self.config['state_check_interval'])
                    continue

                # 1. Rate limit
                if not self._enough_time_passed():
                    await asyncio.sleep(...)
                    continue

                # 2. Read current network state from InfluxDB
                state = await self.agg.query_<your_metric>(...)

                # 3. Read current intent for this agent
                intent = self.state.get_intent(self.agent_type)
                if not intent: continue

                # 4. Ask the LLM for a decision
                decision = await self.make_decision(state, intent)

                # 5. Apply via MCP tool
                if decision:
                    await self.apply_decision(decision, state)
                    self.last_decision_time = current_time

            except Exception as e:
                print(f"[Beam Agent] Error: {e}")

            await asyncio.sleep(self.config['state_check_interval'])
```

The pattern matters: **status check → rate limit → read state → read intent →
decide → apply**. The existing agents and the L2 Manager assume this loop
shape.

---

## 2. Step-by-step

### 2a. Add a config block

Edit [`shared/config.py`](../shared/config.py):

```python
BEAM_AGENT_CONFIG = {
    'min_decision_interval': 1.0,    # seconds between LLM-driven decisions
    'state_check_interval': 0.1,     # poll cadence for status / intent
    'default_beam_id': 0,
    # any agent-specific knobs
}
```

If your agent reads tuning knobs from the environment (like the scheduler
reads `SCHED_MIN_LIMIT`), add them here with `os.environ.get()`:

```python
'min_beam_id': int(os.environ.get('BEAM_MIN_ID', '0')),
```

### 2b. Create the agent module

Create `agents/beam_agent.py`:

```python
import asyncio
from agents.base_agent import BaseAgent
from fastmcp import Client
from shared.config import BEAM_AGENT_CONFIG, INFLUX_CONFIG
from shared.llm_client import create_llm_client
from shared.influx_client import InfluxClient
from shared.influx_aggregations import MetricAggregator
from shared.decision_logger import log_decision


class BeamAgent(BaseAgent):
    def __init__(self, shared_state, llm_type='vllm'):
        super().__init__('beam', shared_state)
        self.client = Client("mcp_local_UL.py")
        self.llm_client = create_llm_client(llm_type, agent_type='beam')
        self.llm_type = llm_type
        self.config = BEAM_AGENT_CONFIG
        self.last_decision_time = 0
        self.influx = InfluxClient(**INFLUX_CONFIG)
        self.agg = MetricAggregator(self.influx)

    async def work_loop(self):
        ...   # see Section 1

    async def make_decision(self, state, intent):
        prompt = self._build_prompt(state, intent)
        response = await self.llm_client.get_completion_with_retry(prompt)
        return self._parse_decision(response)

    async def apply_decision(self, decision, state):
        result = await self.client.call_tool("set_beam_weights", {
            "uid": decision['uid'],
            "beam_id": decision['beam_id'],
            "reasoning": decision.get('reasoning', ''),
        })
        await log_decision(self.influx, 'beam', decision)
```

If your agent needs a new gNB control action, add an MCP tool first
(Section 4) before referencing it in `apply_decision`.

### 2c. Create a standalone runner

Each agent runs in its own container. Create `run_beam.py` at the repo root.
Use [`run_scheduler.py`](../run_scheduler.py) as a template:

```python
"""Standalone Beam Agent runner.

Reads its intent from SHARED_STATE_DIR/subintents.json (written by the
L2 Manager) or falls back to the BEAM_INTENT env var.
"""
import asyncio
import json
import os
import sys
import signal

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from shared.state import SharedState
from agents.beam_agent import BeamAgent

SHARED_STATE_DIR = os.environ.get(
    'SHARED_STATE_DIR', os.path.dirname(os.path.abspath(__file__))
)
SUBINTENTS_PATH = os.path.join(SHARED_STATE_DIR, 'subintents.json')
POLL_INTERVAL = 5  # seconds between intent-file checks


def read_intent_from_file() -> str | None:
    try:
        with open(SUBINTENTS_PATH, 'r') as f:
            data = json.load(f)
        return data.get('beam')            # <-- key must match L2 Manager output
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


async def wait_for_intent(timeout: float = 300) -> str:
    fallback = os.environ.get('BEAM_INTENT', '')
    elapsed = 0
    while elapsed < timeout:
        intent = read_intent_from_file()
        if intent:
            return intent
        if elapsed % 10 == 0:
            print(f"[Beam Runner] Waiting for {SUBINTENTS_PATH} ...")
        await asyncio.sleep(2)
        elapsed += 2
    if fallback:
        print("[Beam Runner] Timeout, using BEAM_INTENT env var")
        return fallback
    raise RuntimeError(f"No beam intent after {timeout}s")


async def intent_watcher(state: SharedState):
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        new_intent = read_intent_from_file()
        if new_intent and new_intent != state.get_intent('beam'):
            print(f"[Beam Runner] Intent updated: {new_intent[:80]}...")
            state.set_intent('beam', new_intent)


async def main():
    llm_type = os.environ.get('LLM_TYPE', 'vllm')
    intent = await wait_for_intent()
    print(f"[Beam Runner] LLM: {llm_type}")
    print(f"[Beam Runner] Intent: {intent}")

    state = SharedState()
    state.set_intent('beam', intent)
    state.set_status('beam', 'running')

    agent = BeamAgent(state, llm_type=llm_type)

    def _shutdown():
        agent.running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await asyncio.gather(
        agent.start(),
        intent_watcher(state),
    )


if __name__ == "__main__":
    asyncio.run(main())
```

Key points:

- The runner creates its own `SharedState` (in-memory, local to this
  container).
- It reads its intent from `subintents.json`, which the L2 Manager writes to
  the shared Docker volume.
- An `intent_watcher` task polls the file so L2 Manager updates propagate
  at runtime.
- Graceful shutdown via `SIGTERM` (Docker sends this on `docker compose stop`).

### 2d. Add a Docker Compose service

Add your agent to [`deploy/docker-compose.yml`](../deploy/docker-compose.yml).
Follow the pattern of the existing `scheduler-agent` service:

```yaml
  beam-agent:
    container_name: agentran-beam
    build:
      context: ..
      dockerfile: Dockerfile
    environment:
      - INFLUXDB_URL=http://influxdb:8086
      - INFLUXDB_TOKEN=${INFLUXDB_TOKEN:-agentran-dev-token}
      - INFLUXDB_ORG=${INFLUXDB_ORG:-mwc}
      - INFLUXDB_BUCKET=${INFLUXDB_BUCKET:-mwc-live}
      - GNB_API_URL=http://10.71.0.140:8000
      - VLLM_URL=http://ollama:11434
      - VLLM_MODEL=${OLLAMA_MODEL:-gpt-oss:20b}
      - LLM_TYPE=vllm
      - SHARED_STATE_DIR=/app/shared-state
      - PYTHONUNBUFFERED=1
    command: python3 run_beam.py
    volumes:
      - agent-shared-state:/app/shared-state
    depends_on:
      influxdb:
        condition: service_healthy
      oai-gnb:
        condition: service_healthy
      l2-manager:
        condition: service_healthy
    restart: unless-stopped
    networks:
      agentran:
        ipv4_address: 10.71.0.174       # pick an unused IP in the subnet
```

All agent containers share the same `Dockerfile` (Python 3.11-slim) and
the same `agent-shared-state` volume. The `command` override selects which
runner to execute.

### 2e. Teach the L2 Manager about your agent

The L2 Manager decomposes a high-level intent into per-agent sub-intents.
Edit the prompt in
[`agents/l2_manager_agent.py`](../agents/l2_manager_agent.py) so it emits a
`beam` sub-intent alongside the existing `power`, `scheduler`, and `prb`
ones.

Then update [`shared/decision_logger.py`](../shared/decision_logger.py)'s
`save_subintents()` if needed — the L2 Manager writes the dict it produces,
so the new key (`beam`) will appear in `subintents.json` automatically as
long as the prompt asks for it.

---

## 3. Adding a new MCP tool

If your agent needs a control surface that doesn't exist yet, add it to
[`mcp_local_UL.py`](../mcp_local_UL.py):

```python
@mcp.tool()
async def set_beam_weights(uid: int, beam_id: int, reasoning: str = "") -> str:
    """Set the beamforming weight set for a UE.

    Args:
        uid: UE identifier
        beam_id: index into the codebook
        reasoning: free-text rationale (logged on the gNB side)
    """
    payload = {"commands": [{"uid": uid, "beam_id": beam_id, "reasoning": reasoning}]}
    data = await make_api_request("POST", "/api/v1/beam-control", payload)
    if not data:
        return "Unable to send beam control command."
    if data.get("status") == "error":
        return f"Error: {data.get('message')}"
    return "Beam weights set."
```

The corresponding REST endpoint must exist in the gNB's
`UL_control_termination.py` (or equivalent). Tools are auto-discovered by
the FastMCP `Client` — no registration step needed.

**Note on MCP and Docker:** The MCP client runs `mcp_local_UL.py` as a
subprocess. That subprocess does not inherit the parent container's env
vars automatically. The scheduler agent works around this by writing
`GNB_API_URL` to a `.gnb_api_url` file that `mcp_local_UL.py` reads as a
fallback. If your agent also talks to the gNB API via MCP, follow the same
pattern.

---

## 4. Decision logging

Use [`shared/decision_logger.py`](../shared/decision_logger.py)'s
`log_decision()` so decisions land in both a per-agent JSON file (readable
by the Flask UI) and InfluxDB (Grafana panels):

```python
await log_decision(self.influx, 'beam', {
    'uid': decision['uid'],
    'beam_id': decision['beam_id'],
    'reasoning': decision.get('reasoning', ''),
    'timestamp': datetime.now().isoformat(),
})
```

---

## 5. Testing

### In isolation (no Docker)

Mirror [`run_scheduler.py`](../run_scheduler.py) for a quick local test:

```bash
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=agentran-dev-token \
VLLM_URL=http://localhost:11434 \
VLLM_MODEL=gpt-oss:20b \
LLM_TYPE=vllm \
BEAM_INTENT="Maximize SINR for high-priority UEs." \
  python3 run_beam.py
```

This is the fastest way to iterate on your prompt without bringing the full
stack up. The runner will fall back to the `BEAM_INTENT` env var if no
`subintents.json` file exists.

### Single container against the running stack

If the rest of the stack is already running via `docker compose up`:

```bash
cd deploy/
docker compose run --rm \
  -e BEAM_INTENT="Maximize SINR for high-priority UEs." \
  beam-agent
```

This starts only your agent container, connected to the existing network and
shared volume.

---

## 6. Prompt design

A few things that consistently help:

- **Always include the current intent verbatim.** It's set per-agent by the
  L2 Manager and changes between cycles. Don't hardcode goals into the
  system prompt.
- **Show the network state in a structured form** (per-UE summary, recent
  decision history, current limits). The other agents format this as
  human-readable text inside the prompt — see how
  [`agents/scheduling_agent.py`](../agents/scheduling_agent.py) builds its
  prompt.
- **Demand structured output and parse strictly.** Existing agents use XML
  tags (e.g. `<policy>...</policy>`) around a JSON block, then regex to
  extract it. Reject and retry on parse errors rather than letting a
  malformed response slip through.
- **Tell the model the constraints** (e.g. "beam_id must be between 0 and
  15"). The MCP tool also enforces them, but the model picks better values
  when it knows the bounds.
- **Include short recent history** (last 3-10 decisions) so the model can
  notice oscillation or convergence. `state.get_history(...)` is there for
  this.

---

## 7. Common pitfalls

- **Forgetting the rate limit.** Without it, an agent can issue dozens of
  LLM calls per second on a busy network state. Always check
  `last_decision_time` against `min_decision_interval`.
- **Blocking calls inside `work_loop`.** Everything is asyncio. Use
  `aiohttp` / `httpx.AsyncClient` / `await`-able primitives. A blocking
  `requests.get()` will stall the agent's event loop.
- **Caching state across loop iterations.** UEs reconnect, RNTIs get reused.
  Look at [`agents/prb_agent.py`](../agents/prb_agent.py)'s
  `_prune_stale_ues` for the pattern: track UID changes, invalidate cached
  decisions when the identity changes.
- **Logging via `print` only.** It works, but it's not structured. Prefix
  every line with `[Beam Agent]` so logs are greppable in
  `docker compose logs`.
- **Trusting the LLM blindly.** Validate every decision before applying it
  (range checks, sanity checks against the current state). A confidently
  wrong LLM can destabilize the network in seconds.
- **Forgetting the MCP env-var workaround.** The FastMCP subprocess doesn't
  inherit Docker env vars. If your tool needs `GNB_API_URL`, write it to a
  file first (see Section 3).
