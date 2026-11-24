# 交换机配置重载台（Flask · 双栈 HTTPS 竞速下发）

值班员点击“重载配置”后，控制台通过**内置的本地双栈 HTTPS 样例设备**完成一次严格规则的竞速下发，并在网页展示设备回执、竞速时间线、三种判定以及 IPv4/IPv6 两路共用的重载计数。

## 做成的功能

### 核心竞速规则（`app/race_client.py`）

1. **IPv6 优先**：先连接 `https://[::1]:8443/reload`。
2. **250ms 并行窗口**：从操作开始计时，IPv6 在 250ms 内没有通过 TLS 证书校验，立即在旁并行启动 IPv4（`https://127.0.0.1:8443/reload`）。
3. **提前失败立即切 IPv4**：IPv6 若在 250ms 内连接被拒/RST/证书校验失败，检测到终态后**立刻**启动 IPv4，不等满窗口。
4. **同一设备名校验两路证书**：两路 TLS 都以 `server_hostname="switch-demo.local"` 做 SNI 与 hostname 校验，信任同一张内置 CA；证书 SAN 含该设备名、`localhost`、`127.0.0.1`、`::1`。设备名不匹配时两路都会被拒（有自测覆盖）。
5. **最先通过者只发一次 POST**：证书校验通过后在同一把锁内原子选出唯一 winner，仅它在该连接上发送**一次** POST；另一路无论仍在握手还是已通过待选，都会被立即关闭释放。`parallel_delay=0` 的双路齐发用例连跑 15 次，计数始终为 1。
6. **整次操作 3 秒上限**：默认 `deadline=3.0s`，到点关闭所有残留 socket。
7. **未下发（`not_sent`）**：截止时 POST **尚未开始发送**（例如双路握手都卡住），标红“未下发”，设备计数不增加。
8. **结果未知（`unknown`）且不重发**：一旦 POST **已经开始发送**，之后任何断连/超时/回执不完整都判定为“结果未知”，**不会**换另一路重发；设备侧共用计数最多 +1，可据此核对设备是否其实已执行。
9. “是否已开始发送”与“截止/取消”在同一把锁下协调，消除“到点边界上误标状态”的竞态。

### 内置双栈样例设备（`app/device.py` + `app/state.py`）

- 同端口分别绑定 `::1`（AF_INET6）与 `127.0.0.1`（AF_INET），两个 `ThreadingHTTPServer` 独立线程服务，共用同一个 `DeviceState`。
- `POST /reload`：在共用锁内递增**两路共用**的 `reload_count` 与分栈计数 `stack_count.{ipv6,ipv4}`，返回 JSON 回执（含新计数、实际服务栈、request_id、时间）。
- 故障注入（可按栈分别设置）：
  - `ok`：正常 TLS + 回执；
  - `stall`：accept TCP 后**卡住 TLS 握手**（不回 ServerHello），用于演示“IPv6 握手卡住”；
  - `reset`：accept 后立即 RST，用于演示“IPv6 提前失败”。
- `disconnect_after_execute`：计数 +1（设备已执行）后不发完整回执、直接 RST 断连，用于演示“设备执行后断连 → 结果未知”。
- `GET /health` 设备栈探活。

### 网页控制台（`app/templates/index.html` + `app/static`）

- 大号**两路共用重载计数**，以及 IPv6/IPv4 分计数、运行时长、最近 30 条设备执行记录。
- 重载按钮（执行中转圈并禁用）、三态结果横幅：
  - 绿色“已下发，收到设备回执”+ 格式化回执 JSON；
  - 红色“未下发”；
  - 橙色“结果未知”。
- winner 路径、总耗时、POST 是否已开始、逐毫秒**竞速时间线**（TCP、证书校验、250ms 窗口、winner、释放另一路、POST 开始/发出、回执/失败、3s 截止等），以及每路证书 CN/SAN/校验名与错误明细。
- 演示控制面板：IPv6/IPv4 三态单选、执行后断连开关、五个一键场景按钮（双栈正常 / IPv6 卡住 / IPv6 提前失败 / 执行后断连 / 双路均不可用）、计数清零；状态每 2.5s 自动刷新。
- 纯原生 HTML/CSS/JS，无外部 CDN/字体，离线可用。

### 部署

- `Dockerfile`：`python:3.12-slim`，安装 Flask + cryptography，非 root 运行，带 healthcheck。
- `docker-compose.yml`：单服务 `reload-console`，映射 `http://localhost:8080`。
- Flask 值班台与样例设备在**同一容器网络命名空间**内经 `::1` / `127.0.0.1` 互访，因此**不需要**为 Docker daemon 开启 IPv6；只要容器内 lo 上有 `::1`（Docker 默认如此）即可。
- 证书在启动时用 `cryptography` 自动生成到 `/app/certs`（Dockerfile 已将该目录分配给非 root 用户；无需挂载卷）。想取出内置 CA 可执行 `docker compose cp reload-console:/app/certs/ca.pem ./ca.pem`。

## 目录结构

