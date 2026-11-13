import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import board_agent  # noqa: E402
import board_camera  # noqa: E402
import board_link  # noqa: E402
import board_bridge  # noqa: E402
import server  # noqa: E402


def voiced(msg, **fields):
    return {"src": "voiced", "level": "INFO", "target": "voiced::session", "msg": msg, "fields": fields}


class BridgeTranslationTests(unittest.TestCase):
    def setUp(self):
        self.t = board_bridge.Translator("192.0.2.7")

    def faces(self, events):
        return [e.get("expression_id") or e.get("state_id") for e in events if e["type"] in ("expression", "state")]

    def test_every_event_passes_the_lab_schema(self):
        items = [voiced("voiced starting", version="0.17.9"), voiced("session started; listening"),
                 voiced("user", text="你好", doa_deg="228"), voiced("robot", text="你好呀", muted="false"),
                 voiced("first reply audio after the user finished", ms="852"), voiced("reply spoken", secs="3.4"),
                 {"src": "robot", "head": [0.1, -0.2, 0.5, 0.05]},
                 {"src": "services", "services": {"robotd": "active", "voiced": "active", "visiond": "active"}, "uptime_s": 18,
                  "temp_c": 73.4, "load_1m": 2.09},
                 {"src": "vision", "event": {"kind": "enter", "people": 1}}, {"src": "agent", "msg": "x"}]
        for item in items:
            for event in self.t.feed(item, 0.0):
                with self.subTest(event=event):
                    server.validate_event(event)

    def test_conversation_turn_drives_faces_and_returns_to_rest_after_the_reply(self):
        self.assertEqual(self.faces(self.t.feed(voiced("session started; listening"), 0)), ["D01"])
        events = self.t.feed(voiced("user", text="今天几号", doa_deg="90"), 1)
        self.assertEqual(self.faces(events), ["F04"])
        self.assertIn({"type": "caption", "source": "board", "kind": "user_final", "text": "今天几号", "is_final": True}, events)
        self.assertEqual([e["angle_deg"] for e in events if e["type"] == "doa"], [270.0])  # mirrored
        self.assertEqual(self.faces(self.t.feed(voiced("first reply audio after the user finished"), 2)), ["F02"])
        self.t.feed(voiced("reply spoken", secs="3.0"), 2.4)
        self.assertEqual(self.t.tick(5.0), [])
        self.assertEqual(self.faces(self.t.tick(5.4)), ["D01"])

    def test_emotion_faces_carry_their_paired_action_and_rest_faces_do_not(self):
        listen = [e for e in self.t.feed(voiced("session started; listening"), 0) if e["type"] == "expression"]
        think = [e for e in self.t.feed(voiced("user", text="你好"), 1) if e["type"] == "expression"]
        self.assertNotIn("action_id", listen[0])
        self.assertEqual((think[0]["expression_id"], think[0]["action_id"]), ("F04", "A04"))
        for event in listen + think:
            server.validate_event(event)

    def test_muted_reply_and_meeting_mode_stay_off_the_speaking_face(self):
        self.assertEqual(self.faces(self.t.feed(voiced("meeting mode: listening only"), 0)), ["R01"])
        events = self.t.feed(voiced("user", text="下周三上线"), 1) + self.t.feed(voiced("robot", text="好", muted="true"), 1)
        self.assertEqual(self.faces(events), [])
        self.assertEqual([e for e in events if e.get("kind") == "assistant"], [])
        self.assertEqual(self.faces(self.t.feed(voiced("meeting ended", utterances="3"), 2)), ["D01"])

    def test_barge_in_plays_once_and_an_offline_voiced_shows_blocked(self):
        self.t.feed(voiced("first reply audio after the user finished"), 0)
        cut = [e for e in self.t.feed(voiced("user talked over the reply; cutting it"), 1) if e["type"] == "expression"]
        self.assertEqual((cut[0]["expression_id"], cut[0]["mode"], cut[0]["action_id"]), ("F13", "full", "A13"))
        down = self.t.feed({"src": "services", "services": {"robotd": "active", "voiced": "inactive"}, "uptime_s": 9}, 2)
        self.assertEqual(self.faces(down), ["R06"])
        self.assertFalse(down[0]["voice_active"])

    def test_volume_mapping_only_attenuates_and_round_trips(self):
        self.assertEqual(board_bridge.volume_raw(100), 0)
        self.assertEqual(board_bridge.volume_raw(0), 720)
        self.assertEqual(board_bridge.volume_raw(50), 160)  # −20 dB at 1/8 dB a step
        for p in range(0, 101):
            self.assertEqual(board_bridge.volume_percent(board_bridge.volume_raw(p)), p)
        st = self.t.feed({"src": "services", "services": {"voiced": "active"}, "uptime_s": 1,
                          "volume_raw": 80, "brightness": 200, "brightness_max": 255}, 0)[0]
        self.assertEqual((st["volume_raw"], st["volume_pct"], st["brightness_pct"]), (80, 75, 78))
        sys_st = self.t.feed({"src": "services", "services": {}, "uptime_s": 360, "temp_c": 73.41, "load_1m": 2.094,
                              "cpu_pct": 6}, 0)[0]
        self.assertEqual((sys_st["cpu_temp_c"], sys_st["load_1m"], sys_st["uptime_s"]), (73.4, 2.09, 360))
        self.assertEqual(sys_st["cpu_pct"], 6)
        self.assertNotIn("cpu_pct", self.t.feed({"src": "services", "services": {}, "uptime_s": 1}, 0)[0])
        share = self.t.feed({"src": "services", "services": {}, "uptime_s": 1,
                             "service_cpu": {"voiced": 2.5, "mediad": 130, "visiond": "x"}}, 0)[0]
        self.assertEqual(share["service_cpu"], {"voiced": 2.5, "mediad": 100.0})
        server.validate_event(share)

    def turn(self, lines):
        events = []
        for ts, msg, fields in lines:
            item = voiced(msg, **fields)
            item["ts"] = ts
            events += self.t.feed(item, 0)
        return [e for e in events if e["type"] == "metric"]

    def test_first_word_is_the_exact_voiced_log_value_not_alsa_or_capture(self):
        metrics = self.turn([
            ("2026-11-02T12:30:46.735093Z", "user", {"text": "哈喽", "doa_deg": "54"}),
            ("2026-11-02T12:30:47.977889Z", "first reply audio after the user finished", {"ms": "1243"}),
            ("2026-11-02T12:30:47.979524Z", "reply audible after the user's last word", {"e2e_ms": "2168", "alsa_ms": "19"}),
        ])
        self.assertEqual(metrics[-1]["metrics"], {"server_first_ms": 1243, "e2e_ms": 2168})
        self.assertEqual(self.t.feed({"src": "latency", "user_ts": "2026-11-02T12:30:46.735093Z", "e2e_ms": 2351, "end_ms": 902}, 0), [])
        self.assertEqual(self.t.turn["metrics"]["e2e_ms"], 2168)
        for event in metrics:
            server.validate_event(event)

    def test_wake_tones_and_duplicate_audio_do_not_replace_first_reply(self):
        self.turn([("2026-11-02T12:30:43.920742Z", "woken", {})])
        self.assertEqual(self.turn([("2026-11-02T12:30:43.922386Z", "reply audible after the user's last word", {"e2e_ms": "670"})]), [])
        self.test_first_word_is_the_exact_voiced_log_value_not_alsa_or_capture()
        self.assertEqual(self.turn([("2026-11-02T12:30:48.050000Z", "reply audible after the user's last word", {"e2e_ms": "19"})]), [])
        self.assertEqual(self.t.turn["metrics"]["e2e_ms"], 2168)

    def test_doa_uses_cloud_start_notice_until_direction_publication(self):
        self.turn([("2026-11-02T12:30:44Z", "user started speaking", {}),
                   ("2026-11-02T12:30:45Z", "user started speaking", {})])
        events = self.turn([("2026-11-02T12:30:46.735093Z", "user", {"text": "一", "doa_deg": "54"})])
        self.assertEqual(events[0]["metrics"], {"doa_ms": 2735.093})
        server.validate_event(events[0])
        self.assertIsNone(self.t.speech_start_ts)
        # No new start: do not reuse the first utterance's notice.
        self.assertEqual(self.turn([("2026-11-02T12:30:48Z", "user", {"text": "二", "doa_deg": "35", "doa_ms": "0.42"})]), [])

    def test_doa_barge_in_notice_is_also_a_start(self):
        self.turn([("2026-11-02T12:43:09.166582Z", "user talked over the reply; cutting it", {})])
        events = self.turn([("2026-11-02T12:43:11.698246Z", "user", {"text": "一", "doa_deg": "60"})])
        self.assertEqual(events[0]["metrics"]["doa_ms"], 2531.664)

    def test_doa_missing_angle_reversed_time_and_new_session_do_not_get_a_duration(self):
        self.turn([("2026-11-02T12:30:44Z", "user started speaking", {})])
        self.assertEqual(self.turn([("2026-11-02T12:30:46Z", "user", {"text": "一"})]), [])
        self.assertEqual(self.turn([("2026-11-02T12:30:48Z", "user", {"text": "二", "doa_deg": "35"})]), [])
        self.turn([("2026-11-02T12:30:50Z", "user started speaking", {})])
        self.assertEqual(self.turn([("2026-11-02T12:30:49Z", "user", {"text": "三", "doa_deg": "35"})]), [])
        self.turn([("2026-11-02T12:30:51Z", "user started speaking", {}),
                   ("2026-11-02T12:30:52Z", "reconnecting", {})])
        self.assertEqual(self.turn([("2026-11-02T12:30:53Z", "user", {"text": "四", "doa_deg": "35"})]), [])

    def test_first_turn_after_waking_is_marked_and_intent_finds_its_own_turn(self):
        # The real 2026-11-02 02:49 sequence on .7: woken, "Hello." (intent 5.2 s, ending after the
        # next turn began), then the next turn with its own intent.
        self.turn([("2026-11-02T02:49:39.551171Z", "woken", {})])
        first = self.turn([("2026-11-02T02:49:43.054698Z", "user", {"text": "Hello."}),
                           ("2026-11-02T02:49:43.831831Z", "first reply audio after the user finished", {"ms": "777"})])
        self.assertTrue(first[-1]["woken"])
        events = self.turn([("2026-11-02T02:49:48.485049Z", "user", {"text": "Hello."}),
                            ("2026-11-02T02:49:49.020446Z", "intent L2", {"ms": "5188"}),
                            ("2026-11-02T02:49:49.114413Z", "first reply audio after the user finished", {"ms": "629"}),
                            ("2026-11-02T02:49:52.749114Z", "intent L2", {"ms": "3634"})])
        by_turn = {e["turn_id"]: e for e in events}
        self.assertEqual(by_turn["t1"]["metrics"]["intent_ms"], 5188)
        self.assertEqual(by_turn["t2"]["metrics"]["intent_ms"], 3634)
        self.assertNotIn("woken", by_turn["t2"])
        for event in events:
            server.validate_event(event)

    def test_meeting_turns_are_not_measured(self):
        self.t.feed(voiced("meeting mode: listening only"), 0)
        self.assertEqual(self.turn([("2026-10-30T07:00:00Z", "user", {"text": "x"}),
                                    ("2026-10-30T07:00:01Z", "reply spoken", {"secs": "1"})]), [])
        self.assertEqual(self.t.feed({"src": "latency", "user_ts": "2026-10-30T07:00:00Z", "e2e_ms": 900, "end_ms": 600}, 0), [])
        self.assertEqual(board_bridge.board_time("2026-10-30T07:00:00.5Z") % 60, 0.5)
        self.assertIsNone(board_bridge.board_time("garbage"))

    def test_head_pose_is_clamped_to_lab_limits(self):
        self.assertEqual(board_bridge.head_pose([0.0, 0.0, 3.0, -1.0]),
                         {"head_yaw": 60, "head_pitch_down": 0.0, "head_roll": -10})
        self.assertEqual(board_bridge.head_pose([1, 2]), {})


