#!/usr/bin/env python3
"""
Unified Multi-Agent Web Interface.
Launches main.py with per-agent LLM selection and a high-level intent.
Displays an agent graph and decision panels for all agents.
"""

from flask import Flask, render_template_string, jsonify, request
import subprocess
import os
import sys
import signal
import json
import atexit
import requests

app = Flask(__name__)

# In-memory state
current_intent = None
running_process = None
agent_llm_choices = {
    'l2_manager': 'claude',
    'power': 'claude',
    'scheduler': 'claude',
    'prb': 'fine-tuned',
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(SCRIPT_DIR, 'main.py')
API_BASE = "http://YOUR_GNB_API_URL"

DECISION_LOG_FILES = {
    'power': os.path.join(SCRIPT_DIR, 'power_decisions.json'),
    'scheduler': os.path.join(SCRIPT_DIR, 'scheduler_decisions.json'),
    'prb': os.path.join(SCRIPT_DIR, 'decision_log.json'),
}
SUBINTENTS_FILE = os.path.join(SCRIPT_DIR, 'subintents.json')


def cleanup_on_exit():
    global running_process
    if running_process is not None and running_process.poll() is None:
        print("\n[Cleanup] Killing agent process before exit...")
        try:
            os.killpg(os.getpgid(running_process.pid), signal.SIGTERM)
            running_process.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(running_process.pid), signal.SIGKILL)
            except Exception:
                pass
        running_process = None


atexit.register(cleanup_on_exit)


