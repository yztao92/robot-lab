#!/usr/bin/env python3
"""RK3576 实板 → Robot Lab 实时桥接（robotd / voiced / visiond / waked / translated / mediad 新栈）。

经 SSH 把 scripts/board_agent.py 送到板端用系统 python3 运行（板端不安装、不写任何文件），
读取它输出的 NDJSON，翻译成 Robot Lab 事件后 POST 到本地 /api/events：

- voiced 日志 → 实时字幕、DOA（镜像后）、对话阶段（待唤醒 / 聆听 / 思考 / 说话 / 会议 / 翻译 / 断开）
- waked 日志 → 唤醒词命中；translated 日志 → 同传原文/译文字幕与 T01–T08 翻译状态
- mediad 日志 → 板上屏幕实际播放的情绪表情（face.rs：18 种情绪 → F 系列，播完回待机）
- robotd robot.state → 头部姿态（head 三轴，轴向与符号未标定）
- systemd 服务状态、温度、CPU 占用与负载、开机时长、音量与亮度、visiond 人数 → 顶部状态栏

用法：python3 scripts/board_bridge.py --board-ip 192.168.0.7
回放：python3 scripts/board_bridge.py --replay agent.jsonl   （逐行读 board_agent 输出）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

AGENT = Path(__file__).with_name("board_agent.py")

# 对话阶段 → 屏幕素材。改表情只改这张表。
FACES = {
    "connecting": ("state", "R07"),          # 状态核查中：voiced 启动、连豆包
    "listening": ("expression", "D01"),      # 默认待机：会话在线，等人说话
    "thinking": ("expression", "F04"),       # 深思：用户说完，等回复第一帧语音
    "speaking": ("expression", "F02"),       # 喜悦：机器人在说
    "barge_in": ("expression", "F13"),       # 紧张：被打断，完整播放一次
    "meeting": ("state", "R01"),             # 记录 · 等待发言
    "confirm": ("state", "R05"),             # 等待确认：智能体在问人
    "offline": ("state", "R06"),             # 任务受阻：会话断开 / voiced 未运行
    "dormant": ("expression", "D01"),        # 默认待机：安静一阵后等唤醒词（waked）
    "translate_connecting": ("state", "R07"),  # translated 接手，正在连同传
}
PHASE_LABELS = {
    "connecting": "连接中", "listening": "聆听", "thinking": "思考", "speaking": "说话",
    "barge_in": "被打断", "meeting": "会议记录", "confirm": "等待确认", "offline": "会话断开",
    "dormant": "待唤醒", "translate_connecting": "翻译 · 连接中",
    "translate_listening": "翻译 · 聆听", "translate_preparing": "翻译 · 准备译文", "translate_speaking": "翻译 · 播报",
}
# Translation phases → T states, by direction: (listening, preparing, speaking). The catalog only
# draws 中→英 and 英→中; a pair from Chinese uses the first, anything else (English, or ja/fr/… into
# Chinese) the second.
TRANSLATION_STATES = {"zh": ("T01", "T02", "T03"), "other": ("T05", "T06", "T07")}
# mediad face.rs: the 18 expressions a person's words can pick → the catalog's F series (Fnn pairs Ann).
EMOTION_FACES = {
    "calm": "F01", "joy": "F02", "obsession": "F03", "ponder": "F04", "sad": "F05", "grief": "F06",
    "accept": "F07", "trust": "F08", "admiration": "F09", "dull": "F10", "disgust": "F11", "contempt": "F12",
    "nervous": "F13", "fear": "F14", "panic": "F15", "worry": "F16", "angry": "F17", "irritable": "F18",
}
# voiced's doa_deg is counter-clockwise in the mic array's frame, and on the board that comes out
# mirrored against the Lab's base (robot-left positive): a voice from the robot's left turned the
# preview right. Mirrored here, once, so the slider and injected events keep the documented sign.
DOA_MIRROR = True
LAB_HEAD_LIMITS = {"head_yaw": (-60, 60), "head_pitch_down": (-30, 12), "head_roll": (-10, 10)}

# AW88394 `aw_dev_N_rx_volume`, 0..720: attenuation in 1/8 dB, larger = quieter, 720 = 90 dB mute.
# Confirmed on .8 from SYSCTRL2 (reg 0x05, VOL[15:6] = ((v/48)<<6)+(v%48), v = init 16 + ctl):
# ctl 0 → 0x040a, ctl 48 → 0x140a. A normal boot reads 0 (today's level, under asound.conf's −7 dB
# brown-out cap), so 100 % = 0 and the slider only ever attenuates. 50 % ≈ −20 dB, 1 % ≈ −40 dB.
VOLUME_SPAN_DB = 40.0
VOLUME_RAW_MAX = 720


def volume_raw(percent: int) -> int:
    if percent <= 0:
        return VOLUME_RAW_MAX
    return min(VOLUME_RAW_MAX, round((100 - percent) / 100 * VOLUME_SPAN_DB * 8))


def volume_percent(raw: int) -> int:
    if raw >= VOLUME_RAW_MAX:
        return 0
    return max(1, min(100, round(100 - raw / 8 / VOLUME_SPAN_DB * 100)))


def fan_pwm(percent: int) -> int:
    """The RK3576 pwm-fan is `pwm-active-low`: the driver drives duty = 255 − pwm1, so pwm1 255 is
    stopped and 0 is full speed (boot: cooling level 64 reads back as 191)."""
    return 255 - round(255 * percent / 100)


def fan_percent(pwm: int) -> int:
    return round(100 * (255 - pwm) / 255)


def brightness_percent(value: int, maximum: int) -> int:
    return round(100 * value / maximum) if maximum > 0 else 0


def doa_world(board_deg: float) -> float:
    """voiced doa_deg → the Lab's world heading in [0, 360)."""
    deg = -board_deg if DOA_MIRROR else board_deg
    return round(deg % 360, 1) % 360


