import os

# Agent timing configuration
L2_MANAGER_CONFIG = {
    # 'update_interval': 180,  # Update sub-intents every hour (3600 seconds)
    'default_intent': "Optimize the network for both MTC and eMBB users. MTC devices should have sufficient bandwidth for their needs while minimizing battery consumption. eMBB devices should maximize their throughput."
}

POWER_AGENT_CONFIG = {
    'min_decision_interval': 2.0,      # Minimum seconds between decisions
    'state_check_interval': 0.1,       # How often to check for new state
    'default_power': 350,              # Default SNR target on startup (35dB x 10)
}

SCHEDULER_AGENT_CONFIG = {
    'min_decision_interval': float(os.environ.get('SCHED_DECISION_INTERVAL', '5.0')),
    'state_check_interval': 0.1,    # How often to check for state changes
    'default_fwa_limit': int(os.environ.get('SCHED_DEFAULT_FWA_LIMIT', '20')),
    'default_mtc_limit': int(os.environ.get('SCHED_DEFAULT_MTC_LIMIT', '20')),
    'min_limit': int(os.environ.get('SCHED_MIN_LIMIT', '1')),
    'max_limit': int(os.environ.get('SCHED_MAX_LIMIT', '20')),
    'history_size': 10            # How many decisions to keep in history
}

PRB_AGENT_CONFIG = {
    'min_decision_interval': 1.0,   # Seconds between decisions (fast loop)
    'state_check_interval': 0.1,    # How often to check for new state
    'rsrp_window_seconds': 3,       # How far back to look for RSRP data
    'rsrp_resolution_ms': 100,      # Aggregation resolution for RSRP timeseries
    # Neighbor cell to PRB overlap mapping (static, from cell geometry)
    # 20 MHz @ 30 kHz SCS = 51 PRBs, overlap = 10 PRBs each edge
    # Must match rsrp_monitor_prb.c: -b 20 -s 30 -o 10
    'neighbor_prb_map': {
        'cell2': {'start_prb': 0, 'num_prbs': 10, 'label': 'neighbor low edge'},
        'cell3': {'start_prb': 41, 'num_prbs': 10, 'label': 'neighbor high edge'},
    },
}

# InfluxDB Configuration
INFLUX_CONFIG = {
    'url': os.environ.get('INFLUXDB_URL', 'http://YOUR_INFLUXDB_URL:8086'),
    'org': os.environ.get('INFLUXDB_ORG', 'mwc'),
    'bucket': os.environ.get('INFLUXDB_BUCKET', 'mwc-live'),
    'token': os.environ.get('INFLUXDB_TOKEN', 'YOUR_INFLUXDB_TOKEN'),
    'measurement': 'dl_scheduler',
}

# LLM Configuration (Ollama - legacy, kept for backward compat)
LLM_CONFIG = {
    'api_url': os.environ.get('OLLAMA_URL', 'http://YOUR_OLLAMA_URL'),
    'model': os.environ.get('OLLAMA_MODEL', 'qwen3-coder:30b'),
    'timeout': 30,
    'max_retries': 3
}

# vLLM / Ollama Configuration (OpenAI-compatible API — used by default in Docker deploy)
VLLM_CONFIG = {
    'api_url': os.environ.get('VLLM_URL', 'http://localhost:11434'),
    'model': os.environ.get('VLLM_MODEL', 'gpt-oss:20b'),
    'timeout': 300,
    'max_retries': 3,
}

# Fine-tuned vLLM model configuration
# Base URL for fine-tuned LoRA adapters served by a single vLLM instance.
FINETUNED_BASE_URL = 'http://YOUR_VLLM_URL'
FINETUNED_TIMEOUT = 30
FINETUNED_MAX_RETRIES = 3

# Per-agent LoRA adapter names exposed by the vLLM server.
FINETUNED_MODELS = {
    'l2_manager': 'l2_manager',
    'power': 'power_agent',
    'scheduler': 'scheduler_agent',
    'prb': 'prb_blocking_agent',
}

# Claude LLM Configuration (via Colosseum proxy, OpenAI-compatible)
CLAUDE_LLM_CONFIG = {
    'api_url': 'http://YOUR_CLAUDE_PROXY_URL',
    'model': 'claude-4.5-sonnet',
    'timeout': 60,
    'max_retries': 3,
    'endpoint_path': '/chat/completions',
}
