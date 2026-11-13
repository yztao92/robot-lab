"""Runs ON the RK3576 board, for Robot Lab's camera window. Started by the Lab as
`ssh root@<ip> python3 -u -c "<this file>" --scale 1 --fps 4`, so stdin stays free for the heartbeat.

visiond owns /dev/video11 (the ISP's main path) and publishes features, never pixels, so this reads
the ISP's self path, /dev/video12, beside it: visiond keeps running and its events keep flowing, and
nothing here touches the sensor, whose exposure belongs to visiond and the 3A engine. media-bringup.md
records a parallel reader on a self path killing the pipeline on the earlier prototype; on the RK3576
(.7, 2026-11-02) visiond logged nothing and kept its pid. It ends when the Mac's heartbeat stops
(window closed, Lab gone, network lost), on SIGHUP/SIGTERM, a closed stdout, or an error.

`--device /dev/video11` is the old way, kept as a fallback: visiond is stopped for as long as this
runs and started again on every way out. Restarting it starts its camera and NPU together, and that
peak cost .7 an input power loss. Nothing is installed or written on the board.

Capture is visiond's own (visiond/src/camera.rs, measured on this board): V4L2 multi-planar NV12
1920x1080, the sensor's full output, with mmap buffers; on the main path, the IMX355's exposure and
gains re-applied after STREAMON, since every STREAMON there resets them to a black frame.

stdout is binary records: 4-byte kind, <HHI width height length>, payload.
  b"NV12"  zlib(NV12 frame, downscaled by --scale)
  b"NOTE"  UTF-8 text: what happened (visiond stopped, streaming, the error that ended it)
Python 3.10 standard library only: the board's stock python3.
"""
import argparse
import ctypes
import fcntl
import mmap
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib

MAINPATH, SELFPATH = "/dev/video11", "/dev/video12"
SENSOR = "/dev/v4l-subdev2"
WIDTH, HEIGHT = 1920, 1080
HEARTBEAT_S = 10.0

# ioctl numbers and struct layouts: aarch64 Linux, as dumped on this board (camera.rs).
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
VIDIOC_S_CTRL = 0xC008561C
BUF_TYPE_CAPTURE_MPLANE = 9
MEMORY_MMAP = 1
FIELD_NONE = 1
PIX_FMT_NV12 = 0x3231564E
# IMX355 exposure / analogue / digital gain, and the values visiond lights it up with.
SENSOR_CONTROLS = ((0x00980911, 2605), (0x009E0903, 960), (0x009F0905, 2000))


class PlanePixFormat(ctypes.Structure):
    _fields_ = [("sizeimage", ctypes.c_uint32), ("bytesperline", ctypes.c_uint32), ("reserved", ctypes.c_uint8 * 12)]


class PixFormatMplane(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32), ("pixelformat", ctypes.c_uint32),
                ("field", ctypes.c_uint32), ("colorspace", ctypes.c_uint32), ("plane_fmt", PlanePixFormat * 8),
                ("num_planes", ctypes.c_uint8), ("flags", ctypes.c_uint8), ("ycbcr_enc", ctypes.c_uint8),
                ("quantization", ctypes.c_uint8), ("xfer_func", ctypes.c_uint8), ("reserved", ctypes.c_uint8 * 7)]


class FormatUnion(ctypes.Union):
    _fields_ = [("pix_mp", PixFormatMplane), ("raw", ctypes.c_uint64 * 25)]


class Format(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("fmt", FormatUnion)]  # 4 bytes of padding before fmt


class RequestBuffers(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("type", ctypes.c_uint32), ("memory", ctypes.c_uint32),
                ("capabilities", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 1)]


class PlaneM(ctypes.Union):
    _fields_ = [("mem_offset", ctypes.c_uint32), ("userptr", ctypes.c_uint64), ("fd", ctypes.c_int32)]


class Plane(ctypes.Structure):
    _fields_ = [("bytesused", ctypes.c_uint32), ("length", ctypes.c_uint32), ("m", PlaneM),
                ("data_offset", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 11)]


