"""End-to-end checks of the local inbound bridge and real media responses."""
import http.client
import importlib.util
import json
from pathlib import Path
import queue
import threading
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("robot_simulator_server", Path(__file__).resolve().parents[1] / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class FakeCamera:
    def __init__(self):
        self.started, self.frame, self.error = [], None, None

    def status(self):
        return {"ok": True, "state": "live" if self.started else "off", "message": ""}

    def start(self, scale, fps):
        if self.error:
            raise self.error
        self.started.append((scale, fps))
        return self.status()

    def stop(self, message="已关闭"):
        self.started.clear()
        return self.status()

    def wait_frame(self, after, timeout=3.0):
        return self.frame if self.frame and self.frame[0] > after else None


class FakeLink:
    """Stands in for board_link.BoardLink: records ssh commands and plays back canned results."""

    def __init__(self, result=None, error=None):
        self.current, self.calls, self.result, self.error = "192.0.2.7", [], result, error
        self.selected, self.inputs = [], []
        self.camera = FakeCamera()

    def run(self, host, remote, timeout=25, input=None):
        self.calls.append(remote)
        self.inputs.append(input)
        if self.error:
            raise self.error
        return self.result(remote) if callable(self.result) else self.result

    def select(self, host, password=None):
        if not server.board_link.valid_host(host):
            raise ValueError("bad host")
        if password == "wrong":
            raise server.board_link.AuthRequired("root@x 认证失败")
        self.selected.append((host, password))
        self.current = host
        return "Host is down" if host == "192.0.2.99" else None

    def listing(self):
        return {"current": self.current, "boards": [], "scanned_at": None}


def board(voiced="active", up=90, extra=""):
    """What the status command prints on a board: one key=value per line."""
    return (f"svc_robotd=active\nsvc_voiced={voiced}\nsvc_visiond=active\nsvc_waked=active\n"
            f"svc_translated=active\nsvc_mediad=inactive\nup={up}\nload=2.09\ncpu=6\nmem=41\n"
            f"temp=73500\n{extra}").encode()


def done(stdout, rc=0):
    return server.subprocess.CompletedProcess([], rc, stdout=stdout)


class BoardLinkTests(unittest.TestCase):
    def test_log_stream_recovers_when_shared_ssh_master_returns(self):
        host = "192.0.2.7"
        events = []
        link = server.board_link.BoardLink(events.append, initial=host)
        first = mock.Mock(stdout=iter(()))
        second = mock.Mock(stdout=iter(()))
        first.poll.return_value = 255
        second.poll.return_value = 0

        def recover_master():
            self.assertIsNone(link.proc)

        def pump(*args):
            if link.proc is second:
                link.generation += 1

        with (mock.patch.object(link, "ensure_master", side_effect=[
                  None, server.board_link.AuthRequired("需要密码"), None]) as ensure,
              mock.patch.object(link, "_master_ready", side_effect=[False, True]) as ready,
              mock.patch.object(server.board_link.subprocess, "Popen", side_effect=[first, second]),
              mock.patch.object(server.board_link.board_bridge, "pump", side_effect=pump),
              mock.patch.object(server.board_link.time, "sleep", side_effect=lambda _: recover_master())):
            link._bridge(0, host)

        self.assertIsNone(link.proc)
        self.assertEqual(ensure.call_count, 3)
        self.assertEqual(ready.call_count, 2)
        self.assertFalse(events[0]["board_online"])
        self.assertTrue(events[1]["auth_required"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = mock.patch.object(server.Handler, "log_message")
        cls.logs.start()
        cls.server = server.SimulatorServer(("127.0.0.1", 0), link=FakeLink(done(board())))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.logs.stop()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def post(self, value, extra_headers=None):
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        return self.request("POST", "/api/events", json.dumps(value), headers)

    def test_health_does_not_claim_connected_hardware(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["hardware_verified"])
        self.assertEqual(data["transport"], "local_http_sse")

    def test_repository_metadata_is_not_served(self):
        for path in ("/.git/config", "/%2egit/HEAD", "/.env", "/data/../.git/HEAD"):
            self.assertEqual(self.request("GET", path)[0], 403, path)

    def test_catalog_contains_only_current_expression_assets(self):
        status, _, body = self.request("GET", "/data/catalog.json")
        self.assertEqual(status, 200)
        catalog = json.loads(body)
        self.assertEqual([len(catalog[k]) for k in ("actions", "expressions", "states")], [21, 23, 16])
        self.assertEqual(sum(len(a["stages"]) for a in catalog["actions"]), 49)
        for face in catalog["expressions"] + catalog["states"]:
            for mode in ("full", "loop"):
                status, headers, _ = self.request("HEAD", face[mode])
                self.assertEqual(status, 200, face[mode])
                self.assertEqual(headers["Content-Type"], "video/mp4")

    def test_protocol_is_original_without_copy_or_conversion(self):
        status, _, body = self.request("GET", "/data/ServoActionList.json")
        self.assertEqual(status, 200)
        self.assertEqual(body, server.PROTOCOL.read_bytes())

    def test_validation_rejects_invalid_events(self):
        events = [
            [], {}, {"type": "unknown", "source": "board"},
            {"type": "action", "source": "board", "action_id": "A22"},
            {"type": "action", "source": "board", "action_id": []},
            {"type": "action", "source": "board", "action_id": "A01", "angle": 5},
            {"type": "expression", "source": "injected", "expression_id": "F02", "mode": "random"},
            {"type": "expression", "source": "board", "expression_id": "F02", "mode": "loop", "action_id": "A03"},
            {"type": "expression", "source": "board", "expression_id": "D01", "mode": "loop", "action_id": "A01"},
            {"type": "state", "source": "cloud", "state_id": "T00"},
            {"type": "doa", "source": "board", "angle_deg": True, "frame": "world"},
            {"type": "doa", "source": "board", "angle_deg": float("nan"), "frame": "world"},
            {"type": "doa", "source": "board", "angle_deg": 360, "frame": "world"},
            {"type": "doa", "source": "board", "angle_deg": -1, "frame": "world"},
            {"type": "doa", "source": "board", "angle_deg": 50, "frame": "robot"},
            {"type": "stop", "source": "pc", "timestamp": "2026-10-29T10:00:00"},
        ]
        before = self.server.hub.seq
        for event in events:
            with self.subTest(event=event):
                status, _, _ = self.post(event)
                self.assertEqual(status, 400)
        self.assertEqual(self.server.hub.seq, before)

    def test_board_status_and_pose_from_bridge_are_accepted(self):
        for event in ({"type": "board_status", "source": "board", "services": {"voiced": "active"},
                       "turn_phase": "聆听", "people": 1, "cpu_temp_c": 73.5, "load_1m": 2.09, "cpu_pct": 6},
                      {"type": "pose", "source": "board", "joints": {"head_yaw": 12.5, "head_roll": -3}}):
            with self.subTest(event=event):
                self.assertEqual(self.post(event)[0], 200)
        for joints in ({}, {"neck": 1}, {"head_yaw": float("nan")}, {"head_yaw": "1"}, [1]):
            with self.subTest(joints=joints):
                self.assertEqual(self.post({"type": "pose", "source": "board", "joints": joints})[0], 400)

    def test_board_control_only_starts_or_stops_voiced(self):
        link = FakeLink(done(board(voiced="inactive", up=5)))
        server.run_board_control(link, {"cmd": "stop"})
        self.assertEqual(link.calls[0], "systemctl stop voiced")
        for cmd in ({"cmd": "send_text", "text": "x"}, {"cmd": "switch_mode", "mode": "1"}, {"cmd": "stop; reboot"}):
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                server.run_board_control(link, cmd)

    def test_volume_only_attenuates_and_brightness_is_bounded(self):
        link = FakeLink(done(board(up=5, extra="vol=160\nbl=128 bl_max=255\n")))
        status = server.run_board_control(link, {"cmd": "volume", "percent": 50})  # calls[-1] is the status read
        self.assertIn("aw_dev_0_rx_volume 160;", link.calls[-2])
        self.assertIn("aw_dev_1_rx_volume 160", link.calls[-2])
        self.assertEqual((status["volume_raw"], status["volume_pct"], status["brightness_pct"]), (160, 50, 50))
        server.run_board_control(link, {"cmd": "volume", "percent": 100})
        self.assertIn("rx_volume 0;", link.calls[-2])  # 100 % is the normal level: never louder
        server.run_board_control(link, {"cmd": "brightness", "percent": 0})
        self.assertIn("* 5 / 100", link.calls[-2])  # floor so the screen never goes black
        for cmd in ("volume", "brightness"):
            for bad in (-1, 101, 50.5, "50", None, True):
                with self.subTest(cmd=cmd, percent=bad), self.assertRaises(ValueError):
                    server.run_board_control(link, {"cmd": cmd, "percent": bad})

    def test_fan_sets_pwm_duty_on_the_globbed_hwmon_node(self):
        link = FakeLink(done(board(extra="fan=191\n")))
        status = server.run_board_control(link, {"cmd": "fan", "percent": 50})
        self.assertIn("echo 127 > $f", link.calls[-2])
        self.assertIn("pwm-fan/hwmon/hwmon*/pwm1", link.calls[-2])
        self.assertEqual(status["fan_pct"], 25)  # boot level 64 reads back as 191: active-low
        server.run_board_control(link, {"cmd": "fan", "percent": 100})
        self.assertIn("echo 0 > $f", link.calls[-2])   # full speed
        server.run_board_control(link, {"cmd": "fan", "percent": 0})
        self.assertIn("echo 255 > $f", link.calls[-2])  # stopped
        with self.assertRaises(ValueError):
            server.run_board_control(link, {"cmd": "fan", "percent": 101})

    def test_servo_commands_are_refused(self):
        with self.assertRaises(ValueError):
            server.run_board_control(FakeLink(), {"cmd": "reset_pose"})

    def test_board_control_timeout_returns_error_reply(self):
        self.server.link.error = server.subprocess.TimeoutExpired("ssh", 25)
        try:
            status, _, body = self.request("POST", "/api/board/control", json.dumps({"cmd": "start"}),
                                           {"Content-Type": "application/json"})
        finally:
            self.server.link.error = None
        self.assertEqual(status, 504)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("超时", data["error"])

    def test_status_reads_each_service_and_reports_auth_and_offline(self):
        status = server.query_board_status(FakeLink(done(board(voiced="inactive", up=412), rc=3)))
        self.assertTrue(status["board_online"])
        self.assertEqual(status["services"], {"robotd": "active", "voiced": "inactive", "visiond": "active",
                                             "waked": "active", "translated": "active", "mediad": "inactive"})
        self.assertFalse(status["voice_active"])
        self.assertEqual(status["uptime_s"], 412)
        self.assertEqual((status["cpu_temp_c"], status["load_1m"], status["cpu_pct"]), (73.5, 2.09, 6))
        self.assertEqual(status["mem_pct"], 41)
        self.assertNotIn("volume_pct", status)  # a missing figure is left out, not misread
        auth = server.query_board_status(FakeLink(error=server.board_link.AuthRequired("denied")))
        self.assertTrue(auth["auth_required"])
        offline = server.query_board_status(FakeLink(error=server.board_link.BoardError("Host is down")))
        self.assertFalse(offline["board_online"])
        self.assertEqual(offline["board_error"], "Host is down")

    def test_select_switches_board_or_asks_for_a_password(self):
        post = lambda body: self.request("POST", "/api/board/select", json.dumps(body), {"Content-Type": "application/json"})
        status, _, body = post({"host": "192.0.2.8"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["board"]["current"], "192.0.2.8")
        status, _, body = post({"host": "192.0.2.9", "password": "wrong"})
        self.assertEqual(status, 401)
        self.assertTrue(json.loads(body)["auth_required"])
        self.assertEqual(self.server.link.current, "192.0.2.8")
        status, _, body = post({"host": "192.0.2.99"})
        self.assertEqual((status, json.loads(body)["board_online"], json.loads(body)["board_error"]), (200, False, "Host is down"))
        for bad in ({"host": "-oProxyCommand=x"}, {"host": "192.0.2.9", "password": ""}, {"host": "a b"}):
            with self.subTest(bad=bad):
                self.assertEqual(post(bad)[0], 400)

    def test_camera_window_opens_streams_raw_frames_and_closes(self):
        camera = self.server.link.camera
        post = lambda body: self.request("POST", "/api/camera", json.dumps(body), {"Content-Type": "application/json"})
        status, _, body = post({"cmd": "start", "scale": 2, "fps": 4})
        self.assertEqual((status, json.loads(body)["state"]), (200, "live"))
        self.assertEqual(camera.started, [(2, 4.0)])
        self.assertEqual(post({"cmd": "start"})[0], 200)
        self.assertEqual(camera.started[-1], (1, 4.0))
        self.assertEqual(self.request("GET", "/api/camera/frame?after=0")[0], 204)
        camera.frame = (3, 4, 2, bytes(12))
        status, headers, body = self.request("GET", "/api/camera/frame?after=0")
        self.assertEqual((status, headers["X-Frame-Seq"], headers["X-Frame-Width"], headers["X-Frame-Height"], len(body)),
                         (200, "3", "4", "2", 12))
        self.assertEqual(self.request("GET", "/api/camera/frame?after=3")[0], 204)
        self.assertEqual(self.request("GET", "/api/camera/frame?after=x")[0], 400)
        self.assertEqual(json.loads(post({"cmd": "stop"})[2])["state"], "off")
        for bad in ({"cmd": "start", "scale": 3}, {"cmd": "start", "fps": 61}, {"cmd": "start", "fps": 0}, {"cmd": "reboot"}, {"cmd": "start", "scale": True}):
            with self.subTest(bad=bad):
                self.assertEqual(post(bad)[0], 400)
        camera.error = server.board_link.BoardError("Host is down")
        status, _, body = post({"cmd": "start"})
        self.assertEqual((status, json.loads(body)["error"]), (502, "Host is down"))
        camera.error = server.board_link.AuthRequired("root@x 认证失败")
        self.assertEqual(post({"cmd": "start"})[0], 401)
        camera.error, camera.frame = None, None

    def test_board_expression_may_play_once(self):
        self.assertEqual(self.post({"type": "expression", "source": "board", "expression_id": "F02", "mode": "once",
                                    "emotion": "joy"})[0], 200)
        self.assertEqual(self.post({"type": "expression", "source": "board", "expression_id": "F02", "mode": "twice"})[0], 400)

    def test_local_origin_guard_and_size_bound(self):
        self.assertEqual(self.post({"type": "reset", "source": "injected"}, {"Origin": "https://outside.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/health", headers={"Host": "outside.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/events", b"{}", {"Content-Type": "text/plain"})[0], 415)
        # The server rejects an oversized declared body before reading it.
        self.assertEqual(self.request("POST", "/api/events", b"", {"Content-Type": "application/json", "Content-Length": str(server.MAX_BODY + 1)})[0], 413)

    def test_path_traversal_is_blocked(self):
        for path in ["/../AGENTS.md", "/%2E%2E/AGENTS.md", "/media/../%E5%B1%95%E7%A4%BA.html", "/media/%2e%2e/%2e%2e/AGENTS.md", "/%00"]:
            with self.subTest(path=path):
                self.assertIn(self.request("GET", path)[0], (400, 403))

    def test_media_range_supports_seeking(self):
        catalog = json.loads((server.ROOT / "data/catalog.json").read_text())
        path = catalog["expressions"][0]["full"]
        status, _, full = self.request("GET", path)
        self.assertEqual(status, 200)
        status, headers, body = self.request("GET", path, headers={"Range": "bytes=10-31"})
        self.assertEqual((status, body), (206, full[10:32]))
        self.assertEqual(headers["Content-Range"], f"bytes 10-31/{len(full)}")
        status, _, body = self.request("GET", path, headers={"Range": "bytes=-20"})
        self.assertEqual((status, body), (206, full[-20:]))
        for value in [f"bytes={len(full)}-", "bytes=20-10", "bytes=-0", "bytes=0-1,5-6"]:
            self.assertEqual(self.request("GET", path, headers={"Range": value})[0], 416)

    def test_sse_fanout_matches_posted_event_and_order(self):
        connections = [http.client.HTTPConnection("127.0.0.1", self.port, timeout=3) for _ in range(2)]
        responses = []
        try:
            for conn in connections:
                conn.request("GET", "/api/events")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.readline(), b"event: ready\n")
                response.readline()
                self.assertEqual(response.readline(), b"\n")
                responses.append(response)
            for angle in (0, 90):
                status, _, body = self.post({"type": "doa", "source": "board", "angle_deg": angle, "frame": "world"})
                self.assertEqual(status, 200)
                sent = json.loads(body)["event"]
                for response in responses:
                    self.assertEqual(response.readline().decode(), f"id: {sent['seq']}\n")
                    received = json.loads(response.readline().decode().removeprefix("data: "))
                    self.assertEqual(received, sent)
                    self.assertEqual(response.readline(), b"\n")
        finally:
            for response in responses:
                response.close()
            for conn in connections:
                conn.close()

    def test_slow_subscriber_gets_explicit_gap_instead_of_silent_loss(self):
        hub = server.EventHub(queue_size=1)
        channel = hub.subscribe()
        hub.publish({"type": "stop", "source": "injected"})
        hub.publish({"type": "reset", "source": "injected"})
        self.assertIsNone(channel.get_nowait())
        self.assertEqual(hub.health()["dropped_clients"], 1)
        self.assertEqual(hub.health()["subscribers"], 0)
        with self.assertRaises(queue.Empty):
            channel.get_nowait()


if __name__ == "__main__":
    unittest.main()
