"""容器/本地启动入口：先生成证书，再启动双栈样例设备与 Flask 值班台。"""
from __future__ import annotations

import os

from .certutil import DEFAULT_DEVICE_NAME, ensure_certs
from .device import start_background_device
from .state import DeviceState
from .web import create_app


def main() -> None:
    device_name = os.environ.get("DEVICE_NAME", DEFAULT_DEVICE_NAME)
    device_port = int(os.environ.get("DEVICE_PORT", "8443"))
    web_host = os.environ.get("WEB_HOST", "0.0.0.0")
    web_port = int(os.environ.get("WEB_PORT", "8080"))

    paths = ensure_certs(device_name)
    state = DeviceState(device_name=device_name)
    device = start_background_device(
        port=device_port,
        cert_path=paths["server_cert"],
        key_path=paths["server_key"],
        state=state,
    )
    print(f"[device] dual-stack HTTPS sample switch {device_name} on port {device.port}")
    print(f"[device] IPv6 https://[::1]:{device.port}  IPv4 https://127.0.0.1:{device.port}")
    print(f"[console] Flask reload console on http://{web_host}:{web_port}")

    app = create_app(
        device=device,
        state=state,
        ca_cert=paths["ca_cert"],
        device_name=device_name,
    )
    # threaded=True：值班员等待最长 3s 的竞速期间，状态轮询仍可响应
    app.run(host=web_host, port=web_port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
