"""Opt-in, direct HTTPS for a single configured VPS IPv4 address."""

import ipaddress
from http.server import ThreadingHTTPServer
import ssl
import subprocess
import tempfile
from pathlib import Path


def validate_ip(value, mode, password):
    if mode != "live" or len(password) < 16:
        raise ValueError("VPS 访问需要真实模式和至少 16 位的管理员密码")
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError("RELAY_VPS_IP 请填写 VPS 的 IPv4 地址，不要带协议或端口") from exc
    if address.is_unspecified or address.is_multicast or int(address) == 0xffffffff:
        raise ValueError("RELAY_VPS_IP 必须是可访问的主机地址")
    return str(address)


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
