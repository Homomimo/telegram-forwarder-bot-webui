"""Direct HTTPS for the configured or detected VPS IPv4 address."""

import ipaddress
from http.client import HTTPException
from http.server import ThreadingHTTPServer
import ssl
import subprocess
import tempfile
from pathlib import Path
from urllib import request


PUBLIC_IPV4_URL = "https://api.ipify.org"
# The standalone updater reads this container-local file with docker exec.
RUNTIME_IP_FILE = Path("/tmp/telegram-forwarder-webui-ip.json")


def validate_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError("RELAY_VPS_IP 请填写 VPS 的 IPv4 地址，不要带协议或端口") from exc
    if address.is_unspecified or address.is_multicast or int(address) == 0xffffffff:
        raise ValueError("RELAY_VPS_IP 必须是可访问的主机地址")
    return str(address)


def resolve_ip(override):
    override = (override or "").strip()
    if override:
        return validate_ip(override), "手动配置"

    try:
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(PUBLIC_IPV4_URL, timeout=5) as response:
            address = ipaddress.IPv4Address(response.read(64).decode("ascii").strip())
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError("响应不是公网 IPv4")
    except (OSError, HTTPException, ValueError) as exc:
        raise ValueError("无法自动探测 VPS 公网 IPv4。请检查容器的 IPv4 出网，或手动设置 RELAY_VPS_IP。") from exc
    return str(address), "自动探测"


def tls_context(data, ip):
    # ip is canonicalized before any filesystem or subprocess use.
    ip = str(ipaddress.IPv4Address(ip))
    parent = data / "tls"
    dest = parent / ip
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not dest.exists():
        # Publish a complete pair or none. Never silently replace a trusted cert.
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            pair = Path(temp) / "pair"
            pair.mkdir(mode=0o700)
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                "ec_paramgen_curve:P-256", "-noenc", "-sha256", "-days", "365",
                "-subj", f"/CN={ip}", "-addext", f"subjectAltName=IP:{ip}",
                "-addext", "basicConstraints=critical,CA:FALSE", "-addext",
                "extendedKeyUsage=serverAuth", "-addext", "keyUsage=critical,digitalSignature",
                "-keyout", str(pair / "key.pem"), "-out", str(pair / "cert.pem"),
            ], check=True, timeout=30, stdout=subprocess.DEVNULL)
            (pair / "key.pem").chmod(0o600)
            pair.rename(dest)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(dest / "cert.pem", dest / "key.pem")
    return context


class IPHTTPSServer(ThreadingHTTPServer):
    def __init__(self, addr, handler, ip, context):
        self.vps_ip = ip
        self.context = context
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        # TLS negotiation happens in the request thread, not the accept loop.
        request.settimeout(10)
        try:
            conn = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        with conn:
            conn.settimeout(30)
            try:
                super().finish_request(conn, client_address)
            finally:
                # Deliver early HTTP errors even when the peer is still sending a
                # request body. The accept-loop socket was detached by wrap_socket.
                conn.settimeout(1)
                try:
                    plain = conn.unwrap()
                except OSError:
                    self.shutdown_request(conn)
                else:
                    self.shutdown_request(plain)
