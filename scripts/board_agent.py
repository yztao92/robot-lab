"""Runs ON the RK3576 board, fed over ssh stdin: `ssh root@<ip> python3 -u - < board_agent.py`.

Read-only observer of the robot stack (robotd / voiced / visiond / waked / translated / mediad).
Nothing is installed on the board and nothing is written to it. Prints one JSON object per line on stdout:

  {"src": "voiced",  "ts": ..., "level": "INFO", "target": "voiced::session", "msg": "user", "fields": {...}}
  {"src": "waked" | "translated" | "mediad", ...same shape...}                    # their journals
  {"src": "robot",   "head": [4 rad]}                                                 # on change, <= 5 Hz
  {"src": "vision",  "event": {...}}                                                  # as visiond emits
  {"src": "services", "services": {...}, "uptime_s": ..., "temp_c": ..., "cpu_pct": ..., "service_cpu": {...}, "mem_pct": ..., "npu_pct": ..., "cpu_mhz": ..., "load_1m": ..., volume, backlight}  # every 5 s
  {"src": "agent",   "msg": "..."}                                                    # this script's own notes

Python 3.10 standard library only: the board's stock python3.
"""
import glob
import json
import math
import operator
import os
from collections import deque
import re
import socket
import subprocess
import sys
import threading
import time

ROBOTD_SOCK = "/run/robotd.sock"
VISION_SOCK = "/run/visiond/events.sock"
SERVICES = ("robotd", "voiced", "visiond", "waked", "translated", "mediad")
# Units whose tracing lines are followed: the wake word, translation mode and the screen's face.
JOURNALS = ("voiced", "waked", "translated", "mediad")
VOLUME_CTL = "name=aw_dev_0_rx_volume"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# tracing's default format: "<ts> LEVEL target: message key=value key=value"
LINE = re.compile(r"^(\S+)\s+(TRACE|DEBUG|INFO|WARN|ERROR)\s+([\w:]+):\s+(.*)$")
FIELD_START = re.compile(r"(?:^| )([a-z_][a-z0-9_]*)=")

_out = threading.Lock()
_problems = {}


def emit(obj):
    with _out:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def problem(channel, message=None):
    """Report a channel's failure once, and its recovery once, instead of on every retry."""
    if _problems.get(channel) == message:
        return
    if message is None:
        emit({"src": "agent", "msg": f"{channel}: recovered"})
    else:
        emit({"src": "agent", "msg": f"{channel}: {message}"})
    _problems[channel] = message


def parse_tracing(text):
    """Split a tracing line into message and fields. Display values (`%text`) are unquoted and
    may contain spaces, so a value runs until the next ` key=`."""
    text = ANSI.sub("", text).strip()
    m = LINE.match(text)
    if not m:
        return None
    ts, level, target, rest = m.groups()
    starts = list(FIELD_START.finditer(rest))
    msg = rest[: starts[0].start()].strip() if starts else rest.strip()
    fields = {}
    for i, f in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(rest)
        value = rest[f.end():end].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        fields[f.group(1)] = value
    return {"ts": ts, "level": level, "target": target, "msg": msg, "fields": fields}


def journal_entry(line):
    """One `journalctl -o json` line → (unit stem, message), or None. journald hands a message with
    control characters (tracing's ANSI colours) over as a list of byte values, not a string."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    message = entry.get("MESSAGE")
    if isinstance(message, list):
        message = bytes(b for b in message if isinstance(b, int) and 0 <= b < 256).decode("utf-8", "replace")
    unit = str(entry.get("_SYSTEMD_UNIT") or entry.get("SYSLOG_IDENTIFIER") or "")
    unit = unit[:-len(".service")] if unit.endswith(".service") else unit
    if not isinstance(message, str) or unit not in JOURNALS:
        return None
    return unit, message


# ── Offline diagnostic helpers only: the live agent does not start this capture ─────────────
# voiced's own e2e_ms takes "the last word" as the last 10 ms over a fixed level while the robot is
# silent, so room sound during the wait for the reply keeps moving it (20–110 ms against a 600 ms+
# service on the boards). Here both ends come from one capture of `voice_in`, which asound.conf
# shares (dsnoop) with voiced:
#   reply audible = the amplifier sense channel (an electrical copy of the speaker) leaving silence
#   last word     = the end of the user's speech on the mics, looked for just before the service's
#                   own end: it calls the turn over `end_smooth_window_ms` of silence after the last
#                   word, so the word ended no later than that before the `user` line.
VOICE_IN = ["arecord", "-q", "-D", "voice_in", "-c", "6", "-r", "48000", "-f", "S16_LE", "-t", "raw",
            "--period-time=10000", "--buffer-time=80000"]
RATE, CHANNELS, BLOCK = 48000, 6, 480  # 10 ms blocks
MICS, SENSE = (0, 1, 2, 3), 5           # voice_in: ch 0-3 mics, ch 5 what the speaker plays
SENSE_ON = 64                           # sense sample level: silence reads 0-1, a reply thousands
SILENT_BEFORE = 15                      # blocks of silence before an onset counts (150 ms)
HISTORY_S = 15
DEFAULT_END_WINDOW_S = 0.6              # voiced's default end_smooth_window_ms


def rms(samples):
    """RMS of a sequence of int16 samples. map(mul) keeps the loop in C: well under 1 % of a core for
    voice_in's five channels of interest at 100 blocks a second. (audioop is gone from Python 3.13.)"""
    return math.sqrt(sum(map(operator.mul, samples, samples)) / len(samples)) if samples else 0.0


