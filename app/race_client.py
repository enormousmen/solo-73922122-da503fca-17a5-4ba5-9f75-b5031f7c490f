"""双栈竞速重载客户端。

规则（与值班台需求一一对应）：

1. 先连 IPv6（目标字面地址 ``::1``），TLS 证书校验使用同一个 ``device_name``。
2. IPv6 在 ``parallel_delay``（默认 0.25s）内没有通过证书校验，就并行启动 IPv4。
3. IPv6 在等待窗口内提前失败，则立刻启动 IPv4，不等满 250ms。
4. 两路都用同一个 ``device_name`` 做 SNI / hostname 校验，信任同一张内置 CA。
5. 最先通过证书校验的一路成为 winner，在该连接上发送且仅发送一次 POST；
   落败连接在 winner 选定的瞬间即被取消，**即使它的 TLS 握手还卡着、
   任何回执都还没到**。
6. 整次操作上限 ``deadline``（默认 3.0s），到点关闭所有残留连接。
7. 截止时 POST 尚未开始发送 -> ``not_sent``（未下发）。
8. POST 已开始发送但没拿到完整回执 -> ``unknown``（结果未知），绝不换路重发。

取消机制的关键实现细节（修复“界面显示已释放、连接实际还开着”）：

- ``ssl.SSLContext.wrap_socket`` 在握手返回前就会 ``detach()`` 原始 socket，
  协调线程手里的原始对象会变成无效 fd（``fileno() == -1``），对它调
  ``shutdown/close`` 只会得到 ``EBADF`` 并被静默吞掉。
- 因此每路登记连接时用 ``os.dup`` 复制一个**独立的取消 fd**：它与业务 fd
  指向同一个内核 socket file description，不受 ``detach`` 影响，也不会被
  fd 号复用误伤。取消时对 dup fd 直接 ``os.shutdown``，可打断阻塞中的
  connect / TLS 握手 / recv。
- “实际断开”由工作线程在真正关掉 socket 后发 ``loser_disconnected``
  时间线事件确认；协调线程发起取消时只发 ``loser_cancel_initiated``。
  两个时刻分开记录，前端据此标出落败连接真正断开的时机。
- 建连前、connect 前、TLS 握手前、握手后共四道门槛检查取消状态；
  落败路若尚未建连则直接返回，绝不允许再连设备；证书通过上报与 winner
  竞选在同一把锁内原子完成，落败路不允许报告“证书校验通过”。
"""
from __future__ import annotations

import errno
import http.client
import json
import os
import select
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

# 落败/残留连接被关闭的原因
REASON_WINNER = "winner_selected"      # 另一路已胜出
REASON_DEADLINE = "deadline"           # 3 秒总时限到点
REASON_BOTH_FAILED = "both_failed"     # 两路均提前失败，收尾清理
REASON_POST_DONE = "post_done"         # winner 自己收发结束


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