def face_event(phase: str, face: tuple[str, str] | None = None, mode: str | None = None) -> dict:
    kind, ident = face or FACES[phase]
    if kind == "state":
        return {"type": "state", "source": "board", "state_id": ident}
    event = {"type": "expression", "source": "board", "expression_id": ident,
             "mode": mode or ("full" if phase == "barge_in" else "loop")}
    if ident.startswith("F"):  # F01–F21 pair one-to-one with A01–A21 (catalog screen_id); D01/D02 hold still
        event["action_id"] = "A" + ident[1:]
    return event


def face_views(faces) -> list[dict]:
    """visiond's FaceView, as the camera window draws it; a visiond without the fields sends no place."""
    views = []
    for face in faces if isinstance(faces, list) else []:
        if isinstance(face, dict):
            views.append({key: face[key] for key in ("id", "name", "talking", "bbox", "landmarks") if key in face})
    return views


def board_time(ts: str) -> float | None:
    """A tracing timestamp (board clock, UTC) as epoch seconds."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z$", ts or "")
    if not m:
        return None
    base = dt.datetime.fromisoformat(m.group(1)).replace(tzinfo=dt.timezone.utc).timestamp()
    return base + (float(m.group(2)) if m.group(2) else 0.0)


def head_pose(head: list[float]) -> dict:
    """robotd head = [neck_pitch, head_pitch, head_yaw, head_roll] (rad). Lab axes differ from the
    duck's; only the obvious correspondence is used and none of it is calibrated."""
    if len(head) != 4:
        return {}
    degrees = {"head_yaw": head[2], "head_pitch_down": head[0] + head[1], "head_roll": head[3]}
    out = {}
    for key, rad in degrees.items():
        lo, hi = LAB_HEAD_LIMITS[key]
        out[key] = round(min(hi, max(lo, math.degrees(rad))), 1)
    return out