def end_window_s(text):
    """`end_smooth_window_ms` from voiced.toml's text, in seconds, or voiced's default."""
    m = re.search(r"^\s*end_smooth_window_ms\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) / 1000 if m else DEFAULT_END_WINDOW_S


def tracing_epoch(ts):
    """A tracing timestamp (UTC) as epoch seconds, or None."""
    import datetime as dt
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z$", ts or "")
    if not m:
        return None
    base = dt.datetime.fromisoformat(m.group(1)).replace(tzinfo=dt.timezone.utc).timestamp()
    return base + float(m.group(2) or 0)


def last_word(levels, t_user, window):
    """When the user's last word ended: `levels` is [(block end time, mic RMS)] at 10 ms.

    The level that counts as speech is set by this utterance, not by a fixed number: between the
    room (the median of the service's own silent window) and the utterance's loud blocks, a third
    of the way up in dB. The last word is the end of the last run of three such blocks (a click is
    not a word) no later than the service's end allows, and no earlier than 1.5 s before it.
    Returns (time, None), or (None, why) when it cannot be told."""
    end = t_user - window
    speech = sorted(r for t, r in levels if end - 4.0 <= t <= end)
    quiet = sorted(r for t, r in levels if end + 0.25 <= t <= t_user - 0.05)
    if len(speech) < 30 or len(quiet) < 10:
        return None, "录音不够长"
    room = max(quiet[len(quiet) // 2], 1.0)
    loud = speech[int(len(speech) * 0.95)]
    if loud < room * 2:
        return None, "人声没有高出环境声"
    threshold = max(room * 1.6, room * (loud / room) ** 0.35)
    run, found = 0, None
    for t, r in levels:
        if t < end - 1.5 or t > end + 0.05:
            run = 0
            continue
        run = run + 1 if r >= threshold else 0
        if run >= 3:
            found = t
    return (found, None) if found is not None else (None, "窗口内没有找到人声结尾")


class LatencyProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.levels = deque()   # (block end, mic RMS)
        self.onsets = deque()   # times the speaker went from silence to sound
        self.pending = []   # (tracing ts, epoch) of `user` lines waiting for their reply
        self.window = DEFAULT_END_WINDOW_S

    def turn(self, ts):
        t = tracing_epoch(ts)
        if t is not None:
            with self.lock:
                self.pending.append((ts, t))

    def run(self):
        import array
        lag_window = 3000  # blocks: 30 s
        while True:
            try:
                with open("/etc/robot/voiced.toml") as f:
                    self.window = end_window_s(f.read())
            except OSError:
                pass
            proc = subprocess.Popen(VOICE_IN, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            frames, silent, blocks = 0, SILENT_BEFORE, 0
            lag_old = lag_new = float("inf")
            size = BLOCK * CHANNELS * 2
            while True:
                # 50 ms a read: a fifth of the wakeups, the same 10 ms resolution.
                data = proc.stdout.read(size * 5)
                if len(data) < size:
                    break
                problem("latency capture")
                now = time.time()
                count = len(data) // size
                frames += BLOCK * count
                blocks += count
                # The sample clock against the wall clock: the smallest lag seen lately is the one
                # without pipe buffering in it. Two 30 s halves, so it follows a drifting clock.
                lag_new = min(lag_new, now - frames / RATE)
                if blocks >= lag_window:
                    lag_old, lag_new, blocks = lag_new, float("inf"), 0
                read_end = min(lag_old, lag_new) + frames / RATE
                samples = array.array("h", data[:count * size])
                with self.lock:
                    for k in range(count):
                        block = samples[k * BLOCK * CHANNELS:(k + 1) * BLOCK * CHANNELS]
                        t_end = read_end - (count - 1 - k) * BLOCK / RATE
                        # Every other sample: 240 a channel is plenty for the level of 10 ms of speech.
                        mic = sum(rms(block[c::CHANNELS * 2]) for c in MICS) / len(MICS)
                        sense = block[SENSE::CHANNELS]
                        loud = max(sense) >= SENSE_ON or -min(sense) >= SENSE_ON
                        self.levels.append((t_end, mic))
                        if loud and silent >= SILENT_BEFORE:
                            i = next((i for i, s in enumerate(sense) if abs(s) >= SENSE_ON), 0)
                            self.onsets.append(t_end - BLOCK / RATE + i / RATE)
                        silent = 0 if loud else silent + 1
                    cut = read_end - HISTORY_S
                    while self.levels[0][0] < cut:
                        self.levels.popleft()
                    while self.onsets and self.onsets[0] < cut:
                        self.onsets.popleft()
                    self._settle(read_end)
            proc.kill()
            err = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
            problem("latency capture", (err[-1] if err else "arecord ended") + "; retrying")
            time.sleep(10)

    def _settle(self, now):
        """Each waiting turn, once its reply has sounded (or after 12 s without one: a meeting, a
        command with nothing to say). Called with the lock held."""
        keep = []
        for ts, t_user in self.pending:
            onset = next((o for o in self.onsets if o >= t_user - 0.05), None)
            if onset is None and now < t_user + 12:
                keep.append((ts, t_user))
                continue
            out = {"src": "latency", "user_ts": ts}
            word, why = last_word(self.levels, t_user, self.window)
            if onset is None:
                out["note"] = "没有回复声音"
            elif word is None:
                out["note"] = why
            else:
                out.update(e2e_ms=round((onset - word) * 1000), end_ms=round((t_user - word) * 1000))
            emit(out)
        self.pending = keep





def follow_journals():
    cmd = ["journalctl", "-f", "-n", "0", "-o", "json"]
    for unit in JOURNALS:
        cmd += ["-u", unit]
    while True:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, errors="replace")
        for line in proc.stdout:
            entry = journal_entry(line)
            parsed = entry and parse_tracing(entry[1])
            if parsed:
                emit(dict(src=entry[0], **parsed))
        proc.wait()
        emit({"src": "agent", "msg": "journal ended; restarting"})
        time.sleep(2)


def follow_robot(min_interval=0.2):
    while True:
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(10)
            s.connect(ROBOTD_SOCK)
            s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"robot.subscribe","params":{}}\n')
            stream = s.makefile()
            stream.readline()  # the subscribe result
            problem("robotd subscribe")
            last, last_at = None, 0.0
            for line in stream:
                msg = json.loads(line)
                if msg.get("method") != "robot.state":
                    continue
                st = msg["params"]
                snap = {"head": [round(x, 3) for x in st.get("head", [])]}
                now = time.monotonic()
                if snap != last and now - last_at >= min_interval:
                    emit(dict(src="robot", **snap))
                    last, last_at = snap, now
        except Exception as e:
            problem("robotd subscribe", str(e))
        time.sleep(3)


def follow_vision():
    while True:
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(VISION_SOCK)
            problem("visiond events")
            for line in s.makefile():
                if line.strip():
                    emit({"src": "vision", "event": json.loads(line)})
        except Exception as e:
            problem("visiond events", str(e))
        time.sleep(3)


def levels():
    """Speaker attenuation (raw AW88394 steps), backlight and fan duty, for the page's sliders."""
    out = {}
    m = re.search(r": values=(\d+)", subprocess.run(["amixer", "-c", "0", "cget", VOLUME_CTL],
                                                     capture_output=True, text=True).stdout)
    if m:
        out["volume_raw"] = int(m.group(1))
    fans = sorted(glob.glob("/sys/devices/platform/pwm-fan/hwmon/hwmon*/pwm1"))
    if fans:
        try:
            with open(fans[0]) as f:
                out["fan_pwm"] = int(f.read())
        except (OSError, ValueError):
            pass
    lights = sorted(glob.glob("/sys/class/backlight/*"))
    if lights:
        try:
            with open(lights[0] + "/brightness") as f, open(lights[0] + "/max_brightness") as g:
                out["brightness"], out["brightness_max"] = int(f.read()), int(g.read())
        except (OSError, ValueError):
            pass
    return out


def cpu_times():
    """(busy, total) jiffies over all cores from /proc/stat's first line. guest/guest_nice are
    already inside user/nice, and idle + iowait count as idle."""
    with open("/proc/stat") as f:
        user, nice, system_, idle, iowait, irq, softirq, steal = (int(v) for v in f.readline().split()[1:9])
    total = user + nice + system_ + idle + iowait + irq + softirq + steal
    return total - idle - iowait, total


def cpu_percent(before, after):
    """Share of all cores busy between two cpu_times() samples, 0-100, or None if no time passed."""
    busy, total = after[0] - before[0], after[1] - before[1]
    return round(100 * busy / total) if total > 0 else None


def service_usage():
    """CPU microseconds each service has used so far, across its whole cgroup (children too)."""
    usage = {}
    for service in SERVICES:
        try:
            with open(f"/sys/fs/cgroup/system.slice/{service}.service/cpu.stat") as f:
                usage[service] = int(f.readline().split()[1])
        except (OSError, ValueError, IndexError):
            pass  # not running: no cgroup
    return usage


def service_percent(before, after, seconds, cores):
    """Each service's share of all cores between two service_usage() samples, the same unit as
    cpu_percent. A service that restarted in between has a counter below its last, so no figure."""
    return {service: round(100 * (used - before[service]) / (seconds * 1e6 * cores), 1)
            for service, used in after.items() if used >= before.get(service, used + 1)} if seconds > 0 else {}


def memory_percent(text):
    """Share of RAM in use from /proc/meminfo: what MemAvailable says cannot be had back. Page
    cache counts as free, so this is not the `free` command's "used"."""
    fields = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)", text, re.M))
    total, available = int(fields.get("MemTotal", 0)), int(fields.get("MemAvailable", 0))
    return round(100 * (total - available) / total) if total and "MemAvailable" in fields else None


def npu_percent(text):
    """Mean of the cores in /sys/kernel/debug/rknpu/load: `NPU load:  Core0:  1%, Core1:  0%, `."""
    cores = [int(n) for n in re.findall(r"Core\d+:\s*(\d+)%", text)]
    return round(sum(cores) / len(cores)) if cores else None


def cpu_ceiling():
    """(allowed, full) MHz of the fastest cluster. Thermal throttling lowers the allowed one, and a
    CPU percentage means little without it: 20 % of 408 MHz is not 20 % of 2208."""
    allowed, full = 0, 0
    for policy in glob.glob("/sys/devices/system/cpu/cpufreq/policy*"):
        with open(policy + "/scaling_max_freq") as f, open(policy + "/cpuinfo_max_freq") as g:
            allowed, full = max(allowed, int(f.read()) // 1000), max(full, int(g.read()) // 1000)
    return allowed, full


def system():
    """Hottest thermal zone and the 1-minute load: robotd runs --fake, so its own health (battery,
    mode) is simulated and not reported."""
    out = {}
    temps = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(zone) as f:
                temps.append(int(f.read()) / 1000)
        except (OSError, ValueError):
            pass
    if temps:
        out["temp_c"] = round(max(temps), 1)
    with open("/proc/loadavg") as f:
        out["load_1m"] = float(f.read().split()[0])
    try:  # debugfs, root only: the board_agent runs as root
        with open("/sys/kernel/debug/rknpu/load") as f:
            npu = npu_percent(f.read())
        if npu is not None:
            out["npu_pct"] = npu
    except OSError:
        pass
    with open("/proc/meminfo") as f:
        memory = memory_percent(f.read())
    if memory is not None:
        out["mem_pct"] = memory
    try:
        out["cpu_mhz"], out["cpu_mhz_full"] = cpu_ceiling()
    except (OSError, ValueError):
        pass
    return out


def poll_status():
    last = cpu_times()
    last_usage, last_at = service_usage(), time.monotonic()
    time.sleep(1)  # so the first report already has a usage figure
    while True:
        now = cpu_times()
        busy = cpu_percent(last, now)
        last = now
        usage, at = service_usage(), time.monotonic()
        shares = service_percent(last_usage, usage, at - last_at, os.cpu_count() or 1)
        last_usage, last_at = usage, at
        states = subprocess.run(["systemctl", "is-active", *SERVICES], capture_output=True, text=True).stdout.split()
        with open("/proc/uptime") as f:
            uptime = int(float(f.read().split()[0]))
        emit(dict({"src": "services", "services": dict(zip(SERVICES, states)), "uptime_s": uptime, "service_cpu": shares},
                  **levels(), **system(), **({} if busy is None else {"cpu_pct": busy})))
        time.sleep(5)


def main():
    emit({"src": "agent", "msg": "board agent started"})
    for target in (follow_journals, follow_robot, follow_vision):
        threading.Thread(target=target, daemon=True).start()
    poll_status()


if __name__ == "__main__":
    main()
