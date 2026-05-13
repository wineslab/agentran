#!/usr/bin/env python3
"""
5G Power Control MCP Server using FastMCP
"""

from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP
import json

# Initialize FastMCP server
mcp = FastMCP("5g-power-control")

# Constants
import os
import sys
import pathlib

# fastmcp subprocess doesn't inherit parent env vars, so also check a
# config file written by the parent (scheduling_agent) at startup.
_config_file = pathlib.Path(__file__).parent / ".gnb_api_url"
_url_from_file = _config_file.read_text().strip() if _config_file.exists() else None
API_BASE = os.environ.get("GNB_API_URL") or _url_from_file or "http://YOUR_GNB_API_URL"
API_TIMEOUT = 30.0
print(f"[MCP] API_BASE={API_BASE}", file=sys.stderr)

async def make_api_request(
    method: str,
    endpoint: str,
    json_data: Optional[Dict] = None
) -> Dict[str, Any] | None:
    """Make a request to the 5G API with proper error handling."""
    url = f"{API_BASE}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            if method == "GET":
                response = await client.get(url, timeout=API_TIMEOUT)
            elif method == "POST":
                response = await client.post(url, json=json_data, timeout=API_TIMEOUT)
            elif method == "DELETE":
                response = await client.delete(url, timeout=API_TIMEOUT)
            else:
                return None

            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}

@mcp.tool()
async def set_power_control(uid: int, snr_target: int, reasoning: str = "") -> str:
    """Set power control parameters for a specific UE.

    Args:
        uid: UE identifier (integer)
        snr_target: Target SNR value in db x 10 (between -20 and 350)
        reasoning: Reason for adjustment
    """
    if not -20 <= snr_target <= 350:
        return "Error: SNR target must be between -20 and 350"

    payload = {
        "commands": [{
            "uid": uid,
            "snr_target": snr_target,
            "reasoning": reasoning or f"Setting SNR target to {snr_target} dB x 10"
        }]
    }

    data = await make_api_request("POST", "/api/v1/power-control", payload)

    if not data:
        return "Unable to send power control command."

    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"

    # Format the results
    results = data.get("results", [])
    if results:
        result = results[0]
        if result.get("status") == "success":
            return f"Successfully set uid {result.get('uid', uid)} to SNR target {result.get('snr_target', snr_target)} dB"
        else:
            return f"Failed to set power control: {result.get('message', 'Unknown error')}"

    return "Power control command sent."

@mcp.tool()
async def update_scheduler_config(fwa_value: int, mtc_value: int, reasoning: str = "") -> str:
    """Update Uplink scheduler configuration with new eMBB and MTC limits. The underlying scheduler is Proportional Fairness with maximum throughput per user for each UE class

    Args:
        fwa_value: eMBB limit throughput in mbps
        mtc_value: MTC limit throughput in mbps  
        reasoning: Reason for the scheduler update
    """
    # Validate inputs
    if fwa_value < 0 or mtc_value < 0:
        return "Error: eMBB and MTC values must be non-negative"
    
    payload = {
        "fwa_value": fwa_value,
        "mtc_value": mtc_value,
        "reasoning": reasoning or f"Updating scheduler limits: eMBB={fwa_value}, MTC={mtc_value}"
    }
    
    data = await make_api_request("POST", "/api/v1/scheduler-config", payload)
    
    if not data:
        return "Unable to update scheduler configuration."
    
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    
    if data.get("status") == "success":
        config = data.get("config", {})
        return f"""Successfully updated scheduler configuration:
- eMBB limit: {config.get('fwa_value')}
- MTC limit: {config.get('mtc_value')}
- Config file: {config.get('config_file')}
- Reasoning: {config.get('reasoning', 'None provided')}"""
    
    return "Scheduler configuration update sent."

@mcp.tool()
async def prbmask_block(start_prb: int, num_prbs: int) -> str:
    """Block a contiguous range of Physical Resource Blocks (PRBs) in the uplink.
    Blocked PRBs will not be allocated to any UE by the scheduler.

    Args:
        start_prb: Starting PRB index (0-based)
        num_prbs: Number of consecutive PRBs to block
    """
    data = await make_api_request("POST", "/api/v1/prbmask/block", {"start_prb": start_prb, "num_prbs": num_prbs})
    if not data:
        return "Unable to send PRB block command."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "PRBs blocked.")

