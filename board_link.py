"""SSH to the selected RK3576 board, and the bridge that streams it into the event hub.

Boards are found by scanning the Mac's own subnets: port 22 open with an Ubuntu ssh banner is a
candidate, and a candidate that key auth reaches is a robot when it runs or has installed robotd. One ssh
ControlMaster per board carries every call (status, control, the agent stream), so a board is
authenticated once. Key auth is tried first; when the board refuses it, the caller gets
AuthRequired and the page asks for root's password. A password lives only in this process's
memory and reaches ssh through SSH_ASKPASS; it is never written to disk.

The camera window rides the same master: CameraFeed runs scripts/board_camera.py on the board, which
reads the ISP's self path beside visiond while a page is watching; visiond keeps running.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import board_bridge

USER = "root"
CONFIG = Path.home() / ".config" / "robot-lab" / "boards.json"
# Unix socket paths are capped near 104 bytes on macOS, so not under $TMPDIR.
CONTROL_PATH = "/tmp/robotlab-%C"
HOST = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,62}[A-Za-z0-9])?)$")
# ssh keeps the first value of a repeated -o, so ConnectTimeout is given per call, not here.
COMMON = ["-o", f"ControlPath={CONTROL_PATH}", "-o", "StrictHostKeyChecking=no",
          "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]


class BoardError(Exception):
    """The board could not be reached (offline, refused, timed out)."""


class AuthRequired(BoardError):
    """The board answered but refused key auth (or the password given)."""


def valid_host(host: str) -> bool:
    return isinstance(host, str) and bool(HOST.fullmatch(host)) and not host.startswith("-")


def local_subnets(max_hosts: int = 1024) -> list[ipaddress.IPv4Network]:
    """The Mac's own IPv4 LANs, skipping loopback, point-to-point tunnels and anything too big to sweep."""
    out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    nets = []
    for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+) (?:--> \S+ )?netmask (0x[0-9a-f]{8})", out):
        addr, mask = m.group(1), int(m.group(2), 16)
        net = ipaddress.IPv4Network((addr, bin(mask).count("1")), strict=False)
        if addr.startswith("127.") or net.num_addresses < 4 or net.num_addresses > max_hosts or "-->" in m.group(0):
            continue
        if net not in nets:
            nets.append(net)
    return nets


