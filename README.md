# ChronyTop

A real-time terminal monitoring tool for `chrony` time synchronization, inspired by `top`. ChronyTop provides live visualization of clock offset, frequency drift, jitter, and NTP source health with an intuitive ncurses interface.

![ChronyTop Screenshot](screenshot.png)

## Features

### Core Monitoring
- **Real-time tracking metrics**: System time offset, RMS jitter, frequency drift, and skew
- **Live sparkline graphs**: Visual representation of offset, RMS, frequency, and skew over time
- **CPU temperature monitoring**: Tracks package temperatures via `coretemp` hwmon sensors with thermal zone fallback
- **Temperature-frequency coupling analysis**: Detects correlations between CPU temperature changes and clock drift

### NTP Source Analysis
- **Comprehensive source trust scoring**: Evaluates each NTP source based on:
  - Reachability and staleness
  - Offset magnitude and estimated error
  - Standard deviation from `sourcestats`
  - Frequency skew stability
  - Stratum preference
- **Network noise detection**: Compares selected source standard deviation against median to identify outlier conditions
- **Poll interval tracking**: Displays current polling intervals for active sources
- **Sourcestats integration**: Merges `chronyc sources -v` and `sourcestats -v` data for enriched analysis

### Health Monitoring
- **Automated alerts**: Warns about large offsets, high jitter, excessive drift, oscillator instability
- **Time jump detection**: Identifies system time discontinuities and suspend/resume events
- **Color-coded status**: Green/yellow/red indicators for quick health assessment

## Requirements

- **chrony**: The chrony NTP daemon must be installed and running
- **Python 3.6+**: Standard library only (curses, subprocess, time, re, statistics, os, glob)
- **Linux**: Requires sysfs access for CPU temperature monitoring
- **Permissions**: Must be able to execute `chronyc` commands (may require sudo)

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/chronytop.git
cd chronytop

# Make executable
chmod +x chronytop.py

# Run
./chronytop.py
```

Or install system-wide:

```bash
sudo cp chronytop.py /usr/local/bin/chronytop
sudo chmod +x /usr/local/bin/chronytop
chronytop
```

## Usage

```bash
# Basic usage
./chronytop.py

# If chronyc requires sudo
sudo ./chronytop.py
```

**Controls:**
- `q` - Quit
- Updates automatically every second

## How It Works

ChronyTop polls three `chronyc` commands every second:
1. `chronyc tracking` - System clock metrics
2. `chronyc sources -v` - NTP source status and offsets
3. `chronyc sourcestats -v` - Statistical analysis of sources

It also reads CPU package temperatures from:
- `/sys/class/hwmon/hwmon*/` (coretemp sensors, preferred)
- `/sys/class/thermal/thermal_zone*/` (x86_pkg_temp fallback)

## Understanding the Display

### Tracking Metrics
- **Offset**: How far your system clock is from true time (±50ms scale)
- **RMS**: Root mean square of offset measurements (jitter/noise, 0-50ms scale)
- **Freq**: Frequency correction being applied in ppm (±100ppm scale)
- **Skew**: Estimated error in frequency measurement (0-20ppm scale)

### Source Trust Scoring (0-100)
Sources are scored based on multiple factors:
- **80-100**: Excellent - Low jitter, good reachability, selected source
- **55-79**: Fair - Some issues but usable
- **0-54**: Poor - High jitter, unreachable, or stale

**Flags**:
- `UNREACHABLE` - Cannot contact source
- `BAD` - Source marked bad by chrony
- `TOO_VAR` - Too much variance
- `STALE` - Haven't received update recently
- `OFF>Xms` - Offset exceeds threshold
- `SD>Xms` - Standard deviation too high
- `FSKEW>X` - Frequency skew too high
- `FALSETICKER?` - Likely providing incorrect time

### Network Noise Indicator
Compares the selected source's standard deviation against the median of all sources:
- **OK**: Selected source stddev is reasonable
- **ELEVATED**: 2x median + 0.2ms gap
- **OUTLIER**: 3x median + 0.5ms gap (may indicate network issues)

### Health Alerts
- **CLOCK STEP / LARGE OFFSET**: Offset >50ms
- **HIGH OFFSET**: Offset >10ms
- **JITTER (RMS HIGH)**: RMS >10ms
- **DRIFT (FREQ HIGH)**: Frequency >100ppm
- **UNSTABLE OSC (SKEW HIGH)**: Skew >5ppm
- **TIME JUMP**: Large sudden offset change
- **SUSPEND/PAUSE DETECTED**: Monotonic clock gap detected

## Configuration

### Adjusting Graph Scales
Edit these constants at the top of the script:
```python
OFFSET_SCALE = (-0.050, 0.050)   # ±50ms
RMS_SCALE    = (0.000, 0.050)    # 0-50ms
FREQ_SCALE   = (-100, 100)       # ±100ppm
SKEW_SCALE   = (0, 20)           # 0-20ppm
TEMP_SCALE   = (20.0, 90.0)      # 20-90°C
```

### Adjusting History Window
```python
self.history_size = 120  # Number of samples to keep (120 = 2 minutes at 1Hz)
```

### Maximum Source Display
```python
MAX_SRC_ROWS = 7  # Maximum sources shown in trust panel
```

## Troubleshooting

### "chronyc not found / not executable"
- Install chrony: `sudo apt install chrony` (Debian/Ubuntu) or `sudo yum install chrony` (RHEL/CentOS)
- Ensure chronyc is in your PATH
- Try running with sudo: `sudo ./chronytop.py`

### "No sources parsed"
- Check if chronyd is running: `systemctl status chronyd`
- Verify chrony configuration: `cat /etc/chrony/chrony.conf`
- Ensure you have NTP sources configured

### CPU temperature shows "-"
- Not all systems expose temperature sensors via sysfs
- VMs typically don't have temperature sensors
- Check manually: `cat /sys/class/hwmon/hwmon*/name`

### Updates are too slow (17+ minutes)
Your chrony polling interval is at maximum (1024s). To see more frequent updates:

Edit `/etc/chrony/chrony.conf` and add to server lines:
```
server ntp.example.com iburst minpoll 4 maxpoll 6
```

Then restart: `sudo systemctl restart chronyd`

This sets polling to 16-64 seconds instead of up to 1024 seconds.

## Performance Notes

ChronyTop executes three `chronyc` commands per second. For most systems this is negligible overhead, but if you want to optimize:

1. **Cache sourcestats**: The `sourcestats` data changes slowly and could be cached for 10-30 seconds
2. **Reduce update frequency**: Change `time.sleep(1)` to `time.sleep(2)` for 2-second updates
3. **Disable temperature monitoring**: Comment out the `poll_cpu_temps()` call if not needed

## Contributing

Contributions welcome! Areas for improvement:
- Auto-scaling graphs based on actual data ranges
- Configuration file support
- Export/logging capability
- Additional chrony metrics
- MacOS support (using different temp sensors)
- Windows support (using w32time instead of chrony)

## License

MIT License - see LICENSE file for details

## Author

Created by [Your Name]

## Acknowledgments

- Inspired by `top`, `htop`, and similar system monitoring tools
- Built for the `chrony` NTP implementation
- Thanks to the chrony project for excellent time synchronization software