@dataclass
class _Conn:
    """一路竞速连接的运行时登记。"""

    stack: str
    cancel_fd: int
    sock: socket.socket | None
    canceled: bool = False
    close_claimed: bool = False        # 关闭权已被某个线程认领（锁内）
    closed: threading.Event = field(default_factory=threading.Event)  # 实际关闭完成
    close_reason: str | None = None
    fd_released: bool = False


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
        self._conns: dict[str, _Conn] = {}
        self._v4_started = False
        self._winner: str | None = None
        self._post_started = False
        self._canceled = False
        # 落败路在哪个阶段被拦截（未建连 / 握手后判负），用于结果汇总
        self._blocked: dict[str, dict[str, str]] = {}

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

    def _barred_reason(self, stack: str) -> str | None:
        """调用方持锁。返回该路是否已被禁止继续，以及原因。"""
        if self._canceled:
            return REASON_DEADLINE
        if self._winner is not None and self._winner != stack:
            return REASON_WINNER
        return None

    def _register_conn(self, stack: str, sock: socket.socket) -> _Conn:
        # dup 独立取消 fd：不受 SSL wrap_socket 的 detach 影响
        cancel_fd = os.dup(sock.fileno())
        conn = _Conn(stack=stack, cancel_fd=cancel_fd, sock=sock)
        with self._lock:
            self._conns[stack] = conn
        return conn

    def _cancel_conn(self, conn: _Conn, reason: str) -> None:
        """协调侧发起取消：标记 + 直接系统调用打断阻塞操作。

        真正的 socket 关闭与 ``loser_disconnected`` 事件由工作线程在
        ``_finish_conn`` 中完成，保证“显示断开”与“实际断开”一致。
        Python 没有 ``os.shutdown``，用 ``socket.socket(fileno=dup_fd)``
        包一层发系统调用；该包装对象独占 dup fd，关掉它不影响业务 fd。
        """
        with self._lock:
            if conn.canceled or conn.close_claimed or conn.closed.is_set() or conn.fd_released:
                # 已关闭/正在关闭的连接其 dup fd 号可能已被内核复用给后续连接，
                # 绝不能再对该 fd 号发 shutdown（否则会误伤 winner）。
                return
            conn.canceled = True
            conn.close_reason = reason
        self._emit("loser_cancel_initiated", conn.stack, reason=reason)
        try:
            kicker = socket.socket(fileno=conn.cancel_fd)
            try:
                kicker.shutdown(socket.SHUT_RDWR)
            finally:
                # detach 取回 dup fd，避免包装对象关闭时连带关掉它
                kicker.detach()
        except OSError:
            # ENOTCONN（connect 尚未完成）/ EBADF（已关）等场景忽略；
            # 门槛轮询与收尾兜底会处理。
            pass

    def _finish_conn(self, conn: _Conn, reason: str) -> None:
        """工作线程退出前幂等关闭连接，并发出“实际断开”事件。"""
        with self._lock:
            if conn.close_claimed:
                # 关闭权已被另一线程认领，等待它真正关完即可，杜绝同一 fd 关两次
                conn.closed.wait(timeout=0.5)
                return
            conn.close_claimed = True
            conn.close_reason = reason
            sock = conn.sock
            conn.sock = None
            fd = conn.cancel_fd
            conn.fd_released = True
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
        # winner 自己收发结束走 connection_closed；落败/截止/两路失败才是 loser_disconnected
        event = "connection_closed" if reason == REASON_POST_DONE else "loser_disconnected"
        self._emit(
            event,
            conn.stack,
            reason=reason,
            t_ms_actual=int((time.monotonic() - self._t0) * 1000),
        )
        conn.closed.set()

    def _cancel_all(self, reason: str, *, skip_winner_post: bool) -> None:
        with self._lock:
            conns = list(self._conns.values())
            winner = self._winner
            post_started = self._post_started
        for conn in conns:
            if skip_winner_post and conn.stack == winner and post_started:
                continue
            self._cancel_conn(conn, reason)

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
        early_finish = False
        while True:
            rem = self._remaining()
            if rem <= 0:
                self._emit("deadline_reached")
                break
            if self._post_done.wait(timeout=min(0.02, rem)):
                break
            if self._both_failed_without_winner():
                self._emit("both_paths_failed_early")
                early_finish = True
                break

        result = self._finalize(early_finish=early_finish)

        # 等所有工作线程响应取消并确认实际断开（仅为了时间线完整/进程整洁）
        for t in self._threads:
            t.join(timeout=0.8)
        # 极端情况下线程没退出：协调线程兜底硬关，杜绝连接泄漏
        with self._lock:
            leftover = [c for c in self._conns.values() if not c.closed.is_set()]
        for conn in leftover:
            self._finish_conn(conn, reason=REASON_DEADLINE)
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

    def _connect_with_cancel(
        self, conn: _Conn, raw: socket.socket, connect_addr: tuple[Any, ...]
    ) -> None:
        """非阻塞 connect + 轮询取消，保证 SYN-SENT 阶段也能被及时取消。"""
        raw.setblocking(False)
        try:
            raw.connect(connect_addr)
        except BlockingIOError:
            pass
        except OSError:
            raw.setblocking(True)
            raise
        while True:
            with self._lock:
                if conn.canceled or self._canceled:
                    raw.setblocking(True)
                    raise OSError(errno.ECANCELED, "connection canceled before established")
            rem = self._remaining()
            if rem <= 0:
                raw.setblocking(True)
                raise socket.timeout("deadline during connect")
            _, writable, errored = select.select([], [raw], [raw], min(0.02, rem))
            if writable or errored:
                # 无论落在可写还是异常集合，都必须查 SO_ERROR：
                # 对端 RST/拒连时错误挂在这里，不查会拖到 SSL 握手才报。
                err = raw.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err:
                    raw.setblocking(True)
                    raise OSError(err, os.strerror(err))
                if writable:
                    break
                # 仅 errored 但 SO_ERROR 暂时为 0：继续轮询由后续检查兜底
        raw.setblocking(True)

    def _attempt(self, stack: str) -> None:
        host = HOSTS[stack]
        family = socket.AF_INET6 if stack == IPV6 else socket.AF_INET
        conn: _Conn | None = None
        raw: socket.socket | None = None
        try:
            # 门槛 1：连 socket 对象都还没创建 —— 落败路永不建连
            with self._lock:
                barred = self._barred_reason(stack)
            if barred is not None:
                self._blocked[stack] = {"stage": "before_socket", "reason": barred}
                self._emit("path_blocked_before_connect", stack, reason=barred, stage="before_socket")
                return

            raw = socket.socket(family, socket.SOCK_STREAM)
            conn = self._register_conn(stack, raw)

            # 门槛 2：connect 前再查一次
            with self._lock:
                barred = self._barred_reason(stack)
            if barred is not None:
                self._blocked[stack] = {"stage": "before_connect", "reason": barred}
                self._emit("path_blocked_before_connect", stack, reason=barred)
                self._cancel_conn(conn, barred)
                return

            connect_addr: tuple[Any, ...]
            if family == socket.AF_INET6:
                connect_addr = (host, self.port, 0, 0)
            else:
                connect_addr = (host, self.port)
            t_connect = time.monotonic()
            self._connect_with_cancel(conn, raw, connect_addr)
            self._emit(
                "tcp_connected",
                stack,
                connect_ms=int((time.monotonic() - t_connect) * 1000),
            )

            # 门槛 3：TLS 握手前
            with self._lock:
                barred = self._barred_reason(stack)
            if barred is not None:
                self._blocked[stack] = {"stage": "before_tls_handshake", "reason": barred}
                self._emit("path_blocked_before_connect", stack, reason=barred)
                self._cancel_conn(conn, barred)
                return

            # wrap_socket 成功即代表 TLS 握手 + 证书校验（含 hostname）通过；
            # 阻塞期间若被取消，dup fd 上的 shutdown 会打断它。
            raw.settimeout(max(0.02, self._remaining()))
            t_hs = time.monotonic()
            tls_sock = self._ctx.wrap_socket(raw, server_hostname=self.device_name)
            raw = None  # fd 所有权已移交 TLS socket（cancel_fd 仍独立有效）
            hs_ms = int((time.monotonic() - t_hs) * 1000)

            with self._lock:
                if conn.canceled:
                    should_close = tls_sock
                else:
                    conn.sock = tls_sock
                    should_close = None
            if should_close is not None:
                self._blocked[stack] = {"stage": "after_tls_handshake", "reason": REASON_WINNER}
                try:
                    should_close.close()
                except OSError:
                    pass
                return

            cert = tls_sock.getpeercert() or {}
            subject = dict(x[0] for x in cert.get("subject", ()))
            san = cert.get("subjectAltName", ())
            cert_info = {
                "subject_cn": subject.get("commonName"),
                "subject_alt_name": [f"{k}:{v}" for k, v in san],
                "cipher": (tls_sock.cipher() or [None])[0],
                "version": tls_sock.version(),
                "handshake_ms": hs_ms,
                "verified_against_name": self.device_name,
            }

            # 门槛 4（原子）：证书通过上报与 winner 竞选在同一把锁内。
            # 落败路即使握手刚完成，也不允许报告“证书校验通过”，立即判负关闭。
            with self._lock:
                loser_reason = self._barred_reason(stack)
                if loser_reason is None:
                    self._winner = stack
                    winner_sock: ssl.SSLSocket | None = tls_sock
                    self._cert_info[stack] = cert_info
                else:
                    winner_sock = None
                    self._blocked[stack] = {
                        "stage": "after_tls_handshake",
                        "reason": loser_reason,
                    }

            if winner_sock is None:
                # 另一路已先通过：本连接立即取消，绝不发送、不上报校验通过
                self._cancel_conn(conn, loser_reason)  # type: ignore[arg-type]
                return

            self._emit(
                "cert_verified", stack, handshake_ms=hs_ms, cn=cert_info["subject_cn"]
            )
            self._cert_passed[stack].set()
            self._settled[stack].set()
            self._emit("winner_selected", stack)

            # 选定胜路即关闭落败连接（无论它还在握手还是已通过待选）
            self._cancel_other(stack)

            self._send_and_receive(stack, tls_sock, conn)

        except BaseException as exc:  # 包括 socket.timeout / ssl.SSLError / OSError
            err = f"{type(exc).__name__}: {exc}"
            with self._lock:
                canceled = conn is not None and conn.canceled
                winner_is_this = self._winner == stack
                post_started = self._post_started
            if canceled:
                # 取消主动打断造成的异常：不是设备故障，不计 failed、不发错误时间线
                return
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
            if conn is not None:
                with self._lock:
                    owner = self._winner == stack and self._post_started
                reason = REASON_POST_DONE if owner else (
                    conn.close_reason or self._blocked.get(stack, {}).get("reason")
                    or REASON_DEADLINE
                )
                self._finish_conn(conn, reason=reason)

    def _cancel_other(self, winner_stack: str) -> None:
        other = OTHER[winner_stack]
        with self._lock:
            conn = self._conns.get(other)
        if conn is not None:
            self._cancel_conn(conn, REASON_WINNER)
        # 另一路尚未创建连接：它的四道门槛会自行拦截，不会再建连

    # ---------- 仅 winner 执行一次 ----------

    def _send_and_receive(
        self, stack: str, sock: ssl.SSLSocket, conn: _Conn
    ) -> None:
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
            self._post_done.set()

    # ---------- 汇总 ----------

    def _finalize(self, *, early_finish: bool) -> RaceResult:
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
            self._canceled = True
            post_started = self._post_started
            winner = self._winner

        # 对所有残留连接发起取消（winner 已开始 POST 的连接除外，
        # 它由 _send_and_receive 自己关闭，unknown 判定也依赖它的异常路径）
        self._cancel_all(
            REASON_BOTH_FAILED if early_finish else REASON_DEADLINE,
            skip_winner_post=True,
        )

        # 等工作线程确认实际关闭，让 loser_disconnected 落在本次时间线内
        with self._lock:
            conns = list(self._conns.values())
        for conn in conns:
            conn.closed.wait(timeout=0.6)

        elapsed_ms = int((time.monotonic() - self._t0) * 1000)
        attempts: dict[str, Any] = {}
        for s in STACKS:
            blocked = self._blocked.get(s)
            conn = self._conns.get(s)
            attempts[s] = {
                "host": HOSTS[s],
                "error": self._attempt_errors.get(s),
                "cert": self._cert_info.get(s),
                "cert_passed": self._cert_passed[s].is_set(),
                "failed": self._failed[s].is_set(),
                "blocked": blocked is not None,
                "blocked_stage": blocked.get("stage") if blocked else None,
                "blocked_reason": blocked.get("reason") if blocked else None,
                "close_reason": conn.close_reason if conn else None,
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