class Translator:
    """board_agent 的一行 → 若干 Robot Lab 事件。持有对话阶段与「说完」计时。"""

    def __init__(self, board_ip: str):
        self.board_ip = board_ip
        self.phase = None
        self.meeting = False
        self.speak_started = None
        self.speak_until = None
        self.services = {}
        self.turn: dict | None = None  # per-turn latency figures, keyed by board timestamps
        self.turns: list[dict] = []     # the last few, for board_agent's measurements arriving late
        self.speech_start_ts: float | None = None  # board receipt of ASR_INFO, consumed once per user
        self.woken_ts: float | None = None
        self.turn_seq = 0
        self.asleep = False            # voiced is waiting for the wake word
        self.translating = False       # translated has the microphone
        self.translate_from = None     # the pair's source language, once translated says it
        self.translate_until = None    # when a spoken translation is over (estimated from its length)
        self.face_lib = None           # mediad found libemotion (True), said it has none (False), or unknown
        self.screen = None             # what the Lab's screen was last told: "expression" or "state"

    @property
    def face_live(self) -> bool:
        """mediad is drawing the board's screen, so the Lab's screen follows it rather than the turn."""
        return self.face_lib is True or (self.face_lib is None and self.services.get("mediad") == "active")

    def _set_phase(self, phase: str, now: float, force_face: bool = False, face: tuple[str, str] | None = None,
                   mode: str | None = None) -> list[dict]:
        if phase == self.phase and not force_face:
            return []
        self.phase = phase
        if phase != "speaking":
            self.speak_started = self.speak_until = None
        if phase != "translate_speaking":
            self.translate_until = None
        status = {"type": "board_status", "source": "board", "turn_phase": PHASE_LABELS[phase]}
        event = face_event(phase, face, mode)
        if self.face_live and event["type"] == "expression":
            # On the board the face changes only on an emotion (mediad); the turn leaves it on idle,
            # so a phase only clears a state screen the Lab put up itself.
            if self.screen != "state":
                return [status]
            event = {"type": "expression", "source": "board", "expression_id": "D01", "mode": "loop"}
        self.screen = event["type"]
        return [event, status]

    def _resting(self) -> str:
        if self.meeting:
            return "meeting"
        return "dormant" if self.asleep else "listening"

    def _translation_face(self, step: int) -> tuple[str, str]:
        return "state", TRANSLATION_STATES["zh" if self.translate_from == "zh" else "other"][step]

    def tick(self, now: float) -> list[dict]:
        if self.phase == "speaking" and self.speak_until is not None and now >= self.speak_until:
            return self._set_phase(self._resting(), now)
        if self.phase == "translate_speaking" and self.translate_until is not None and now >= self.translate_until:
            return self._set_phase("translate_listening", now, face=self._translation_face(0))
        return []

    def feed(self, item: dict, now: float) -> list[dict]:
        src = item.get("src")
        if src == "voiced":
            return self._voiced(item, now)
        if src == "waked":
            return self._waked(item)
        if src == "translated":
            return self._translated(item, now)
        if src == "mediad":
            return self._mediad(item)
        if src == "robot":
            pose = head_pose(item.get("head") or [])
            return [{"type": "pose", "source": "board", "joints": pose}] if pose else []
        if src == "services":
            self.services = item.get("services") or {}
            if self.services.get("voiced") in ("inactive", "failed"):
                self.speech_start_ts = None
            status = {"type": "board_status", "source": "board", "board_online": True, "board_ip": self.board_ip,
                      "services": self.services, "voice_active": self.services.get("voiced") == "active",
                      "uptime_s": int(item.get("uptime_s") or 0)}
            for key, field in (("temp_c", "cpu_temp_c"), ("load_1m", "load_1m")):
                if isinstance(item.get(key), (int, float)):
                    status[field] = round(float(item[key]), 1 if key == "temp_c" else 2)
            for key in ("cpu_pct", "mem_pct", "npu_pct"):
                if isinstance(item.get(key), (int, float)):
                    status[key] = max(0, min(100, round(item[key])))
            for key in ("cpu_mhz", "cpu_mhz_full"):
                if isinstance(item.get(key), int):
                    status[key] = item[key]
            if isinstance(item.get("service_cpu"), dict):
                status["service_cpu"] = {name: max(0.0, min(100.0, float(share)))
                                         for name, share in item["service_cpu"].items()
                                         if isinstance(share, (int, float))}
            if isinstance(item.get("volume_raw"), int):
                status["volume_raw"] = item["volume_raw"]
                status["volume_pct"] = volume_percent(item["volume_raw"])
            if isinstance(item.get("fan_pwm"), int):
                status["fan_pct"] = fan_percent(item["fan_pwm"])
            if isinstance(item.get("brightness"), int) and isinstance(item.get("brightness_max"), int):
                status["brightness_pct"] = brightness_percent(item["brightness"], item["brightness_max"])
            out = [status]
            if self.services.get("voiced") != "active":
                out += self._set_phase("offline", now)
            return out
        if src == "vision":
            ev = item.get("event") or {}
            if ev.get("kind") == "faces":  # every look, a couple a second: for the camera window, not the log
                return [{"type": "faces", "source": "board", "faces": face_views(ev.get("faces"))}]
            out = []
            if "people" in ev:
                out.append({"type": "board_status", "source": "board", "people": int(ev["people"])})
            if ev.get("kind") != "room":
                out.append(self._log("info", "visiond", json.dumps(ev, ensure_ascii=False)))
            return out
        if src == "latency":
            return self._latency(item)
        if src == "agent":
            return [self._log("info", "agent", str(item.get("msg", "")))]
        return []

    def _metric(self, t: dict) -> dict:
        m = t["metrics"]
        event = {"type": "metric", "source": "board", "turn_id": t["id"], "metrics": dict(m)}
        if t.get("note"):
            event["note"] = t["note"]
        if t.get("woken"):
            event["woken"] = True
        return event

    def _turn_metrics(self, msg: str, f: dict, ts: float | None) -> list[dict]:
        """Use voiced's own e2e_ms, server audio ms, intent ms and optional doa_ms.

        No extra microphone capture is needed. Missing source metrics remain missing.
        """
        if msg in ("user started speaking", "user talked over the reply; cutting it"):
            if ts is not None and self.speech_start_ts is None:
                self.speech_start_ts = ts
            return []
        if msg in ("voiced starting", "session started; listening", "session ended", "reconnecting", "woken"):
            self.speech_start_ts = None
        if msg == "woken":
            self.turn = None  # The wake acknowledgement is not the previous turn's reply.
            self.woken_ts = ts
            return []
        if msg == "user":
            started, self.speech_start_ts = self.speech_start_ts, None
            if self.meeting or ts is None:
                self.turn = None
                return []
            self.turn_seq += 1
            woken = self.woken_ts is not None and 0 <= ts - self.woken_ts < 15
            self.woken_ts = None
            self.turn = {"id": f"t{self.turn_seq}", "user_ts": ts, "speech_start_ts": started, "metrics": {}, "woken": woken}
            self.turns = (self.turns + [self.turn])[-8:]
            return []
        t = self.turn
        if t is None:
            return []
        m = t["metrics"]
        try:
            if msg == "first reply audio after the user finished":
                m["server_first_ms"] = int(f["ms"])
                t["first_ts"] = ts
            elif msg == "reply audible after the user's last word":
                # Ignore wake tones/announcements and later blocks of the same reply.
                if ts is None or t.get("first_ts") is None or not 0 <= ts - t["first_ts"] <= 2 or "e2e_ms" in m:
                    return []
                m["e2e_ms"] = int(f["e2e_ms"])
            elif msg == "intent L2":
                # It starts with the reply's first audio and can outlast the next turn's start:
                # its own turn is the one whose first audio is its end less its length.
                ms = int(f["ms"])
                started = ts - ms / 1000 if ts is not None else None
                t = next((x for x in reversed(self.turns) if started is not None and x.get("first_ts") is not None
                          and abs(x["first_ts"] - started) < 0.3), t)
                t["metrics"]["intent_ms"] = ms
            else:
                return []
        except (KeyError, ValueError):
            return []
        return [self._metric(t)]

    def _latency(self, item: dict) -> list[dict]:
        # Legacy capture estimates must never overwrite voiced's log value.
        return []

    def _log(self, level: str, logger: str, message: str) -> dict:
        return {"type": "board_log", "source": "board", "level": level, "logger": logger, "message": message[:260]}

    def _voiced(self, item: dict, now: float) -> list[dict]:
        msg, f = item.get("msg", ""), item.get("fields", {})
        level = {"ERROR": "error", "WARN": "warn"}.get(item.get("level"), "info")
        detail = " ".join(f"{k}={v}" for k, v in f.items() if k not in ("text",))
        out = []
        if msg not in ("user", "robot"):  # those two become captions instead
            out.append(self._log(level, item.get("target", "voiced"), (msg + (" · " + detail if detail else ""))))

        out += self._turn_metrics(msg, f, board_time(item.get("ts", "")))

        if msg == "voiced starting":
            self.meeting = False
            out += self._set_phase("connecting", now)
        elif msg == "session started; listening":
            out += self._set_phase(self._resting(), now)
        elif msg in ("session ended", "reconnecting"):
            out += self._set_phase("offline", now)
        elif msg == "user":
            text = f.get("text", "")
            if text:
                out.append({"type": "caption", "source": "board", "kind": "user_final", "text": text, "is_final": True})
            doa = f.get("doa_deg")
            if doa not in (None, "", "None"):
                try:
                    angle = float(doa)
                    if not math.isfinite(angle):
                        raise ValueError("non-finite DOA")
                    event = {"type": "doa", "source": "board", "angle_deg": doa_world(angle), "frame": "world",
                             "detail": f"voiced doa_deg={doa}，已镜像，未标定"}
                    if self.turn:
                        event["turn_id"] = self.turn["id"]
                    out.append(event)
                    # Both timestamps are from voiced on the board. The user log publishes
                    # the DOA direction which causes this Lab turn command; PC transit is excluded.
                    started = self.turn.get("speech_start_ts") if self.turn else None
                    ended = board_time(item.get("ts", ""))
                    if started is not None and ended is not None and ended >= started:
                        self.turn["metrics"]["doa_ms"] = round((ended - started) * 1000, 3)
                        out.append(self._metric(self.turn))
                except ValueError:
                    pass
            if not self.meeting:
                out += self._set_phase("thinking", now)
        elif msg == "robot":
            text = f.get("text", "")
            if text and f.get("muted") != "true":
                out.append({"type": "caption", "source": "board", "kind": "assistant", "text": text, "is_final": True})
        elif msg == "announcing":
            out.append({"type": "caption", "source": "board", "kind": "assistant", "text": f.get("text", ""), "is_final": True})
        elif msg == "first reply audio after the user finished":
            out += self._set_phase("speaking", now)
            self.speak_started, self.speak_until = now, now + 20.0  # until `reply spoken` gives the length
        elif msg == "reply spoken" and self.phase == "speaking":
            try:
                self.speak_until = (self.speak_started or now) + float(f.get("secs", "0")) + 0.3
            except ValueError:
                pass
        elif msg == "user talked over the reply; cutting it":
            out += self._set_phase("barge_in", now, force_face=True)
            self.phase = "listening"
        elif msg == "meeting mode: listening only":
            self.meeting = True
            out += self._set_phase("meeting", now)
        elif msg in ("meeting mode ended", "meeting ended"):
            self.meeting = False
            out += self._set_phase("listening", now)
        elif msg == "asking the person":
            out += self._set_phase("confirm", now)
        elif msg in ("question answered", "confirmation answered"):
            out += self._set_phase(self._resting(), now)
        elif msg == "quiet: waiting for the wake word":
            self.asleep = True
            out += self._set_phase(self._resting(), now)
        elif msg == "woken":
            self.asleep = False
            out += self._set_phase("listening", now, face=("expression", "D02"), mode="once")
        elif msg in ("no waked: staying awake", "waked went away: staying awake"):
            self.asleep = False
            if self.phase == "dormant":
                out += self._set_phase(self._resting(), now)
        elif msg == "translated takes over":
            self.translating, self.asleep, self.translate_from = True, False, None
            out += self._set_phase("translate_connecting", now)
        elif msg == "conversation resumes":
            self.translating, self.translate_from = False, None
            out += self._set_phase(self._resting(), now)
        return out

    def _waked(self, item: dict) -> list[dict]:
        """The wake word itself; voiced's `woken` is what changes the phase."""
        msg, f = item.get("msg", ""), item.get("fields", {})
        if msg == "mic level, last 30 s":
            return []  # every 30 s, and only a level
        level = {"ERROR": "error", "WARN": "warn"}.get(item.get("level"), "info")
        detail = " ".join(f"{k}={v}" for k, v in f.items())
        text = "唤醒词 Hey Buddy" + (" · " + detail if detail else "") if msg == "wake word" else msg + (" · " + detail if detail else "")
        return [self._log(level, item.get("target", "waked"), text)]

    def _translated(self, item: dict, now: float) -> list[dict]:
        """Simultaneous interpretation: the source sentence and its translation as captions, and the
        T states for listening → preparing → speaking."""
        msg, f = item.get("msg", ""), item.get("fields", {})
        level = {"ERROR": "error", "WARN": "warn"}.get(item.get("level"), "info")
        out = []
        if msg != "subtitle":
            detail = " ".join(f"{k}={v}" for k, v in f.items())
            out.append(self._log(level, item.get("target", "translated"), msg + (" · " + detail if detail else "")))
        if msg == "connected to simultaneous interpretation":
            self.translate_from = f.get("from")
        elif msg == "translating":
            out += self._set_phase("translate_listening", now, face=self._translation_face(0))
        elif msg == "subtitle":
            text = f.get("text", "")
            if f.get("event") == "652" and text:  # the source sentence, finished
                out.append({"type": "caption", "source": "board", "kind": "user_final", "text": text,
                            "is_final": True, "detail": "translation_source"})
                out += self._set_phase("translate_preparing", now, face=self._translation_face(1))
            elif f.get("event") == "655" and text:  # its translation, finished
                out.append({"type": "caption", "source": "board", "kind": "assistant", "text": text,
                            "is_final": True, "detail": "translation"})
                out += self._set_phase("translate_speaking", now, face=self._translation_face(2))
                # No log marks the end of the spoken translation; about a quarter second a character.
                self.translate_until = now + min(12.0, max(2.0, 0.25 * len(text)))
        elif msg == "first voice after the sentence began" and self.phase != "translate_speaking":
            out += self._set_phase("translate_speaking", now, face=self._translation_face(2))
            self.translate_until = now + 6.0
        return out

    def _mediad(self, item: dict) -> list[dict]:
        """The board's screen: mediad picks an expression from what the person said and plays it once.
        The board plays no motion with it (mediad only draws, robotd runs --fake), but the design pairs
        every state's face with its body (Fnn ↔ Ann, the catalog's screen_id), so the Lab's robot
        shows both, as the design would, and queues them like any other performance."""
        msg, f = item.get("msg", ""), item.get("fields", {})
        level = {"ERROR": "error", "WARN": "warn"}.get(item.get("level"), "info")
        detail = " ".join(f"{k}={v}" for k, v in f.items())
        out = [self._log(level, item.get("target", "mediad"), msg + (" · " + detail if detail else ""))]
        if msg == "face ready":
            self.face_lib = True
        elif msg.startswith("no ") and msg.endswith(": no expressions"):
            self.face_lib = False
        elif msg in ("expression, from a word", "expression, from the model"):
            face = EMOTION_FACES.get(f.get("expression", ""))
            if face:
                self.face_lib = True
                self.screen = "expression"
                out.append({"type": "expression", "source": "board", "expression_id": face, "mode": "once",
                            "action_id": "A" + face[1:], "emotion": f["expression"]})
        return out


