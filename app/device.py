"""内置本地双栈 HTTPS 样例交换机。

- 同一个端口（默认 8443）分别绑定 ``::1``（IPv6）与 ``127.0.0.1``（IPv4），
  两个 ThreadingHTTPServer 在独立线程中 serve。
- 两个栈共享同一个 :class:`~app.state.DeviceState`，重载计数共用。
- 支持三种每栈故障模式：
  * ``ok``    正常完成 TLS 握手并处理 POST；
  * ``stall`` accept TCP 后卡住 TLS 握手（持续吃掉/忽略 ClientHello，不回 ServerHello）；
  * ``reset`` accept 后立刻 RST/关闭，制造“提前失败”。
- 支持 ``disconnect_after_execute``：计数 +1（设备确实执行）后不回完整 HTTP 响应直接断连。
"""
from __future__ import annotations

import http.server
import json
import socket
import ssl
import struct
import threading
import time
from pathlib import Path
from typing import Any

from .state import MODE_RESET, MODE_STALL, DeviceState

STALL_MAX_HOLD_S = 15.0


class ReloadHandler(http.server.BaseHTTPRequestHandler):
    server_version = "GSBSampleSwitch/1.0"
    protocol_version = "HTTP/1.1"

    # 静默默认访问日志，事件已进入共享 history / 控制台时间线
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # -- helpers -------------------------------------------------------

    @property
    def device_state(self) -> DeviceState:
        return self.server.device_state  # type: ignore[attr-defined]

    def _write_json(self, status: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    # -- routes --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            self._write_json(200, {"ok": True, "stack": self.server.stack_name})  # type: ignore
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/reload":
            self._write_json(404, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        request_id = str(payload.get("request_id", ""))

        stack = self.server.stack_name  # type: ignore[attr-defined]
        peer = str(self.client_address)
        snap = self.device_state.register_reload(stack=stack, request_id=request_id, peer=peer)

        # 模拟设备执行重载
        delay = max(0.0, float(getattr(self.device_state, "execution_delay", 0.0)))
        if delay:
            time.sleep(delay)

        if self.device_state.settings.get("disconnect_after_execute"):
            # 设备已执行（计数已增加），但不发送任何完整回执，直接断开 TCP。
            self.close_connection = True
            conn = self.connection
            try:
                conn.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),  # l_onoff=1, l_linger=0 -> 关闭时 RST
                )
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            return

        receipt = {
            "ok": True,
            "device": self.device_state.device_name,
            "message": "running-config reloaded; interfaces renegotiated",
            "reload_count": snap["reload_count"],
            "stack_served": stack,
            "request_id": request_id,
            "executed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write_json(200, receipt)


class DeviceHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET
    stack_name = "ipv4"

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[http.server.BaseHTTPRequestHandler],
        *,
        state: DeviceState,
        stack_name: str,
        tls_context: ssl.SSLContext,
    ) -> None:
        self.device_state = state
        self.stack_name = stack_name
        self.tls_context = tls_context
        super().__init__(server_address, handler_cls)

    # -- 故障注入：在 TLS 握手之前做手脚 --------------------------------

    def _abrupt_reset(self, sock: socket.socket) -> None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _hold_handshake(self, sock: socket.socket) -> None:
        """卡住 TLS 握手：TCP 已 accept，但绝不调用 wrap_socket。

        循环收掉客户端发来的 ClientHello（及重传），直到：
        - 演示开关被关闭；
        - 对端关闭（recv 得到 EOF/错误）；
        - 达到最大保持时间，防止线程永久泄漏。

        recv 轮询间隔取较短的 50ms，使“取消卡住”的模式切换能很快被 accept 线程感知。
        """
        sock.settimeout(0.05)
        start = time.monotonic()
        try:
            while (
                self.device_state.get_mode(self.stack_name) == MODE_STALL
                and time.monotonic() - start < STALL_MAX_HOLD_S
            ):
                try:
                    chunk = sock.recv(4096)
                    if chunk == b"":
                        # 对端 FIN：证明值班台的取消操作真的把连接关掉了
                        try:
                            peer = str(sock.getpeername())
                        except OSError:
                            peer = ""
                        self.device_state.record_stall_eof(self.stack_name, peer=peer)
                        return
                except socket.timeout:
                    continue
                except OSError:
                    return
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def get_request(self):  # type: ignore[override]
        sock, addr = self.socket.accept()
        try:
            mode = self.device_state.get_mode(self.stack_name)
            if mode == MODE_RESET:
                self._abrupt_reset(sock)
                raise OSError(f"demo: {self.stack_name} connection reset before TLS")
            if mode == MODE_STALL:
                self._hold_handshake(sock)  # 内部负责 close
                raise OSError(f"demo: {self.stack_name} TLS handshake stall released")

            sock.settimeout(5.0)
            tls_sock = self.tls_context.wrap_socket(sock, server_side=True)
            return tls_sock, addr
        except BaseException:
            # wrap_socket 失败等情况下确保原始 socket 不泄漏
            try:
                sock.close()
            except OSError:
                pass
            raise


class V4DeviceServer(DeviceHTTPServer):
    address_family = socket.AF_INET


class V6DeviceServer(DeviceHTTPServer):
    address_family = socket.AF_INET6


class DualStackDevice:
    def __init__(
        self,
        *,
        port: int,
        cert_path: str | Path,
        key_path: str | Path,
        state: DeviceState | None = None,
        host_v4: str = "127.0.0.1",
        host_v6: str = "::1",
    ) -> None:
        self.requested_port = port
        self.state = state or DeviceState()
        self.host_v4 = host_v4
        self.host_v6 = host_v6

        tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        # 先绑 IPv6；端口传 0 时由内核分配，再让 IPv4 复用同一端口号
        self.v6 = V6DeviceServer(
            (host_v6, port, 0, 0),
            ReloadHandler,
            state=self.state,
            stack_name="ipv6",
            tls_context=tls_ctx,
        )
        actual_port = self.v6.server_address[1]
        self.v4 = V4DeviceServer(
            (host_v4, actual_port),
            ReloadHandler,
            state=self.state,
            stack_name="ipv4",
            tls_context=tls_ctx,
        )
        self.port = actual_port
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        t6 = threading.Thread(target=self.v6.serve_forever, name="device-v6", daemon=True)
        t4 = threading.Thread(target=self.v4.serve_forever, name="device-v4", daemon=True)
        t6.start()
        t4.start()
        self._threads = [t6, t4]

    def stop(self) -> None:
        for httpd in (self.v4, self.v6):
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass

    def base_urls(self) -> dict[str, str]:
        return {
            "ipv6": f"https://[{self.host_v6}]:{self.port}",
            "ipv4": f"https://{self.host_v4}:{self.port}",
        }


def start_background_device(
    *,
    port: int,
    cert_path: str | Path,
    key_path: str | Path,
    state: DeviceState | None = None,
) -> DualStackDevice:
    device = DualStackDevice(port=port, cert_path=cert_path, key_path=key_path, state=state)
    device.start()
    return device