class BufferM(ctypes.Union):
    _fields_ = [("offset", ctypes.c_uint32), ("userptr", ctypes.c_uint64), ("planes", ctypes.POINTER(Plane)),
                ("fd", ctypes.c_int32)]


class Buffer(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32), ("bytesused", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("field", ctypes.c_uint32), ("timestamp", ctypes.c_uint64 * 2),
                ("timecode", ctypes.c_uint8 * 16), ("sequence", ctypes.c_uint32), ("memory", ctypes.c_uint32),
                ("m", BufferM), ("length", ctypes.c_uint32), ("reserved2", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32)]


assert ctypes.sizeof(Format) == 208 and ctypes.sizeof(RequestBuffers) == 20
assert ctypes.sizeof(Plane) == 64 and ctypes.sizeof(Buffer) == 88

_out = threading.Lock()


def record(kind: bytes, payload: bytes, width: int = 0, height: int = 0) -> None:
    with _out:
        sys.stdout.buffer.write(kind + struct.pack("<HHI", width, height, len(payload)) + payload)
        sys.stdout.buffer.flush()


def note(text: str) -> None:
    record(b"NOTE", text.encode("utf-8"))


def downscale(frame, scale: int) -> bytes:
    """NV12 at WIDTHxHEIGHT → NV12 at 1/scale, by taking every scale-th sample (rows and columns).
    Chroma is interleaved U,V per 2x2 block, so a chroma row keeps every scale-th pair."""
    if scale == 1:
        return bytes(frame[: WIDTH * HEIGHT * 3 // 2])
    w = WIDTH // scale
    out = bytearray()
    for row in range(0, HEIGHT, scale):
        start = row * WIDTH
        out += frame[start:start + WIDTH:scale]
    uv = WIDTH * HEIGHT
    pair = bytearray(w)
    for row in range(0, HEIGHT // 2, scale):
        start = uv + row * WIDTH
        pair[0::2] = frame[start:start + WIDTH:2 * scale]
        pair[1::2] = frame[start + 1:start + WIDTH:2 * scale]
        out += pair
    return bytes(out)


def set_sensor_controls() -> None:
    try:
        fd = os.open(SENSOR, os.O_RDWR)
    except OSError as e:
        note(f"{SENSOR} 打不开，未设曝光增益：{e}")
        return
    try:
        for cid, value in SENSOR_CONTROLS:
            try:
                fcntl.ioctl(fd, VIDIOC_S_CTRL, struct.pack("<Ii", cid, value))
            except OSError as e:
                note(f"传感器控制 0x{cid:x} 设置失败：{e}")
    finally:
        os.close(fd)


def capture(scale: int, fps: float, alive, device: str = SELFPATH) -> None:
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    maps = []
    streaming = False
    try:
        fmt = Format(type=BUF_TYPE_CAPTURE_MPLANE)
        fmt.fmt.pix_mp.width, fmt.fmt.pix_mp.height = WIDTH, HEIGHT
        fmt.fmt.pix_mp.pixelformat, fmt.fmt.pix_mp.field = PIX_FMT_NV12, FIELD_NONE
        fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)
        got = fmt.fmt.pix_mp
        if (got.width, got.height, got.pixelformat, got.num_planes) != (WIDTH, HEIGHT, PIX_FMT_NV12, 1):
            raise RuntimeError(f"摄像头协商结果不符：{got.width}x{got.height} fourcc={got.pixelformat:08x} planes={got.num_planes}")
        if got.plane_fmt[0].bytesperline != WIDTH:
            raise RuntimeError(f"NV12 行宽 {got.plane_fmt[0].bytesperline} ≠ {WIDTH}，不支持行填充")

        req = RequestBuffers(count=4, type=BUF_TYPE_CAPTURE_MPLANE, memory=MEMORY_MMAP)
        fcntl.ioctl(fd, VIDIOC_REQBUFS, req)
        if req.count < 2:
            raise RuntimeError(f"驱动只给了 {req.count} 个缓冲")
        for i in range(req.count):
            plane, buf = Plane(), Buffer(index=i, type=BUF_TYPE_CAPTURE_MPLANE, memory=MEMORY_MMAP, length=1)
            buf.m.planes = ctypes.pointer(plane)
            fcntl.ioctl(fd, VIDIOC_QUERYBUF, buf)
            maps.append(mmap.mmap(fd, plane.length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                                  offset=plane.m.mem_offset))
            fcntl.ioctl(fd, VIDIOC_QBUF, buf)

        fcntl.ioctl(fd, VIDIOC_STREAMON, ctypes.c_uint32(BUF_TYPE_CAPTURE_MPLANE))
        streaming = True
        if device == MAINPATH:  # on the self path the sensor is visiond's, and stays as it is
            set_sensor_controls()
        w, h = WIDTH // scale, HEIGHT // scale
        note(f"采集中：{WIDTH}x{HEIGHT} NV12 → {w}x{h}，{fps:g} fps")

        need = WIDTH * HEIGHT * 3 // 2
        interval, next_at = 1.0 / fps, 0.0
        while alive():
            if not select.select([fd], [], [], 1.0)[0]:
                continue
            plane, buf = Plane(), Buffer(type=BUF_TYPE_CAPTURE_MPLANE, memory=MEMORY_MMAP, length=1)
            buf.m.planes = ctypes.pointer(plane)
            try:
                fcntl.ioctl(fd, VIDIOC_DQBUF, buf)
            except BlockingIOError:
                continue
            now = time.monotonic()
            if now >= next_at and plane.bytesused >= need and buf.index < len(maps):
                next_at = now + interval
                frame = downscale(maps[buf.index], scale)
                record(b"NV12", zlib.compress(frame, 1), w, h)
            fcntl.ioctl(fd, VIDIOC_QBUF, buf)
    finally:
        if streaming:
            try:
                fcntl.ioctl(fd, VIDIOC_STREAMOFF, ctypes.c_uint32(BUF_TYPE_CAPTURE_MPLANE))
            except OSError:
                pass
        for m in maps:
            m.close()
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--fps", type=float, default=4.0)
    parser.add_argument("--device", default=SELFPATH)
    args = parser.parse_args()
    fps = min(15.0, max(0.5, args.fps))

    beat = [time.monotonic()]
    ended = threading.Event()

    def heartbeat():
        # Any byte is a beat; end of stdin is the Mac letting go. os.read, not sys.stdin: a daemon
        # thread blocked inside the buffered reader holds its lock, and that aborted the interpreter
        # at exit (Fatal Python error: _enter_buffered_busy).
        while os.read(0, 64):
            beat[0] = time.monotonic()
        ended.set()

    threading.Thread(target=heartbeat, daemon=True).start()

    def leave(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, leave)

    # Starting counts as running: right after boot visiond is still `activating` for ~20 s, and a
    # capture opened then left visiond without its camera until someone restarted it.
    state = subprocess.run(["systemctl", "is-active", "visiond"], capture_output=True, text=True).stdout.strip()
    was_active = args.device == MAINPATH and state in ("active", "activating", "reloading")
    try:
        if was_active:
            subprocess.run(["systemctl", "stop", "visiond"], check=True, timeout=20)
            note("已暂停 visiond（摄像头只能有一个读者），关闭画面后自动恢复")
        capture(args.scale, fps, lambda: not ended.is_set() and time.monotonic() - beat[0] < HEARTBEAT_S, args.device)
        note("画面已关闭")
        return 0
    except (BrokenPipeError, SystemExit):
        return 0
    except Exception as e:  # report it, then still give the camera back
        try:
            note(f"采集失败：{type(e).__name__}: {e}")
        except OSError:
            pass
        return 1
    finally:
        if was_active:
            # --no-block: visiond's start may wait on its models; the camera is already released.
            subprocess.run(["systemctl", "start", "--no-block", "visiond"], timeout=20)


if __name__ == "__main__":
    sys.exit(main())