def post_event(port: int, payload: dict) -> None:
    req = Request(f"http://127.0.0.1:{port}/api/events", method="POST",
                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"})
    try:
        with urlopen(req, timeout=3.0) as resp:
            resp.read()
    except Exception as exc:
        print(f"[warn] 推送失败 {payload.get('type')}: {exc}", file=sys.stderr, flush=True)


def pump(lines, translator: Translator, send) -> None:
    """Read agent lines from an iterator on a thread; tick the speaking timer between them."""
    inbox: queue.Queue = queue.Queue()

    def reader():
        for line in lines:
            inbox.put(line)
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    while True:
        try:
            line = inbox.get(timeout=0.25)
        except queue.Empty:
            line = ""
        if line is None:
            return
        now = time.monotonic()
        events = translator.tick(now)
        if line.strip():
            try:
                events += translator.feed(json.loads(line), now)
            except (ValueError, TypeError, KeyError) as exc:
                print(f"[warn] 跳过无法解析的板端行: {exc}: {line[:120]}", file=sys.stderr, flush=True)
        for event in events:
            send(event)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--board-ip", default="192.168.0.7", help="RK3576 主板 IP（默认 192.168.0.7）")
    parser.add_argument("--simulator-port", type=int, default=8769, help="本地 Robot Lab 端口（默认 8769）")
    parser.add_argument("--replay", type=Path, help="回放一份 board_agent 输出（NDJSON），不连板")
    args = parser.parse_args()
    send = lambda event: post_event(args.simulator_port, event)  # noqa: E731

    if args.replay:
        with args.replay.open(encoding="utf-8") as lines:
            pump(lines, Translator(args.board_ip), lambda e: send(dict(e, source="replay")))
        return 0

    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
           "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
           f"root@{args.board_ip}", "exec python3 -u -"]
    try:
        while True:
            print(f"[bridge] 连接 {args.board_ip} …", flush=True)
            with AGENT.open("rb") as script:
                proc = subprocess.Popen(cmd, stdin=script, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1)
                pump(proc.stdout, Translator(args.board_ip), send)
                rc = proc.wait()
            print(f"[bridge] 板端连接结束 (code={rc})，3s 后重连", flush=True)
            send({"type": "board_status", "source": "board", "board_online": False, "board_ip": args.board_ip,
                  "turn_phase": "离线"})
            time.sleep(3.0)
    except KeyboardInterrupt:
        print("\n[bridge] 已停止。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
