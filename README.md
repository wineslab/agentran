# AgentRAN: Multi-Agent Framework for AI-RAN Control

A hierarchical multi-agent system for autonomous 5G RAN control. LLM-powered agents read real-time network KPMs from InfluxDB and issue control actions to a 5G gNBs. The framework is extensible and multiple agents can be composed on top of this infrastructure ([docs/CREATING_AGENTS.md](docs/CREATING_AGENTS.md)).

The current implementation is designed to work with the companion RAN stack fork:
[wineslab/oai-release-agentRAN](https://github.com/wineslab/oai-release-agentRAN) (branch `dl-lua-scheduler`), included here as the `oai/` submodule. Note that this is temporary - changes in the scheduler that are required to exercise the agents are being upstreamed to OAI.

## Roadmap

- [x] Hierarchical multi-agent framework with intent decomposition (L2 Manager → sub-agents)
- [x] Multiple LLM backends: Claude, Qwen (Ollama), fine-tuned LoRA models (vLLM)
- [x] OAI integration: Lua **DL** scheduler with KPM export via named pipe (UL scheduler also available)
- [x] E2-like interface via InfluxDB + Python forwarder/control server
- [x] Per-UE power control, scheduler config (DL throughput limits), PRB blocking
- [x] Runtime intent update UI
- [x] Self-contained Docker Compose deployment of the full 5G SA stack + agents
- [ ] Replace InfluxDB shim with a proper E2 interface
- [ ] Deploy agents as proper xApps on a Near-RT RIC
- [ ] Port to OCUDU and NVIDIA Aerial

## Architecture

```
┌─────────────────────────────────────────────────────┐
│      wineslab/oai-release-agentRAN (OAI fork)       │
│                                                     │
│  gNB + Lua DL scheduler                             │
│       │ /tmp/dl_metrics.pipe                        │
│       ▼                                             │
│  dl_metrics_forwarder.py ──────────► InfluxDB       │
│                                          ▲          │
│  UL_control_termination.py :8000         │          │
│       ▲ REST                             │          │
└───────┼──────────────────────────────────┼──────────┘
        │                                  │
        │         ← E2-like layer →        │
        │   (KPM aggregation + control)    │
        │                                  │
┌───────┼──────────────────────────────────┼──────────┐
│       │    wineslab/multi-agent-commag   │          │
│       │                                  │          │
│  ┌────┴──────────────────────────────────┴────────┐ │
│  │             L2 Manager Agent                   │ │
│  │   decomposes high-level intent into per-agent  │ │
│  │  sub-intents at startup, writes subintents.json│ │
│  └───────────┬──────────────┬────────────┬────────┘ │
│              │              │            │          │
│         Power Agent   Scheduler Agent  PRB Agent    │
│          (example)      (deployed)     (example)    │
│              │              │            │          │
│              └──────────────┴────────────┘          │
│                          │                          │
│                   REST → gNB API                    │
└─────────────────────────────────────────────────────┘
```

There is no formal RIC in this stack (adding a RIC, xApps, and dApps is in the roadmap). Instead, InfluxDB together with the two Python processes in the OAI fork (`dl_metrics_forwarder.py` and `UL_control_termination.py`) collectively play the role of the E2 layer: KPMs are aggregated from the gNB and written to InfluxDB (uplink telemetry), while agent control decisions are logged there too and dispatched to the gNB via the REST API (downlink control). The agents in this repo act as xApp-equivalents consuming that interface.

The Docker Compose deployment in `deploy/` currently runs the **L2 Manager** (one-shot at startup, decomposes intent into sub-intents and exits) and the **Scheduler Agent** (continuous decision loop, drives DL throughput limits). The Power and PRB agents are examples of other `agents/` but are not exercised in the current release. See [docs/CREATING_AGENTS.md](docs/CREATING_AGENTS.md) for how to add another agent as a container.

## Quick start — Docker Compose

End-to-end 5G SA + agents in one `docker compose up`:

```bash
cd deploy/
cp .env.example .env   # edit Ollama model, InfluxDB token, intent
docker compose build
docker compose up
```

The stack brings up:

- **OAI 5G CN**: MySQL, AMF, SMF, UPF, ext-dn
- **OAI RAN (RFsim)**: gNB with the DL Lua scheduler, nrUE, DL traffic generator (`iperf3 -R` from ext-dn into the UE tunnel)
- **InfluxDB** for KPM telemetry
- **Ollama** with GPU passthrough, pre-pulls the model (default `gpt-oss:20b`)
- **AI-RAN agents**: `l2-manager` + `scheduler-agent`, sharing `subintents.json` over a named volume

See [deploy/.env.example](deploy/.env.example) for the full list of knobs.

You can also update the agents to work with commercial LLM APIs, if preferred to local models.

## OAI Compilation (manual)

If you want to run the gNB outside Docker (e.g. on hardware against a real RU), build the OAI fork with the telnet server library enabled:

**USRP:**
```bash
./build_oai -w USRP --gNB --build-lib telnetsrv
```

**FHI 7.2 (O-RU):**
```bash
./build_oai -w AW2SORAN --gNB --build-lib telnetsrv
```

The `--build-lib telnetsrv` flag also produces `libtelnetsrv_dl.so`, `libtelnetsrv_ul.so`, and `libtelnetsrv_prbmask.so` — the scheduler and PRB-mask modules that `UL_control_termination.py` drives at runtime. Launch with `--telnetsrv` to activate.

Load the DL scheduler at startup via the `LUA_SCHED` env var (UL scheduler is optional via `LUA_SCHED_UL`):

```bash
export LUA_SCHED=/path/to/pf_dl_pipe_logger.lua
nr-softmodem -O your_config.conf --telnetsrv
```

Or at runtime via telnet:
```
telnet localhost 9090
> lua_source path "/path/to/pf_dl_pipe_logger.lua"
```

See the OAI documentation in `oai/doc/BUILD.md` and `oai/doc/RUNMODEM.md` for full details.

## OAI Fork — What It Exposes

The OAI fork runs two Python processes alongside the gNB (started by the gNB image's entrypoint):

**`dl_metrics_forwarder.py`** (and the analogous `ul_metrics_forwarder.py`) — reads scheduling metrics from a named pipe written by the Lua scheduler and forwards them to InfluxDB.
```bash
python3 dl_metrics_forwarder.py --experiment my_experiment
```
DL forwarder reads from `/tmp/dl_metrics.pipe`; UL forwarder reads from `/tmp/ul_metrics.pipe`.

**`UL_control_termination.py`** — FastAPI server on port 8000. Accepts REST commands and translates them to OAI telnet commands (port 9090). Despite the historical name it now drives both DL and UL scheduler controls.

| Endpoint | Description |
|---|---|
| `POST /api/v1/power-control` | Set per-UE SNR target |
| `POST /api/v1/scheduler-config` | Set DL eMBB/MTC throughput limits |
| `POST /api/v1/prbmask/block` | Block a PRB range |
| `POST /api/v1/prbmask/ue/block` | Block PRBs for a specific UE |
| `GET  /api/v1/health` | Health check |

The gNB telnet interface must be reachable on `localhost:9090` (standard OAI telnet port).

## Multi-Agent Framework

### Agents

| Agent | Role | Default LLM | Deployed in `docker compose` |
|---|---|---|---|
| L2 Manager | Decomposes high-level intent into sub-intents (one-shot at startup) | vllm/Ollama | yes (`l2-manager`) |
| Scheduler | Adjusts DL eMBB/MTC throughput limits each cycle | vllm/Ollama | yes (`scheduler-agent`) |
| Power Control | Sets per-UE SNR targets based on throughput/interference metrics | claude | available, not containerized |
| PRB Blocking | Blocks PRBs for UEs causing inter-cell interference | fine-tuned | available, not containerized |

### Configuration

All endpoints and credentials come from environment variables — see [`shared/config.py`](shared/config.py) for the full list and defaults. The Docker Compose path drives these via [`deploy/.env.example`](deploy/.env.example); copy to `.env` and edit.

The variables the agents actually read at runtime:

| Variable | Default | Used by |
|---|---|---|
| `INFLUXDB_URL` / `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | (bundled InfluxDB) | all agents |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://ollama:11434` / `gpt-oss:20b` | Ollama-based agents |
| `VLLM_URL` / `VLLM_MODEL` | (set per overlay) | vLLM-based agents (also used by the Ollama backend, since Ollama exposes an OpenAI-compatible API) |
| `CLAUDE_PROXY_URL` / `CLAUDE_MODEL` | — | Claude-based agents |
| `GNB_API_URL` | `http://oai-gnb:8000` | Scheduler / PRB / Power agents (MCP target) |
| `INTENT` / `DEFAULT_INTENT` | see `.env.example` | L2 Manager |
| `SCHED_MIN_LIMIT` / `SCHED_MAX_LIMIT` / `SCHED_DEFAULT_FWA_LIMIT` / `SCHED_DEFAULT_MTC_LIMIT` / `SCHED_DECISION_INTERVAL` | sane defaults | Scheduler |
| `LLM_TYPE` | `vllm` | All agent runners |

### LLM Backends

| Name | Flag / `LLM_TYPE` | Backend |
|---|---|---|
| Claude | `claude` | OpenAI-compatible proxy (`CLAUDE_PROXY_URL`) |
| Qwen | `qwen` | Ollama (`OLLAMA_URL`, e.g. `qwen3-coder:30b`) |
| Fine-tuned | `fine-tuned` | vLLM server with per-agent LoRA adapters (`VLLM_URL`) |
| Generic vLLM / OpenAI-compatible | `vllm` | Anything OpenAI-compatible at `VLLM_URL` — including Ollama itself, which is the default in `deploy/docker-compose.yml` |

Updating the intent at runtime is supported by a small Flask UI:
```bash
python3 update_intent_flask_v2.py   # serves on :5000
```

### Neighbor Cell Monitor (`neighbor_monitor/`)

A third component runs on a separate host with a USRP (X410) that emulates the RF environment of neighboring cells. The USRP has 4 channels across 2 daughterboards, used for two distinct roles:

- **Channels A:0 / A:1** (DB0) — receive signals from the two neighboring cells and report their RSRP. These emulate the RSRP measurements a real UE would report from adjacent cells.
- **Channels B:0 / B:1** (DB1) — receive on frequencies overlapping with the serving cell's PRB edges to detect inter-cell interference.

All measurements are taken every 100ms and written to InfluxDB. The PRB Agent reads both streams to decide which PRBs to preemptively block for UEs at the cell edge.

**Build:**
```bash
gcc -o rsrp_monitor neighbor_monitor/rsrp_monitor.c -luhd -lm -lpthread -lcurl
```

**Run (one instance per channel):**
```bash
# Neighboring cell RSRP — DB0 RF0
./rsrp_monitor -a "addr=192.168.10.2" -f 3600 -b 20 -g 30 -s "A:0"
# Interference detection — DB1 RF1
./rsrp_monitor -a "addr=192.168.10.2" -f 3817.5 -b 5 -g 30 -s "B:1"
```

Set `INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, and `INFLUX_BUCKET` in `rsrp_monitor.c` before building.

## Deployment

The canonical deployment is Docker Compose at [`deploy/docker-compose.yml`](deploy/docker-compose.yml) — see **Quick start** above.

### Kubernetes / OpenShift

Kustomize manifests mirroring the Docker Compose stack live under [`deploy/k8s/`](deploy/k8s/) (agent side: InfluxDB, Ollama, agents) and [`deploy/k8s/oai/`](deploy/k8s/oai/) (OAI CN + RAN). Two overlay flavors are provided per side: `openshift-dev/` (cluster-internal image registry, `nfs-client` storage class, privileged SCC binding for the OAI data-plane Pods) and `public-example/` (vanilla Kubernetes).

A one-shot driver, [`deploy/k8s/openshift-deploy.sh`](deploy/k8s/openshift-deploy.sh), runs the full from-scratch path on an OpenShift cluster: namespace, SCCs, Docker Hub pull-secret, agent image build, OAI CN apply, RAN BuildConfigs + builds, RAN apply, and three agentic-loop intent tests:

```bash
NS=my-namespace \
DOCKERHUB_USER=… DOCKERHUB_TOKEN=… \
deploy/k8s/openshift-deploy.sh
```

See [`deploy/k8s/oai/README.md`](deploy/k8s/oai/README.md) for the manual step-by-step (SCC binding, `oc create configmap mysql-init` for the binary UE keys, the `kubectl kustomize --load-restrictor=LoadRestrictionsNone` flag) and [`deploy/k8s/oai/ran/README.md`](deploy/k8s/oai/ran/README.md) for the RAN-side specifics (BinaryBuilds of the agentRAN OAI fork, init-container IP templating for NGAP, `devices.kubevirt.io/tun` for the nrUE).

### Legacy

The top-level `Dockerfile` and `deployment.yaml` predate the bundled stack and are kept for backward compatibility with the single-image, agents-only deployment path; new work targets the `deploy/` tree.
