"""生成演示用内置 CA 与服务器证书。

证书 SAN 同时包含设备名、127.0.0.1 和 ::1。
重载客户端无论走 IPv4 还是 IPv6，都使用同一个 device_name 作为 SNI/主机名校验目标。
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DEFAULT_DEVICE_NAME = "switch-demo.local"


def cert_dir() -> Path:
    path = Path(os.environ.get("CERT_DIR", Path(__file__).resolve().parent.parent / "certs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_key(path: Path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ensure_certs(device_name: str = DEFAULT_DEVICE_NAME, directory: Path | str | None = None) -> dict[str, Path]:
    directory = Path(directory) if directory else cert_dir()
    directory.mkdir(parents=True, exist_ok=True)
    ca_cert_path = directory / "ca.pem"
    ca_key_path = directory / "ca-key.pem"
    server_cert_path = directory / "server.pem"
    server_key_path = directory / "server-key.pem"

    if server_cert_path.exists() and server_key_path.exists() and ca_cert_path.exists():
        return {
            "ca_cert": ca_cert_path,
            "server_cert": server_cert_path,
            "server_key": server_key_path,
        }

    now = dt.datetime.now(dt.timezone.utc)

    # 自签 CA
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "GSB Demo Switch CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Reload Console Demo"),
        ]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    _write_key(ca_key_path, ca_key)

    # 服务器证书
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, device_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GSB Sample Switch"),
        ]
    )
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName(device_name),
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address("::1")),
        ]
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    _write_key(server_key_path, server_key)

    return {
        "ca_cert": ca_cert_path,
        "server_cert": server_cert_path,
        "server_key": server_key_path,
    }


if __name__ == "__main__":
    paths = ensure_certs()
    for name, path in paths.items():
        print(f"{name}: {path}")
