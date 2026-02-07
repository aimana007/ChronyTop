#!/usr/bin/env python3
# ChronyTop 2 enhanced:
# - tracking + labeled graphs + health
# - sources trust + poll interval + sourcestats + network noise
# - CPU temp (coretemp sysfs) with robust fallbacks
# - rate-limited chronyc calls (tracking 1s, sources 5s, sourcestats 20s)
# - reach visualization (8-bit dot bar)
# - chronyd/sync health: detects daemon down + no reachable/selected sources

import curses, subprocess, time, re, statistics, os, glob
from collections import deque
from datetime import datetime

OFFSET_SCALE = (-0.050, 0.050)   # seconds (-50ms..+50ms)
RMS_SCALE    = (0.000, 0.050)    # seconds (0..50ms)
FREQ_SCALE   = (-100, 100)       # ppm
SKEW_SCALE   = (0, 20)           # ppm (tracking skew)
TEMP_SCALE   = (20.0, 90.0)      # °C (adjust if your environment differs)

MAX_SRC_ROWS = 7

# Rate limiting for chronyc calls
TRACKING_REFRESH_S    = 1.0
SOURCES_REFRESH_S     = 5.0
SOURCESTATS_REFRESH_S = 20.0

# Chrony "stale" thresholds (seconds since last successful poll)
TRACKING_STALE_S    = 5.0
SOURCES_STALE_S     = 15.0
SOURCESTATS_STALE_S = 60.0


def fmt_ms(seconds, signed=False):
    ms = seconds * 1000.0
    return f"{ms:+.3f}ms" if signed else f"{ms:.3f}ms"


def fmt_ppm(v, signed=False):
    return f"{v:+.3f}ppm" if signed else f"{v:.3f}ppm"


def fmt_c(v):
    return f"{v:.1f}°C"


def window_stats(data, n=None):
    if not data:
        return (0.0, 0.0, 0.0)
    xs = list(data)[-n:] if n else list(data)
    return (min(xs), sum(xs) / len(xs), max(xs))