class BoardLinkTests(unittest.TestCase):
    def setUp(self):
        self.link = board_link.BoardLink(lambda e: None, initial="192.0.2.7", config=Path("/nonexistent/x.json"))

    def test_probe_classifies_robot_password_and_other_hosts(self):
        cases = [
            (done(0, b"LB3576\nRockchip RK3576 NEARDI LZ201 Board\nROBOTD\n"), "robot"),
            (done(255, b"", b"root@x: Permission denied (publickey,password).\n"), "auth"),
            (done(0, b"nas\nsome board\n"), None),
            (done(255, b"", b"ssh: connect to host x port 22: Host is down\n"), None),
        ]
        for result, state in cases:
            with self.subTest(state=state), unittest.mock.patch.object(board_link.subprocess, "run", return_value=result):
                found = self.link.probe("192.0.2.9")
                self.assertEqual(found and found["state"], state)
        with unittest.mock.patch.object(board_link.subprocess, "run", return_value=cases[0][0]):
            self.assertEqual(self.link.probe("192.0.2.9")["name"], "LB3576")

    def test_password_goes_through_askpass_env_never_argv(self):
        self.link.passwords["192.0.2.7"] = "s3cret"
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw.get("env")))
            return done(1 if "-O" in cmd else 0)

        with unittest.mock.patch.object(board_link.subprocess, "run", side_effect=fake_run):
            self.link.ensure_master("192.0.2.7")
        cmd, env = calls[-1]
        self.assertNotIn("s3cret", " ".join(cmd))
        self.assertEqual(env["ROBOTLAB_SSH_PASSWORD"], "s3cret")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertNotIn("BatchMode=yes", cmd)

    def test_refused_auth_drops_the_stored_password(self):
        self.link.passwords["192.0.2.7"] = "wrong"

        def fake_run(cmd, **kw):
            if "-O" in cmd:
                return done(1)
            kw["stderr"].write(b"root@192.0.2.7: Permission denied (publickey,password).\n")
            return done(255)

        with unittest.mock.patch.object(board_link.subprocess, "run", side_effect=fake_run), \
                self.assertRaises(board_link.AuthRequired):
            self.link.ensure_master("192.0.2.7")
        self.assertNotIn("192.0.2.7", self.link.passwords)

    def test_hosts_that_could_become_ssh_options_are_rejected(self):
        for host in ("-oProxyCommand=sh", "a b", "x;reboot", "", "1.2.3.4\n"):
            self.assertFalse(board_link.valid_host(host), host)
        self.assertTrue(board_link.valid_host("192.168.0.8"))


