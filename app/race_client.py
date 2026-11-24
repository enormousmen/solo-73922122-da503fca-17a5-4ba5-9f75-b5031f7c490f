"""双栈竞速重载客户端。

规则（与值班台需求一一对应）：

1. 先连 IPv6（目标字面地址 ``::1``），TLS 证书校验使用同一个 ``device_name``。
2. IPv6 在 ``parallel_delay``（默认 0.25s）内没有通过证书校验，就并行启动 IPv4。
3. IPv6 在等待窗口内提前失败，则立刻启动 IPv4，不等满 250ms。
4. 两路都用同一个 ``device_name`` 做 SNI / hostname 校验，信任同一张内置 CA。
5. 最先通过证书校验的一路成为 winner，在该连接上发送且仅发送一次 POST；
   其余连接立刻关闭释放。
6. 整次操作上限 ``deadline``（默认 3.0s）。
7. 截止时 POST 尚未开始发送 -> ``not_sent``（未下发）。
8. POST 已开始发送但没拿到完整回执 -> ``unknown``（结果未知），绝不换路重发。

实现采用“每路一个工作线程 + 协调线程（调用 run 的线程）”，
winner 判定与“是否已开始发送”都在同一把锁下与截止时刻做原子协调。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IPV6 = "ipv6"
IPV4 = "ipv4"
STACKS = (IPV6, IPV4)
OTHER = {IPV6: IPV4, IPV4: IPV6}

HOSTS = {
    IPV6: os.environ.get("DEVICE_HOST_V6", "::1"),
    IPV4: os.environ.get("DEVICE_HOST_V4", "127.0.0.1"),
}

PARALLEL_DELAY = float(os.environ.get("RACE_PARALLEL_DELAY", "0.25"))
DEADLINE = float(os.environ.get("RACE_DEADLINE", "3.0"))

STATUS_DELIVERED = "delivered"
STATUS_NOT_SENT = "not_sent"
STATUS_UNKNOWN = "unknown"

STATUS_TEXT = {
    STATUS_DELIVERED: "已下发，收到设备回执",
    STATUS_NOT_SENT: "未下发",
    STATUS_UNKNOWN: "结果未知",
}


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass
class RaceResult:
    request_id: str
    status: str
    status_text: str
    device_name: str
    winner: str | None = None
    post_started: bool = False
    receipt: dict[str, Any] | None = None
    error: str | None = None
    elapsed_ms: int = 0
    parallel_delay_s: float = PARALLEL_DELAY
    deadline_s: float = DEADLINE
    timeline: list[dict[str, Any]] = field(default_factory=list)
    attempts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "status_text": self.status_text,
            "device_name": self.device_name,
            "winner": self.winner,
            "post_started": self.post_started,
            "receipt": self.receipt,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "parallel_delay_ms": int(self.parallel_delay_s * 1000),
            "deadline_ms": int(self.deadline_s * 1000),
            "timeline": self.timeline,
            "attempts": self.attempts,
        }


def build_client_context(ca_cert: str | Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True          # 按 device_name 校验
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca_cert))
    return ctx


class ReloadRace:
    """一次重载操作对应一个 ReloadRace 实例（不可复用）。"""

    def __init__(
        self,
        *,
        port: int,
        ca_cert: str | Path,
        device_name: str = "switch-demo.local",
        path: str = "/reload",
        parallel_delay: float = PARALLEL_DELAY,
        deadline: float = DEADLINE,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.port = port
        self.ca_cert = Path(ca_cert)
        self.device_name = device_name
        self.path = path
        self.parallel_delay = parallel_delay
        self.deadline = deadline
        self.request_id = request_id or f"req-{uuid.uuid4().hex[:12]}"
        self.payload = payload or {}

        self._t0 = time.monotonic()
        self._deadline_mono = self._t0 + deadline

        self._lock = threading.RLock()
        self._timeline: list[dict[str, Any]] = []
        self._socks: dict[str, socket.socket] = {}
        self._v4_started = False
        self._winner: str | None = None
        self._post_started = False
        self._canceled = False

        self._cert_passed = {s: threading.Event() for s in STACKS}
        self._failed = {s: threading.Event() for s in STACKS}
        # 任一路到达终态（证书通过 或 失败）都会 set 对应的 settled
        self._settled = {s: threading.Event() for s in STACKS}
        self._post_done = threading.Event()

        self._attempt_errors: dict[str, str] = {}
        self._cert_info: dict[str, dict[str, Any]] = {}
        self._receipt: dict[str, Any] | None = None
        self._post_error: str | None = None
        self._post_started_mono: float | None = None

        self._ctx = build_client_context(self.ca_cert)
        self._threads: list[threading.Thread] = []

    # ---------- 工具 ----------

    def _remaining(self) -> float:
        return self._deadline_mono - time.monotonic()

    def _emit(self, event: str, stack: str | None = None, **detail: Any) -> None:
        item = {
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "event": event,
        }
        if stack:
            item["stack"] = stack
        item.update(detail)
        with self._lock:
            self._timeline.append(item)

    def _register_sock(self, stack: str, sock: socket.socket) -> None:
        with self._lock:
            self._socks[stack] = sock

    def _release_stack(self, stack: str) -> bool:
        with self._lock:
            sock = self._socks.pop(stack, None)
        if sock is None:
            return False
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        return True

    def _release_all(self) -> None:
        with self._lock:
            items = list(self._socks.items())
            self._socks.clear()
        for _stack, sock in items:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ---------- 主入口 / 协调逻辑 ----------

    def run(self) -> RaceResult:
        body = json.dumps(
            {"request_id": self.request_id, "device": self.device_name, **self.payload}
        ).encode("utf-8")
        self._body = body
        self._request_bytes = (
            f"POST {self.path} HTTP/1.1\r\n"
            f"Host: {self.device_name}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii") + body

        self._emit(
            "race_start",
            detail={
                "device": self.device_name,
                "targets": {s: f"https://{HOSTS[s]}:{self.port}" for s in STACKS},
                "parallel_delay_ms": int(self.parallel_delay * 1000),
                "deadline_ms": int(self.deadline * 1000),
            },
        )

        t6 = threading.Thread(target=self._attempt, args=(IPV6,), name="race-v6", daemon=True)
        self._threads.append(t6)
        t6.start()

        # 阶段 1：先等 IPv6，最多 parallel_delay；提前失败立刻切 IPv4
        wait_s = min(self.parallel_delay, max(0.0, self._remaining()))
        self._settled[IPV6].wait(wait_s)
        if self._cert_passed[IPV6].is_set():
            self._emit("ipv6_passed_within_window")
        elif self._failed[IPV6].is_set():
            self._emit("ipv6_early_failure_detected")
            self._start_v4(reason="ipv6_early_failure")
        else:
            # 等待超时（250ms 到点）且 IPv6 仍未通过校验
            if self._remaining() > 0:
                self._emit("parallel_window_elapsed")
                self._start_v4(reason="parallel_delay_250ms")

        # 阶段 2：等 POST 完成，或截止，或两路都提前失败
        while True:
            rem = self._remaining()
            if rem <= 0:
                self._emit("deadline_reached")
                break
            if self._post_done.wait(timeout=min(0.02, rem)):
                break
            if self._both_failed_without_winner():
                self._emit("both_paths_failed_early")
                break

        result = self._finalize()

        # 给工作线程一点时间响应 socket 关闭（仅为了测试/进程整洁）
        for t in self._threads:
            t.join(timeout=0.5)
        return result

    def _both_failed_without_winner(self) -> bool:
        with self._lock:
            if self._winner is not None:
                return False
            v4_running = self._v4_started
        if not self._failed[IPV6].is_set():
            return False
        if not v4_running:
            return False
        return self._failed[IPV4].is_set()

    def _start_v4(self, reason: str) -> None:
        with self._lock:
            if self._v4_started:
                return
            self._v4_started = True
        self._emit("start_path", IPV4, reason=reason)
        t4 = threading.Thread(target=self._attempt, args=(IPV4,), name="race-v4", daemon=True)
        self._threads.append(t4)
        t4.start()

    # ---------- 单路尝试 ----------

    def _attempt(self, stack: str) -> None:
        host = HOSTS[stack]
        family = socket.AF_INET6 if stack == IPV6 else socket.AF_INET
        raw: socket.socket | None = None
        try:
            with self._lock:
                if self._canceled:
                    return

            raw = socket.socket(family, socket.SOCK_STREAM)
            raw.settimeout(max(0.02, self._remaining()))
            self._register_sock(stack, raw)

            connect_addr: tuple[Any, ...]
            if family == socket.AF_INET6:
                connect_addr = (host, self.port, 0, 0)
            else:
                connect_addr = (host, self.port)
            t_connect = time.monotonic()
            raw.connect(connect_addr)
            self._emit(
                "tcp_connected",
                stack,
                connect_ms=int((time.monotonic() - t_connect) * 1000),
            )

            # wrap_socket(do_handshake_on_connect 默认 True)：
            # 返回成功即代表 TLS 握手 + 证书校验（含 hostname）通过。
            raw.settimeout(max(0.02, self._remaining()))
            t_hs = time.monotonic()
            tls_sock = self._ctx.wrap_socket(raw, server_hostname=self.device_name)
            raw = None  # 所有权已移交 TLS socket
            hs_ms = int((time.monotonic() - t_hs) * 1000)

            with self._lock:
                if self._canceled:
                    should_close = tls_sock
                else:
                    self._socks[stack] = tls_sock
                    should_close = None
            if should_close is not None:
                should_close.close()
                return

            cert = tls_sock.getpeercert() or {}
            subject = dict(x[0] for x in cert.get("subject", ()))
            san = cert.get("subjectAltName", ())
            self._cert_info[stack] = {
                "subject_cn": subject.get("commonName"),
                "subject_alt_name": [f"{k}:{v}" for k, v in san],
                "cipher": (tls_sock.cipher() or [None])[0],
                "version": tls_sock.version(),
                "handshake_ms": hs_ms,
                "verified_against_name": self.device_name,
            }
            self._emit("cert_verified", stack, handshake_ms=hs_ms, cn=subject.get("commonName"))
            self._cert_passed[stack].set()
            self._settled[stack].set()

            if not self._claim_winner(stack):
                # 另一路已经先通过：本连接立即释放，绝不发送
                self._emit("loser_released", stack)
                try:
                    tls_sock.close()
                except OSError:
                    pass
                return

            self._send_and_receive(stack, tls_sock)

        except BaseException as exc:  # 包括 socket.timeout / ssl.SSLError / OSError
            err = f"{type(exc).__name__}: {exc}"
            with self._lock:
                winner_is_this = self._winner == stack
                post_started = self._post_started
            if winner_is_this and post_started:
                # 发送开始后的任何失败都归类为“结果未知”，且不重发
                self._post_error = self._post_error or err
                self._emit("post_connection_error", stack, error=err)
                self._post_done.set()
            else:
                self._attempt_errors[stack] = err
                self._failed[stack].set()
                self._settled[stack].set()
                self._emit("attempt_failed", stack, error=err)
        finally:
            # 若 winner 已进入/完成自己的收发，socket 由 _send_and_receive 关闭；
            # 这里的 pop-close 是幂等兜底。
            with self._lock:
                owner = self._winner == stack and self._post_started
            if not owner:
                self._release_stack(stack)

    def _claim_winner(self, stack: str) -> bool:
        with self._lock:
            if self._canceled or self._winner is not None:
                return False
            self._winner = stack
        self._emit("winner_selected", stack)
        # 最先通过 -> 立即释放另一路（无论它还在握手还是已通过待判定）。
        # 若另一路尚未创建 socket，则等它自己运行时看到 winner 后自行释放。
        if self._release_stack(OTHER[stack]):
            self._emit("other_path_released", OTHER[stack])
        return True

    # ---------- 仅 winner 执行一次 ----------

    def _send_and_receive(self, stack: str, sock: ssl.SSLSocket) -> None:
        try:
            # 与截止时刻原子协调：截止已到 / 已被取消，则不能再开始发送，
            # 最终归类为 not_sent。
            with self._lock:
                if self._canceled or self._remaining() <= 0:
                    self._post_error = "deadline before POST start"
                    self._post_done.set()
                    return
                self._post_started = True
                self._post_started_mono = time.monotonic()

            self._emit("post_started", stack, bytes=len(self._request_bytes))
            sock.settimeout(max(0.02, self._remaining()))
            sock.sendall(self._request_bytes)
            self._emit("post_sent", stack)

            # 用 http.client 解析响应；连接被掐断 / 响应不完整会抛异常或
            # IncompleteRead，统一落入“结果未知”。
            sock.settimeout(max(0.02, self._remaining()))
            response = http.client.HTTPResponse(sock, method="POST")
            response.begin()
            status = response.status
            reason = response.reason
            raw_body = response.read()  # Content-Length 不足时抛 IncompleteRead
            if self._remaining() <= 0:
                raise socket.timeout("deadline reached while reading receipt")
            try:
                receipt_json = json.loads(raw_body.decode("utf-8")) if raw_body else None
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"receipt not valid JSON: {exc}") from exc
            if receipt_json is None:
                raise ValueError("empty receipt body")

            self._receipt = {
                "http_status": status,
                "http_reason": reason,
                "body": receipt_json,
                "bytes": len(raw_body),
            }
            self._emit("receipt_received", stack, http_status=status)
        except BaseException as exc:
            self._post_error = f"{type(exc).__name__}: {exc}"
            self._emit("receipt_failed", stack, error=self._post_error)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._release_stack(stack)
            self._post_done.set()

    # ---------- 汇总 ----------

    def _finalize(self) -> RaceResult:
        with self._lock:
            if self._receipt is not None:
                status = STATUS_DELIVERED
                error = None
            elif self._post_started:
                status = STATUS_UNKNOWN
                error = self._post_error or "connection lost before complete receipt"
            else:
                status = STATUS_NOT_SENT
                error = self._post_error or "no path passed certificate verification in time"
            # 通知所有残留工作线程停止，并关闭剩余 socket
            self._canceled = True
            post_started = self._post_started
            winner = self._winner

        self._release_all()

        elapsed_ms = int((time.monotonic() - self._t0) * 1000)
        attempts: dict[str, Any] = {}
        for s in STACKS:
            attempts[s] = {
                "host": HOSTS[s],
                "error": self._attempt_errors.get(s),
                "cert": self._cert_info.get(s),
                "cert_passed": self._cert_passed[s].is_set(),
                "failed": self._failed[s].is_set(),
            }

        self._emit("finalized", detail={"status": status, "post_started": post_started})

        return RaceResult(
            request_id=self.request_id,
            status=status,
            status_text=STATUS_TEXT[status],
            device_name=self.device_name,
            winner=winner,
            post_started=post_started,
            receipt=self._receipt,
            error=error,
            elapsed_ms=elapsed_ms,
            parallel_delay_s=self.parallel_delay,
            deadline_s=self.deadline,
            timeline=list(self._timeline),
            attempts=attempts,
        )


def run_reload(
    *,
    port: int,
    ca_cert: str | Path,
    device_name: str = "switch-demo.local",
    parallel_delay: float = PARALLEL_DELAY,
    deadline: float = DEADLINE,
) -> RaceResult:
    return ReloadRace(
        port=port,
        ca_cert=ca_cert,
        device_name=device_name,
        parallel_delay=parallel_delay,
        deadline=deadline,
    ).run()