@mcp.tool()
async def prbmask_unblock(start_prb: int, num_prbs: int) -> str:
    """Unblock a contiguous range of Physical Resource Blocks (PRBs) in the uplink.

    Args:
        start_prb: Starting PRB index (0-based)
        num_prbs: Number of consecutive PRBs to unblock
    """
    data = await make_api_request("POST", "/api/v1/prbmask/unblock", {"start_prb": start_prb, "num_prbs": num_prbs})
    if not data:
        return "Unable to send PRB unblock command."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "PRBs unblocked.")

@mcp.tool()
async def prbmask_show() -> str:
    """Get the current PRB blocking mask state, showing which PRBs are blocked (X) and allowed (.)."""
    data = await make_api_request("GET", "/api/v1/prbmask")
    if not data:
        return "Unable to retrieve PRB mask."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("raw_response", "No mask data returned.")

@mcp.tool()
async def prbmask_apply(mask: str) -> str:
    """Set the full PRB blocking mask as a string. Each character represents one PRB:
    '.' means allowed, 'X' means blocked. The string length should match the BWP size.

    Args:
        mask: PRB mask string (e.g. '....XXXX....' to block PRBs 4-7)
    """
    data = await make_api_request("POST", "/api/v1/prbmask/set", {"mask": mask})
    if not data:
        return "Unable to set PRB mask."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "PRB mask applied.")

@mcp.tool()
async def prbmask_clear() -> str:
    """Clear the PRB blocking mask, unblocking all PRBs so the scheduler can use the full bandwidth."""
    data = await make_api_request("DELETE", "/api/v1/prbmask")
    if not data:
        return "Unable to clear PRB mask."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "PRB mask cleared.")

# --- Per-UE PRB mask tools ---

@mcp.tool()
async def prbmask_ue_block(uid: int, start_prb: int, num_prbs: int) -> str:
    """Block a contiguous range of PRBs for a specific UE only.
    Other UEs are unaffected. Use this for per-UE interference management.

    Args:
        uid: UE identifier (integer)
        start_prb: Starting PRB index (0-based)
        num_prbs: Number of consecutive PRBs to block
    """
    data = await make_api_request("POST", "/api/v1/prbmask/ue/block",
                                  {"uid": uid, "start_prb": start_prb, "num_prbs": num_prbs})
    if not data:
        return "Unable to send per-UE PRB block command."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "Per-UE PRBs blocked.")

@mcp.tool()
async def prbmask_ue_unblock(uid: int, start_prb: int, num_prbs: int) -> str:
    """Unblock a contiguous range of PRBs for a specific UE.

    Args:
        uid: UE identifier (integer)
        start_prb: Starting PRB index (0-based)
        num_prbs: Number of consecutive PRBs to unblock
    """
    data = await make_api_request("POST", "/api/v1/prbmask/ue/unblock",
                                  {"uid": uid, "start_prb": start_prb, "num_prbs": num_prbs})
    if not data:
        return "Unable to send per-UE PRB unblock command."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "Per-UE PRBs unblocked.")

@mcp.tool()
async def prbmask_ue_apply(uid: int, mask: str) -> str:
    """Set the full per-UE PRB blocking mask as a string. Each character represents one PRB:
    '.' means allowed, 'X' means blocked. Only affects the specified UE.

    Args:
        uid: UE identifier (integer)
        mask: PRB mask string (e.g. 'XXXX....' to block PRBs 0-3 for this UE)
    """
    data = await make_api_request("POST", "/api/v1/prbmask/ue/set",
                                  {"uid": uid, "mask": mask})
    if not data:
        return "Unable to set per-UE PRB mask."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "Per-UE PRB mask applied.")

@mcp.tool()
async def prbmask_ue_show(uid: int) -> str:
    """Get a specific UE's PRB blocking mask, showing which PRBs are blocked (X) and allowed (.).

    Args:
        uid: UE identifier (integer)
    """
    data = await make_api_request("GET", f"/api/v1/prbmask/ue/{uid}")
    if not data:
        return "Unable to retrieve per-UE PRB mask."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("raw_response", "No per-UE mask data returned.")

@mcp.tool()
async def prbmask_ue_clear(uid: int) -> str:
    """Clear a specific UE's PRB blocking mask, restoring full bandwidth for that UE.

    Args:
        uid: UE identifier (integer)
    """
    data = await make_api_request("DELETE", f"/api/v1/prbmask/ue/{uid}")
    if not data:
        return "Unable to clear per-UE PRB mask."
    if data.get("status") == "error":
        return f"Error: {data.get('message', 'Unknown error')}"
    return data.get("message", "Per-UE PRB mask cleared.")

if __name__ == "__main__":
    # Run the server using stdio transport
    mcp.run(transport='stdio')