def _signal_handler(signum, frame):
    cleanup_on_exit()
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Multi-Agent Control</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0d1117;
            min-height: 100vh;
            color: #e0e0e0;
            padding: 12px;
        }

        /* Header */
        .header { text-align: center; margin-bottom: 10px; }
        .header h1 { color: #fff; font-size: 18px; font-weight: 700; letter-spacing: 0.5px; }

        /* Status pill */
        .status-pill {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 4px 12px; border-radius: 20px; font-size: 11px;
            font-weight: 600; margin-bottom: 10px;
        }
        .status-pill.running { background: rgba(40,167,69,0.15); color: #5cb85c; border: 1px solid rgba(40,167,69,0.3); }
        .status-pill.stopped { background: rgba(255,255,255,0.05); color: #666; border: 1px solid rgba(255,255,255,0.08); }
        .status-dot { width: 6px; height: 6px; border-radius: 50%; }
        .running .status-dot { background: #28a745; animation: pulse 2s infinite; }
        .stopped .status-dot { background: #444; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

        /* Current intent display */
        .current-intent-bar {
            background: rgba(102,126,234,0.08); border: 1px solid rgba(102,126,234,0.2);
            border-radius: 8px; padding: 8px 12px; margin-bottom: 10px;
            display: flex; align-items: flex-start; gap: 8px;
        }
        .current-intent-bar .label {
            font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
            color: #667eea; white-space: nowrap; padding-top: 2px;
        }
        .current-intent-bar .value {
            font-size: 12px; color: #c0c8e0; line-height: 1.4; flex: 1;
        }
        .current-intent-bar .value.empty { color: #444; font-style: italic; }

        /* Agent graph — compact horizontal */
        .agent-graph {
            display: flex; align-items: center; justify-content: center;
            gap: 6px; margin-bottom: 10px; flex-wrap: wrap;
        }
        .agent-chip {
            background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px; padding: 6px 8px; text-align: center; min-width: 0; flex: 1;
        }
        .agent-chip.manager { border-color: rgba(102,126,234,0.35); background: rgba(102,126,234,0.06); }
        .agent-chip .chip-name { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; color: #8892b0; margin-bottom: 3px; }
        .agent-chip select {
            width: 100%; padding: 3px 4px; border-radius: 4px; font-size: 10px;
            background: rgba(0,0,0,0.4); color: #ccc; border: 1px solid rgba(255,255,255,0.1);
        }
        .agent-arrow { color: #333; font-size: 14px; flex-shrink: 0; }

        /* Intent input */
        .intent-box {
            background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
            border-radius: 8px; padding: 10px; margin-bottom: 10px;
        }
        .intent-box label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #667eea; display: block; margin-bottom: 4px; }
        textarea {
            width: 100%; padding: 8px; border: 1px solid rgba(255,255,255,0.1);
            border-radius: 6px; font-size: 12px; font-family: inherit;
            resize: none; height: 52px; background: rgba(0,0,0,0.3); color: #e0e0e0; line-height: 1.4;
        }
        textarea:focus { outline: none; border-color: #667eea; }
        .btn-row { display: flex; gap: 6px; margin-top: 8px; }
        .btn-row button {
            flex: 1; padding: 8px; border: none; border-radius: 6px;
            font-size: 12px; font-weight: 700; cursor: pointer; transition: all 0.15s;
        }
        .btn-launch { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; }
        .btn-launch:hover { box-shadow: 0 2px 12px rgba(102,126,234,0.3); }
        .btn-launch:disabled { background: #333; cursor: not-allowed; }
        .btn-stop { background: rgba(220,53,69,0.15); color: #dc3545; border: 1px solid rgba(220,53,69,0.25); }
        .btn-stop:disabled { opacity: 0.3; cursor: not-allowed; }

        /* Restart gNB / Reset buttons */
        .btn-restart, .btn-reset-default {
            display: inline-flex; align-items: center; gap: 5px;
            padding: 6px 14px; border-radius: 6px; font-size: 11px; font-weight: 700;
            cursor: pointer; transition: all 0.15s; margin-bottom: 10px;
        }
        .btn-restart {
            border: 1px solid rgba(255,193,7,0.3);
            background: rgba(255,193,7,0.1); color: #ffc107;
        }
        .btn-restart:hover { background: rgba(255,193,7,0.2); box-shadow: 0 2px 8px rgba(255,193,7,0.15); }
        .btn-restart:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-reset-default {
            border: 1px solid rgba(220,53,69,0.3);
            background: rgba(220,53,69,0.1); color: #dc3545;
        }
        .btn-reset-default:hover { background: rgba(220,53,69,0.2); box-shadow: 0 2px 8px rgba(220,53,69,0.15); }
        .btn-reset-default:disabled { opacity: 0.4; cursor: not-allowed; }

        /* Message toast */
        .toast { position: fixed; top: 12px; right: 12px; padding: 8px 14px; border-radius: 6px; font-size: 11px; display: none; z-index: 100; animation: slideIn 0.2s; }
        .toast.success { background: rgba(40,167,69,0.9); color: #fff; }
        .toast.error { background: rgba(220,53,69,0.9); color: #fff; }
        .toast.show { display: block; }
        @keyframes slideIn { from{transform:translateX(20px);opacity:0} to{transform:translateX(0);opacity:1} }

        /* Decision sections — vertical stack */
        .decisions-stack { display: flex; flex-direction: column; gap: 8px; }

        .agent-section {
            background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07);
            border-radius: 8px; overflow: hidden;
        }

        /* Sub-intent banner — prominent */
        .subintent {
            padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        .subintent-header {
            display: flex; align-items: center; gap: 6px; margin-bottom: 4px;
        }
        .subintent-icon {
            width: 18px; height: 18px; border-radius: 4px; display: flex;
            align-items: center; justify-content: center; font-size: 10px; flex-shrink: 0;
        }
        .subintent-icon.power { background: rgba(99,179,237,0.2); color: #63b3ed; }
        .subintent-icon.scheduler { background: rgba(72,187,120,0.2); color: #48bb78; }
        .subintent-icon.prb { background: rgba(237,137,54,0.2); color: #ed8936; }
        .subintent-title {
            font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px;
        }
        .subintent-title.power { color: #63b3ed; }
        .subintent-title.scheduler { color: #48bb78; }
        .subintent-title.prb { color: #ed8936; }
        .subintent-body {
            font-size: 13px; line-height: 1.45; color: #d0d0d0; font-weight: 500;
        }
        .subintent-body.empty { color: #444; font-style: italic; font-weight: 400; font-size: 11px; }

        /* Decision feed */
        .decision-feed { padding: 6px 10px; }
        .decision-feed .feed-item {
            padding: 4px 8px; border-radius: 4px; font-size: 11px;
            margin-bottom: 3px; font-family: 'SF Mono', 'Consolas', monospace;
            border-left: 3px solid transparent; transition: opacity 0.3s;
        }
        /* Fade out older entries */
        .decision-feed .feed-item:nth-child(1) { opacity: 1; }
        .decision-feed .feed-item:nth-child(2) { opacity: 0.85; }
        .decision-feed .feed-item:nth-child(3) { opacity: 0.65; }
        .decision-feed .feed-item:nth-child(4) { opacity: 0.45; }
        .decision-feed .feed-item:nth-child(5) { opacity: 0.3; }

        /* Power colors */
        .feed-item.power-up { border-left-color: #63b3ed; background: rgba(99,179,237,0.08); color: #90cdf4; }
        .feed-item.power-down { border-left-color: #4299e1; background: rgba(66,153,225,0.06); color: #63b3ed; }
        .feed-item.power-same { border-left-color: #2a4365; background: rgba(42,67,101,0.15); color: #4a7ab5; }
        .feed-item.power-init { border-left-color: #1a365d; background: rgba(26,54,93,0.1); color: #3a6a9e; }

        /* Scheduler colors */
        .feed-item.sched-change { border-left-color: #48bb78; background: rgba(72,187,120,0.08); color: #9ae6b4; }
        .feed-item.sched-same { border-left-color: #276749; background: rgba(39,103,73,0.12); color: #4a9e6e; }
        .feed-item.sched-init { border-left-color: #1a4731; background: rgba(26,71,49,0.1); color: #3a8a5e; }

        /* PRB colors */
        .feed-item.prb-block { border-left-color: #ed8936; background: rgba(237,137,54,0.1); color: #fbd38d; }
        .feed-item.prb-clear { border-left-color: #48bb78; background: rgba(72,187,120,0.08); color: #9ae6b4; }

        .feed-item .ts { color: #555; font-size: 9px; margin-right: 4px; }
        .feed-item .arrow-icon { font-size: 10px; }
        .feed-empty { color: #333; font-size: 11px; font-style: italic; padding: 4px 0; }

        .loading { display: inline-block; width: 10px; height: 10px; border: 2px solid rgba(255,255,255,0.2); border-top: 2px solid #667eea; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { 0%{transform:rotate(0deg)} 100%{transform:rotate(360deg)} }
    </style>
</head>
<body>
    <div class="header"><h1>Multi-Agent Control</h1></div>

    <div style="display:flex; align-items:center; justify-content:center; gap:10px; margin-bottom:10px;">
        <div id="status-pill" class="status-pill stopped" style="margin-bottom:0">
            <div class="status-dot"></div>
            <span id="status-text">Stopped</span>
        </div>
        <button class="btn-restart" onclick="restartGnb()" id="restart-btn" style="margin-bottom:0">&#x1F504; Restart gNB</button>
        <button class="btn-reset-default" onclick="resetToDefault()" id="reset-btn" style="margin-bottom:0">&#x21BA; Reset to Default</button>
    </div>

    <!-- Current intent display (visible to all clients) -->
    <div class="current-intent-bar">
        <div class="label">Active Intent</div>
        <div class="value empty" id="current-intent-display">No intent set</div>
    </div>

    <!-- Agent Graph — compact row -->
    <div class="agent-graph">
        <div class="agent-chip manager">
            <div class="chip-name">L2 Manager</div>
            <select id="llm-l2_manager">
                <option value="claude" selected>Claude</option>
                <option value="qwen">Qwen</option>
                <option value="fine-tuned">Fine-tuned</option>
            </select>
        </div>
        <div class="agent-arrow">&#x25B6;</div>
        <div class="agent-chip">
            <div class="chip-name">Power</div>
            <select id="llm-power">
                <option value="claude" selected>Claude</option>
                <option value="qwen">Qwen</option>
                <option value="fine-tuned">Fine-tuned</option>
            </select>
        </div>
        <div class="agent-chip">
            <div class="chip-name">Scheduler</div>
            <select id="llm-scheduler">
                <option value="claude" selected>Claude</option>
                <option value="qwen">Qwen</option>
                <option value="fine-tuned">Fine-tuned</option>
            </select>
        </div>
        <div class="agent-chip">
            <div class="chip-name">PRB</div>
            <select id="llm-prb">
                <option value="fine-tuned" selected>Fine-tuned</option>
                <option value="claude">Claude</option>
                <option value="qwen">Qwen</option>
            </select>
        </div>
    </div>

    <!-- Intent -->
    <div class="intent-box">
        <label>Operator Intent</label>
        <textarea id="new-intent" placeholder="e.g. Maximize throughput while protecting neighbors...">Maximize the overall throughput of the system while protecting neighbor cells from interference.</textarea>
        <div class="btn-row">
            <button class="btn-stop" onclick="stopAgents()" id="stop-btn" disabled>Stop</button>
            <button class="btn-launch" onclick="launchAgents()" id="launch-btn">Launch</button>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <!-- Decision sections — stacked vertically -->
    <div class="decisions-stack">
        <!-- Power -->
        <div class="agent-section">
            <div class="subintent" id="subintent-power">
                <div class="subintent-header">
                    <div class="subintent-icon power">&#x26A1;</div>
                    <div class="subintent-title power">Power Control</div>
                </div>
                <div class="subintent-body empty" id="subintent-power-text">Waiting for L2 Manager...</div>
            </div>
            <div class="decision-feed" id="decisions-power"><div class="feed-empty">No decisions yet</div></div>
        </div>

        <!-- Scheduler -->
        <div class="agent-section">
            <div class="subintent" id="subintent-scheduler">
                <div class="subintent-header">
                    <div class="subintent-icon scheduler">&#x2696;</div>
                    <div class="subintent-title scheduler">Scheduler</div>
                </div>
                <div class="subintent-body empty" id="subintent-scheduler-text">Waiting for L2 Manager...</div>
            </div>
            <div class="decision-feed" id="decisions-scheduler"><div class="feed-empty">No decisions yet</div></div>
        </div>

        <!-- PRB -->
        <div class="agent-section">
            <div class="subintent" id="subintent-prb">
                <div class="subintent-header">
                    <div class="subintent-icon prb">&#x1F6E1;</div>
                    <div class="subintent-title prb">PRB / Interference</div>
                </div>
                <div class="subintent-body empty" id="subintent-prb-text">Waiting for L2 Manager...</div>
            </div>
            <div class="decision-feed" id="decisions-prb"><div class="feed-empty">No decisions yet</div></div>
        </div>
    </div>

    <script>
        const MAX_VISIBLE = 5;

        window.addEventListener('DOMContentLoaded', async () => {
            await loadState();
            await loadDecisions();
            await loadSubintents();
        });

        let initialStateLoaded = false;
        async function loadState() {
            try {
                const resp = await fetch('/api/state');
                const data = await resp.json();
                updateStatus(data.running);
                updateCurrentIntent(data.intent);
                if (!initialStateLoaded && data.llm_choices) {
                    for (const [agent, llm] of Object.entries(data.llm_choices)) {
                        const sel = document.getElementById('llm-' + agent);
                        if (sel) sel.value = llm;
                    }
                    initialStateLoaded = true;
                }
            } catch (e) { console.error('loadState:', e); }
        }

        function updateStatus(running) {
            const pill = document.getElementById('status-pill');
            const txt = document.getElementById('status-text');
            const stopBtn = document.getElementById('stop-btn');
            pill.className = 'status-pill ' + (running ? 'running' : 'stopped');
            txt.textContent = running ? 'Running' : 'Stopped';
            stopBtn.disabled = !running;
        }

        function updateCurrentIntent(intent) {
            const el = document.getElementById('current-intent-display');
            if (intent) {
                el.textContent = intent;
                el.className = 'value';
            } else {
                el.textContent = 'No intent set';
                el.className = 'value empty';
            }
        }

        function getLLMChoices() {
            return {
                l2_manager: document.getElementById('llm-l2_manager').value,
                power: document.getElementById('llm-power').value,
                scheduler: document.getElementById('llm-scheduler').value,
                prb: document.getElementById('llm-prb').value,
            };
        }

        async function launchAgents() {
            const intent = document.getElementById('new-intent').value.trim();
            if (!intent) { showToast('Enter an intent first', 'error'); return; }
            const btn = document.getElementById('launch-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span>';
            try {
                const resp = await fetch('/api/launch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ intent, llm_choices: getLLMChoices() }),
                });
                const data = await resp.json();
                if (data.success) { showToast('Launched', 'success'); await loadState(); }
                else showToast(data.error, 'error');
            } catch (e) { showToast(e.message, 'error'); }
            finally { btn.disabled = false; btn.textContent = 'Launch'; }
        }

        async function stopAgents() {
            const btn = document.getElementById('stop-btn');
            btn.disabled = true;
            try {
                const resp = await fetch('/api/stop', { method: 'POST' });
                const data = await resp.json();
                if (data.success) { showToast('Stopped', 'success'); await loadState(); }
                else showToast(data.error, 'error');
            } catch (e) { showToast(e.message, 'error'); }
            finally { btn.disabled = false; }
        }

        async function restartGnb() {
            const btn = document.getElementById('restart-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> Restarting...';
            try {
                const resp = await fetch('/api/restart-gnb', { method: 'POST' });
                const data = await resp.json();
                if (data.success) showToast('gNB restarted', 'success');
                else showToast(data.error || 'Restart failed', 'error');
            } catch (e) { showToast(e.message, 'error'); }
            finally { btn.disabled = false; btn.innerHTML = '&#x1F504; Restart gNB'; }
        }

        async function resetToDefault() {
            const btn = document.getElementById('reset-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span> Resetting...';
            try {
                const resp = await fetch('/api/reset-default', { method: 'POST' });
                const data = await resp.json();
                if (data.success) {
                    showToast('Reset to defaults', 'success');
                    await loadState();
                    await loadDecisions();
                    await loadSubintents();
                } else {
                    showToast(data.error || 'Reset failed', 'error');
                }
            } catch (e) { showToast(e.message, 'error'); }
            finally { btn.disabled = false; btn.innerHTML = '&#x21BA; Reset to Default'; }
        }

        function showToast(text, type) {
            const el = document.getElementById('toast');
            el.textContent = text;
            el.className = 'toast ' + type + ' show';
            setTimeout(() => el.classList.remove('show'), 3000);
        }

        let lastDecisionsJson = '';
        async function loadDecisions() {
            try {
                const resp = await fetch('/api/decisions');
                const data = await resp.json();
                const j = JSON.stringify(data);
                if (j === lastDecisionsJson) return;
                lastDecisionsJson = j;
                renderPower(data.power || []);
                renderScheduler(data.scheduler || []);
                renderPRB(data.prb || []);
            } catch (e) { console.error('loadDecisions:', e); }
        }

        function renderPower(decisions) {
            const el = document.getElementById('decisions-power');
            if (!decisions.length) { el.innerHTML = '<div class="feed-empty">No decisions yet</div>'; return; }
            const items = [...decisions].reverse().slice(0, MAX_VISIBLE);
            el.innerHTML = items.map(d => {
                const time = (d.timestamp || '').split(' ')[1] || '';
                const snr = d.snr_target !== undefined ? (d.snr_target / 10).toFixed(1) : '?';
                const prev = d.previous_snr_target;
                const ue = d.ue || d.uid || '?';
                let cls, detail;
                if (d.action === 'initial_setup' || d.action === 'new_ue_init') {
                    cls = 'power-init';
                    detail = `UE ${ue} = ${snr} dB (init)`;
                } else if (d.action === 'no_change') {
                    cls = 'power-same';
                    detail = `UE ${ue} = ${snr} dB (hold)`;
                } else if (prev !== undefined && prev !== null) {
                    const prevDb = (prev / 10).toFixed(1);
                    const up = d.snr_target > prev;
                    cls = up ? 'power-up' : 'power-down';
                    const arrow = up ? '&#x25B2;' : '&#x25BC;';
                    detail = `UE ${ue}: ${prevDb} <span class="arrow-icon">${arrow}</span> ${snr} dB`;
                } else {
                    cls = 'power-up';
                    detail = `UE ${ue} = ${snr} dB`;
                }
                return `<div class="feed-item ${cls}"><span class="ts">${time}</span> ${detail}</div>`;
            }).join('');
        }

        function renderScheduler(decisions) {
            const el = document.getElementById('decisions-scheduler');
            if (!decisions.length) { el.innerHTML = '<div class="feed-empty">No decisions yet</div>'; return; }
            const items = [...decisions].reverse().slice(0, MAX_VISIBLE);
            el.innerHTML = items.map(d => {
                const time = (d.timestamp || '').split(' ')[1] || '';
                const embb = d.fwa_limit !== undefined ? d.fwa_limit : '?';
                const mtc = d.mtc_limit !== undefined ? d.mtc_limit : '?';
                let cls;
                if (d.action === 'initial_setup') cls = 'sched-init';
                else if (d.action === 'no_change') cls = 'sched-same';
                else cls = 'sched-change';
                const prevEmbb = d.previous_fwa_limit;
                let detail;
                if (prevEmbb !== undefined && prevEmbb !== null && d.action === 'decision') {
                    const prevMtc = d.previous_mtc_limit || '?';
                    detail = `eMBB: ${prevEmbb}<span class="arrow-icon">&#x25B6;</span>${embb} | MTC: ${prevMtc}<span class="arrow-icon">&#x25B6;</span>${mtc} mbps`;
                } else {
                    detail = `eMBB=${embb} | MTC=${mtc} mbps`;
                }
                return `<div class="feed-item ${cls}"><span class="ts">${time}</span> ${detail}</div>`;
            }).join('');
        }

        function renderPRB(decisions) {
            const el = document.getElementById('decisions-prb');
            if (!decisions.length) { el.innerHTML = '<div class="feed-empty">No decisions yet</div>'; return; }
            const items = [...decisions].reverse().slice(0, MAX_VISIBLE);
            el.innerHTML = items.map(d => {
                const time = (d.timestamp || '').split(' ')[1] || '';
                const cls = d.action === 'block' ? 'prb-block' : 'prb-clear';
                const icon = d.action === 'block' ? '&#x1F6D1;' : '&#x2705;';
                return `<div class="feed-item ${cls}"><span class="ts">${time}</span> ${icon} UE ${d.ue||'?'} ${d.neighbor||''} [${d.action}]</div>`;
            }).join('');
        }

        async function loadSubintents() {
            try {
                const resp = await fetch('/api/intents');
                const data = await resp.json();
                for (const agent of ['power', 'scheduler', 'prb']) {
                    const el = document.getElementById('subintent-' + agent + '-text');
                    if (data[agent]) {
                        el.textContent = data[agent];
                        el.className = 'subintent-body';
                    } else {
                        el.textContent = 'Waiting for L2 Manager...';
                        el.className = 'subintent-body empty';
                    }
                }
            } catch (e) { console.error('loadSubintents:', e); }
        }

        setInterval(async () => { await loadState(); await loadDecisions(); await loadSubintents(); }, 3000);

        document.getElementById('new-intent').addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); launchAgents(); }
        });
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/state')
def get_state():
    global running_process
    running = False
    if running_process is not None:
        if running_process.poll() is None:
            running = True
        else:
            running_process = None
    return jsonify({
        'intent': current_intent,
        'running': running,
        'llm_choices': agent_llm_choices,
    })


AGENT_TO_LOG = {
    'power': 'power',
    'scheduler': 'scheduler',
    'prb': 'prb',
}

@app.route('/api/launch', methods=['POST'])
def launch_agents():
    global current_intent, running_process, agent_llm_choices

    data = request.get_json()
    intent = data.get('intent', '').strip()
    llm_choices = data.get('llm_choices', {})

    if not intent:
        return jsonify({'success': False, 'error': 'Empty intent'}), 400

    was_running = running_process is not None and running_process.poll() is None
    prev_intent = current_intent
    prev_llm_choices = dict(agent_llm_choices)

    # Detect what changed
    intent_changed = (intent != prev_intent) or not was_running
    changed_agents = set()
    for agent_key in ['l2_manager', 'power', 'scheduler', 'prb']:
        if llm_choices.get(agent_key) != prev_llm_choices.get(agent_key):
            changed_agents.add(agent_key)

    # Stop existing process if running
    if was_running:
        os.killpg(os.getpgid(running_process.pid), signal.SIGTERM)
        running_process.wait()
        running_process = None
        # Clear PRB masks on stop
        try:
            resp = requests.delete(f"{API_BASE}/api/v1/prbmask", timeout=5, verify=False)
            print(f"[Reset] DELETE /api/v1/prbmask -> {resp.status_code}")
        except Exception as e:
            print(f"[Reset] Failed to clear PRB masks: {e}")

    current_intent = intent
    agent_llm_choices.update(llm_choices)

    skip_l2_init = False

    if intent_changed:
        # Intent changed: full reset — clear all logs and subintents
        print(f"[Launch] Intent changed, full restart")
        for path in list(DECISION_LOG_FILES.values()) + [SUBINTENTS_FILE]:
            if os.path.exists(path):
                os.remove(path)
    else:
        # Intent same: only clear decision logs for agents whose LLM changed
        skip_l2_init = 'l2_manager' not in changed_agents
        for agent_key, log_key in AGENT_TO_LOG.items():
            if agent_key in changed_agents:
                path = DECISION_LOG_FILES.get(log_key)
                if path and os.path.exists(path):
                    os.remove(path)
                    print(f"[Launch] Cleared logs for {agent_key} (LLM changed)")
        if changed_agents:
            print(f"[Launch] LLM changed for: {', '.join(changed_agents)}, intent unchanged")
        else:
            print(f"[Launch] Restarting with same config")

    cmd = [
        'python3', MAIN_SCRIPT,
        '--intent', intent,
        '--l2-llm', agent_llm_choices.get('l2_manager', 'claude'),
        '--power-llm', agent_llm_choices.get('power', 'claude'),
        '--scheduler-llm', agent_llm_choices.get('scheduler', 'claude'),
        '--prb-llm', agent_llm_choices.get('prb', 'fine-tuned'),
    ]
    if skip_l2_init:
        cmd.append('--skip-l2-init')

    running_process = subprocess.Popen(
        cmd, cwd=SCRIPT_DIR, start_new_session=True,
    )

    return jsonify({
        'success': True,
        'pid': running_process.pid,
        'intent_changed': intent_changed,
        'changed_agents': list(changed_agents),
        'skip_l2_init': skip_l2_init,
    })


def reset_on_stop():
    """Clear PRB masks and decision logs."""
    try:
        resp = requests.delete(f"{API_BASE}/api/v1/prbmask", timeout=5, verify=False)
        print(f"[Reset] DELETE /api/v1/prbmask -> {resp.status_code}")
    except Exception as e:
        print(f"[Reset] Failed to clear PRB masks: {e}")
    for path in list(DECISION_LOG_FILES.values()) + [SUBINTENTS_FILE]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


@app.route('/api/stop', methods=['POST'])
def stop_agents():
    global running_process

    if running_process is None or running_process.poll() is not None:
        running_process = None
        reset_on_stop()
        return jsonify({'success': True, 'message': 'No agents were running'})

    try:
        os.killpg(os.getpgid(running_process.pid), signal.SIGTERM)
        running_process.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(running_process.pid), signal.SIGKILL)
            running_process.wait(timeout=3)
        except Exception:
            pass
    running_process = None
    reset_on_stop()
    return jsonify({'success': True})


@app.route('/api/intents')
def get_intents():
    try:
        if os.path.exists(SUBINTENTS_FILE):
            with open(SUBINTENTS_FILE, 'r') as f:
                return jsonify(json.load(f))
    except Exception:
        pass
    return jsonify({})


@app.route('/api/restart-gnb', methods=['POST'])
def restart_gnb():
    try:
        resp = requests.post(f"{API_BASE}/api/v1/gnb/restart", timeout=130, verify=False)
        data = resp.json()
        if data.get('status') == 'success':
            return jsonify({'success': True, 'message': data.get('message', 'OK')})
        else:
            return jsonify({'success': False, 'error': data.get('message', 'Unknown error')}), 500
    except requests.Timeout:
        return jsonify({'success': False, 'error': 'Restart timed out'}), 504
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/reset-default', methods=['POST'])
def reset_to_default():
    """Stop agents and reset gNB to defaults: 20 Mbps scheduler limits,
    default SNR targets, and no blocked PRBs."""
    global running_process, current_intent

    errors = []

    # 1. Stop running agents
    if running_process is not None and running_process.poll() is None:
        try:
            os.killpg(os.getpgid(running_process.pid), signal.SIGTERM)
            running_process.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(running_process.pid), signal.SIGKILL)
                running_process.wait(timeout=3)
            except Exception:
                pass
        running_process = None

    # 2. Clear PRB masks (no blocked PRBs)
    try:
        resp = requests.delete(f"{API_BASE}/api/v1/prbmask", timeout=5, verify=False)
        print(f"[ResetDefault] DELETE /api/v1/prbmask -> {resp.status_code}")
    except Exception as e:
        errors.append(f"PRB mask clear failed: {e}")
        print(f"[ResetDefault] Failed to clear PRB masks: {e}")

    # 3. Reset scheduler to 20 Mbps for both slices
    try:
        payload = {"fwa": 20, "mtc": 20}
        resp = requests.put(
            f"{API_BASE}/api/v1/slicemask",
            json=payload, timeout=5, verify=False,
        )
        print(f"[ResetDefault] PUT /api/v1/slicemask {payload} -> {resp.status_code}")
    except Exception as e:
        errors.append(f"Scheduler reset failed: {e}")
        print(f"[ResetDefault] Failed to reset scheduler: {e}")

    # 4. Reset SNR targets to default (130 = 13.0 dB)
    try:
        resp_ues = requests.get(f"{API_BASE}/api/v1/ues", timeout=5, verify=False)
        ues = resp_ues.json() if resp_ues.ok else []
        for ue in ues:
            uid = ue.get('id') or ue.get('uid') or ue.get('ue')
            if uid is not None:
                payload = {"snr_target": 130}
                resp = requests.put(
                    f"{API_BASE}/api/v1/ue/{uid}/snr",
                    json=payload, timeout=5, verify=False,
                )
                print(f"[ResetDefault] PUT /api/v1/ue/{uid}/snr {payload} -> {resp.status_code}")
    except Exception as e:
        errors.append(f"SNR target reset failed: {e}")
        print(f"[ResetDefault] Failed to reset SNR targets: {e}")

    # 5. Clear decision logs, subintents, and current intent
    current_intent = None
    for path in list(DECISION_LOG_FILES.values()) + [SUBINTENTS_FILE]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    if errors:
        return jsonify({
            'success': True,
            'warnings': errors,
            'message': 'Reset completed with some warnings',
        })
    return jsonify({'success': True, 'message': 'Reset to defaults complete'})


@app.route('/api/decisions')
def get_decisions():
    result = {}
    for agent, path in DECISION_LOG_FILES.items():
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    result[agent] = json.load(f)
            else:
                result[agent] = []
        except Exception:
            result[agent] = []
    return jsonify(result)


@app.after_request
def add_no_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


if __name__ == '__main__':
    print("=" * 60)
    print("Multi-Agent Control - Unified Web Interface")
    print("=" * 60)
    print("\nStarting web server on http://0.0.0.0:5001")
    print("Press Ctrl+C to stop\n")
    app.run(host='0.0.0.0', port=5001, debug=True)