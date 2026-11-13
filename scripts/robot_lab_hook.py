#!/usr/bin/env python3
"""板端 (`smaitagent-runtime`) -> Robot Lab (`http://127.0.0.1:8769/api/events`) 实时事件转发与防回声自激钩子。

设计原则：
1. 100% 异步非阻塞（后台单守护线程 + 有界队列，超时 0.8s，任何异常静默降级，绝不阻塞主语音/LLM 链路）。
2. 唤醒门控感知（Dormant 过滤）+ 防回声自激（Echo Guard）：
   - 未唤醒 (`dormant`) 时，麦克风环境背景音触发的 `listening -> thinking -> listening` 空转不推送给 Robot Lab；
   - 记录机器人自身播报状态（`_is_speaking` / `_last_speaking_end_ts`）与最近一次回复文本（`_last_assistant_norm`），
     精准拦截喇叭尾音回声（如“有什么我可以帮你的吗”），彻底杜绝唤醒后自言自语死循环；
   - 命中唤醒词 (`Hey buddy`) 进入 `awake` 时，立即推送 `D02`（默认唤醒）；
   - 唤醒超时回到 `dormant` 时，自动推送 `D01`（默认待机）。
3. 完整映射：
   - 18 种具名情绪 (`app.output.expression.v1`) -> Robot Lab 复合动作 `A01..A21`（自动联动 `F01..F21` 表情）
   - 轮次状态 (`app.output.event.v1`) -> `D02`(聆听) / `F04`(思考) / `stop`(打断) / `D01`(待机)
   - 模式切换 (`meeting` / `tscy` / `normal`) + 字幕状态 -> 录音态 `R00..R07` / 翻译态 `T01..T08`
   - `device_control` 技能 -> `action` / `doa` / `reset` / `stop`
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from typing import Optional
from urllib.request import Request, urlopen

ROBOT_LAB_URL = os.environ.get("ROBOT_LAB_URL", "http://127.0.0.1:8769/api/events")
ROBOT_LAB_HOST = os.environ.get("ROBOT_LAB_HOST", "127.0.0.1:8769")

# 板端 18 种具名情绪 + 扩展名 -> Robot Lab 复合动作 ID (A01..A21)
EMOTION_TO_ACTION: dict[str, str] = {
    "calm": "A01",        # 平静
    "joy": "A02",         # 喜悦
    "obsession": "A03",   # 入迷
    "ponder": "A04",      # 深思
    "sad": "A05",         # 难过
    "grief": "A06",       # 悲痛
    "accept": "A07",      # 接受
    "trust": "A08",       # 信任
    "admiration": "A09",  # 敬佩
    "dull": "A10",        # 无趣
    "disgust": "A11",     # 厌恶
    "contempt": "A12",    # 嫌弃
    "nervous": "A13",     # 紧张
    "fear": "A14",        # 害怕
    "panic": "A15",       # 恐慌
    "worry": "A16",       # 烦忧
    "angry": "A17",       # 生气
    "irritable": "A18",   # 暴躁
    "interested": "A19",  # 感兴趣
    "expect": "A20",      # 盼望
    "vigilant": "A21",    # 警惕
}

_queue: queue.Queue[dict] = queue.Queue(maxsize=128)
_worker_started = False
_worker_lock = threading.Lock()

# 状态追踪（避免表情动作刚发出就被 milliseconds 后的 completed/default 覆盖）
_last_action_ts: float = 0.0
_last_action_turn: Optional[str] = None
_last_state_id: Optional[str] = None
_last_state_ts: float = 0.0
_last_expr_id: Optional[str] = None
_last_expr_ts: float = 0.0

# 唤醒门控状态（开启唤醒词时默认 False，无唤醒词或 omni 模式默认 True）
_wake_gate_enabled: bool = os.environ.get("WAKE_WORD_ENABLED", "1") not in ("0", "false", "False")
_is_awake: bool = not _wake_gate_enabled or os.environ.get("OMNI_ENABLED", "") == "1"

# 防回声自激追踪（记录 TTS 播报状态与最近助手文本）
_is_speaking: bool = False
_last_speaking_end_ts: float = 0.0
_last_assistant_norm: str = ""
_last_assistant_ts: float = 0.0

_NORM_RE = re.compile(r"[\s，。！？、,.!?~～—－\-_「」『』【】（）()\[\]{}:：;；\"'“”‘’]+")


def _norm_text(s: str) -> str:
    return _NORM_RE.sub("", (s or "").strip().lower())


def is_likely_echo(user_text: str) -> bool:
    """判断一段 ASR 文本是否是板端自己喇叭播报出来的回声。"""
    now = time.monotonic()
    # 1. 正在播报或播报刚结束 1.2s 内的尾音窗口
    if _is_speaking or (now - _last_speaking_end_ts) < 1.2:
        return True
    # 2. 最近 15s 内助手刚说过的话的子串或高度重合回声
    norm = _norm_text(user_text)
    if not norm:
        return False
    if _last_assistant_norm and (now - _last_assistant_ts) < 15.0:
        if len(norm) >= 3 and norm in _last_assistant_norm:
            return True
        # 检查 2-gram 重合率（应对 ASR 把“小旭”听成“小徐”等微调同音回声）
        if len(norm) >= 4 and len(_last_assistant_norm) >= 4:
            bigrams = {norm[i : i + 2] for i in range(len(norm) - 1)}
            assistant_bigrams = {
                _last_assistant_norm[i : i + 2] for i in range(len(_last_assistant_norm) - 1)
            }
            if bigrams and (len(bigrams & assistant_bigrams) / len(bigrams)) >= 0.6:
                return True
    return False


def _detect_initial_mode() -> str:
    if os.environ.get("MEETING_ENABLED", "") == "1":
        return "meeting"
    if os.environ.get("TSCY_ENABLED", "") == "1":
        return "tscy"
    return "normal"


_current_mode: str = _detect_initial_mode()


def _worker_loop() -> None:
    while True:
        payload = _queue.get()
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = Request(
                ROBOT_LAB_URL,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Host": ROBOT_LAB_HOST,
                },
                method="POST",
            )
            with urlopen(req, timeout=0.8) as resp:
                resp.read()
        except Exception:
            pass
        finally:
            _queue.task_done()


def _enqueue(payload: dict) -> None:
    global _worker_started
    if not _worker_started:
        with _worker_lock:
            if not _worker_started:
                t = threading.Thread(target=_worker_loop, name="robot-lab-hook", daemon=True)
                t.start()
                _worker_started = True
    payload = dict(payload, source="board")
    try:
        _queue.put_nowait(payload)
    except queue.Full:
        try:
            _queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _queue.put_nowait(payload)
        except queue.Full:
            pass


def _enqueue_state_dedup(state_id: str, min_interval: float = 0.4) -> None:
    global _last_state_id, _last_state_ts
    now = time.monotonic()
    if state_id == _last_state_id and (now - _last_state_ts) < min_interval:
        return
    _last_state_id = state_id
    _last_state_ts = now
    _enqueue({"type": "state", "state_id": state_id})


def _enqueue_expr_dedup(expression_id: str, mode: str = "loop", min_interval: float = 0.5) -> None:
    global _last_expr_id, _last_expr_ts
    now = time.monotonic()
    if expression_id == _last_expr_id and (now - _last_expr_ts) < min_interval:
        return
    _last_expr_id = expression_id
    _last_expr_ts = now
    _enqueue({"type": "expression", "expression_id": expression_id, "mode": mode})


def emit_wake_state(awake: bool, reason: str = "") -> None:
    """当 `ModeGate` 切换唤醒/休眠状态时调用。"""
    global _is_awake, _last_expr_id
    prev = _is_awake
    _is_awake = awake
    if awake != prev or reason:
        _enqueue(
            {
                "type": "board_status",
                "wake_state": "awake" if awake else "dormant",
                "detail": reason or "",
            }
        )
    if _current_mode != "normal":
        return
    now = time.monotonic()
    if awake and not prev:
        _last_expr_id = None
        _enqueue_expr_dedup("D02", mode="loop", min_interval=0.0)
    elif not awake and prev:
        if (now - _last_action_ts) >= 4.0:
            _last_expr_id = None
            _enqueue_expr_dedup("D01", mode="loop", min_interval=0.0)


def emit_expression(expression: str, turn_id: Optional[str] = None) -> None:
    """当 `OutputPublisher.expression` 下发语义表情时调用。"""
    global _last_action_ts, _last_action_turn, _last_expr_id
    expr = (expression or "").strip().lower()
    now = time.monotonic()
    action_id = EMOTION_TO_ACTION.get(expr)
    if action_id:
        _last_action_ts = now
        _last_action_turn = turn_id
        _last_expr_id = None
        _enqueue(
            {
                "type": "action",
                "action_id": action_id,
                "emotion": expr,
                "turn_id": str(turn_id or ""),
            }
        )
        return
    if expr in ("idle", "default"):
        # 若刚触发过具名动作（< 5s），让动作自然播完，不立刻覆盖屏幕表情
        if (now - _last_action_ts) < 5.0:
            return
        target_face = "D02" if _is_awake else "D01"
        _enqueue_expr_dedup(target_face, mode="loop")


def emit_turn_event(
    event_name: str,
    turn_id: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    """当 `OutputPublisher.event` 下发轮次状态事件时调用。"""
    global _is_speaking, _last_speaking_end_ts
    ev = (event_name or "").strip().lower()
    now = time.monotonic()
    is_voice_turn = bool(turn_id and str(turn_id).startswith("turn_voice_"))

    if ev == "speaking":
        _is_speaking = True
    elif _is_speaking and ev in ("listening", "completed", "idle", "interrupted", "cancelled", "failed"):
        _is_speaking = False
        _last_speaking_end_ts = now

    if not (is_voice_turn and not _is_awake and ev in ("listening", "thinking")):
        _enqueue(
            {
                "type": "board_status",
                "turn_phase": ev,
                "turn_id": str(turn_id or ""),
            }
        )

    if _current_mode == "meeting":
        if ev == "listening":
            _enqueue_state_dedup("R02")
        elif ev in ("speaking", "thinking"):
            _enqueue_state_dedup("R03")
        elif ev in ("interrupted", "cancelled"):
            _enqueue_state_dedup("R04")
        elif ev == "failed":
            _enqueue_state_dedup("R06")
        elif ev in ("completed", "idle"):
            _enqueue_state_dedup("R01")
        return

    if _current_mode == "tscy":
        if ev == "listening":
            _enqueue_state_dedup("T01")
        elif ev == "thinking":
            _enqueue_state_dedup("T02")
        elif ev == "speaking":
            _enqueue_state_dedup("T03")
        elif ev in ("completed", "idle"):
            _enqueue_state_dedup("T08")
        return

    # 普通对话模式 (voice / omni / assistant)
    # 若语音门控处于未唤醒 (dormant) 状态，忽略语音通道因环境音产生的 listening/thinking 空转
    if is_voice_turn and not _is_awake and ev in ("listening", "thinking"):
        return

    if ev == "listening":
        if (now - _last_action_ts) >= 4.5:
            _enqueue_expr_dedup("D02" if _is_awake else "D01", mode="loop")
    elif ev == "thinking":
        # 如果当前轮次还没出具名情绪动作，先切 F04 深思表情（不抢占 7 轴舵机）
        if turn_id != _last_action_turn and (now - _last_action_ts) > 3.0:
            _enqueue_expr_dedup("F04", mode="loop")
    elif ev in ("interrupted", "cancelled"):
        _enqueue({"type": "stop"})
        _enqueue_expr_dedup("D02" if _is_awake else "D01", mode="loop", min_interval=0.0)
    elif ev == "failed":
        _enqueue({"type": "state", "state_id": "R06"})


def emit_mode_change(mode: str) -> None:
    """当 `ModeController.enter` 热切换模式时调用。"""
    global _current_mode, _last_state_id, _last_expr_id
    m = (mode or "normal").strip().lower()
    _current_mode = m
    _last_state_id = None
    _last_expr_id = None
    _enqueue({"type": "board_status", "mode": m})
    if m == "meeting":
        _enqueue_state_dedup("R00", min_interval=0.0)
        _enqueue_state_dedup("R01", min_interval=0.0)
    elif m == "tscy":
        _enqueue_state_dedup("T08", min_interval=0.0)
    else:
        _enqueue({"type": "reset"})


def _is_mostly_english(text: str) -> bool:
    ascii_letters = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    cjk_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return ascii_letters > cjk_chars * 2 and ascii_letters >= 2


_omni_expr_gen = None


async def _run_omni_expression(user_text: str) -> None:
    """在 omni 模式下，收到 user_final 字幕时异步调用 ExpressionGenerator 驱动 Robot Lab 3D 动作与表情。"""
    global _omni_expr_gen
    try:
        if _omni_expr_gen is None:
            from agent_runtime.core.config import Config
            from agent_runtime.frontend.expression_generator import build_expression_generator

            _omni_expr_gen = build_expression_generator(Config.from_env())
        expr = await _omni_expr_gen.generate(user_text)
        if expr is not None:
            emit_expression(expr.value, turn_id="omni")
    except Exception:
        pass


def emit_caption_state(kind: str, text: str) -> None:
    """记录助手播报文本（用于回声过滤），并在 omni / meeting / tscy 模式下根据实时字幕流驱动状态与动作。"""
    global _last_assistant_norm, _last_assistant_ts, _last_speaking_end_ts
    if not text or not text.strip():
        return
    if kind in ("user_interim", "user_final", "assistant"):
        _enqueue({"type": "caption", "kind": kind, "text": text.strip()})
    if kind == "assistant":
        _last_assistant_norm = _norm_text(text)
        _last_assistant_ts = time.monotonic()
        _last_speaking_end_ts = time.monotonic()

    if _current_mode == "meeting":
        if kind == "user_interim":
            _enqueue_state_dedup("R02", min_interval=1.2)
        elif kind == "user_final":
            _enqueue_state_dedup("R01", min_interval=0.2)
        elif kind == "assistant":
            _enqueue_state_dedup("R03", min_interval=0.2)
    elif _current_mode == "tscy":
        eng = _is_mostly_english(text)
        if kind == "user_interim":
            _enqueue_state_dedup("T05" if eng else "T01", min_interval=1.0)
        elif kind == "user_final":
            _enqueue_state_dedup("T06" if eng else "T02", min_interval=0.2)
        elif kind == "assistant":
            _enqueue_state_dedup("T03" if eng else "T07", min_interval=0.2)
    elif os.environ.get("OMNI_ENABLED", "") == "1":
        now = time.monotonic()
        if kind == "user_interim":
            if (now - _last_action_ts) >= 4.5:
                _enqueue_expr_dedup("D02", mode="loop", min_interval=1.5)
        elif kind == "user_final":
            if (now - _last_action_ts) >= 3.0:
                _enqueue_expr_dedup("F04", mode="loop", min_interval=0.5)
            try:
                import asyncio

                asyncio.get_running_loop().create_task(_run_omni_expression(text))
            except RuntimeError:
                pass


def emit_device_control(params: dict) -> None:
    """当 `DeviceControlSkill.run` 被调用时转发动作或 DOA 朝向。"""
    if not isinstance(params, dict):
        return
    # 1. 显式 DOA / 角度参数
    for key in ("angle_deg", "angle", "doa", "yaw"):
        if key in params:
            try:
                angle = float(params[key]) % 360.0
                _enqueue({"type": "doa", "angle_deg": angle, "frame": "world"})
                return
            except (ValueError, TypeError):
                pass

    # 2. 显式 action_id (A01..A21)
    action_id = str(params.get("action_id") or "").upper()
    if action_id in {f"A{i:02d}" for i in range(1, 22)}:
        _enqueue({"type": "action", "action_id": action_id})
        return

    action = str(params.get("action") or params.get("command") or "").strip().lower()
    if not action:
        return

    if any(k in action for k in ("reset", "回中", "归位", "归中")):
        _enqueue({"type": "reset"})
    elif any(k in action for k in ("stop", "停止", "暂停")):
        _enqueue({"type": "stop"})
    elif any(k in action for k in ("left", "左转", "往左", "向左")):
        _enqueue({"type": "doa", "angle_deg": 90, "frame": "world"})
    elif any(k in action for k in ("right", "右转", "往右", "向右")):
        _enqueue({"type": "doa", "angle_deg": 270, "frame": "world"})
    elif any(k in action for k in ("back", "后转", "向后", "转身")):
        _enqueue({"type": "doa", "angle_deg": 180, "frame": "world"})
    elif any(k in action for k in ("front", "center", "前", "正前")):
        _enqueue({"type": "doa", "angle_deg": 0, "frame": "world"})
    elif any(k in action for k in ("nod", "点头", "同意", "接受")):
        _enqueue({"type": "action", "action_id": "A07"})
    elif any(k in action for k in ("shake", "摇头", "嫌弃", "拒绝")):
        _enqueue({"type": "action", "action_id": "A12"})
    elif any(k in action for k in ("wave", "happy", "joy", "dance", "挥手", "开心", "欢呼", "跳舞")):
        _enqueue({"type": "action", "action_id": "A02"})
    elif action in EMOTION_TO_ACTION:
        _enqueue({"type": "action", "action_id": EMOTION_TO_ACTION[action]})
    else:
        _enqueue({"type": "action", "action_id": "A19"})
