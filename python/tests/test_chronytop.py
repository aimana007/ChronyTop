#!/usr/bin/env python3
import os
import sys
import time
import unittest
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chronytop import TimeTop, spark_rows


def make_timetop():
    """Build a TimeTop instance without running curses init (no real terminal needed)."""
    tt = TimeTop.__new__(TimeTop)
    tt.offset_history = deque(maxlen=120)
    tt.rms_history = deque(maxlen=120)
    tt.freq_history = deque(maxlen=120)
    tt.skew_history = deque(maxlen=120)
    tt.temp_history = deque(maxlen=120)
    tt.last_offset = None
    tt.last_monotonic = time.monotonic()
    return tt


TRACKING_SYNCED = """Reference ID    : 05A15E0C (5.161.94.12)
Stratum         : 4
Ref time (UTC)  : Sat Jun 13 19:04:59 2026
System time     : 0.002224203 seconds fast of NTP time
Last offset     : +0.010590672 seconds
RMS offset      : 0.014815575 seconds
Frequency       : 5.016 ppm slow
Residual freq   : +14.214 ppm
Skew            : 176.307 ppm
Root delay      : 0.093492009 seconds
Root dispersion : 0.018840309 seconds
Update interval : 64.4 seconds
Leap status     : Normal
"""

TRACKING_UNSYNCED = """Reference ID    : 7F7F0101 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000123456 seconds slow of NTP time
Last offset     : +0.000000000 seconds
RMS offset      : 0.000045678 seconds
Frequency       : 10.123 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.456 ppm
Root delay      : 0.500000000 seconds
Root dispersion : 0.250000000 seconds
Update interval : 0.0 seconds
Leap status     : Not synchronised
"""

TRACKING_LEAP_PENDING = TRACKING_SYNCED.replace("Leap status     : Normal", "Leap status     : Insert second")

SOURCES_V_SAMPLE = """
  .-- Source mode  '^' = server, '=' = peer, '#' = local clock.
 / .- Source state '*' = current best, '+' = combined, '-' = not combined,
| /   '?' = unreachable, 'x' = time may be in error, '~' = time too variable.
||                                                 .- xxxx [ yyyy ] +/- zzzz
||      Reachability register (octal) -.           |  xxxx = adjusted offset,
||      Log2(Polling interval) --.      |           |  yyyy = measured offset,
||                                \\     |           |  zzzz = estimated error.
||                                 |    |            \\
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^* 5.161.94.12                   2   6   377    23   -123us[ -130us] +/-   45ms
^+ 192.168.1.1                   3   6   377    45   +1.2ms[ +1.1ms] +/-   12ms
^? 10.0.0.5                       0   6     0     -    +0ns[   +0ns] +/-    0ns
^x 1.2.3.4                        2   6   376    12  -100ms[ -100ms] +/-   10ms
^~ 2.2.2.2                        2   6   377    34    +5ms[  +5ms] +/-    5ms
"""

SOURCESTATS_V_SAMPLE = """
Name/IP Address            NP  NR  Span  Frequency  Freq Skew  Offset  Std Dev
==============================================================================
5.161.94.12                 32  16   519m     -0.123      0.456   -123us   789us
192.168.1.1                 16   8    257s      0.045      0.234   +1.2ms   2.1ms
"""


class ParseTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()

    def test_empty_input(self):
        self.assertIsNone(self.tt.parse_tracking(""))
        self.assertIsNone(self.tt.parse_tracking(None))

    def test_synced(self):
        data = self.tt.parse_tracking(TRACKING_SYNCED)
        self.assertAlmostEqual(data["offset"], 0.002224203)
        self.assertAlmostEqual(data["rms"], 0.014815575)
        self.assertAlmostEqual(data["freq"], 5.016)
        self.assertAlmostEqual(data["skew"], 176.307)
        self.assertEqual(data["stratum"], 4)
        self.assertEqual(data["refid"], "05A15E0C (5.161.94.12)")
        self.assertEqual(data["leap"], "Normal")
        self.assertAlmostEqual(data["root_delay"], 0.093492009)
        self.assertAlmostEqual(data["root_dispersion"], 0.018840309)

    def test_slow_offset_is_negative(self):
        data = self.tt.parse_tracking(TRACKING_UNSYNCED)
        self.assertAlmostEqual(data["offset"], -0.000123456)

    def test_not_synchronised(self):
        data = self.tt.parse_tracking(TRACKING_UNSYNCED)
        self.assertEqual(data["leap"], "Not synchronised")
        self.assertEqual(data["stratum"], 0)
        self.assertAlmostEqual(data["root_delay"], 0.5)
        self.assertAlmostEqual(data["root_dispersion"], 0.25)


class ParseSourcesVTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()
        self.sources = self.tt.parse_sources_v(SOURCES_V_SAMPLE)

    def test_count_and_legend_filtered(self):
        self.assertEqual(len(self.sources), 5)

    def test_selected_source(self):
        s = self.sources[0]
        self.assertEqual(s["ms"], "^*")
        self.assertEqual(s["key"], "5.161.94.12")
        self.assertEqual(s["stratum"], 2)
        self.assertEqual(s["poll"], 6)
        self.assertEqual(s["reach"], 0o377)
        self.assertEqual(s["reach_octal"], "377")
        self.assertEqual(s["last_rx"], 23)
        self.assertAlmostEqual(s["offset_s"], -123e-6)
        self.assertAlmostEqual(s["err_s"], 45e-3)

    def test_combined_source_ms_units(self):
        s = self.sources[1]
        self.assertEqual(s["ms"], "^+")
        self.assertAlmostEqual(s["offset_s"], 1.2e-3)
        self.assertAlmostEqual(s["err_s"], 12e-3)

    def test_unreachable_source(self):
        s = self.sources[2]
        self.assertEqual(s["ms"], "^?")
        self.assertEqual(s["reach"], 0)
        self.assertIsNone(s["last_rx"])

    def test_falseticker_source(self):
        s = self.sources[3]
        self.assertEqual(s["ms"], "^x")
        self.assertAlmostEqual(s["offset_s"], -0.1)

    def test_too_variable_source(self):
        s = self.sources[4]
        self.assertEqual(s["ms"], "^~")


class ParseSourcestatsVTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()
        self.ss_map = self.tt.parse_sourcestats_v(SOURCESTATS_V_SAMPLE)

    def test_keys(self):
        self.assertEqual(set(self.ss_map.keys()), {"5.161.94.12", "192.168.1.1"})

    def test_span_and_units(self):
        s = self.ss_map["5.161.94.12"]
        self.assertEqual(s["np"], 32)
        self.assertEqual(s["nr"], 16)
        self.assertAlmostEqual(s["span_s"], 519 * 60.0)
        self.assertAlmostEqual(s["freq_ppm"], -0.123)
        self.assertAlmostEqual(s["fskew_ppm"], 0.456)
        self.assertAlmostEqual(s["offset_s"], -123e-6)
        self.assertAlmostEqual(s["stddev_s"], 789e-6)

    def test_seconds_span(self):
        s = self.ss_map["192.168.1.1"]
        self.assertAlmostEqual(s["span_s"], 257.0)
        self.assertAlmostEqual(s["offset_s"], 1.2e-3)
        self.assertAlmostEqual(s["stddev_s"], 2.1e-3)


class MergeSourcestatsTests(unittest.TestCase):
    def test_merge(self):
        tt = make_timetop()
        sources = tt.parse_sources_v(SOURCES_V_SAMPLE)
        ss_map = tt.parse_sourcestats_v(SOURCESTATS_V_SAMPLE)
        tt.merge_sourcestats(sources, ss_map)

        merged = sources[0]
        self.assertAlmostEqual(merged["ss_stddev_s"], 789e-6)
        self.assertAlmostEqual(merged["ss_freq_ppm"], -0.123)

        unmatched = sources[2]  # 10.0.0.5 has no sourcestats entry
        self.assertIsNone(unmatched["ss_stddev_s"])


class ReachDotsTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()

    def test_unknown(self):
        self.assertEqual(self.tt.reach_dots(None), "????????")

    def test_all_up(self):
        self.assertEqual(self.tt.reach_dots(0o377), "●" * 8)

    def test_all_down(self):
        self.assertEqual(self.tt.reach_dots(0), "○" * 8)

    def test_newest_left_ordering(self):
        self.assertEqual(self.tt.reach_dots(0b00000001, newest_left=True), "○○○○○○○●")
        self.assertEqual(self.tt.reach_dots(0b00000001, newest_left=False), "●○○○○○○○")


class ChronyOutIsErrorTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()

    def test_none_and_empty(self):
        self.assertTrue(self.tt.chrony_out_is_error(None))
        self.assertTrue(self.tt.chrony_out_is_error(""))
        self.assertTrue(self.tt.chrony_out_is_error("   \n"))

    def test_daemon_errors(self):
        self.assertTrue(self.tt.chrony_out_is_error("506 Cannot talk to daemon"))
        self.assertTrue(self.tt.chrony_out_is_error("chronyc: Connection refused"))

    def test_normal_output_is_not_error(self):
        self.assertFalse(self.tt.chrony_out_is_error(TRACKING_SYNCED))

    def test_number_containing_506_is_not_error(self):
        out = TRACKING_SYNCED.replace("0.002224203", "0.000050612")
        self.assertFalse(self.tt.chrony_out_is_error(out))


class SourceTrustTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()

    def test_excellent_selected_source(self):
        src = {
            "ms": "^*",
            "reach": 0o377,
            "last_rx": 6,
            "offset_s": 0.0001,
            "err_s": 0.005,
            "ss_stddev_s": 0.0005,
            "ss_fskew_ppm": 0.1,
            "stratum": 1,
        }
        score, flags, col = self.tt.source_trust(src)
        self.assertEqual(score, 100)
        self.assertEqual(flags, [])
        self.assertEqual(col, 1)

    def test_falseticker_scores_poorly(self):
        src = {
            "ms": "^x",
            "reach": 0o377,
            "last_rx": 10,
            "offset_s": 0.15,
            "err_s": 0.2,
            "ss_stddev_s": 0.02,
            "ss_fskew_ppm": 3.0,
            "stratum": 3,
        }
        score, flags, col = self.tt.source_trust(src)
        self.assertIn("FALSETICKER", flags)
        self.assertEqual(score, 0)
        self.assertEqual(col, 3)

    def test_unreachable_source_flagged(self):
        src = {"ms": "^?", "reach": 0, "last_rx": None}
        score, flags, col = self.tt.source_trust(src)
        self.assertIn("UNREACH", flags)
        self.assertIn("RCH=0", flags)
        self.assertEqual(col, 3)


# Healthy sync at high poll intervals, where LastRx is legitimately in the hundreds
SOURCES_HIGH_POLL = """MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^* 193.163.204.113               2   9   377   283  -5861us[-5361us] +/-   20ms
^+ 107.161.86.210                2  10   377   538  +3148us[+3640us] +/-   28ms
^+ 23.150.41.123                 2  10   377    38m +8259us[+8259us] +/-   36ms
^- 23.142.248.8                  2   6   377     5h +5847us[+6339us] +/-   47ms
"""

AGES_FRESH = {"tracking": 0.5, "sources -v": 1.0, "sourcestats -v": 2.0}


class LastRxStalenessTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()
        self.sources = self.tt.parse_sources_v(SOURCES_HIGH_POLL)

    def test_lastrx_units_parsed(self):
        rx = [s["last_rx"] for s in self.sources]
        self.assertEqual(rx, [283, 538, 38 * 60, 5 * 3600])
        self.assertEqual(self.sources[2]["last_rx_raw"], "38m")

    def test_span_years(self):
        self.assertEqual(self.tt._parse_span_seconds("2y"), 2 * 365 * 86400.0)

    def test_high_poll_lastrx_is_not_stale(self):
        for s in self.sources[:2]:
            _, flags, _ = self.tt.source_trust(s)
            self.assertNotIn("STALE", flags)
            self.assertNotIn("AGING", flags)

    def test_aging_and_stale_relative_to_poll(self):
        # poll 10 (1024s): 38m = 2280s > 2048s -> AGING
        _, flags, _ = self.tt.source_trust(self.sources[2])
        self.assertIn("AGING", flags)
        # poll 6 (64s): 5h -> STALE
        _, flags, _ = self.tt.source_trust(self.sources[3])
        self.assertIn("STALE", flags)

    def test_low_poll_thresholds_unchanged(self):
        self.assertEqual(self.tt.rx_stale_limits({"poll": 6}), (128, 256))
        self.assertEqual(self.tt.rx_stale_limits({"poll": None}), (128, 256))

    def test_no_all_stale_alert_at_high_poll(self):
        alerts = self.tt.chrony_sync_health(self.sources, AGES_FRESH)
        self.assertFalse(any("ALL SOURCES STALE" in txt for txt, _ in alerts))
        self.assertIn(("CHRONY SYNC: OK", 1), alerts)

    def test_all_stale_alert_still_fires(self):
        for s in self.sources:
            s["last_rx"] = 10 * 86400
        alerts = self.tt.chrony_sync_health(self.sources, AGES_FRESH)
        self.assertTrue(any("ALL SOURCES STALE" in txt for txt, _ in alerts))


class HealthLeapStatusTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()

    def test_not_synchronised_alert(self):
        alerts = self.tt.health(tracking={"leap": "Not synchronised"})
        self.assertTrue(any("NOT SYNCHRONISED" in txt and col == 3 for txt, col in alerts))

    def test_leap_second_pending_alert(self):
        alerts = self.tt.health(tracking={"leap": "Insert second"})
        self.assertTrue(any(txt == "LEAP SECOND PENDING: Insert second" and col == 2 for txt, col in alerts))

    def test_normal_leap_no_alert(self):
        alerts = self.tt.health(tracking={"leap": "Normal"})
        self.assertEqual(alerts, [])


class HelperFunctionTests(unittest.TestCase):
    def setUp(self):
        self.tt = make_timetop()

    def test_to_seconds(self):
        self.assertEqual(self.tt._to_seconds(1, "s"), 1)
        self.assertAlmostEqual(self.tt._to_seconds(1000, "ms"), 1.0)
        self.assertAlmostEqual(self.tt._to_seconds(1_000_000, "us"), 1.0)
        self.assertAlmostEqual(self.tt._to_seconds(1_000_000_000, "ns"), 1.0)

    def test_parse_span_seconds(self):
        self.assertEqual(self.tt._parse_span_seconds("30"), 30.0)
        self.assertEqual(self.tt._parse_span_seconds("5m"), 300.0)
        self.assertEqual(self.tt._parse_span_seconds("2h"), 7200.0)
        self.assertEqual(self.tt._parse_span_seconds("1d"), 86400.0)
        self.assertIsNone(self.tt._parse_span_seconds(""))
        self.assertIsNone(self.tt._parse_span_seconds("bogus"))

    def test_normalize_name(self):
        self.assertEqual(self.tt._normalize_name("foo.example.com>"), "foo.example.com")
        self.assertEqual(self.tt._normalize_name("foo.example.com"), "foo.example.com")
        self.assertEqual(self.tt._normalize_name(""), "")
        self.assertEqual(self.tt._normalize_name(None), "")


class SparkRowsTests(unittest.TestCase):
    SCALE = (0.0, 1.0)

    def legacy_spark(self, data, width, scale):
        """The original single-row algorithm, kept as a reference."""
        minv, maxv = scale
        chars = " ▁▂▃▄▅▆▇█"
        ncols = min(width, len(data))
        out = ""
        for val in list(data)[len(data) - ncols:]:
            if val < minv:
                out += "▼"
            elif val > maxv:
                out += "▲"
            else:
                n = max(0, min(8, int((val - minv) / (maxv - minv) * 8)))
                if val > minv and n == 0:
                    n = 1
                out += chars[n]
        return out

    def test_height_one_matches_legacy(self):
        data = [-0.5, 0.0, 0.001, 0.2, 0.5, 0.875, 0.99, 1.0, 1.5]
        self.assertEqual(spark_rows(data, 20, 1, self.SCALE), [self.legacy_spark(data, 20, self.SCALE)])

    def test_multi_row_stacks_blocks(self):
        rows = spark_rows([0.0, 0.5, 1.0], 10, 2, self.SCALE)
        self.assertEqual(rows, [
            "  █",   # top
            " ██",   # bottom
        ])

    def test_multi_row_partial_top(self):
        # 0.75 of 2 rows = 12/16 levels: bottom full, top half
        rows = spark_rows([0.0, 0.75], 10, 2, self.SCALE)
        self.assertEqual(rows, [" ▄", " █"])

    def test_tiny_value_gets_visible_bar(self):
        rows = spark_rows([0.0, 0.0001], 10, 3, self.SCALE)
        self.assertEqual(rows[-1], " ▁")
        self.assertEqual(rows[0], "  ")

    def test_clip_markers(self):
        rows = spark_rows([-1.0, 2.0], 10, 3, self.SCALE)
        self.assertEqual(rows, [" ▲", " █", "▼█"])

    def test_width_keeps_newest(self):
        rows = spark_rows([0.0, 0.0, 1.0, 1.0], 2, 1, self.SCALE)
        self.assertEqual(rows, ["██"])

    def test_degenerate_inputs(self):
        self.assertEqual(spark_rows([0.5], 10, 3, self.SCALE), [])
        self.assertEqual(spark_rows([0.1, 0.2], 10, 3, (1.0, 1.0)), [])
        self.assertEqual(spark_rows([0.1, 0.2], 0, 3, self.SCALE), [])


if __name__ == "__main__":
    unittest.main()
