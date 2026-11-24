"""端到端自测：真实启动双栈 HTTPS 样例设备并跑通所有判定分支。

运行：
    python -m unittest discover -s tests -v
或：
    python tests/test_selftest.py
"""
from __future__ import annotations

import json
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


def count_established_to_port(port: int) -> tuple[int, int]:
    """统计本机与指定端口仍处于 ESTABLISHED 的 (v4 数, v6 数)。"""
    needle = f":{port:04X}"

    def read(path: str) -> int:
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            return 0
        return sum(
            1
            for line in lines
            if line.split()[3] == "01" and line.split()[1].endswith(needle)
        )

    return read("/proc/net/tcp"), read("/proc/net/tcp6")


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

    def find_event(self, result: dict, name: str, stack: str | None = None) -> dict | None:
        for e in result["timeline"]:
            if e["event"] == name and (stack is None or e.get("stack") == stack):
                return e
        return None

    def assert_no_lingering(self) -> None:
        # 竞速返回后，设备端口上不允许残留任何 ESTABLISHED 连接
        time.sleep(0.05)
        v4, v6 = count_established_to_port(self.device.port)
        self.assertEqual((v4, v6), (0, 0), f"存在残留连接 v4={v4} v6={v6}")

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
        self.assert_no_lingering()

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
        # 新取消语义：一次发起取消 + 一次落败连接实际断开
        initiated = [e for e in result["timeline"] if e["event"] == "loser_cancel_initiated"]
        disconnected = [
            e for e in result["timeline"]
            if e["event"] == "loser_disconnected" and e.get("reason") == "winner_selected"
        ]
        self.assertEqual(len(initiated), 1)
        self.assertEqual(len(disconnected), 1)
        self.assertEqual(initiated[0]["stack"], disconnected[0]["stack"])
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

    # ---------- 本次缺陷修复的专项回归 ----------

    def test_09_stall_loser_socket_actually_closed_and_timeline_marked(self) -> None:
        """IPv6 握手卡住、IPv4 胜出后：

        1. 时间线必须同时记录“发起取消”和落败连接“实际断开”两个时刻；
        2. 设备侧 stall 线程必须收到对端 FIN（证明连接真关，而非只改界面）；
        3. /proc/net 里不允许残留 ESTABLISHED；
        4. 实际断开只能发生在发起取消之后。
        """
        self.state.update_settings({"ipv6_mode": MODE_STALL})
        result = self.run_race()
        self.assertEqual(result["winner"], IPV4)

        initiated = self.find_event(result, "loser_cancel_initiated", IPV6)
        disconnected = self.find_event(result, "loser_disconnected", IPV6)
        self.assertIsNotNone(initiated, "缺少落败取消事件")
        self.assertIsNotNone(disconnected, "缺少落败连接实际断开事件")
        self.assertEqual(initiated["reason"], "winner_selected")
        self.assertEqual(disconnected["reason"], "winner_selected")
        self.assertGreaterEqual(
            disconnected["t_ms_actual"], initiated["t_ms"],
            "实际断开时刻不应早于发起取消",
        )

        # 设备侧确实观测到了 FIN（给服务端 50ms 轮询一点余量）
        deadline = time.monotonic() + 1.0
        eofs = []
        while time.monotonic() < deadline:
            eofs = self.state.stall_eofs()
            if any(e["stack"] == IPV6 for e in eofs):
                break
            time.sleep(0.03)
        self.assertTrue(
            any(e["stack"] == IPV6 for e in eofs),
            f"设备侧未观测到 IPv6 落败连接的 FIN：{eofs}",
        )
        self.assert_no_lingering()

    def test_10_loser_never_connected_does_not_connect_or_pass_cert(self) -> None:
        """落败一路若尚未开始建连，之后也不能再连设备、不能报告证书校验通过。

        IPv6 正常时几十 ms 内胜出；把 parallel_delay 拉到 2.5s，v4 在整个
        竞速周期内根本不会被调度建连。
        """
        result = self.run_race(parallel_delay=2.5)
        self.assertEqual(result["status"], "delivered", result.get("error"))
        self.assertEqual(result["winner"], IPV6)
        # v4 从未启动，自然没有 TCP 连接、没有证书结论、没有失败
        self.assertIsNone(self.find_event(result, "start_path", IPV4))
        self.assertIsNone(self.find_event(result, "tcp_connected", IPV4))
        self.assertFalse(result["attempts"]["ipv4"]["cert_passed"])
        self.assertFalse(result["attempts"]["ipv4"]["failed"])
        self.assert_no_lingering()

    def test_11_parallel_zero_loser_blocked_from_cert_report(self) -> None:
        """双路齐发时落败路即使握手刚完成，也不允许报告证书校验通过。"""
        result = self.run_race(parallel_delay=0.0)
        self.assertEqual(result["status"], "delivered")
        winner = result["winner"]
        loser = IPV4 if winner == IPV6 else IPV6
        self.assertTrue(result["attempts"][winner]["cert_passed"])
        self.assertFalse(
            result["attempts"][loser]["cert_passed"],
            "落败路不允许报告证书校验通过",
        )
        self.assert_no_lingering()

    def test_12_stall_scenario_repeated_no_fd_leak(self) -> None:
        """连续 5 次 stall 接管，每次结束后都不得有残留连接、计数恰好 +1。"""
        self.state.update_settings({"ipv6_mode": MODE_STALL})
        for i in range(5):
            result = self.run_race()
            self.assertEqual(result["status"], "delivered", f"第 {i} 次: {result.get('error')}")
            self.assertEqual(result["winner"], IPV4)
            self.assert_no_lingering()
        self.assertEqual(self.state.reload_count, 5)
        self.assertEqual(self.state.stack_count["ipv4"], 5)
        self.assertEqual(self.state.stack_count["ipv6"], 0)

    def test_13_timeline_serializable_and_events_well_formed(self) -> None:
        self.state.update_settings({"ipv6_mode": MODE_STALL})
        result = self.run_race()
        # 全部时间线事件必须可 JSON 序列化（前端直接消费）
        encoded = json.dumps(result["timeline"], ensure_ascii=False)
        self.assertIn("loser_disconnected", encoded)
        for e in result["timeline"]:
            self.assertIn("t_ms", e)
            self.assertIn("event", e)
        # winner 正常收尾事件与落败断开事件不能混用
        self.assertIsNone(self.find_event(result, "loser_disconnected", IPV4))
        self.assertIsNotNone(self.find_event(result, "connection_closed", IPV4))

    def test_14_gate1_blocks_thread_that_starts_after_winner(self) -> None:
        """门槛 1：工作线程在 winner 已选定后才被调度时，
        连 socket 都不允许创建，attempts 必须标记 before_socket 拦截。"""
        race = ReloadRace(
            port=self.device.port,
            ca_cert=self.certs["ca_cert"],
            device_name="switch-demo.local",
            parallel_delay=2.0,  # 协调线程不会主动拉起 v4
        )
        # 先让正常竞速跑完：v6 胜出，v4 从未启动
        result = race.run().to_dict()
        self.assertEqual(result["winner"], IPV6)
        self.assertNotIn(IPV4, race._conns)

        # 竞速结束后才调度 v4 工作线程：门槛 1 必须直接拦截，不产生任何连接
        race._attempt(IPV4)
        self.assertNotIn(IPV4, race._conns, "门槛 1 拦截时不得注册任何 socket")
        blocked = race._blocked[IPV4]
        self.assertEqual(blocked["stage"], "before_socket")
        # 竞速结束后全局 _canceled 已置位，门槛优先报 deadline（语义仍是禁止建连）
        self.assertIn(blocked["reason"], ("deadline", "winner_selected"))
        # 该事件发生在 run() 返回之后，直接查内部时间线
        names = [e["event"] for e in race._timeline if e.get("stack") == IPV4]
        self.assertIn("path_blocked_before_connect", names)
        self.assertNotIn("tcp_connected", names)
        self.assert_no_lingering()


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
        # API 返回里必须带新的断开观测与落败断开事件
        events = [e["event"] for e in body["result"]["timeline"]]
        self.assertIn("loser_cancel_initiated", events)
        self.assertIn("loser_disconnected", events)
        self.assertIn("stall_eofs", body["device"])

    def test_api_reset_count(self) -> None:
        r = self.client.post("/api/reset-count")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["state"]["reload_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
