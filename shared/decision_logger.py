"""Centralized decision and intent logger.

Writes to both JSON files (for Flask UI) and InfluxDB (for Grafana).
"""

import json
import os
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED_STATE_DIR = os.environ.get('SHARED_STATE_DIR', SCRIPT_DIR)


def _append_to_json(filepath: str, entry: dict, max_entries: int = 200):
    """Append an entry to a JSON log file (list of dicts)."""
    log = []
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                log = json.load(f)
        except (json.JSONDecodeError, IOError):
            log = []
    log.append(entry)
    # Keep only the most recent entries
    if len(log) > max_entries:
        log = log[-max_entries:]
    with open(filepath, 'w') as f:
        json.dump(log, f, indent=2)


def _escape_influx(value: str) -> str:
    """Escape a string value for InfluxDB line protocol."""
    return value.replace('"', '\\"')


async def log_decision(influx, agent_type: str, fields: dict, tags: dict = None):
    """Log an agent decision to both JSON and InfluxDB.

    Args:
        influx: InfluxClient instance (with write() method)
        agent_type: 'power', 'scheduler', or 'prb'
        fields: Dict of field values (agent-specific)
        tags: Optional extra tags (e.g. {"ue": "0x1234", "uid": "1"})
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {"timestamp": timestamp, "agent_type": agent_type, **fields}

    # Write to JSON file
    json_path = os.path.join(SCRIPT_DIR, f"{agent_type}_decisions.json")
    _append_to_json(json_path, entry)

    # Write to InfluxDB
    tag_parts = [f"agent_decisions,agent_type={agent_type}"]
    if tags:
        for k, v in tags.items():
            if v is not None:
                tag_parts[0] += f",{k}={v}"

    field_parts = []
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, str):
            field_parts.append(f'{k}="{_escape_influx(v)}"')
        elif isinstance(v, bool):
            field_parts.append(f"{k}={'true' if v else 'false'}")
        elif isinstance(v, int):
            field_parts.append(f"{k}={v}i")
        elif isinstance(v, float):
            field_parts.append(f"{k}={v}")

    if field_parts:
        line = f"{tag_parts[0]} {','.join(field_parts)} {int(time.time() * 1000)}"
        try:
            await influx.write(line)
        except Exception as e:
            print(f"[DecisionLogger] InfluxDB write error: {e}")


async def log_intent(influx, agent_type: str, intent: str, high_level_intent: str = ""):
    """Log an intent assignment to InfluxDB.

    Args:
        influx: InfluxClient instance
        agent_type: Which agent this intent is for
        intent: The sub-intent text
        high_level_intent: The original high-level intent
    """
    escaped_intent = _escape_influx(intent)
    escaped_hl = _escape_influx(high_level_intent)

    line = (
        f'agent_intents,agent_type={agent_type} '
        f'intent="{escaped_intent}",high_level_intent="{escaped_hl}" '
        f'{int(time.time() * 1000)}'
    )
    try:
        await influx.write(line)
    except Exception as e:
        print(f"[DecisionLogger] InfluxDB intent write error: {e}")


def save_subintents(intents: dict):
    """Save current sub-intents to a JSON file for the Flask UI to read.

    Args:
        intents: Dict like {"power": "...", "scheduler": "...", "prb": "..."}
    """
    path = os.path.join(SHARED_STATE_DIR, "subintents.json")
    data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **intents,
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def clear_decision_logs():
    """Remove all decision log JSON files."""
    for name in ["power_decisions.json", "scheduler_decisions.json", "prb_decisions.json", "decision_log.json", "subintents.json"]:
        path = os.path.join(SCRIPT_DIR, name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
