"""共享的样例设备状态。

IPv4 / IPv6 两个监听栈共用同一个 DeviceState，因此重载计数是“两路共用”的。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

MODE_OK = "ok"
MODE_STALL = "stall"   # 接受 TCP 后卡住 TLS 握手
MODE_RESET = "reset"   # 接受后立即 RST/关闭，制造“提前失败”

VALID_MODES = (MODE_OK, MODE_STALL, MODE_RESET)

MODE_LABELS = {
    MODE_OK: "正常",
    MODE_STALL: "握手卡住",
    MODE_RESET: "快速拒绝",
}


class DeviceState:
    def __init__(self, device_name: str = "switch-demo.local", execution_delay: float = 0.15):
        self.device_name = device_name
        # 正常下发后模拟设备处理的耗时；断连模式下“执行”到断连之间的耗时
        self.execution_delay = execution_delay
        self.started_at = time.time()
        self._lock = threading.RLock()
        self.reload_count = 0
        self.stack_count = {"ipv4": 0, "ipv6": 0}
        self.settings: dict[str, Any] = {
            "ipv6_mode": MODE_OK,
            "ipv4_mode": MODE_OK,
            "disconnect_after_execute": False,
        }
        self._history: deque[dict[str, Any]] = deque(maxlen=30)
        # stall 握手期间收到对端 FIN（对端确实把连接关掉了）的观测记录
        self._stall_eofs: deque[dict[str, Any]] = deque(maxlen=30)

    def get_mode(self, stack: str) -> str:
        with self._lock:
            return self.settings.get(f"{stack}_mode", MODE_OK)

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "ipv6_mode" in patch:
                mode = patch["ipv6_mode"]
                if mode not in VALID_MODES:
                    raise ValueError(f"invalid ipv6_mode: {mode!r}")
                self.settings["ipv6_mode"] = mode
            if "ipv4_mode" in patch:
                mode = patch["ipv4_mode"]
                if mode not in VALID_MODES:
                    raise ValueError(f"invalid ipv4_mode: {mode!r}")
                self.settings["ipv4_mode"] = mode
            if "disconnect_after_execute" in patch:
                self.settings["disconnect_after_execute"] = bool(
                    patch["disconnect_after_execute"]
                )
            return dict(self.settings)

    def register_reload(self, stack: str, request_id: str, peer: str = "") -> dict[str, Any]:
        """设备真正执行一次重载时调用；两个栈共用同一计数。"""
        with self._lock:
            self.reload_count += 1
            if stack in self.stack_count:
                self.stack_count[stack] += 1
            entry = {
                "seq": self.reload_count,
                "time": time.time(),
                "stack": stack,
                "request_id": request_id,
                "peer": peer,
                "disconnect": bool(self.settings["disconnect_after_execute"]),
            }
            self._history.append(entry)
            return {
                "reload_count": self.reload_count,
                "stack_count": dict(self.stack_count),
                "entry": dict(entry),
            }

    def reset_count(self) -> None:
        with self._lock:
            self.reload_count = 0
            self.stack_count = {"ipv4": 0, "ipv6": 0}
            self._history.clear()
            self._stall_eofs.clear()

    def record_stall_eof(self, stack: str, peer: str = "") -> None:
        """设备侧在 stall 握手中观测到对端关闭（recv 得到 EOF）时调用。

        用来端到端证明：值班台对落败连接的“取消”确实让对端收到了 FIN，
        而不是只在界面上把它标记为已释放。
        """
        with self._lock:
            self._stall_eofs.append(
                {"time": time.time(), "stack": stack, "peer": peer}
            )

    def stall_eofs(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._stall_eofs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "device_name": self.device_name,
                "reload_count": self.reload_count,
                "stack_count": dict(self.stack_count),
                "settings": dict(self.settings),
                "mode_labels": dict(MODE_LABELS),
                "uptime_s": round(time.time() - self.started_at, 1),
                "history": list(reversed(self._history)),
                "stall_eofs": list(self._stall_eofs),
            }
