"""Metric aggregation layer on top of InfluxDB.

Produces per-UE and per-class summaries from InfluxDB slot-level data.

Also provides interference monitoring data from the RSRP monitor
(measured_interference measurement).
"""

import math
import time
from shared.influx_client import InfluxClient


class MetricAggregator:
    """Aggregates raw InfluxDB scheduler data into agent-consumable formats."""

    def __init__(self, influx: InfluxClient):
        self.influx = influx

    async def query_ue_summary(self, window_seconds: int = 10) -> dict | None:
        """Produce a network_state dict consumed by agents.

        Returns:
        {
            "status": "success",
            "summary": {
                "mtc_count": int,
                "fwa_count": int,
                "fwa_activity_mbps": float,
                "avg_mtc_throughput": float,
                "avg_fwa_throughput": float,
            },
            "analysis": {
                "mtc_ues": { rnti: {"throughput": float, "bler": float} },
                "fwa_ues": { rnti: {"throughput": float, "bler": float} },
            },
            "timestamp": int  (ms)
        }
        """
        rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{window_seconds}s)
                |> filter(fn: (r) => r._measurement == "{self.influx.measurement}")
                |> filter(fn: (r) => r._field == "throughput" or r._field == "bler")
                |> group(columns: ["rnti", "uid", "fiveQI", "_field"])
                |> mean()
        ''')

        if not rows:
            return None

        # Build per-UE metrics
        ue_data = {}  # rnti -> {"throughput": ..., "bler": ..., "fiveQI": ...}
        rnti_uid_map = {}  # rnti -> uid (int)
        for row in rows:
            rnti = row.get("rnti", "")
            five_qi = row.get("fiveQI", "0")
            field = row.get("_field", "")
            value = row.get("_value", "0")

            if rnti not in ue_data:
                ue_data[rnti] = {"throughput": 0.0, "bler": 0.0, "fiveQI": five_qi}

            # Capture uid tag
            uid_str = row.get("uid", "")
            if uid_str and rnti not in rnti_uid_map:
                try:
                    rnti_uid_map[rnti] = int(uid_str)
                except (ValueError, TypeError):
                    pass

            try:
                v = float(value)
                # Throughput is stored in bps in InfluxDB; convert to Mbps
                if field == "throughput":
                    v = v / 1e6
                ue_data[rnti][field] = round(v, 4)
            except (ValueError, TypeError):
                pass

        # Classify into eMBB (fiveQI=9, broadband) vs MTC (other)
        # Matches Lua scheduler: fiveQI == 9 → eMBB, otherwise → MTC
        mtc_ues = {}
        fwa_ues = {}
        for rnti, data in ue_data.items():
            entry = {"throughput": data["throughput"], "bler": data["bler"],
                     "uid": rnti_uid_map.get(rnti)}
            if data["fiveQI"] == "9":
                fwa_ues[rnti] = entry
            else:
                mtc_ues[rnti] = entry

        # Compute summaries
        mtc_throughputs = [u["throughput"] for u in mtc_ues.values()]
        fwa_throughputs = [u["throughput"] for u in fwa_ues.values()]

        avg_mtc = round(sum(mtc_throughputs) / len(mtc_throughputs), 1) if mtc_throughputs else 0.0
        avg_fwa = round(sum(fwa_throughputs) / len(fwa_throughputs), 1) if fwa_throughputs else 0.0
        fwa_activity = round(sum(fwa_throughputs), 1)

        return {
            "status": "success",
            "summary": {
                "mtc_count": len(mtc_ues),
                "fwa_count": len(fwa_ues),
                "fwa_activity_mbps": fwa_activity,
                "avg_mtc_throughput": avg_mtc,
                "avg_fwa_throughput": avg_fwa,
            },
            "analysis": {
                "mtc_ues": mtc_ues,
                "fwa_ues": fwa_ues,
            },
            "timestamp": int(time.time() * 1000),
        }

    async def query_class_summary(self, window_seconds: int = 10) -> dict | None:
        """Extended summary with min/max per class (for scheduler agent).

        Returns the same shape as SchedulerControlAgent.extract_network_summary():
        {
            "mtc_count": int,
            "fwa_count": int,           # eMBB UE count
            "fwa_activity": float,      # eMBB aggregate activity
            "avg_mtc_throughput": float, "min_mtc_throughput": float, "max_mtc_throughput": float,
            "avg_fwa_throughput": float, "min_fwa_throughput": float, "max_fwa_throughput": float,
        }
        """
        state = await self.query_ue_summary(window_seconds)
        if not state:
            return None

        analysis = state["analysis"]
        mtc_throughputs = [u["throughput"] for u in analysis["mtc_ues"].values()]
        fwa_throughputs = [u["throughput"] for u in analysis["fwa_ues"].values()]

        return {
            "mtc_count": state["summary"]["mtc_count"],
            "fwa_count": state["summary"]["fwa_count"],
            "fwa_activity": state["summary"]["fwa_activity_mbps"],
            "avg_mtc_throughput": round(sum(mtc_throughputs) / len(mtc_throughputs), 1) if mtc_throughputs else 0,
            "min_mtc_throughput": round(min(mtc_throughputs), 1) if mtc_throughputs else 0,
            "max_mtc_throughput": round(max(mtc_throughputs), 1) if mtc_throughputs else 0,
            "avg_fwa_throughput": round(sum(fwa_throughputs) / len(fwa_throughputs), 1) if fwa_throughputs else 0,
            "min_fwa_throughput": round(min(fwa_throughputs), 1) if fwa_throughputs else 0,
            "max_fwa_throughput": round(max(fwa_throughputs), 1) if fwa_throughputs else 0,
        }

    async def query_ue_history(self, rnti: str, window_seconds: int = 60, resolution_seconds: int = 1) -> list[dict]:
        """Per-UE time-bucketed history for the future PRB agent.

        Returns list of dicts, one per time bucket:
        [
            {"time": "...", "throughput": float, "bler": float, "pusch_snrx10": float,
             "cqi": float, "dl_rsrp": float, "allocated_rbs": float, "allocated_rb_start": float},
            ...
        ]
        """
        fields = ["throughput", "bler", "pusch_snrx10", "cqi", "dl_rsrp", "allocated_rbs", "allocated_rb_start"]
        field_filter = " or ".join(f'r._field == "{f}"' for f in fields)

        rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{window_seconds}s)
                |> filter(fn: (r) => r._measurement == "{self.influx.measurement}")
                |> filter(fn: (r) => r.rnti == "{rnti}")
                |> filter(fn: (r) => {field_filter})
                |> group(columns: ["_field"])
                |> aggregateWindow(every: {resolution_seconds}s, fn: mean, createEmpty: false)
                |> group()
                |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"])
        ''')

        result = []
        for row in rows:
            entry = {"time": row.get("_time", "")}
            for field in fields:
                val = row.get(field)
                if val is not None and val != "":
                    try:
                        v = float(val)
                        # Throughput is stored in bps in InfluxDB; convert to Mbps
                        if field == "throughput":
                            v = v / 1e6
                        entry[field] = round(v, 2)
                    except (ValueError, TypeError):
                        pass
            if len(entry) > 1:  # has at least one field beyond "time"
                result.append(entry)
        return result

    async def query_prb_quality(self, window_seconds: int = 30) -> list[dict]:
        """Per-PRB-region quality indicators for the future PRB agent.

        Groups data by allocated_rb_start to identify regions with high BLER or low SNR.
        Returns list of dicts:
        [
            {"rb_start": int, "avg_bler": float, "avg_snr": float, "sample_count": int},
            ...
        ]
        """
        rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{window_seconds}s)
                |> filter(fn: (r) => r._measurement == "{self.influx.measurement}")
                |> filter(fn: (r) => r._field == "bler" or r._field == "pusch_snrx10" or r._field == "allocated_rb_start")
                |> group(columns: ["_field"])
                |> group()
                |> pivot(rowKey: ["_time", "rnti"], columnKey: ["_field"], valueColumn: "_value")
                |> filter(fn: (r) => exists r.allocated_rb_start and exists r.bler and exists r.pusch_snrx10)
                |> sort(columns: ["allocated_rb_start"])
        ''')

        # Bin the results by rb_start regions (groups of 10 PRBs)
        bins = {}  # bin_start -> {bler_sum, snr_sum, count}
        BIN_SIZE = 10
        for row in rows:
            try:
                rb_start = int(float(row.get("allocated_rb_start", 0)))
                bler = float(row.get("bler", 0))
                snr = float(row.get("pusch_snrx10", 0))
            except (ValueError, TypeError):
                continue

            bin_start = (rb_start // BIN_SIZE) * BIN_SIZE
            if bin_start not in bins:
                bins[bin_start] = {"bler_sum": 0.0, "snr_sum": 0.0, "count": 0}
            bins[bin_start]["bler_sum"] += bler
            bins[bin_start]["snr_sum"] += snr
            bins[bin_start]["count"] += 1

        result = []
        for rb_start in sorted(bins.keys()):
            b = bins[rb_start]
            result.append({
                "rb_start": rb_start,
                "avg_bler": round(b["bler_sum"] / b["count"], 4),
                "avg_snr": round(b["snr_sum"] / b["count"], 1),
                "sample_count": b["count"],
            })
        return result

    async def query_prb_agent_state(self, window_seconds: int = 3, resolution_ms: int = 100) -> dict | None:
        """State snapshot for the PRB blocking agent.

        Combines scheduler allocation data with per-PRB power measurements
        from the USRP monitor to build per-UE serving cell RSRP timeseries.

        The USRP monitor (rsrp_monitor_prb.c) reports per-PRB power on ch0/ch1
        (serving cells A:0/A:1). The pinning scheduler fixes UE allocations for
        ~100ms (200 slots at SCS 30kHz). By cross-referencing, we compute
        per-UE RSRP as seen by each serving cell — the key predictive feature
        for interference forecasting on ch2/ch3 (neighbor cells).

        Returns:
        {
            "ues": {
                "<rnti>": {
                    "serving_rsrp": float,          # avg dl_rsrp from gNB over window
                    "target_snrx10": float,         # current power control target
                    "bwp_start": int,
                    "bwp_size": int,
                    "allocated_rb_start": int,      # pinned PRB start (-1 if unknown)
                    "allocated_rbs": int,           # pinned PRB count (0 if unknown)
                },
                ...
            },
            "per_ue_rsrp": {
                "<rnti>": {
                    "ch0": [{"time": "...", "power": float}, ...],
                    "ch1": [{"time": "...", "power": float}, ...],
                },
                ...
            },
            "neighbors": {
                "ch0": [{"time": "...", "power": float}, ...],
                "ch1": [{"time": "...", "power": float}, ...],
            },
            "neighbors_latest": {
                "ch0": float,
                "ch1": float,
            },
        }
        """
        # 1. Per-UE metrics from scheduler: RSRP, SNR target, BWP, allocation
        #    Zero dl_rsrp values are filtered out post-query (0 = no measurement in C code)
        ue_rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{window_seconds}s)
                |> filter(fn: (r) => r._measurement == "{self.influx.measurement}")
                |> filter(fn: (r) => r._field == "dl_rsrp" or r._field == "target_snrx10"
                                  or r._field == "bwp_start" or r._field == "bwp_size"
                                  or r._field == "allocated_rb_start" or r._field == "allocated_rbs")
                |> group(columns: ["rnti", "uid", "_field"])
                |> mean()
        ''')

        ues = {}
        rnti_uid_map = {}  # rnti -> uid (int)
        if ue_rows:
            for row in ue_rows:
                rnti = row.get("rnti", "")
                field = row.get("_field", "")
                val = row.get("_value", "")
                if not rnti:
                    continue
                if rnti not in ues:
                    ues[rnti] = {}
                # Capture uid tag
                uid_str = row.get("uid", "")
                if uid_str and rnti not in rnti_uid_map:
                    try:
                        rnti_uid_map[rnti] = int(uid_str)
                    except (ValueError, TypeError):
                        pass
                try:
                    ues[rnti][field] = round(float(val), 2)
                except (ValueError, TypeError):
                    pass

        # Normalize field names; treat dl_rsrp == 0 as no data (C code sentinel)
        ue_result = {}
        for rnti, data in ues.items():
            rsrp = data.get("dl_rsrp")
            if rsrp is not None and rsrp == 0.0:
                rsrp = None
            ue_result[rnti] = {
                "uid": rnti_uid_map.get(rnti),
                "serving_rsrp": rsrp,
                "target_snrx10": data.get("target_snrx10"),
                "bwp_start": int(data.get("bwp_start", 0)),
                "bwp_size": int(data.get("bwp_size", 0)),
                "allocated_rb_start": int(data.get("allocated_rb_start", -1)),
                "allocated_rbs": int(data.get("allocated_rbs", 0)),
            }

        resolution_flux = f"{resolution_ms}ms"

        # 2. Per-PRB power timeseries from ch0/ch1 (serving cells)
        #    rsrp_monitor_prb.c reports prb_0..prb_N per channel every 5ms
        prb_rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{window_seconds}s)
                |> filter(fn: (r) => r._measurement == "measured_interference")
                |> filter(fn: (r) => r.source == "rsrp_monitor")
                |> filter(fn: (r) => r._field =~ /^prb_[0-9]+$/)
                |> filter(fn: (r) => r.channel == "0" or r.channel == "1")
                |> group(columns: ["channel", "_field"])
                |> aggregateWindow(every: {resolution_flux}, fn: mean, createEmpty: false)
                |> sort(columns: ["_time"])
        ''')

        # Build per-PRB power matrix: {channel: {time_str: {prb_idx: power_dbm}}}
        ch_prb_data = {"0": {}, "1": {}}
        if prb_rows:
            for row in prb_rows:
                ch = row.get("channel", "")
                field = row.get("_field", "")
                t = row.get("_time", "")
                val = row.get("_value", "")
                if ch not in ch_prb_data or not field.startswith("prb_"):
                    continue
                try:
                    prb_idx = int(field[4:])  # "prb_X" -> X
                    power = float(val)
                except (ValueError, IndexError):
                    continue
                if t not in ch_prb_data[ch]:
                    ch_prb_data[ch][t] = {}
                ch_prb_data[ch][t][prb_idx] = power

        # 3. Compute per-UE RSRP timeseries from serving cells
        #    For each UE, average ch0/ch1 per-PRB power at their allocated PRBs.
        #    Averaging is done in linear domain (mW) then converted back to dBm.
        per_ue_rsrp = {}
        for rnti, ue_data in ue_result.items():
            rb_start = ue_data["allocated_rb_start"]
            num_rbs = ue_data["allocated_rbs"]
            if rb_start < 0 or num_rbs <= 0:
                continue

            per_ue_rsrp[rnti] = {"ch0": [], "ch1": []}

            for ch in ["0", "1"]:
                ch_key = f"ch{ch}"
                time_data = ch_prb_data.get(ch, {})

                for t in sorted(time_data.keys()):
                    prb_powers = time_data[t]
                    # Collect power (dBm) at UE's allocated PRBs
                    ue_prb_powers = []
                    for prb in range(rb_start, rb_start + num_rbs):
                        if prb in prb_powers:
                            ue_prb_powers.append(prb_powers[prb])

                    if ue_prb_powers:
                        # Average in linear domain, convert back to dBm
                        linear_sum = sum(10.0 ** (p / 10.0) for p in ue_prb_powers)
                        avg_dbm = 10.0 * math.log10(linear_sum / len(ue_prb_powers) + 1e-20)
                        per_ue_rsrp[rnti][ch_key].append({
                            "time": t,
                            "power": round(avg_dbm, 2),
                        })

        # 4. Aggregate total_power timeseries for ch0/ch1 (context)
        total_rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{window_seconds}s)
                |> filter(fn: (r) => r._measurement == "measured_interference")
                |> filter(fn: (r) => r.source == "rsrp_monitor")
                |> filter(fn: (r) => r._field == "total_power")
                |> filter(fn: (r) => r.channel == "0" or r.channel == "1")
                |> group(columns: ["channel"])
                |> aggregateWindow(every: {resolution_flux}, fn: mean, createEmpty: false)
                |> sort(columns: ["_time"])
        ''')

        neighbors = {"ch0": [], "ch1": []}
        if total_rows:
            for row in total_rows:
                ch = row.get("channel", "")
                val = row.get("_value", "")
                t = row.get("_time", "")
                key = f"ch{ch}"
                if key in neighbors and val != "":
                    try:
                        neighbors[key].append({"time": t, "power": round(float(val), 2)})
                    except (ValueError, TypeError):
                        pass

        neighbors_latest = {}
        for key, series in neighbors.items():
            neighbors_latest[key] = series[-1]["power"] if series else None

        return {
            "ues": ue_result,
            "per_ue_rsrp": per_ue_rsrp,
            "neighbors": neighbors,
            "neighbors_latest": neighbors_latest,
        }

    async def query_interference_history(self, window_seconds: int = 3, resolution_ms: int = 100) -> dict | None:
        """RSRP interference timeseries from the USRP monitor (ground truth).

        The rsrp_monitor_prb.c program pushes to InfluxDB:
          measurement: measured_interference
          tags: source=rsrp_monitor, channel=0..3, port=A:0/A:1/B:0/B:1
          fields: total_power (dBm), prb_0..prb_N (per-PRB dBm), center_freq, bandwidth

        Channel mapping:
          0 (A:0), 1 (A:1) = serving cells (our operator, predictive inputs)
          2 (B:0), 3 (B:1) = neighbor cells (other operator, interference ground truth)

        Interference is detected when neighbor channel 2 or 3 > -70 dBm.

        Returns:
        {
            "channels": {
                "0": [{"time": "...", "power": float}, ...],  # serving A:0
                "1": [{"time": "...", "power": float}, ...],  # serving A:1
                "2": [{"time": "...", "power": float}, ...],  # neighbor B:0
                "3": [{"time": "...", "power": float}, ...],  # neighbor B:1
            },
            "interference_detected": bool,  # any neighbor channel > -70 dBm now
            "latest": {
                "0": float, "1": float, "2": float, "3": float  # latest power per channel
            }
        }
        """
        resolution_flux = f"{resolution_ms}ms"
        rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{window_seconds}s)
                |> filter(fn: (r) => r._measurement == "measured_interference")
                |> filter(fn: (r) => r.source == "rsrp_monitor")
                |> filter(fn: (r) => r._field == "total_power")
                |> group(columns: ["channel"])
                |> aggregateWindow(every: {resolution_flux}, fn: mean, createEmpty: false)
                |> sort(columns: ["_time"])
        ''')

        if not rows:
            return None

        channels = {"0": [], "1": [], "2": [], "3": []}
        for row in rows:
            ch = row.get("channel", "")
            val = row.get("_value", "")
            t = row.get("_time", "")
            if ch in channels and val != "":
                try:
                    channels[ch].append({"time": t, "power": round(float(val), 2)})
                except (ValueError, TypeError):
                    pass

        # Latest value per channel
        latest = {}
        for ch, series in channels.items():
            latest[ch] = series[-1]["power"] if series else None

        # Interference detected if neighbor channels (2 or 3) > -70 dBm
        interference = False
        for ch in ["2", "3"]:
            if latest.get(ch) is not None and latest[ch] > -70.0:
                interference = True
                break

        return {
            "channels": channels,
            "interference_detected": interference,
            "latest": latest,
        }

    async def query_last_interference_alert(self, lookback_seconds: int = 60, threshold_dbm: float = -70.0) -> dict:
        """Find the last time each neighbor channel exceeded the interference threshold.

        Returns:
        {
            "cell2": {"time": "ISO timestamp", "power": float} or None,
            "cell3": {"time": "ISO timestamp", "power": float} or None,
        }
        """
        rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{lookback_seconds}s)
                |> filter(fn: (r) => r._measurement == "measured_interference")
                |> filter(fn: (r) => r.source == "rsrp_monitor")
                |> filter(fn: (r) => r._field == "total_power")
                |> filter(fn: (r) => r.channel == "2" or r.channel == "3")
                |> filter(fn: (r) => r._value > {threshold_dbm})
                |> group(columns: ["channel"])
                |> last()
        ''')

        # channel "2" = cell2 (neighbor low edge), channel "3" = cell3 (neighbor high edge)
        result = {"cell2": None, "cell3": None}
        ch_to_cell = {"2": "cell2", "3": "cell3"}

        if rows:
            for row in rows:
                ch = row.get("channel", "")
                cell_key = ch_to_cell.get(ch)
                if cell_key:
                    t = row.get("_time", "")
                    val = row.get("_value", "")
                    try:
                        result[cell_key] = {"time": t, "power": round(float(val), 2)}
                    except (ValueError, TypeError):
                        pass

        return result

    async def query_overlap_prb_ranges(self, lookback_seconds: int = 30) -> dict | None:
        """Discover the actual overlap PRB ranges from the RSRP monitor edge data.

        Queries the distinct prb_N field names for edge=low (cell2) and
        edge=high (cell3) channels to determine the real overlap ranges
        as configured on the RSRP monitor (via its -o flag).

        Returns:
            {
                'cell2': {'start_prb': int, 'num_prbs': int, 'label': str},
                'cell3': {'start_prb': int, 'num_prbs': int, 'label': str},
            }
            or None if no edge data is found.
        """
        rows = await self.influx.query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{lookback_seconds}s)
                |> filter(fn: (r) => r._measurement == "measured_interference")
                |> filter(fn: (r) => r.source == "rsrp_monitor")
                |> filter(fn: (r) => r._field =~ /^prb_[0-9]+$/)
                |> filter(fn: (r) => r.edge == "low" or r.edge == "high")
                |> group(columns: ["edge", "_field"])
                |> last()
        ''')

        if not rows:
            return None

        # Collect PRB indices per edge
        edge_prbs = {"low": set(), "high": set()}
        for row in rows:
            edge = row.get("edge", "")
            field = row.get("_field", "")
            if edge in edge_prbs and field.startswith("prb_"):
                try:
                    edge_prbs[edge].add(int(field[4:]))
                except ValueError:
                    pass

        if not edge_prbs["low"] and not edge_prbs["high"]:
            return None

        result = {}
        if edge_prbs["low"]:
            prbs = sorted(edge_prbs["low"])
            result["cell2"] = {
                "start_prb": prbs[0],
                "num_prbs": len(prbs),
                "label": "neighbor low edge",
            }
        if edge_prbs["high"]:
            prbs = sorted(edge_prbs["high"])
            result["cell3"] = {
                "start_prb": prbs[0],
                "num_prbs": len(prbs),
                "label": "neighbor high edge",
            }

        return result