def src(name, msg, level="INFO", **fields):
    return {"src": name, "level": level, "target": f"{name}::main", "msg": msg, "fields": fields}


class NewStackTests(unittest.TestCase):
    """waked / translated / mediad, and the DOA mirror (code/robot @ eab4481)."""

    def setUp(self):
        self.t = board_bridge.Translator("192.0.2.7")

    def faces(self, events):
        return [e.get("expression_id") or e.get("state_id") for e in events if e["type"] in ("expression", "state")]

    def phases(self, events):
        return [e["turn_phase"] for e in events if e["type"] == "board_status" and "turn_phase" in e]

    def test_doa_is_mirrored_into_the_labs_left_positive_frame(self):
        for board, lab in ((0, 0.0), (90, 270.0), (228, 132.0), (359.96, 0.0), (180, 180.0)):
            with self.subTest(board=board):
                self.assertEqual(board_bridge.doa_world(board), lab)
                server.validate_event({"type": "doa", "source": "board", "angle_deg": board_bridge.doa_world(board),
                                       "frame": "world"})

    def test_sleep_and_wake_follow_voiced(self):
        self.t.feed(voiced("session started; listening"), 0)
        quiet = self.t.feed(voiced("quiet: waiting for the wake word"), 1)
        self.assertEqual((self.faces(quiet), self.phases(quiet)), (["D01"], ["待唤醒"]))
        self.assertEqual(self.faces(self.t.feed(voiced("session started; listening"), 2)), [])  # still asleep
        self.assertEqual(self.t.phase, "dormant")
        woke = [e for e in self.t.feed(voiced("woken"), 3) if e["type"] == "expression"]
        self.assertEqual((woke[0]["expression_id"], woke[0]["mode"]), ("D02", "once"))
        self.assertEqual(self.t.phase, "listening")
        self.t.feed(voiced("quiet: waiting for the wake word"), 4)
        self.assertEqual(self.phases(self.t.feed(voiced("waked went away: staying awake", level="WARN"), 5)), ["聆听"])

    def test_wake_word_is_logged_and_the_mic_level_is_not(self):
        self.assertEqual(self.t.feed(src("waked", "mic level, last 30 s", loudest_rms="900", gain="1.0"), 0), [])
        hit = self.t.feed(src("waked", "wake word", rms="3100", gain="1.2"), 1)
        self.assertEqual([e["type"] for e in hit], ["board_log"])
        self.assertIn("Hey Buddy", hit[0]["message"])

    def test_translation_mode_shows_t_states_and_both_subtitles(self):
        self.t.feed(voiced("session started; listening"), 0)
        self.assertEqual(self.faces(self.t.feed(voiced("translated takes over"), 1)), ["R07"])
        self.t.feed(src("translated", "translation mode"), 1)
        self.t.feed(src("translated", "connected to simultaneous interpretation", logid="x", **{"from": "zh", "to": "en"}), 2)
        self.assertEqual(self.faces(self.t.feed(src("translated", "translating", ms="420"), 2)), ["T01"])
        heard = self.t.feed(src("translated", "subtitle", event="652", text="今天天气很好"), 3)
        self.assertEqual(self.faces(heard), ["T02"])
        self.assertIn({"type": "caption", "source": "board", "kind": "user_final", "text": "今天天气很好", "is_final": True,
                       "detail": "translation_source"}, heard)
        said = self.t.feed(src("translated", "subtitle", event="655", text="The weather is nice today"), 4)
        self.assertEqual(self.faces(said), ["T03"])
        self.assertEqual([e["kind"] for e in said if e["type"] == "caption"], ["assistant"])
        self.assertEqual(self.t.tick(5.0), [])
        self.assertEqual(self.faces(self.t.tick(4 + 6.3)), ["T01"])
        back = self.t.feed(voiced("conversation resumes", ended="Said"), 20)
        self.assertEqual((self.faces(back), self.phases(back)), (["D01"], ["聆听"]))
        for event in heard + said + back:
            server.validate_event(event)

    def test_translation_into_chinese_uses_the_english_side(self):
        self.t.feed(voiced("translated takes over"), 0)
        self.t.feed(src("translated", "connected to simultaneous interpretation", **{"from": "ja", "to": "zh"}), 1)
        self.assertEqual(self.faces(self.t.feed(src("translated", "translating"), 1)), ["T05"])
        self.assertEqual(self.faces(self.t.feed(src("translated", "subtitle", event="652", text="こんにちは"), 2)), ["T06"])

    def test_mediad_expression_plays_once_and_the_turn_stops_driving_the_face(self):
        self.t.feed({"src": "services", "services": {"voiced": "active", "mediad": "active"}, "uptime_s": 90}, 0)
        self.assertEqual(self.faces(self.t.feed(voiced("session started; listening"), 0)), [])
        self.assertEqual(self.faces(self.t.feed(voiced("user", text="我今天好开心"), 1)), [])  # no F04 on the board
        face = [e for e in self.t.feed(src("mediad", "expression, from a word", expression="joy"), 1.1)
                if e["type"] == "expression"]
        self.assertEqual(face, [{"type": "expression", "source": "board", "expression_id": "F02", "mode": "once",
                                 "action_id": "A02", "emotion": "joy"}])
        server.validate_event(face[0])
        self.assertEqual(self.faces(self.t.feed(voiced("first reply audio after the user finished"), 2)), [])
        # A state screen the Lab put up is still cleared back to idle.
        self.assertEqual(self.faces(self.t.feed(voiced("meeting mode: listening only"), 3)), ["R01"])
        self.assertEqual(self.faces(self.t.feed(voiced("meeting ended"), 4)), ["D01"])

    def test_every_mediad_expression_maps_into_the_catalog(self):
        catalog = json.loads((Path(__file__).resolve().parents[1] / "data" / "catalog.json").read_text())
        ids = {e["id"]: e["name"] for e in catalog["expressions"]}
        self.assertEqual(len(board_bridge.EMOTION_FACES), 18)
        for emotion, face in board_bridge.EMOTION_FACES.items():
            with self.subTest(emotion=emotion):
                self.assertIn(face, ids)
                self.assertIn("A" + face[1:], {a["id"] for a in catalog["actions"]})
        unknown = self.t.feed(src("mediad", "expression, from the model", expression="default", ms="2600"), 0)
        self.assertEqual(self.faces(unknown), [])

    def test_without_libemotion_the_turn_keeps_driving_the_face(self):
        self.t.feed({"src": "services", "services": {"voiced": "active", "mediad": "active"}, "uptime_s": 90}, 0)
        self.t.feed(src("mediad", "no /usr/lib/libemotion.so: no expressions"), 0)
        self.assertEqual(self.faces(self.t.feed(voiced("user", text="你好"), 1)), ["F04"])