def ssh_banner(host: str, timeout: float = 0.7) -> str | None:
    try:
        with socket.create_connection((host, 22), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(128).decode("ascii", "replace").strip()
    except OSError:
        return None


def _askpass() -> str:
    path = Path(tempfile.gettempdir()) / "robotlab-askpass.sh"
    if not path.exists():
        path.write_text('#!/bin/sh\nprintf "%s\\n" "$ROBOTLAB_SSH_PASSWORD"\n')
        path.chmod(0o700)
    return str(path)


class BoardLink:
    def __init__(self, publish, initial: str | None = None, config: Path = CONFIG):
        self.publish = publish
        self.config = config
        self.lock = threading.Lock()
        self.master_locks: dict[str, threading.Lock] = {}
        self.passwords: dict[str, str] = {}
        self.current = initial if valid_host(initial or "") else self._load()
        self.last_scan: list[dict] = []
        self.scanned_at: float | None = None
        self.scan_lock = threading.Lock()
        self.generation = 0
        self.proc: subprocess.Popen | None = None
        self.camera = CameraFeed(self)

    # ---- the last selected board survives a restart; nothing else is kept ----
    def _load(self):
        try:
            current = json.loads(self.config.read_text()).get("current", "")
            return current if valid_host(current) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _save(self):
        try:
            self.config.parent.mkdir(parents=True, exist_ok=True)
            self.config.write_text(json.dumps({"current": self.current}))
        except OSError:
            pass

    def listing(self) -> dict:
        return {"current": self.current, "boards": self.last_scan, "scanned_at": self.scanned_at}

    # ---- discovery ----
    def probe(self, host: str) -> dict | None:
        """Classify one ssh host: a robot (key or stored password reaches it), one that needs a
        password, or None for anything else."""
        remote = "hostname; tr -d '\\0' </proc/device-tree/model 2>/dev/null; echo; { test -S /run/robotd.sock || test -x /opt/robot/daemon/current/bin/robotd; } && echo ROBOTD"
        try:
            done = subprocess.run(self.ssh_args(host, remote, connect_timeout=3), stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=8)
        except subprocess.TimeoutExpired:
            return None
        if done.returncode == 0:
            lines = [ln.strip() for ln in done.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
            if "ROBOTD" not in lines:
                return None
            return {"host": host, "state": "robot", "name": lines[0] if lines else "",
                    "model": lines[1] if len(lines) > 2 else ""}
        if b"Permission denied" in done.stderr:
            return {"host": host, "state": "auth", "name": "", "model": ""}
        return None

    def scan(self) -> dict:
        """Sweep the local subnets for ssh, keep Ubuntu hosts, and classify them. ~2-5 s on a /24."""
        with self.scan_lock:
            hosts = [str(h) for net in local_subnets() for h in net.hosts()]
            with ThreadPoolExecutor(max_workers=128) as pool:
                banners = dict(zip(hosts, pool.map(ssh_banner, hosts)))
            candidates = [h for h, b in banners.items() if b and "Ubuntu" in b]
            if self.current and self.current not in candidates and banners.get(self.current, "x") is not None:
                candidates.append(self.current)  # a board on another subnet, typed in by hand
            with ThreadPoolExecutor(max_workers=16) as pool:
                found = [r for r in pool.map(self.probe, candidates) if r]
            found.sort(key=lambda r: ipaddress.ip_address(r["host"]) if r["host"][0].isdigit() else r["host"])
            self.last_scan, self.scanned_at = found, time.time()
        return self.listing()

    # ---- ssh ----
    def _master_ready(self, host: str) -> bool:
        try:
            return subprocess.run(["ssh", "-O", "check", *COMMON, f"{USER}@{host}"],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=5).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def ensure_master(self, host: str) -> None:
        """Open (or reuse) the ControlMaster for host; raise AuthRequired or BoardError."""
        lock = self.master_locks.setdefault(host, threading.Lock())
        with lock:
            if self._master_ready(host):
                return
            env = dict(os.environ)
            cmd = ["ssh", "-fNM", "-o", "ControlPersist=600", "-o", "ConnectTimeout=4", *COMMON]
            password = self.passwords.get(host)
            if password is None:
                cmd += ["-o", "BatchMode=yes"]
            else:
                cmd += ["-o", "NumberOfPasswordPrompts=1"]
                env.update(SSH_ASKPASS=_askpass(), SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY", ":0"),
                           ROBOTLAB_SSH_PASSWORD=password)
            # -f leaves a daemon holding stderr, so stderr goes to a file rather than a pipe.
            with tempfile.TemporaryFile() as err:
                try:
                    rc = subprocess.run([*cmd, f"{USER}@{host}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=err, env=env, timeout=15).returncode
                except subprocess.TimeoutExpired:
                    raise BoardError(f"连接 {host} 超时") from None
                err.seek(0)
                message = err.read().decode("utf-8", "replace").strip()
            if rc == 0:
                return
            if "Permission denied" in message or "Too many authentication failures" in message:
                self.passwords.pop(host, None)
                raise AuthRequired(f"{USER}@{host} 认证失败")
            raise BoardError(message.splitlines()[-1] if message else f"无法连接 {host}")

    def ssh_args(self, host: str, remote: str, connect_timeout: int = 4) -> list[str]:
        # Rides the board's master when there is one; otherwise a plain key-only attempt.
        return ["ssh", *COMMON, "-o", f"ConnectTimeout={connect_timeout}", "-o", "BatchMode=yes", f"{USER}@{host}", remote]

    def run(self, host: str, remote: str, timeout: float = 25, input: bytes | None = None) -> subprocess.CompletedProcess:
        self.ensure_master(host)
        return subprocess.run(self.ssh_args(host, remote), input=input, capture_output=True, timeout=timeout,
                              **({} if input is not None else {"stdin": subprocess.DEVNULL}))

    # ---- selection and the streaming bridge ----
    def select(self, host: str, password: str | None = None) -> str | None:
        """Make host current. Auth is checked first; an unreachable board is still selected, and
        why it is unreachable is returned so the caller need not ask again."""
        if not valid_host(host):
            raise ValueError("不是有效的 IP 或主机名")
        if password is not None:
            self.passwords[host] = password
        unreachable = None
        try:
            self.ensure_master(host)
        except AuthRequired:
            raise
        except BoardError as exc:
            unreachable = str(exc)[:200]  # offline boards are selectable; the bridge keeps retrying
        with self.lock:
            changed = host != self.current
            self.current = host
            self._save()
        if changed:
            self.camera.stop("已切换主板，画面已关闭")
        if changed or self.proc is None:
            self.start_bridge()
        return unreachable

    def start_bridge(self) -> None:
        with self.lock:
            self.generation += 1
            generation, host = self.generation, self.current
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
        if host:
            threading.Thread(target=self._bridge, args=(generation, host), daemon=True).start()

    def stop(self) -> None:
        self.camera.stop()
        with self.lock:
            self.generation += 1
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()

    def _live(self, generation: int) -> bool:
        return generation == self.generation

    def _bridge(self, generation: int, host: str) -> None:
        send = lambda event: self._live(generation) and self.publish(event)  # noqa: E731
        while self._live(generation):
            try:
                self.ensure_master(host)
            except AuthRequired as exc:
                send({"type": "board_status", "source": "board", "board_ip": host, "board_online": True,
                      "auth_required": True, "board_error": str(exc)})
                # Status or camera requests can restore the shared master independently.
                # Wait for that master (or a new selection) without retrying credentials.
                while self._live(generation) and not self._master_ready(host):
                    time.sleep(3)
                continue
            except BoardError as exc:
                send({"type": "board_status", "source": "board", "board_ip": host, "board_online": False,
                      "turn_phase": "离线", "board_error": str(exc)[:200]})
                time.sleep(3)
                continue
            with board_bridge.AGENT.open("rb") as script:
                proc = subprocess.Popen(self.ssh_args(host, "exec python3 -u -"), stdin=script,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                        encoding="utf-8", errors="replace", bufsize=1)
            with self.lock:
                if not self._live(generation):
                    proc.terminate()
                    return
                self.proc = proc
            try:
                board_bridge.pump(proc.stdout, board_bridge.Translator(host), send)
            finally:
                with self.lock:
                    if self.proc is proc:
                        self.proc = None
                if proc.poll() is None:
                    proc.terminate()
                proc.wait()
            if self._live(generation):
                send({"type": "board_status", "source": "board", "board_ip": host, "board_online": False,
                      "turn_phase": "离线"})
                time.sleep(3)


CAMERA_SCRIPT = board_bridge.AGENT.with_name("board_camera.py")


def camera_command(scale: int, fps: float) -> str:
    """The script goes as an argument, not on stdin, so stdin can carry the heartbeat."""
    code = base64.b64encode(CAMERA_SCRIPT.read_bytes()).decode("ascii")
    return f"exec python3 -u -c \"import base64;exec(base64.b64decode('{code}'))\" --scale {int(scale)} --fps {fps:g}"


def _read_exact(stream, n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class CameraFeed:
    """The selected board's camera, on demand for the page's camera window.

    While it runs, visiond keeps running: the script reads the ISP's other path (scripts/board_camera.py).
    It ends when the page stops it, when no page has asked for a frame for VIEWER_IDLE_S,
    when the board is switched, or when this process goes: the board side leaves as soon as the
    heartbeat on its stdin stops. Only the newest frame is kept."""

    VIEWER_IDLE_S = 15.0
    BEAT_S = 2.0
    MAX_RECORD = 8 * 1024 * 1024

    def __init__(self, link: BoardLink):
        self.link = link
        self.cond = threading.Condition()
        self.proc: subprocess.Popen | None = None
        self.generation = 0
        self.state, self.message, self.host = "off", "", None
        self.frame: tuple[int, int, int, bytes] | None = None  # (seq, width, height, NV12)
        self.seq = 0
        self.viewed_at = 0.0
        self.arrivals: deque[float] = deque(maxlen=12)

    def status(self) -> dict:
        with self.cond:
            fps = 0.0
            if len(self.arrivals) >= 2 and self.arrivals[-1] > self.arrivals[0]:
                fps = round((len(self.arrivals) - 1) / (self.arrivals[-1] - self.arrivals[0]), 1)
            out = {"ok": True, "state": self.state, "message": self.message, "board_ip": self.host,
                   "frames": self.seq, "fps": fps}
            if self.frame:
                out["width"], out["height"] = self.frame[1], self.frame[2]
            return out

    def start(self, scale: int = 1, fps: float = 4.0) -> dict:
        host = self.link.current
        if not host:
            raise BoardError("未选择主板")
        self.link.ensure_master(host)  # AuthRequired / BoardError go to the caller
        with self.cond:
            if self.proc and self.proc.poll() is None and self.host == host:
                self.viewed_at = time.monotonic()
                return self.status()
            self._stop_locked("")
            self.generation += 1
            generation = self.generation
            proc = subprocess.Popen(self.link.ssh_args(host, camera_command(scale, fps)), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.proc, self.host = proc, host
            self.state, self.message = "starting", "正在打开摄像头…"
            self.viewed_at = time.monotonic()
            self.arrivals.clear()
        threading.Thread(target=self._read, args=(generation, proc), daemon=True).start()
        threading.Thread(target=self._beat, args=(generation, proc), daemon=True).start()
        return self.status()

    def stop(self, message: str = "已关闭") -> dict:
        with self.cond:
            self._stop_locked(message)
        return self.status()

    def _stop_locked(self, message: str) -> None:
        self.generation += 1
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            threading.Thread(target=self._let_go, args=(proc,), daemon=True).start()
        if self.state != "off" or message:
            self.state, self.message = "off", message
        self.frame = None
        self.cond.notify_all()

    @staticmethod
    def _let_go(proc: subprocess.Popen) -> None:
        """Closing stdin is the board's cue to stop streaming; force it only if it hangs."""
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.terminate()

    def wait_frame(self, after: int, timeout: float = 3.0) -> tuple[int, int, int, bytes] | None:
        """The newest frame past `after`, waiting up to `timeout`. Asking counts as watching."""
        deadline = time.monotonic() + timeout
        with self.cond:
            self.viewed_at = time.monotonic()
            while not (self.frame and self.frame[0] > after) and self.state in ("starting", "live"):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            self.viewed_at = time.monotonic()
            return self.frame if self.frame and self.frame[0] > after else None

    def _current(self, generation: int) -> bool:
        return generation == self.generation

    def _note(self, generation: int, text: str, failed: bool = False) -> None:
        with self.cond:
            if not self._current(generation):
                return
            if failed:
                self.state = "error"
            if failed or self.state != "live":
                self.message = text
            self.cond.notify_all()
        self.link.publish({"type": "board_log", "source": "board", "level": "error" if failed else "info",
                           "logger": "camera", "message": text[:260]})

    def _read(self, generation: int, proc: subprocess.Popen) -> None:
        try:
            while True:
                head = _read_exact(proc.stdout, 12)
                if head is None:
                    break
                kind, (width, height, length) = head[:4], struct.unpack("<HHI", head[4:])
                if length > self.MAX_RECORD:
                    raise ValueError("摄像头帧超过长度上限")
                payload = _read_exact(proc.stdout, length)
                if payload is None:
                    break
                if kind == b"NOTE":
                    text = payload.decode("utf-8", "replace")
                    self._note(generation, text, failed=text.startswith("采集失败"))
                elif kind == b"NV12":
                    frame = zlib.decompress(payload)
                    if not width or not height or width % 2 or height % 2 or len(frame) != width * height * 3 // 2:
                        raise ValueError("摄像头帧尺寸或长度不符")
                    with self.cond:
                        if not self._current(generation):
                            break
                        self.seq += 1
                        self.frame = (self.seq, width, height, frame)
                        self.state, self.message = "live", ""
                        self.arrivals.append(time.monotonic())
                        self.cond.notify_all()
        except (OSError, ValueError, zlib.error) as exc:
            self._note(generation, f"采集失败：{exc}", failed=True)
            # A producer can keep writing after a bad record. Do not wait forever on a pipe
            # nobody drains while the heartbeat still tells that producer the viewer is alive.
            proc.terminate()
        rc = proc.wait()
        with self.cond:
            if not self._current(generation):
                return
            self.proc = None
            if self.state != "error":
                self.state = "error"
                self.message = "板端摄像头连接已断开" + (f"（code={rc}）" if rc else "")
            self.frame = None
            self.cond.notify_all()

    def _beat(self, generation: int, proc: subprocess.Popen) -> None:
        while True:
            time.sleep(self.BEAT_S)
            with self.cond:
                if not self._current(generation) or proc.poll() is not None:
                    return
                idle = time.monotonic() - self.viewed_at > self.VIEWER_IDLE_S
            if idle:
                self.stop("没有页面在看，画面已自动关闭")
                return
            try:
                proc.stdin.write(b"\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                return