class TimeTop:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.history_size = 120

        self.offset_history = deque(maxlen=self.history_size)
        self.rms_history    = deque(maxlen=self.history_size)
        self.freq_history   = deque(maxlen=self.history_size)
        self.skew_history   = deque(maxlen=self.history_size)
        self.temp_history   = deque(maxlen=self.history_size)  # max/package temp in °C

        self.last_offset = None
        self.last_monotonic = time.monotonic()

        # CPU temp sources (discovered once; may be empty)
        self.temp_sources = self.discover_temp_sources()

        # Chronyc caching / rate limiting
        self._chrony_cache = {
            "tracking":       {"out": None, "last_try": 0.0, "last_ok": 0.0, "interval": TRACKING_REFRESH_S},
            "sources -v":     {"out": None, "last_try": 0.0, "last_ok": 0.0, "interval": SOURCES_REFRESH_S},
            "sourcestats -v": {"out": None, "last_try": 0.0, "last_ok": 0.0, "interval": SOURCESTATS_REFRESH_S},
        }

        curses.start_color()
        curses.use_default_colors()

        curses.init_pair(1, curses.COLOR_GREEN,   -1)
        curses.init_pair(2, curses.COLOR_YELLOW,  -1)
        curses.init_pair(3, curses.COLOR_RED,     -1)
        curses.init_pair(4, curses.COLOR_CYAN,    -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
        curses.init_pair(6, curses.COLOR_WHITE,   -1)

        curses.curs_set(0)
        self.stdscr.nodelay(1)

        # Graph scaling
        self.autoscale = False  # toggle with 'a' (default: fixed scales)

    # ---------- display helpers ----------
    def addn(self, y, x, s, attr=0, maxw=None):
        try:
            h, w = self.stdscr.getmaxyx()
            if y < 0 or y >= h or x >= w:
                return
            if maxw is None:
                maxw = w - x
            if maxw <= 0:
                return
            self.stdscr.addnstr(y, x, s, maxw, attr)
        except:
            pass

    def divider(self, y, ch="─"):
        h, w = self.stdscr.getmaxyx()
        if 0 <= y < h:
            try:
                self.stdscr.hline(y, 0, ch, max(0, w - 1), curses.color_pair(6))
            except:
                pass

    # ---------- chrony runner ----------
    def run_chronyc(self, cmd, timeout=2):
        try:
            p = subprocess.run(
                ["chronyc"] + cmd.split(),
                capture_output=True, text=True, timeout=timeout
            )
            if p.returncode != 0:
                return (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
            return p.stdout
        except:
            return None

    def chrony_out_is_error(self, out):
        if out is None:
            return True
        s = out.strip()
        if not s:
            return True
        # chronyc typical daemon/socket errors
        needles = [
            "Cannot talk to daemon",
            "506",
            "Could not open command socket",
            "Connection refused",
            "No such file or directory",
            "Operation not permitted",
        ]
        return any(n in s for n in needles)

    def chronyc_cached(self, cmd):
        entry = self._chrony_cache.get(cmd)
        if not entry:
            return self.run_chronyc(cmd)

        now = time.monotonic()
        due = (now - entry["last_try"]) >= entry["interval"]
        if due or entry["out"] is None:
            entry["last_try"] = now
            out = self.run_chronyc(cmd)
            # Accept output only if it's non-empty and not an obvious error
            if out is not None and out.strip() != "" and not self.chrony_out_is_error(out):
                entry["out"] = out
                entry["last_ok"] = now
        return entry["out"]

    def chrony_age(self, cmd):
        entry = self._chrony_cache.get(cmd)
        if not entry:
            return None
        if entry["last_ok"] <= 0:
            return float("inf")
        return max(0.0, time.monotonic() - entry["last_ok"])

    # ---------- sysfs helpers ----------
    def read_text(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
        except:
            return None

    def read_int(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return int(f.read().strip())
        except:
            return None

    # ---------- parsing helpers ----------
    def _search_float(self, out, pattern):
        m = re.search(pattern, out)
        return float(m.group(1)) if m else None

    def _normalize_name(self, name):
        if not name:
            return ""
        return name.rstrip(">")

    def _to_seconds(self, value, unit):
        unit = unit.lower()
        if unit == "s":
            return value
        if unit == "ms":
            return value / 1_000.0
        if unit == "us":
            return value / 1_000_000.0
        if unit == "ns":
            return value / 1_000_000_000.0
        return value

    def _parse_span_seconds(self, s):
        if not s:
            return None
        m = re.match(r"^(\d+)([smhd])?$", s.strip())
        if not m:
            return None
        v = int(m.group(1))
        u = m.group(2)
        if u is None:
            return float(v)
        if u == "s":
            return float(v)
        if u == "m":
            return float(v) * 60.0
        if u == "h":
            return float(v) * 3600.0
        if u == "d":
            return float(v) * 86400.0
        return float(v)

    # ---------- reach visualization ----------
    def reach_dots(self, reach, newest_left=True):
        """
        reach: int 0..255 representing 8-bit reach register.
        This visualization matches observed behavior in practice:
        newest(left) -> oldest(right) when newest_left=True.
        """
        if reach is None:
            return "????????"
        bits = [((reach >> i) & 1) for i in range(8)]
        if newest_left:
            bits = list(reversed(bits))
        return "".join("●" if b else "○" for b in bits)

    # ---------- CPU temperature discovery (robust fallbacks) ----------
    def discover_temp_sources(self):
        sources = []

        coretemp_hwmons = []
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            hw_name = self.read_text(os.path.join(hw, "name"))
            if hw_name == "coretemp":
                coretemp_hwmons.append(hw)

        def collect_hwmon(hw_path, want_label_pred=None):
            out = []
            for inp_path in sorted(glob.glob(os.path.join(hw_path, "temp*_input"))):
                base = os.path.basename(inp_path).replace("_input", "")
                lbl_path = os.path.join(hw_path, f"{base}_label")
                lbl = self.read_text(lbl_path)
                if lbl is None:
                    lbl = base
                if want_label_pred and not want_label_pred(lbl):
                    continue
                out.append({"label": lbl, "path": inp_path, "kind": "hwmon"})
            return out

        # 1) Ideal: Package temps
        for hw in coretemp_hwmons:
            sources.extend(collect_hwmon(hw, want_label_pred=lambda l: "Package" in l))
        if sources:
            def pkg_key(s):
                m = re.search(r"Package id\s+(\d+)", s["label"])
                return int(m.group(1)) if m else 999
            sources.sort(key=pkg_key)
            return sources

        # 2) Common CPU-wide labels
        prefer_labels = ("Tctl", "Tdie", "Physical id", "CPU", "Pkg", "Package")
        for hw in coretemp_hwmons:
            sources.extend(collect_hwmon(hw, want_label_pred=lambda l: any(k in l for k in prefer_labels)))
        if sources:
            return sources

        # 3) Any coretemp temps
        for hw in coretemp_hwmons:
            sources.extend(collect_hwmon(hw))
        if sources:
            return sources

        # 4) thermal zones x86_pkg_temp
        for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            t = self.read_text(os.path.join(tz, "type"))
            if t != "x86_pkg_temp":
                continue
            temp_path = os.path.join(tz, "temp")
            if os.path.exists(temp_path):
                sources.append({"label": f"{os.path.basename(tz)}:{t}", "path": temp_path, "kind": "thermal"})
        if sources:
            return sources

        # 5) any thermal zones (max across all zones)
        for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            t = self.read_text(os.path.join(tz, "type")) or "unknown"
            temp_path = os.path.join(tz, "temp")
            if os.path.exists(temp_path):
                sources.append({"label": f"{os.path.basename(tz)}:{t}", "path": temp_path, "kind": "thermal"})
        return sources

    def poll_cpu_temps(self):
        readings = []
        max_c = None
        for s in self.temp_sources:
            raw = self.read_int(s["path"])
            if raw is None:
                continue
            temp_c = raw / 1000.0
            readings.append((s["label"], temp_c))
            if max_c is None or temp_c > max_c:
                max_c = temp_c
        return readings, max_c

    def temp_freq_coupling(self, window=20):
        if len(self.temp_history) < 3 or len(self.freq_history) < 3:
            return ("Temp↔Freq: -", 2)

        n = min(window, len(self.temp_history), len(self.freq_history))
        t0, t1 = self.temp_history[-n], self.temp_history[-1]
        f0, f1 = self.freq_history[-n], self.freq_history[-1]

        dt = t1 - t0
        df = f1 - f0

        if abs(dt) < 0.2 and abs(df) < 0.2:
            return ("Temp↔Freq: stable", 1)
        if dt > 0.2 and df > 0.2:
            return (f"Temp↔Freq: Temp↑ Freq↑ (Δ{dt:+.1f}°C, Δ{df:+.2f}ppm)", 1)
        if dt < -0.2 and df < -0.2:
            return (f"Temp↔Freq: Temp↓ Freq↓ (Δ{dt:+.1f}°C, Δ{df:+.2f}ppm)", 1)
        return (f"Temp↔Freq: diverge (Δ{dt:+.1f}°C, Δ{df:+.2f}ppm)", 2)

    # ---------- parse chronyc tracking ----------
    def parse_tracking(self, out):
        if not out:
            return None
        data = {}
        m = re.search(r"System time\s+:\s+([-\d.]+)\s+seconds\s+(slow|fast)", out)
        if m:
            v = float(m.group(1))
            direction = m.group(2)
            data["offset"] = v if direction == "fast" else -v
        data["rms"]  = self._search_float(out, r"RMS offset\s+:\s+([-\d.]+)")
        data["freq"] = self._search_float(out, r"Frequency\s+:\s+([-\d.]+)")
        data["skew"] = self._search_float(out, r"Skew\s+:\s+([-\d.]+)")
        for k in ("offset", "rms", "freq", "skew"):
            if data.get(k) is None:
                data[k] = 0.0
        return data

    # ---------- parse chronyc sources -v ----------
    def parse_sources_v(self, out):
        if not out:
            return []
        lines = [ln.rstrip("\n") for ln in out.splitlines()]
        data_lines = []
        for ln in lines:
            if ln.startswith("===="):
                data_lines = []
                continue
            if not ln.strip():
                continue
            if ln.startswith("MS ") or ln.startswith("Name/IP"):
                continue
            if re.match(r"^[\^\=\#\?\~\-\+]{1,2}[\*\+\-\?x]?\s", ln):
                data_lines.append(ln)

        parsed = []
        for ln in data_lines:
            parts = ln.split()
            if len(parts) < 6:
                continue

            ms = parts[0]
            name = parts[1]
            key = self._normalize_name(name)

            try:
                stratum = int(parts[2])
            except:
                stratum = None
            try:
                poll = int(parts[3])
            except:
                poll = None

            reach_raw = parts[4]
            try:
                reach = int(reach_raw, 8) if re.match(r"^[0-7]+$", reach_raw) else None
            except:
                reach = None
            try:
                last_rx = int(parts[5])
            except:
                last_rx = None

            rest = " ".join(parts[6:])
            offset_s = None
            err_s = None
            m = re.search(r"([+\-]?\d+(?:\.\d+)?)(ns|us|ms|s)\[", rest)
            if m:
                offset_s = self._to_seconds(float(m.group(1)), m.group(2))
            m2 = re.search(r"\+\/-\s*([+\-]?\d+(?:\.\d+)?)(ns|us|ms|s)", rest)
            if m2:
                err_s = self._to_seconds(float(m2.group(1)), m2.group(2))

            parsed.append({
                "ms": ms,
                "name": name,
                "key": key,
                "stratum": stratum,
                "poll": poll,
                "reach": reach,
                "reach_octal": reach_raw,
                "last_rx": last_rx,
                "offset_s": offset_s,
                "err_s": err_s,
                "ss_np": None,
                "ss_nr": None,
                "ss_span_s": None,
                "ss_freq_ppm": None,
                "ss_fskew_ppm": None,
                "ss_offset_s": None,
                "ss_stddev_s": None,
                "raw": ln,
            })
        return parsed

    # ---------- parse chronyc sourcestats -v ----------
    def parse_sourcestats_v(self, out):
        if not out:
            return {}
        lines = [ln.rstrip("\n") for ln in out.splitlines()]
        data = {}
        in_table = False
        for ln in lines:
            if ln.startswith("===="):
                in_table = True
                continue
            if not in_table:
                continue
            if not ln.strip():
                continue
            if ln.startswith("Name/IP"):
                continue

            parts = ln.split()
            if len(parts) < 8:
                continue

            name = parts[0]
            key = self._normalize_name(name)

            try:
                np_ = int(parts[1])
            except:
                np_ = None
            try:
                nr_ = int(parts[2])
            except:
                nr_ = None

            span_s = self._parse_span_seconds(parts[3])

            try:
                freq_ppm = float(parts[4])
            except:
                freq_ppm = None
            try:
                fskew_ppm = float(parts[5])
            except:
                fskew_ppm = None

            off_s = None
            std_s = None
            m_off = re.match(r"^([+\-]?\d+(?:\.\d+)?)(ns|us|ms|s)$", parts[6])
            if m_off:
                off_s = self._to_seconds(float(m_off.group(1)), m_off.group(2))
            m_std = re.match(r"^([+\-]?\d+(?:\.\d+)?)(ns|us|ms|s)$", parts[7])
            if m_std:
                std_s = self._to_seconds(float(m_std.group(1)), m_std.group(2))

            data[key] = {
                "name": name,
                "key": key,
                "np": np_,
                "nr": nr_,
                "span_s": span_s,
                "freq_ppm": freq_ppm,
                "fskew_ppm": fskew_ppm,
                "offset_s": off_s,
                "stddev_s": std_s,
                "raw": ln,
            }
        return data

    def merge_sourcestats(self, sources, ss_map):
        if not sources:
            return
        for s in sources:
            ss = ss_map.get(s.get("key", ""))
            if not ss:
                continue
            s["ss_np"] = ss.get("np")
            s["ss_nr"] = ss.get("nr")
            s["ss_span_s"] = ss.get("span_s")
            s["ss_freq_ppm"] = ss.get("freq_ppm")
            s["ss_fskew_ppm"] = ss.get("fskew_ppm")
            s["ss_offset_s"] = ss.get("offset_s")
            s["ss_stddev_s"] = ss.get("stddev_s")

    # ---------- polling interval ----------
    def selected_poll_seconds(self, sources):
        sel = None
        for s in sources:
            if "*" in (s.get("ms") or ""):
                sel = s
                break
        if not sel and sources:
            sel = sources[0]
        if not sel:
            return None, None
        poll = sel.get("poll")
        if poll is None:
            return sel.get("name"), None
        return sel.get("name"), (2 ** poll)

    # ---------- network noise indicator ----------
    def network_noise_indicator(self, sources):
        if not sources:
            return ("Net noise: -", 2)

        sel = None
        for s in sources:
            if "*" in (s.get("ms") or ""):
                sel = s
                break
        if not sel:
            sel = sources[0]

        sel_sd = sel.get("ss_stddev_s")
        if sel_sd is None:
            return ("Net noise: no sourcestats stddev for selected", 2)

        sds = [s.get("ss_stddev_s") for s in sources if s.get("ss_stddev_s") is not None]
        if len(sds) < 2:
            return ("Net noise: insufficient sources with stddev", 2)

        med = statistics.median(sds)
        floor = 50e-6
        ratio = sel_sd / max(med, floor)

        sel_ms = sel_sd * 1000.0
        med_ms = med * 1000.0
        abs_gap_ms = sel_ms - med_ms

        if ratio >= 3.0 and abs_gap_ms >= 0.50:
            status = "OUTLIER"
            col = 3
        elif ratio >= 2.0 and abs_gap_ms >= 0.20:
            status = "ELEVATED"
            col = 2
        else:
            status = "OK"
            col = 1

        text = f"Net noise: sel SD={sel_ms:.3f}ms  median={med_ms:.3f}ms  ratio={ratio:.2f}  {status}"
        return (text, col)

    # ---------- sync health ----------
    def chrony_sync_health(self, sources, ages):
        """
        Returns list of (text, color_pair).
        Red if chronyd appears down or all sources unreachable/no selected.
        """
        alerts = []

        a_trk = ages.get("tracking")
        a_src = ages.get("sources -v")
        a_ss  = ages.get("sourcestats -v")

        # Chronyd / data pipeline checks
        if a_trk == float("inf") or a_src == float("inf"):
            alerts.append(("CHRONYD DOWN / NO DATA (never got valid chronyc output)", 3))
            return alerts  # no point continuing

        if a_trk is not None and a_trk > TRACKING_STALE_S:
            alerts.append((f"CHRONYC STALE: tracking age {a_trk:.1f}s", 3))
        if a_src is not None and a_src > SOURCES_STALE_S:
            alerts.append((f"CHRONYC STALE: sources age {a_src:.1f}s", 3))
        if a_ss is not None and a_ss > SOURCESTATS_STALE_S:
            alerts.append((f"CHRONYC STALE: sourcestats age {a_ss:.1f}s", 2))

        # If sources are stale, don’t over-interpret sync state
        if (a_src is not None and a_src > SOURCES_STALE_S):
            return alerts

        if not sources:
            alerts.append(("NO NTP SOURCES PARSED", 3))
            return alerts

        reachable = 0
        selected = 0
        combined = 0
        unusable = 0
        any_rx_recent = False

        for s in sources:
            ms = s.get("ms") or ""
            r = s.get("reach")
            rx = s.get("last_rx")

            if "*" in ms:
                selected += 1
            if "+" in ms:
                combined += 1
            if "?" in ms:
                unusable += 1

            if r is not None and r > 0:
                reachable += 1

            if rx is not None and rx <= 256:
                any_rx_recent = True

        # Hard failures
        if reachable == 0:
            alerts.append(("NO REACHABLE NTP SOURCES (reach=0 across all)", 3))
        if selected == 0:
            alerts.append(("NO SELECTED SOURCE (* missing) — UNSYNCED", 3))
        if not any_rx_recent:
            alerts.append(("ALL SOURCES STALE (LastRx very old)", 3))

        # Degraded
        if reachable == 1:
            alerts.append(("DEGRADED: only one reachable source", 2))
        elif reachable >= 2 and selected >= 1 and combined >= 1 and not alerts:
            # Only show an explicit OK if nothing else is complaining
            alerts.append(("CHRONY SYNC: OK", 1))

        return alerts

    # ---------- health ----------
    def health(self, sources=None, ages=None):
        alerts = []

        # Chrony sync / daemon status first (high priority)
        if sources is not None and ages is not None:
            alerts.extend(self.chrony_sync_health(sources, ages))

        # Oscillator health only if we have tracking history
        if not self.offset_history:
            return alerts

        off_ms = abs(self.offset_history[-1]) * 1000.0
        rms_ms = abs(self.rms_history[-1]) * 1000.0 if self.rms_history else 0.0
        freq_ppm = abs(self.freq_history[-1]) if self.freq_history else 0.0
        skew_ppm = abs(self.skew_history[-1]) if self.skew_history else 0.0

        if off_ms > 50:
            alerts.append(("CLOCK STEP / LARGE OFFSET", 3))
        elif off_ms > 10:
            alerts.append(("HIGH OFFSET", 2))

        if rms_ms > 10:
            alerts.append(("JITTER (RMS HIGH)", 2))

        if freq_ppm > 100:
            alerts.append(("DRIFT (FREQ HIGH)", 3))

        if skew_ppm > 5:
            alerts.append(("UNSTABLE OSC (SKEW HIGH)", 2))

        now_m = time.monotonic()
        dt_m = now_m - self.last_monotonic
        self.last_monotonic = now_m

        if self.last_offset is not None:
            delta = abs(self.offset_history[-1] - self.last_offset)
            if delta > 0.250:
                alerts.append(("TIME JUMP (OFFSET DELTA)", 3))
            if dt_m > 15:
                alerts.append(("SUSPEND/PAUSE DETECTED (MONOTONIC GAP)", 3))

        self.last_offset = self.offset_history[-1]
        return alerts

    # ---------- trust scoring ----------
    def source_trust(self, src):
        ms = src.get("ms", "")
        reach = src.get("reach")
        last_rx = src.get("last_rx")

        off = src.get("offset_s")
        err = src.get("err_s")
        stddev = src.get("ss_stddev_s")
        fskew  = src.get("ss_fskew_ppm")

        flags = []
        score = 100.0

        if "?" in ms:
            flags.append("UNREACH")
            score -= 55
        if "x" in ms:
            flags.append("BAD")
            score -= 45
        if "~" in ms:
            flags.append("TOO_VAR")
            score -= 20
        if "*" in ms:
            score += 5
        if "+" in ms:
            score += 2

        if reach is None:
            score -= 10
            flags.append("NO_RCH")
        else:
            if reach == 0:
                score -= 45
                flags.append("RCH=0")
            elif reach < 0o17:
                score -= 15
                flags.append("LOW_RCH")

        if last_rx is None:
            score -= 10
            flags.append("NO_RX")
        else:
            if last_rx > 256:
                score -= 25
                flags.append("STALE")
            elif last_rx > 64:
                score -= 15
                flags.append("AGING")

        if off is not None:
            off_ms = abs(off) * 1000.0
            if off_ms > 50:
                score -= 35
                flags.append("OFF>50ms")
            elif off_ms > 10:
                score -= 15
                flags.append("OFF>10ms")
            elif off_ms > 2:
                score -= 5
        else:
            score -= 6
            flags.append("NO_OFF")

        if err is not None:
            err_ms = err * 1000.0
            if err_ms > 100:
                score -= 18
                flags.append("ERR>100ms")
            elif err_ms > 50:
                score -= 12
                flags.append("ERR>50ms")
            elif err_ms > 10:
                score -= 6
                flags.append("ERR>10ms")

        if stddev is None:
            score -= 6
            flags.append("NO_SD")
        else:
            sd_ms = stddev * 1000.0
            if sd_ms > 15:
                score -= 30
                flags.append("SD>15ms")
            elif sd_ms > 5:
                score -= 18
                flags.append("SD>5ms")
            elif sd_ms > 1:
                score -= 7
                flags.append("SD>1ms")

        if fskew is None:
            score -= 3
        else:
            if fskew > 2.0:
                score -= 12
                flags.append("FSKEW>2")
            elif fskew > 1.0:
                score -= 7
                flags.append("FSKEW>1")
            elif fskew > 0.5:
                score -= 3

        st = src.get("stratum")
        if st is None:
            score -= 3
        else:
            if st <= 2:
                score += 2
            elif st >= 10:
                score -= 8
                flags.append("HI_STR")

        score = max(0.0, min(100.0, score))

        if score >= 80 and ("UNREACH" not in flags) and ("BAD" not in flags):
            col = 1
        elif score >= 55:
            col = 2
        else:
            col = 3

        return int(score), flags, col

    # ---------- graphs ----------
    def compute_scale(self, data, fixed_scale, width, signed=False):
        """Return the scale to use for the graph.
        If autoscale is enabled, compute min/max from the visible window and add padding.
        If signed is True, make the scale symmetric around 0 for easier mental parsing.
        """
        if (not self.autoscale) or (not data) or len(data) < 2 or width <= 1:
            return fixed_scale

        mn, _, mx = window_stats(data, n=width)

        if mn == mx:
            # Avoid zero-span scales
            if mn == 0.0:
                mn, mx = -1e-6, 1e-6
            else:
                pad = abs(mn) * 0.10
                mn -= pad
                mx += pad
        else:
            span = mx - mn
            pad = span * 0.10
            mn -= pad
            mx += pad

        if signed:
            m = max(abs(mn), abs(mx))
            mn, mx = -m, m

        return (mn, mx)

    def spark(self, y, x, width, data, scale, attr=0):
        if width <= 0 or len(data) < 2:
            return
        minv, maxv = scale
        if maxv <= minv:
            return

        chars = " ▁▂▃▄▅▆▇█"
        ncols = min(width, len(data))
        start = len(data) - ncols

        for i in range(ncols):
            val = data[start + i]

            # Clip markers (show out-of-range values explicitly)
            if val < minv:
                ch = "▼"
            elif val > maxv:
                ch = "▲"
            else:
                n = int((val - minv) / (maxv - minv) * 8)
                n = max(0, min(8, n))
                if val > minv and n == 0:
                    n = 1
                ch = chars[n]

            try:
                self.stdscr.addstr(y, x + i, ch, attr)
            except:
                pass

    def draw_graph(self, y, x, w, title, data, scale, unit="s", signed=False, color=4):
        if not data:
            self.addn(y, x, f"{title}: no data", curses.color_pair(3) | curses.A_BOLD, w)
            return

        scale_used = self.compute_scale(data, scale, width=w, signed=signed)

        now = data[-1]
        mn, _, mx = window_stats(data, n=w)

        if unit == "s":
            now_s = fmt_ms(now, signed=signed)
            mn_s  = fmt_ms(mn, signed=signed)
            mx_s  = fmt_ms(mx, signed=signed)
            sc_min = fmt_ms(scale_used[0], signed=True)
            sc_max = fmt_ms(scale_used[1], signed=True)
        elif unit == "ppm":
            now_s = fmt_ppm(now, signed=signed)
            mn_s  = fmt_ppm(mn, signed=signed)
            mx_s  = fmt_ppm(mx, signed=signed)
            sc_min = fmt_ppm(scale_used[0], signed=True)
            sc_max = fmt_ppm(scale_used[1], signed=True)
        else:
            now_s = fmt_c(now)
            mn_s  = fmt_c(mn)
            mx_s  = fmt_c(mx)
            sc_min = fmt_c(scale_used[0])
            sc_max = fmt_c(scale_used[1])

        mode = "AUTO" if self.autoscale else "FIX"
        header = f"{title:<6} now:{now_s:>10}  min:{mn_s:>10}  max:{mx_s:>10}  {mode} scale:{sc_min}..{sc_max}"
        self.addn(y, x, header, curses.color_pair(6), w)
        self.spark(y + 1, x, w, data, scale_used, attr=curses.color_pair(color))

    # ---------- sources panel ----------
    def draw_sources_panel(self, top_y, left_x, width, height, sources):
        self.addn(top_y, left_x, "Server Trust (sources -v + sourcestats -v)",
                  curses.color_pair(5) | curses.A_BOLD, width)

        if not sources:
            self.addn(top_y + 1, left_x,
                      "No sources parsed (chrony not running? permissions?)",
                      curses.color_pair(3) | curses.A_BOLD, width)
            return

        enriched = []
        for s in sources:
            score, flags, col = self.source_trust(s)
            enriched.append((score, flags, col, s))

        def sort_key(t):
            score, flags, col, s = t
            ms = s.get("ms", "")
            sel = 1 if "*" in ms else 0
            return (sel, score)

        enriched.sort(key=sort_key, reverse=True)

        row_y = top_y + 1
        hdr = (
            f"{'MS':<3} {'Name':<22} {'Str':>3} {'Poll':>4} "
            f"{'Rch':>4} {'ReachBits':<8} {'Rx':>4} "
            f"{'Off(ms)':>9} {'Err(ms)':>9} {'SD(ms)':>7} {'FSkw':>5} {'Score':>5}  Flags"
        )
        self.addn(row_y, left_x, hdr, curses.color_pair(6) | curses.A_BOLD, width)
        row_y += 1

        max_rows = min(MAX_SRC_ROWS, height - 3)
        for i in range(min(max_rows, len(enriched))):
            score, flags, col, s = enriched[i]

            ms = s.get("ms", "")
            name = s.get("name", "")
            st = s.get("stratum")
            poll = s.get("poll")
            reach = s.get("reach")
            reach_o = s.get("reach_octal") or "-"
            reach_bits = self.reach_dots(reach, newest_left=True)
            rx = s.get("last_rx")

            off = s.get("offset_s")
            err = s.get("err_s")
            stddev = s.get("ss_stddev_s")
            fskew = s.get("ss_fskew_ppm")

            off_ms = (off * 1000.0) if off is not None else None
            err_ms = (err * 1000.0) if err is not None else None
            sd_ms  = (stddev * 1000.0) if stddev is not None else None

            line = (
                f"{ms:<3} "
                f"{name:<22.22} "
                f"{(st if st is not None else '-'):>3} "
                f"{(poll if poll is not None else '-'):>4} "
                f"{reach_o:>4} "
                f"{reach_bits:<8} "
                f"{(rx if rx is not None else '-'):>4} "
                f"{(f'{off_ms:+.2f}' if off_ms is not None else '-'):>9} "
                f"{(f'{err_ms:.2f}' if err_ms is not None else '-'):>9} "
                f"{(f'{sd_ms:.2f}' if sd_ms is not None else '-'):>7} "
                f"{(f'{fskew:.2f}' if fskew is not None else '-'):>5} "
                f"{score:>5}  "
                f"{','.join(flags[:5])}"
            )

            self.addn(row_y + i, left_x, line, curses.color_pair(col), width)

        if height > (3 + MAX_SRC_ROWS):
            self.addn(top_y + height - 1, left_x,
                      "Legend: Poll=log2(seconds). Reach in octal. ReachBits shows newest(left)->oldest(right).",
                      curses.color_pair(6), width)

    # ---------- main loop ----------
    def run(self):
        while True:
            self.stdscr.erase()
            h, w = self.stdscr.getmaxyx()

            self.addn(0, 2, "ChronyTop 2", curses.color_pair(5) | curses.A_BOLD)
            self.addn(1, 2, datetime.now().strftime("%F %T"))

            tracking_out     = self.chronyc_cached("tracking")
            sources_out      = self.chronyc_cached("sources -v")
            sourcestats_out  = self.chronyc_cached("sourcestats -v")

            ages = {
                "tracking": self.chrony_age("tracking"),
                "sources -v": self.chrony_age("sources -v"),
                "sourcestats -v": self.chrony_age("sourcestats -v"),
            }

            # CPU temps (sysfs)
            temp_readings, temp_max = self.poll_cpu_temps()
            if temp_max is not None:
                self.temp_history.append(temp_max)

            if tracking_out is None and sources_out is None:
                self.addn(3, 2, "ERROR: chronyc not found / not executable",
                          curses.color_pair(3) | curses.A_BOLD)
                self.addn(4, 2, "Install chrony/chronyc or fix PATH/permissions.",
                          curses.color_pair(6))
                self.addn(5, 2, "Health: CHRONYD DOWN / NO DATA",
                          curses.color_pair(3) | curses.A_BOLD)
            else:
                t = self.parse_tracking(tracking_out) if (tracking_out and not self.chrony_out_is_error(tracking_out)) else None
                if t:
                    self.offset_history.append(t["offset"])
                    self.rms_history.append(t["rms"])
                    self.freq_history.append(t["freq"])
                    self.skew_history.append(t["skew"])

                sources = self.parse_sources_v(sources_out) if (sources_out and not self.chrony_out_is_error(sources_out)) else []
                ss_map  = self.parse_sourcestats_v(sourcestats_out) if (sourcestats_out and not self.chrony_out_is_error(sourcestats_out)) else {}
                self.merge_sourcestats(sources, ss_map)

                sel_name, poll_s = self.selected_poll_seconds(sources)
                if poll_s is not None:
                    self.addn(2, 2,
                              f"Selected: {sel_name:<24.24}   Poll interval: ~{poll_s}s",
                              curses.color_pair(6))
                else:
                    self.addn(2, 2,
                              f"Selected: {sel_name or '-':<24.24}   Poll interval: -",
                              curses.color_pair(2))

                noise_txt, noise_col = self.network_noise_indicator(sources)
                self.addn(3, 2, noise_txt, curses.color_pair(noise_col) | curses.A_BOLD)

                # CPU temp line
                if temp_max is None:
                    now = time.monotonic()
                    if int(now) % 60 == 0:
                        self.temp_sources = self.discover_temp_sources()
                    self.addn(4, 2, "CPU Temp: - (no readable sensors)", curses.color_pair(2))
                else:
                    parts = []
                    for lbl, tc in temp_readings[:4]:
                        m = re.search(r"Package id\s+(\d+)", lbl)
                        if m:
                            short = f"Pkg{m.group(1)}"
                        else:
                            short = re.sub(r"\s+", "", lbl)[:8]
                        parts.append(f"{short}:{tc:>4.1f}°C")
                    pkg_txt = "  ".join(parts)
                    self.addn(4, 2, f"CPU Temp: {pkg_txt}   Max:{temp_max:>.1f}°C", curses.color_pair(6))

                coupling_txt, coupling_col = self.temp_freq_coupling(window=20)
                self.addn(5, 2, coupling_txt, curses.color_pair(coupling_col))

                lx = 2
                y = 7

                if t:
                    self.addn(y + 0, lx, f"Offset: {t['offset']*1000:+.3f} ms", curses.color_pair(6))
                    self.addn(y + 1, lx, f"RMS:    {t['rms']*1000: .3f} ms", curses.color_pair(6))
                    self.addn(y + 2, lx, f"Freq:   {t['freq']:+.3f} ppm", curses.color_pair(6))
                    self.addn(y + 3, lx, f"Skew:   {t['skew']: .3f} ppm", curses.color_pair(6))
                else:
                    self.addn(y, lx, "No tracking data parsed.", curses.color_pair(3) | curses.A_BOLD)

                gy = y + 5
                self.addn(gy, lx, "Graphs", curses.color_pair(5) | curses.A_BOLD)

                graph_w = max(40, min(w - lx - 2, 100))
                g0 = gy + 1

                self.draw_graph(g0 + 0,  lx, graph_w, "Offset", self.offset_history, OFFSET_SCALE, unit="s",   signed=True)
                self.draw_graph(g0 + 2,  lx, graph_w, "RMS",    self.rms_history,    RMS_SCALE,    unit="s",   signed=False)
                self.draw_graph(g0 + 4,  lx, graph_w, "Freq",   self.freq_history,   FREQ_SCALE,   unit="ppm", signed=True)
                self.draw_graph(g0 + 6,  lx, graph_w, "Skew",   self.skew_history,   SKEW_SCALE,   unit="ppm", signed=False)
                if self.temp_history:
                    self.draw_graph(g0 + 8, lx, graph_w, "Temp", self.temp_history, TEMP_SCALE, unit="c", signed=False)

                hy = g0 + (10 if self.temp_history else 8)
                self.addn(hy, lx, "Health", curses.color_pair(5) | curses.A_BOLD)

                alerts = self.health(sources=sources, ages=ages)
                if not alerts:
                    self.addn(hy + 1, lx + 2, "OK", curses.color_pair(1) | curses.A_BOLD)
                else:
                    # show top alerts, but include any OK line if present
                    shown = 0
                    for txt, col in alerts:
                        if hy + 1 + shown >= h - 3:
                            break
                        self.addn(hy + 1 + shown, lx + 2, txt,
                                  curses.color_pair(col) | curses.A_BOLD)
                        shown += 1

                # Right panel: trust table
                right_min_x = lx + graph_w + 4
                if w - right_min_x >= 70:
                    px = right_min_x
                    py = y
                    ph = max(10, h - py - 2)
                    pw = w - px - 2
                    self.draw_sources_panel(py, px, pw, ph, sources)
                else:
                    py = hy + 4
                    if py < h - 6:
                        pw = w - 4
                        ph = h - py - 1
                        self.draw_sources_panel(py, 2, pw, ph, sources)

            self.divider(h - 2)
            self.addn(
                h - 1, 2,
                f"q=quit   a=autoscale({('ON' if self.autoscale else 'OFF')})   chronyc: tracking({TRACKING_REFRESH_S:.0f}s) sources({SOURCES_REFRESH_S:.0f}s) sourcestats({SOURCESTATS_REFRESH_S:.0f}s)   clip: ▲▼   reachbits: newest→oldest",
                curses.color_pair(6)
            )

            try:
                c = self.stdscr.getch()
                if c == ord("q"):
                    break
                if c == ord("a"):
                    self.autoscale = not self.autoscale
            except:
                pass

            self.stdscr.refresh()
            time.sleep(1)


if __name__ == "__main__":
    curses.wrapper(lambda s: TimeTop(s).run())