class CpuTests(unittest.TestCase):
    def test_cpu_percent_counts_iowait_as_idle_over_the_interval(self):
        stat = io.StringIO("cpu  100 0 50 800 20 0 0 0 7 7\ncpu0 1 2 3 4 5 6 7 8 9 10\n")
        with unittest.mock.patch("builtins.open", return_value=stat):
            self.assertEqual(board_agent.cpu_times(), (150, 970))  # guest (7, 7) is already in user/nice
        self.assertEqual(board_agent.cpu_percent((150, 970), (175, 1070)), 25)
        self.assertIsNone(board_agent.cpu_percent((150, 970), (150, 970)))


class LatencyProbeTests(unittest.TestCase):
    BASE = 1_793_584_000.0

    def ts(self, t):
        import datetime as dt
        return dt.datetime.fromtimestamp(self.BASE + t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def test_end_window_and_timestamps(self):
        self.assertEqual(board_agent.end_window_s("# end_smooth_window_ms = 800\n"), 0.6)
        self.assertEqual(board_agent.end_window_s("[doubao]\nend_smooth_window_ms = 800  # slower\n"), 0.8)
        self.assertAlmostEqual(board_agent.tracing_epoch(self.ts(1.25)), self.BASE + 1.25, places=5)
        self.assertIsNone(board_agent.tracing_epoch("garbage"))

    def test_last_word_ignores_room_sound_in_the_wait(self):
        # 10 ms blocks: room 400, speech 3000 until 3.00 s, room again with a 20 ms bump at 3.40 s
        # (a click in the service's silent window), the `user` line at 3.68 s (600 ms window + 80 ms).
        levels = []
        for i in range(500):
            t = (i + 1) / 100
            r = 3000 if 1.5 < t <= 3.0 else 400
            if 3.40 < t <= 3.42:
                r = 2500
            levels.append((t, r))
        word, why = board_agent.last_word(levels, 3.68, 0.6)
        self.assertIsNone(why)
        self.assertAlmostEqual(word, 3.0, places=6)
        # A quiet speaker still counts: the level is set by the utterance itself.
        soft = [(t, 1100 if 1.5 < t <= 3.0 else 400) for t, _ in levels]
        self.assertAlmostEqual(board_agent.last_word(soft, 3.68, 0.6)[0], 3.0, places=6)
        flat = [(t, 400) for t, _ in levels]
        self.assertEqual(board_agent.last_word(flat, 3.68, 0.6), (None, "人声没有高出环境声"))

    def test_capture_measures_a_turn_end_to_end(self):
        """Synthetic voice_in: 6 channels, speech on the mics until 3.000 s, the `user` line at
        3.680 s, and the reply on the sense channel from 4.3825 s."""
        import array
        import random
        rnd = random.Random(7)
        rate, block = board_agent.RATE, board_agent.BLOCK
        total = int(6.0 * rate)
        reply_at = int(4.3825 * rate)
        clock = {"t": self.BASE}

        def frame(n):
            t = n / rate
            amp = 4000 if 1.5 <= t < 3.0 else 500
            mic = [int(rnd.gauss(0, amp)) for _ in range(4)]
            sense = 3000 if n >= reply_at else 0
            return mic + [0, max(-32768, min(32767, sense))]

        chunks = []
        for start in range(0, total, block):
            samples = array.array("h")
            for n in range(start, start + block):
                samples.extend(frame(n))
            chunks.append(samples.tobytes())

        class Stdout:
            def read(self, size):
                out = b""
                while chunks and len(out) + len(chunks[0]) <= size:
                    out += chunks.pop(0)
                    clock["t"] += block / rate
                return out

        class Proc:
            stdout, stderr = Stdout(), io.BytesIO(b"")

            def kill(self):
                pass

        probe, out = board_agent.LatencyProbe(), []
        probe.turn(self.ts(3.68))

        class Stop(Exception):
            pass

        def stop(_):
            raise Stop

        with unittest.mock.patch.object(board_agent.subprocess, "Popen", return_value=Proc()), \
                unittest.mock.patch.object(board_agent.time, "time", side_effect=lambda: clock["t"] + 0.003), \
                unittest.mock.patch.object(board_agent.time, "sleep", side_effect=stop), \
                unittest.mock.patch.object(board_agent, "emit", side_effect=out.append), \
                unittest.mock.patch("builtins.open", side_effect=OSError):
            with self.assertRaises(Stop):
                probe.run()
        result = [o for o in out if o.get("src") == "latency"]
        self.assertEqual(len(result), 1, out)
        self.assertEqual(result[0]["user_ts"], self.ts(3.68))
        self.assertLessEqual(abs(result[0]["e2e_ms"] - 1382), 10, result[0])
        self.assertLessEqual(abs(result[0]["end_ms"] - 680), 10, result[0])


class BoardLoadTests(unittest.TestCase):
    def test_npu_load_is_the_mean_of_the_cores(self):
        self.assertEqual(board_agent.npu_percent("NPU load:  Core0:  1%, Core1:  0%, \n"), 0)
        self.assertEqual(board_agent.npu_percent("NPU load:  Core0: 40%, Core1: 20%, "), 30)
        self.assertIsNone(board_agent.npu_percent("nothing"))

    def test_the_bridge_passes_npu_and_the_cpu_ceiling(self):
        t = board_bridge.Translator("192.0.2.7")
        st = t.feed({"src": "services", "services": {}, "uptime_s": 1, "npu_pct": 7, "cpu_mhz": 408, "cpu_mhz_full": 2208}, 0)[0]
        self.assertEqual((st["npu_pct"], st["cpu_mhz"], st["cpu_mhz_full"]), (7, 408, 2208))
        server.validate_event(st)


class FaceEventTests(unittest.TestCase):
    def setUp(self):
        self.t = board_bridge.Translator("192.0.2.7")

    def test_faces_go_to_the_camera_window_and_not_the_log(self):
        face = {"id": 64, "bearing_deg": 27.6, "talking": True, "bbox": [0.25, 0.25, 0.1, 0.1],
                "landmarks": [[0.25, 0.3]] * 98}
        events = self.t.feed({"src": "vision", "event": {"at_unix_ms": 1, "kind": "faces", "faces": [face]}}, 0)
        self.assertEqual([e["type"] for e in events], ["faces"])
        self.assertEqual(events[0]["faces"], [{"id": 64, "talking": True, "bbox": face["bbox"], "landmarks": face["landmarks"]}])
        server.validate_event(events[0])

    def test_a_visiond_without_the_fields_and_an_empty_view_still_pass(self):
        old = self.t.feed({"src": "vision", "event": {"kind": "faces", "faces": [{"id": 1, "bearing_deg": 0, "talking": False}]}}, 0)
        self.assertEqual(old[0]["faces"], [{"id": 1, "talking": False}])
        server.validate_event(old[0])
        empty = self.t.feed({"src": "vision", "event": {"kind": "faces", "faces": []}}, 0)
        self.assertEqual(empty[0]["faces"], [])
        server.validate_event(empty[0])

    def test_bad_faces_are_refused(self):
        for faces in ("x", [{"bbox": [1, 2]}], [{"landmarks": [[1]]}], [{"landmarks": [[0, 0]] * 99}],
                      [{"secret": 1}], [{"bbox": [float("nan"), 0, 0, 0]}]):
            with self.subTest(faces=faces), self.assertRaises(ValueError):
                server.validate_event({"type": "faces", "source": "board", "faces": faces})


class MemoryTests(unittest.TestCase):
    def test_cache_counts_as_free_and_a_kernel_without_the_figure_reports_nothing(self):
        text = "MemTotal:       8000000 kB\nMemFree:        1000000 kB\nMemAvailable:   6000000 kB\n"
        self.assertEqual(board_agent.memory_percent(text), 25)
        self.assertIsNone(board_agent.memory_percent("MemTotal: 8000000 kB\n"))
        status = board_bridge.Translator("192.0.2.7").feed({"src": "services", "services": {}, "uptime_s": 1, "mem_pct": 25}, 0)[0]
        self.assertEqual(status["mem_pct"], 25)
        server.validate_event(status)


class ServiceCpuTests(unittest.TestCase):
    def test_a_share_is_of_all_cores_and_a_restarted_service_has_none(self):
        before = {"voiced": 1_000_000, "visiond": 9_000_000}
        after = {"voiced": 5_000_000, "visiond": 1_000, "mediad": 7}
        self.assertEqual(board_agent.service_percent(before, after, 5.0, 8), {"voiced": 10.0})
        self.assertEqual(board_agent.service_percent(before, after, 0, 8), {})


    def test_each_status_report_carries_the_shares(self):
        lines = []
        stop = [None, InterruptedError()]  # the first sleep is the opening one; the second ends the loop
        with unittest.mock.patch.object(board_agent, "emit", lines.append), \
                unittest.mock.patch.object(board_agent, "service_usage", return_value={"voiced": 0}), \
                unittest.mock.patch.object(board_agent, "cpu_times", return_value=(0, 0)), \
                unittest.mock.patch.object(board_agent, "levels", return_value={}), \
                unittest.mock.patch.object(board_agent, "system", return_value={}), \
                unittest.mock.patch.object(board_agent.subprocess, "run",
                                           return_value=unittest.mock.Mock(stdout="active\n")), \
                unittest.mock.patch.object(board_agent, "open", unittest.mock.mock_open(read_data="1.0 2.0"), create=True), \
                unittest.mock.patch.object(board_agent.time, "sleep", side_effect=stop):
            with self.assertRaises(InterruptedError):
                board_agent.poll_status()
        self.assertEqual(lines[0]["service_cpu"], {"voiced": 0.0})


class CameraTests(unittest.TestCase):
    def launch(self, argv):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return unittest.mock.Mock(stdout="active\n", returncode=0)

        with unittest.mock.patch.object(board_camera.subprocess, "run", side_effect=run), \
                unittest.mock.patch.object(board_camera.threading, "Thread"), \
                unittest.mock.patch.object(board_camera, "capture") as capture, \
                unittest.mock.patch.object(board_camera, "note"), \
                unittest.mock.patch.object(board_camera.signal, "signal"), \
                unittest.mock.patch.object(sys, "argv", ["board_camera.py"] + argv):
            self.assertEqual(board_camera.main(), 0)
        return calls, capture

    def test_by_default_it_reads_the_self_path_and_leaves_visiond_alone(self):
        calls, capture = self.launch([])
        self.assertEqual(capture.call_args.args[:2], (1, 4.0))
        self.assertEqual(capture.call_args.args[3], "/dev/video12")
        self.assertNotIn(["systemctl", "stop", "visiond"], calls)
        self.assertNotIn(["systemctl", "start", "--no-block", "visiond"], calls)

    def test_the_main_path_pauses_visiond_and_gives_it_back(self):
        calls, capture = self.launch(["--device", "/dev/video11"])
        self.assertEqual(capture.call_args.args[3], "/dev/video11")
        self.assertIn(["systemctl", "stop", "visiond"], calls)
        self.assertIn(["systemctl", "start", "--no-block", "visiond"], calls)

    def test_command_carries_the_script_and_leaves_stdin_for_the_heartbeat(self):
        import base64
        cmd = board_link.camera_command(2, 4.0)
        self.assertTrue(cmd.startswith("exec python3 -u -c "))
        self.assertTrue(cmd.endswith("--scale 2 --fps 4"))
        code = cmd.split("b64decode('")[1].split("')")[0]
        self.assertEqual(base64.b64decode(code), board_link.CAMERA_SCRIPT.read_bytes())

    def test_feed_parses_records_keeps_the_newest_frame_and_reports_notes(self):
        import io
        import struct
        import zlib
        published = []
        link = unittest.mock.Mock(publish=published.append)
        feed = board_link.CameraFeed(link)
        frame = bytes(range(256)) * (4 * 4 * 3 // 2 // 256 + 1)
        frame = frame[:4 * 4 * 3 // 2]
        rec = lambda kind, payload, w=0, h=0: kind + struct.pack("<HHI", w, h, len(payload)) + payload  # noqa: E731
        stream = (rec(b"NOTE", "已打开摄像头".encode()) + rec(b"NV12", zlib.compress(frame), 4, 4)
                  + rec(b"NV12", zlib.compress(b"short"), 4, 4))
        proc = unittest.mock.Mock(stdout=io.BytesIO(stream))
        proc.wait.return_value = 0
        feed.generation = 1
        feed.state = "starting"
        feed._read(1, proc)
        self.assertEqual(feed.seq, 1)
        self.assertEqual(published[0]["logger"], "camera")
        server.validate_event(published[0])
        self.assertEqual(feed.state, "error")  # the stream ended without being stopped
        self.assertIsNone(feed.wait_frame(0, timeout=0.01))

    def test_stop_hands_the_camera_back_by_closing_stdin(self):
        feed = board_link.CameraFeed(unittest.mock.Mock())
        proc = unittest.mock.Mock()
        proc.poll.return_value = None
        feed.proc, feed.state = proc, "live"
        with unittest.mock.patch.object(board_link.threading, "Thread") as thread:
            status = feed.stop()
        self.assertEqual(status["state"], "off")
        thread.assert_called_once()
        board_link.CameraFeed._let_go(proc)
        proc.stdin.close.assert_called_once()


def done(rc, stdout=b"", stderr=b""):
    return board_link.subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


class AgentParseTests(unittest.TestCase):
    def test_tracing_line_with_ansi_and_spaced_display_values(self):
        line = ("\x1b[2m2026-10-30T07:22:14.048382Z\x1b[0m \x1b[32m INFO\x1b[0m \x1b[2mvoiced::session\x1b[0m\x1b[2m:\x1b[0m "
                "robot \x1b[3mtext\x1b[0m\x1b[2m=\x1b[0m好的 我们 开始 \x1b[3mmuted\x1b[0m\x1b[2m=\x1b[0mfalse")
        parsed = board_agent.parse_tracing(line)
        self.assertEqual((parsed["level"], parsed["msg"]), ("INFO", "robot"))
        self.assertEqual(parsed["fields"], {"text": "好的 我们 开始", "muted": "false"})
        quoted = board_agent.parse_tracing('2026-10-30T07:22:14Z  INFO voiced::session: reply spoken secs="3.4" status=null')
        self.assertEqual(quoted["fields"], {"secs": "3.4", "status": "null"})
        self.assertIsNone(board_agent.parse_tracing("Started Robot realtime voice."))

    def test_a_failing_channel_is_reported_once_and_its_recovery_once(self):
        lines = []
        with unittest.mock.patch.object(board_agent, "emit", lines.append), \
                unittest.mock.patch.dict(board_agent._problems, clear=True):
            for message in (None, "gone", "gone", "gone", None, None):
                board_agent.problem("robotd rpc", message)
        self.assertEqual([line["msg"] for line in lines], ["robotd rpc: gone", "robotd rpc: recovered"])

    def test_journal_json_lines_name_their_unit_and_decode_byte_arrays(self):
        ansi = list("\x1b[32m INFO\x1b[0m".encode())
        self.assertEqual(board_agent.journal_entry(json.dumps({"_SYSTEMD_UNIT": "waked.service", "MESSAGE": ansi})),
                         ("waked", "\x1b[32m INFO\x1b[0m"))
        self.assertEqual(board_agent.journal_entry(json.dumps({"_SYSTEMD_UNIT": "mediad.service", "MESSAGE": "x"})),
                         ("mediad", "x"))
        self.assertIsNone(board_agent.journal_entry(json.dumps({"_SYSTEMD_UNIT": "sshd.service", "MESSAGE": "x"})))
        self.assertIsNone(board_agent.journal_entry("not json"))
        self.assertEqual(set(board_agent.JOURNALS) <= set(board_agent.SERVICES), True)

    def test_agent_output_is_json_lines(self):
        self.assertEqual(json.loads(json.dumps({"src": "agent", "msg": "中文"}, ensure_ascii=False))["msg"], "中文")


if __name__ == "__main__":
    unittest.main()