```
.
├── app/
│   ├── main.py           # 入口：生成证书 → 启动双栈设备 → 启动 Flask
│   ├── web.py            # Flask 工厂与 /api/* 路由
│   ├── race_client.py    # 双栈竞速、250ms 窗口、3s 截止、三态判定
│   ├── device.py         # 双栈 HTTPS 样例设备与故障注入
│   ├── state.py          # 两路共用计数/设置/历史（加锁）
│   ├── certutil.py       # 内置 CA 与 SAN 服务器证书生成
│   ├── templates/index.html
│   └── static/{style.css,app.js}
├── tests/test_selftest.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 启动方式

### Docker Compose（推荐交付方式）

```bash
docker compose up --build
# 打开 http://localhost:8080
```

停止：`docker compose down`（保留证书卷）；连证书卷一起清理：`docker compose down -v`。

### 本地直接运行（无需 Docker）

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
python3 -m app.main
# http://127.0.0.1:8080
```

## 演示操作与预期现象

| 操作 | 预期结果 |
| --- | --- |
| 默认设置直接点“重载交换机配置” | 约几十 ms 内 **IPv6 证书通过并胜出**，不启动 IPv4；绿色成功，共用计数 +1（IPv6 分计数 +1） |
| 点场景“IPv6 握手卡住 → IPv4”再重载 | 时间线显示 `+250ms parallel_window_elapsed` 后启动 IPv4，IPv4 胜出并拿到回执；计数 +1（IPv4 +1，IPv6 +0） |
| 点场景“IPv6 提前失败 → 立即 IPv4” | `start_path` 的 reason 为 `ipv6_early_failure`，通常几十 ms 内即启动 IPv4，**早于 250ms** |
| 点场景“执行后断连 → 结果未知” | 橙色“结果未知”，`POST 已开始=是`，无回执；共用计数仍 +1，且等待后不出现第二次增加（无重发） |
| 点场景“双路均不可用 → 未下发（3s）” | 约 **3000ms** 后红色“未下发”，`POST 已开始=否`、无 winner，计数不变 |
| 任意时刻看顶部计数/底部记录表 | 两路共用同一 `reload_count`，并列出每次实际由哪一栈接收 |

HTTP 直调示例：

```bash
curl -s -X POST localhost:8080/api/settings \
  -H 'Content-Type: application/json' -d '{"ipv6_mode":"stall"}'
curl -s -X POST localhost:8080/api/reload -H 'Content-Type: application/json' -d '{}'
curl -s localhost:8080/api/state
```

## 自测

自动化端到端测试会在临时目录生成证书、随机端口真实启动双栈 TLS 设备，共 **2 个测试类、11 个测试方法**：

```bash
python3 -m unittest discover -s tests -v
```

最近一次实测结果（构建沙箱内，Python 3.11.2 / Flask 3.1.3 / cryptography 50.0.2）：

```
Ran 11 tests in 9.792s
OK
```

覆盖：正常 IPv6 胜出且只发一次 POST；IPv6 stall 后恰在 ~250ms 启动 IPv4 并由其成功；IPv6 RST 后以 ipv6_early_failure 立即启动 IPv4；执行后断连判 unknown 且计数只 +1；双 stall 耗满约 3.0s 判 not_sent；parallel_delay=0 双路齐发仍恰好一次 POST（该用例单独连跑 15 次全部通过）；错误设备名证书校验被拒判 not_sent；设置校验；Flask 首页/healthz/settings/reload/reset 路由。

此外已真实启动 `python -m app.main` 用 curl 验证全部 HTTP 场景，实测数据：

- IPv6 卡住：`status=delivered winner=ipv4`，IPv4 启动于 `+252ms`，reason=`parallel_delay_250ms`；
- 执行后断连：`status=unknown`，错误 `RemoteDisconnected: Remote end closed connection without response`，计数 1→2 且无重发；
- 双路卡住：`status=not_sent`，`elapsed=3000ms`，curl 墙钟 `3.025s`，计数不变；
- 恢复正常：`winner=ipv6`，证书 CN=`switch-demo.local`。

前端另用 jsdom 加载真实 `index.html` + `app.js` 并对接运行中的服务做了点击级回归：依次执行“IPv6 卡住 / 执行后断连 / 双路不可用 / 正常”四个场景，横幅 class、winner、POST 标志、计数、时间线行数、回执区块显隐全部符合预期，`neither` 场景渲染耗时 `elapsed=3000`，且**无任何 JS 报错**。`node --check app.js` 通过；JS 引用的 21 个元素 ID 与模板全部匹配；compose YAML 通过解析校验。

## 未完成项 / 已知限制（如实说明）

1. **本构建沙箱没有 docker / docker compose 可执行程序，也没有 `/var/run/docker.sock`**，因此 `docker compose up --build` 未能在交付前于本机实际跑过；已做的替代验证是：compose YAML 解析通过、Dockerfile 基于官方 `python:3.12-slim` 且依赖均有 manylinux wheel、同一 `python -m app.main` 入口在宿主机 Python 3.11 下端到端实测通过。Docker 构建/启动仍需在有 Docker 的环境执行一次确认。
2. 沙箱网络下载 Chromium 仅约 17KB/s，未做成真实浏览器像素级截图核对；已用 jsdom 完成真实 DOM + 点击 + 接口的前端行为回归（无 JS 错误），外加 JS 语法检查与元素 ID 交叉核对。视觉排版未经过像素级确认。
3. Flask 使用自带开发服务器（`threaded=True`），面向单个值班员的演示/内网低频操作足够；未接 gunicorn，是为了保证“设备 + 控制台同进程、双栈端口与共用状态”只有一份。
4. 样例设备与 CA/服务器证书均为演示用途、自动生成，**不要**用于真实网络设备或生产环境。
5. 3 秒总时限以协调线程的 20ms 轮询 + socket 关闭实现，实测判定时刻约在 3.00–3.02s；非硬实时。
