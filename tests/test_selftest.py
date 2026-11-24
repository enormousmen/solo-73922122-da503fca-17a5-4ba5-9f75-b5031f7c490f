"""端到端自测：真实启动双栈 HTTPS 样例设备并跑通所有判定分支。

运行：
    python -m unittest discover -s tests -v
或：
    python tests/test_selftest.py
"""
from __future__ import annotations

import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.certutil import ensure_certs  # noqa: E402
from app.device import DualStackDevice  # noqa: E402
from app.race_client import (  # noqa: E402
    DEADLINE,
    PARALLEL_DELAY,
    IPV4,
    IPV6,
    ReloadRace,
)
from app.state import MODE_OK, MODE_RESET, MODE_STALL, DeviceState  # noqa: E402


def free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RaceScenarioTests(unittest.TestCase):
    device: DualStackDevice
    state: DeviceState
    certs: dict
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="reload-selftest-")
        cls.certs = ensure_certs("switch-demo.local", Path(cls.tmp.name))
        cls.state = DeviceState(execution_delay=0.05)
        cls.device = DualStackDevice(
            port=free_tcp_port(),
            cert_path=cls.certs["server_cert"],
            key_path=cls.certs["server_key"],
            state=cls.state,
        )
        cls.device.start()
        time.sleep(0.15)  # 等两个 serve_forever 线程就绪

    @classmethod
    def tearDownClass(cls) -> None:
        cls.device.stop()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.state.reset_count()
        self.state.update_settings(
            {
                "ipv6_mode": MODE_OK,
                "ipv4_mode": MODE_OK,
                "disconnect_after_execute": False,
            }
        )
        # 等可能残留的 stall 工作循环感知模式变化（服务端轮询间隔 50ms）
        time.sleep(0.12)

    # -- helpers -------------------------------------------------------

    def run_race(self, **kw) -> dict:
        kwargs = dict(
            port=self.device.port,
            ca_cert=self.certs["ca_cert"],
            device_name="switch-demo.local",
        )
        kwargs.update(kw)
        return ReloadRace(**kwargs).run().to_dict()

    def find_event(self, result: dict, name: str) -> dict | None:
        for e in result["timeline"]:
            if e["event"] == name:
                return e
        return None

    # -- tests ---------------------------------------------------------

    def test_01_happy_path_ipv6_wins_single_post(self) -> None:
        result = self.run_race()
        self.assertEqual(result["status"], "delivered", result.get("error"))
        self.assertEqual(result["winner"], IPV6)
        self.assertTrue(result["post_started"])
        self.assertIsNotNone(result["receipt"])
        self.assertEqual(result["receipt"]["body"]["reload_count"], 1)
        # 只有 IPv6 收到一次 POST，IPv4 从未启动
        snap = self.state.snapshot()
        self.assertEqual(snap["reload_count"], 1)
        self.assertEqual(snap["stack_count"]["ipv6"], 1)
        self.assertEqual(snap["stack_count"]["ipv4"], 0)
        self.assertIsNone(self.find_event(result, "start_path"))

    def test_02_ipv6_stall_starts_ipv4_after_250ms(self) -> None:
        self.state.update_settings({"ipv6_mode": MODE_STALL})
        t0 = time.monotonic()
        result = self.run_race()
        wall = time.monotonic() - t0

        start_v4 = self.find_event(result, "start_path")
        self.assertIsNotNone(start_v4)
        self.assertEqual(start_v4["stack"], IPV4)
        self.assertEqual(start_v4["reason"], "parallel_delay_250ms")
        # 250ms 窗口（放宽到 180~500ms 防止 CI 抖动）
        self.assertGreaterEqual(start_v4["t_ms"], 180)
        self.assertLess(start_v4["t_ms"], 500)

        self.assertEqual(result["status"], "delivered", result.get("error"))
        self.assertEqual(result["winner"], IPV4)
        self.assertLess(wall, 2.0)
        snap = self.state.snapshot()
        self.assertEqual(snap["reload_count"], 1)
        self.assertEqual(snap["stack_count"]["ipv4"], 1)
        self.assertEqual(snap["stack_count"]["ipv6"], 0)

    def test_03_ipv6_fast_reset_starts_ipv4_immediately(self) -> None:
        self.state.update_settings({"ipv6_mode": MODE_RESET})
        result = self.run_race()

        start_v4 = self.find_event(result, "start_path")
        self.assertIsNotNone(start_v4)
        self.assertEqual(start_v4["reason"], "ipv6_early_failure")
        # 提前失败后立即切 IPv4，必须明显早于 250ms 并行窗口
        self.assertLess(start_v4["t_ms"], int(PARALLEL_DELAY * 1000) - 10)
        self.assertIsNone(self.find_event(result, "parallel_window_elapsed"))

        self.assertEqual(result["status"], "delivered", result.get("error"))
        self.assertEqual(result["winner"], IPV4)
        self.assertEqual(self.state.snapshot()["reload_count"], 1)

    def test_04_disconnect_after_execute_is_unknown_no_resend(self) -> None:
        self.state.update_settings({"disconnect_after_execute": True})
        before = self.state.reload_count
        result = self.run_race()

        self.assertTrue(result["post_started"], "POST 应当已经开始发送")
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["receipt"])
        self.assertIn(result["winner"], (IPV6, IPV4))
        # 设备确实执行了一次……
        self.assertEqual(self.state.reload_count, before + 1)
        # ……且没有换路重发导致第二次执行
        time.sleep(0.3)
        self.assertEqual(self.state.reload_count, before + 1)
        total_stack = sum(self.state.stack_count.values())
        self.assertEqual(total_stack, 1)

    def test_05_both_stall_deadline_not_sent(self) -> None:
        self.state.update_settings({"ipv6_mode": MODE_STALL, "ipv4_mode": MODE_STALL})
        t0 = time.monotonic()
        result = self.run_race()
        wall = time.monotonic() - t0

        self.assertFalse(result["post_started"])
        self.assertEqual(result["winner"], None)
        self.assertEqual(result["status"], "not_sent")
        self.assertIsNone(result["receipt"])
        # 应当耗满约 3s 总时限（给调度抖动留余量）
        self.assertGreaterEqual(wall, DEADLINE - 0.25)
        self.assertLess(wall, DEADLINE + 0.8)
        self.assertEqual(self.state.reload_count, 0)

    def test_06_parallel_zero_exactly_one_post(self) -> None:
        # parallel_delay=0：两路几乎同时开跑，验证 winner 唯一且只发一次 POST
        result = self.run_race(parallel_delay=0.0)
        self.assertEqual(result["status"], "delivered", result.get("error"))
        self.assertIn(result["winner"], (IPV6, IPV4))
        winner_event = self.find_event(result, "winner_selected")
        self.assertIsNotNone(winner_event)
        released = [e for e in result["timeline"] if e["event"] == "other_path_released"]
        self.assertEqual(len(released), 1)
        snap = self.state.snapshot()
        self.assertEqual(snap["reload_count"], 1)
        self.assertEqual(sum(snap["stack_count"].values()), 1)

    def test_07_wrong_device_name_rejected_not_sent(self) -> None:
        result = self.run_race(device_name="wrong-name.example.invalid")
        self.assertEqual(result["status"], "not_sent")
        self.assertFalse(result["post_started"])
        self.assertEqual(self.state.reload_count, 0)
        # 两路都应留下证书/校验失败信息
        self.assertTrue(result["attempts"]["ipv6"]["error"])
        self.assertTrue(result["attempts"]["ipv4"]["error"])

    def test_08_settings_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.state.update_settings({"ipv6_mode": "bogus"})


class FlaskRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import flask  # noqa: F401  跳过信息更明确
        from app.web import create_app

        cls.tmp = tempfile.TemporaryDirectory(prefix="reload-web-test-")
        cls.certs = ensure_certs("switch-demo.local", Path(cls.tmp.name))
        cls.state = DeviceState(execution_delay=0.0)
        cls.device = DualStackDevice(
            port=free_tcp_port(),
            cert_path=cls.certs["server_cert"],
            key_path=cls.certs["server_key"],
            state=cls.state,
        )
        cls.device.start()
        cls.app = create_app(
            device=cls.device,
            state=cls.state,
            ca_cert=cls.certs["ca_cert"],
            device_port=cls.device.port,
        )
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.device.stop()
        cls.tmp.cleanup()

    def test_page_and_health(self) -> None:
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("交换机配置重载台", page.get_data(as_text=True))

    def test_api_reload_with_v6_stall(self) -> None:
        r = self.client.post("/api/settings", json={"ipv6_mode": "stall"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/reload", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["result"]["status"], "delivered")
        self.assertEqual(body["result"]["winner"], "ipv4")
        self.assertEqual(body["device"]["stack_count"]["ipv4"], 1)

    def test_api_reset_count(self) -> None:
        r = self.client.post("/api/reset-count")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["state"]["reload_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
