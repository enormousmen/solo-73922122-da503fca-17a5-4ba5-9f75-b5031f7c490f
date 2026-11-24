/* 交换机重载台前端逻辑：原生 JS，无外部依赖 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var EVENT_LABELS = {
    race_start: "竞速开始",
    start_path: "启动连接",
    tcp_connected: "TCP 已连接",
    cert_verified: "证书校验通过",
    ipv6_passed_within_window: "IPv6 在 250ms 窗口内通过",
    ipv6_early_failure_detected: "检测到 IPv6 提前失败",
    parallel_window_elapsed: "250ms 窗口到点，IPv6 尚未通过",
    winner_selected: "成为胜出路径，发送唯一 POST",
    loser_cancel_initiated: "已对落败连接下发关闭指令（取消）",
    loser_disconnected: "落败连接实际断开（对端已关闭）",
    connection_closed: "胜出连接正常关闭",
    path_blocked_before_connect: "落败路在建立前被拦截，未连接设备",
    post_started: "开始发送 POST",
    post_sent: "POST 字节已全部发出",
    receipt_received: "收到完整设备回执",
    receipt_failed: "回执缺失/连接中断 → 结果未知",
    attempt_failed: "该路径尝试失败",
    post_connection_error: "发送后连接错误",
    deadline_reached: "到达 3 秒总时限",
    both_paths_failed_early: "两路均提前失败",
    finalized: "本次操作结束"
  };

  var MODE_EXPLAIN = {
    ipv6: {
      ok: "正常情况下本机回环 TLS 握手只需几十毫秒，IPv6 会在 250ms 内通过并直接胜出，不启动 IPv4。",
      stall: "演示：IPv6 的 TCP 能连上，但 TLS 握手一直卡住；250ms 后值班台并行尝试 IPv4，由 IPv4 完成下发。",
      reset: "演示：IPv6 被设备快速拒绝（RST），值班台检测到提前失败后立即尝试 IPv4，不等 250ms。"
    },
    ipv4: {
      ok: "IPv4 作为并行备份路径，正常时可随时接管。",
      stall: "演示：IPv4 握手也卡住。若与 IPv6 故障叠加，3 秒内无法开始 POST，结果为“未下发”。",
      reset: "演示：IPv4 快速失败。若 IPv6 也未在时限内通过，则判定“未下发”。"
    }
  };

  function api(url, options) {
    return fetch(url, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, options || {})).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) { throw new Error(data.error || ("HTTP " + r.status)); }
        return data;
      });
    });
  }

  function stackPill(stack) {
    if (!stack) return "";
    return '<span class="pill pill-' + stack + '">' + stack.toUpperCase() + "</span>";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---------------- 设备状态 / 计数 / 历史 ---------------- */

  var controlsHydrated = false;
  function hydrateControlsOnce(state) {
    if (controlsHydrated || !state.settings) return;
    ["ipv6", "ipv4"].forEach(function (st) {
      var mode = state.settings[st + "_mode"];
      if (mode) {
        var radio = document.querySelector('input[name="' + st + '_mode"][value="' + mode + '"]');
        if (radio) radio.checked = true;
      }
    });
    $("disconnect").checked = !!state.settings.disconnect_after_execute;
    syncExplain();
    controlsHydrated = true;
  }

  function renderState(state) {
    hydrateControlsOnce(state);
    $("count-total").textContent = state.reload_count;
    $("count-v6").textContent = state.stack_count.ipv6;
    $("count-v4").textContent = state.stack_count.ipv4;
    $("uptime").textContent = state.uptime_s + " s";

    var rows = $("history-body");
    if (!state.history.length) {
      rows.innerHTML = '<tr><td colspan="5" class="empty">暂无重载记录</td></tr>';
    } else {
      rows.innerHTML = state.history.map(function (h) {
        var t = new Date(h.time * 1000).toISOString().substring(11, 19);
        return "<tr>" +
          "<td class='mono'>#" + h.seq + "</td>" +
          "<td class='mono'>" + t + "</td>" +
          "<td>" + stackPill(h.stack) + "</td>" +
          "<td class='mono'>" + escapeHtml(h.request_id) + "</td>" +
          "<td>" + (h.disconnect
            ? '<span class="pill pill-disconnect">执行后断连</span>'
            : '<span class="pill pill-normal">完整回执</span>') + "</td>" +
          "</tr>";
      }).join("");
    }

    // 健康点
    $("health-dot").className = "dot dot-ok";
  }

  function refreshState() {
    return api("/api/state").then(renderState).catch(function () {
      $("health-dot").className = "dot dot-bad";
    });
  }

  /* ---------------- 演示设置 ---------------- */

  function currentSettings() {
    return {
      ipv6_mode: document.querySelector('input[name="ipv6_mode"]:checked').value,
      ipv4_mode: document.querySelector('input[name="ipv4_mode"]:checked').value,
      disconnect_after_execute: $("disconnect").checked
    };
  }

  function syncExplain() {
    ["ipv6", "ipv4"].forEach(function (st) {
      var mode = document.querySelector('input[name="' + st + '_mode"]:checked').value;
      $("explain-" + st).textContent = MODE_EXPLAIN[st][mode] || "";
    });
  }

  var saveTimer = null;
  function saveSettings() {
    syncExplain();
    var tip = $("settings-saved");
    api("/api/settings", { method: "POST", body: JSON.stringify(currentSettings()) })
      .then(function (data) {
        tip.textContent = "已应用 ✓";
        clearTimeout(saveTimer);
        saveTimer = setTimeout(function () { tip.textContent = ""; }, 1600);
        renderState(data.state);
      })
      .catch(function (err) { tip.textContent = "设置失败：" + err.message; });
  }

  document.querySelectorAll('input[name="ipv6_mode"], input[name="ipv4_mode"]').forEach(function (el) {
    el.addEventListener("change", saveSettings);
  });
  $("disconnect").addEventListener("change", saveSettings);

  var SCENARIOS = {
    normal:      { ipv6_mode: "ok",    ipv4_mode: "ok",    disconnect_after_execute: false },
    v6stall:     { ipv6_mode: "stall", ipv4_mode: "ok",    disconnect_after_execute: false },
    v6reset:     { ipv6_mode: "reset", ipv4_mode: "ok",    disconnect_after_execute: false },
    disconnect:  { ipv6_mode: "ok",    ipv4_mode: "ok",    disconnect_after_execute: true  },
    neither:     { ipv6_mode: "stall", ipv4_mode: "stall", disconnect_after_execute: false }
  };

  document.querySelectorAll(".btn-ghost[data-scenario]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var sc = SCENARIOS[btn.getAttribute("data-scenario")];
      if (!sc) return;
      document.querySelector('input[name="ipv6_mode"][value="' + sc.ipv6_mode + '"]').checked = true;
      document.querySelector('input[name="ipv4_mode"][value="' + sc.ipv4_mode + '"]').checked = true;
      $("disconnect").checked = sc.disconnect_after_execute;
      saveSettings();
    });
  });

  $("btn-reset-count").addEventListener("click", function () {
    api("/api/reset-count", { method: "POST" }).then(function (d) { renderState(d.state); });
  });

  /* ---------------- 重载与结果渲染 ---------------- */

  function kindForEvent(ev) {
    if (ev === "cert_verified" || ev === "receipt_received" || ev === "winner_selected") return "ok";
    if (ev === "loser_disconnected") return "cut";
    if (ev === "attempt_failed" || ev === "receipt_failed") return "bad";
    if (ev === "deadline_reached" || ev === "parallel_window_elapsed" ||
        ev === "ipv6_early_failure_detected" || ev === "both_paths_failed_early" ||
        ev === "loser_cancel_initiated" || ev === "path_blocked_before_connect") return "warn";
    return "";
  }

  var REASON_LABELS = {
    winner_selected: "另一路已胜出",
    deadline: "3 秒总时限到点",
    both_failed: "两路均失败后收尾",
    post_done: "收发结束"
  };

  function renderTimeline(events) {
    var wrap = $("timeline-wrap");
    wrap.classList.remove("hidden");
    $("timeline").innerHTML = events.map(function (e) {
      var label = EVENT_LABELS[e.event] || e.event;
      var detailParts = [];
      ["reason", "error", "http_status", "handshake_ms", "cn", "bytes", "connect_ms"].forEach(function (k) {
        if (e[k] !== undefined && e[k] !== null) {
          var v = k === "reason" && REASON_LABELS[e[k]] ? REASON_LABELS[e[k]] : e[k];
          if (typeof v === "string" && v.length > 120) v = v.slice(0, 120) + "…";
          detailParts.push(k + "=" + v);
        }
      });
      // 落败连接：同时显示“发起取消 → 实际断开”的真实时刻差
      if (e.event === "loser_disconnected" && typeof e.t_ms_actual === "number") {
        detailParts.unshift("实际断开于 +" + e.t_ms_actual + "ms");
      }
      if (e.detail && typeof e.detail === "object") {
        // race_start 的大对象不展开
      }
      var cutMark = e.event === "loser_disconnected"
        ? "<span class='cut-badge' title='该连接此刻真正关闭（设备侧收到 FIN）'>● 已断开</span>"
        : "";
      return "<li class='kind-" + kindForEvent(e.event) + "'>" +
        "<span class='t'>+" + e.t_ms + "ms</span>" +
        (e.stack ? "<span class='st st-" + e.stack + "'>" + e.stack + "</span>"
                 : "<span class='st st-system'>sys</span>") +
        "<span class='ev'>" + escapeHtml(label) + "</span>" + cutMark +
        (detailParts.length ? "<span class='detail'>" + escapeHtml(detailParts.join(" · ")) + "</span>" : "") +
        "</li>";
    }).join("");
  }

  function renderResult(payload) {
    var r = payload.result;
    var banner = $("result-banner");
    banner.classList.remove("hidden", "running", "delivered", "not_sent", "unknown");
    banner.classList.add(r.status);

    var icon = r.status === "delivered" ? "✓" : (r.status === "unknown" ? "⚠" : "✕");
    var sub = "";
    if (r.status === "delivered" && r.receipt) {
      sub = "设备 " + escapeHtml(r.receipt.body.device) +
            " 经 " + (r.winner || "").toUpperCase() +
            " 完成执行，共用计数现为 #" + r.receipt.body.reload_count;
    } else if (r.status === "unknown") {
      sub = "POST 已开始发送，但未收到完整回执；不会换路重发。可在下方计数/历史中确认设备是否已执行。";
    } else {
      sub = "3 秒结束前没有任何一路开始发送 POST，未下发到设备。";
    }
    banner.innerHTML = "<span style='font-size:22px'>" + icon + "</span>" +
      "<div>" + escapeHtml(r.status_text) +
      "<span class='sub'>" + sub + "</span></div>";

    $("winner-row").classList.remove("hidden");
    var w = $("winner");
    w.textContent = r.winner ? r.winner.toUpperCase() + "（同一设备名校验通过）" : "无";
    w.className = r.winner ? "winner-" + r.winner : "";
    $("elapsed").textContent = r.elapsed_ms;
    $("post-started").textContent = r.post_started ? "是" : "否";

    if (r.receipt) {
      $("receipt-wrap").classList.remove("hidden");
      $("receipt").textContent = JSON.stringify(r.receipt.body, null, 2);
    } else {
      $("receipt-wrap").classList.add("hidden");
    }
    if (r.error) {
      $("error-wrap").classList.remove("hidden");
      var lines = [r.error, "", "各路径明细："];
      Object.keys(r.attempts).forEach(function (st) {
        var a = r.attempts[st];
        var line = "[" + st + "] cert_passed=" + a.cert_passed +
          " failed=" + a.failed + (a.error ? " error=" + a.error : "") +
          (a.cert ? " cn=" + a.cert.subject_cn + " verified_against=" + a.cert.verified_against_name : "");
        if (a.blocked) {
          line += " | 落败拦截阶段=" + a.blocked_stage + "（" + (REASON_LABELS[a.blocked_reason] || a.blocked_reason) + "）";
        }
        if (a.close_reason) {
          line += " | 连接关闭原因=" + (REASON_LABELS[a.close_reason] || a.close_reason);
        }
        lines.push(line);
      });
      $("error-text").textContent = lines.join("\n");
    } else {
      $("error-wrap").classList.add("hidden");
    }

    renderTimeline(r.timeline);
    renderState(payload.device);
  }

  var running = false;
  $("btn-reload").addEventListener("click", function () {
    if (running) return;
    running = true;
    var btn = $("btn-reload");
    btn.disabled = true;
    btn.classList.add("loading");
    btn.querySelector(".btn-text").textContent = "竞速下发中（最长 3 秒）…";

    var banner = $("result-banner");
    banner.classList.remove("hidden", "delivered", "not_sent", "unknown");
    banner.classList.add("running");
    banner.textContent = "正在双栈竞速：先 IPv6，" + window.APP_CONFIG.parallelDelayMs +
      "ms 未过证书校验则并行 IPv4 …";

    api("/api/reload", { method: "POST", body: "{}" })
      .then(renderResult)
      .catch(function (err) {
        banner.classList.remove("running");
        banner.classList.add("unknown");
        banner.textContent = "请求控制台失败：" + err.message;
      })
      .finally(function () {
        running = false;
        btn.disabled = false;
        btn.classList.remove("loading");
        btn.querySelector(".btn-text").textContent = "重载交换机配置";
        refreshState();
      });
  });

  /* ---------------- 启动 ---------------- */
  syncExplain();
  refreshState();
  setInterval(refreshState, 2500);
})();
