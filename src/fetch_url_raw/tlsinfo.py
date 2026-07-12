"""TLS handshake metadata and certificate extraction helpers."""

from __future__ import annotations

import hashlib
import ssl
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import httpcore

if TYPE_CHECKING:
    pass


def _name_tuples_to_dict(tuples: Any) -> dict[str, str]:
    """Convert ssl.getpeercert() subject/issuer RDNs to a flat dict."""
    out: dict[str, str] = {}
    if not tuples:
        return out
    try:
        for rdn in tuples:
            for key, value in rdn:
                if key not in out:
                    out[str(key)] = str(value)
    except Exception:  # noqa: BLE001 - best-effort parse
        return out
    return out


def _san_list(cert_dict: dict[str, Any] | None) -> list[str]:
    if not cert_dict:
        return []
    sans = cert_dict.get("subjectAltName") or ()
    result: list[str] = []
    for item in sans:
        try:
            kind, value = item
            result.append(f"{kind}:{value}")
        except Exception:  # noqa: BLE001
            result.append(str(item))
    return result


def _cert_time_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ts = ssl.cert_time_to_seconds(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return value


def _fingerprint_sha256(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _parse_der_with_cryptography(der: bytes) -> dict[str, Any] | None:
    """Optional richer parse when ssl.getpeercert() dict is empty (verify off)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
    except Exception:  # noqa: BLE001
        return None
    try:
        cert = x509.load_der_x509_certificate(der)
    except Exception:  # noqa: BLE001
        return None

    def _name_to_dict(name: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            for attr in name:
                key = getattr(attr.oid, "_name", None) or attr.oid.dotted_string
                if key not in out:
                    out[str(key)] = str(attr.value)
        except Exception:  # noqa: BLE001
            return out
        return out

    san_list: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        for name in ext.value:
            label = name.__class__.__name__
            value = getattr(name, "value", None)
            if label == "DNSName" and value is not None:
                san_list.append(f"DNS:{value}")
            elif label == "IPAddress" and value is not None:
                san_list.append(f"IP:{value}")
            elif label == "UniformResourceIdentifier" and value is not None:
                san_list.append(f"URI:{value}")
            elif label == "RFC822Name" and value is not None:
                san_list.append(f"email:{value}")
            else:
                san_list.append(str(name))
    except Exception:  # noqa: BLE001
        pass

    not_before = cert.not_valid_before_utc if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before
    not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after
    def _dt(v: Any) -> str | None:
        try:
            if v.tzinfo is None:
                from datetime import timezone as _tz
                v = v.replace(tzinfo=_tz.utc)
            return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            return str(v) if v is not None else None

    return {
        "subject": _name_to_dict(cert.subject),
        "issuer": _name_to_dict(cert.issuer),
        "san": san_list,
        "not_before": _dt(not_before),
        "not_after": _dt(not_after),
        "serial_number": format(cert.serial_number, "X"),
        "version": int(cert.version.value) + 1 if hasattr(cert.version, "value") else None,
    }


def _cert_from_der(der: bytes, cert_dict: dict[str, Any] | None = None) -> dict[str, Any]:
    pem = ssl.DER_cert_to_PEM_cert(der)
    info: dict[str, Any] = {
        "pem": pem,
        "fingerprint_sha256": _fingerprint_sha256(der),
        "der_size": len(der),
    }
    if cert_dict:
        info["subject"] = _name_tuples_to_dict(cert_dict.get("subject"))
        info["issuer"] = _name_tuples_to_dict(cert_dict.get("issuer"))
        info["san"] = _san_list(cert_dict)
        info["not_before"] = _cert_time_to_iso(cert_dict.get("notBefore"))
        info["not_after"] = _cert_time_to_iso(cert_dict.get("notAfter"))
        info["serial_number"] = cert_dict.get("serialNumber")
        info["version"] = cert_dict.get("version")
        return info

    parsed = _parse_der_with_cryptography(der)
    if parsed:
        info.update(parsed)
    else:
        info["subject"] = {}
        info["issuer"] = {}
        info["san"] = []
        info["not_before"] = None
        info["not_after"] = None
        info["serial_number"] = None
        info["version"] = None
    return info


def _der_chain_from_ssl_object(ssl_object: ssl.SSLObject) -> list[bytes]:
    chain: list[bytes] = []
    try:
        raw_chain = ssl_object.get_unverified_chain()
    except Exception:  # noqa: BLE001
        raw_chain = None
    if raw_chain:
        for item in raw_chain:
            if isinstance(item, (bytes, bytearray)):
                chain.append(bytes(item))
            elif hasattr(item, "public_bytes"):
                try:
                    chain.append(item.public_bytes())  # type: ignore[call-arg]
                except Exception:  # noqa: BLE001
                    continue
    if not chain:
        try:
            leaf = ssl_object.getpeercert(binary_form=True)
        except Exception:  # noqa: BLE001
            leaf = None
        if leaf:
            chain.append(leaf)
    return chain


def build_tls_info(
    ssl_object: ssl.SSLObject,
    *,
    sni: str | None,
    peer_ip: str | None,
    peer_port: int | None,
    verified: bool | None,
) -> dict[str, Any]:
    """Build a structured TLS description from a live SSLObject."""
    version = None
    cipher_name = None
    cipher_bits = None
    alpn = None
    try:
        version = ssl_object.version()
    except Exception:  # noqa: BLE001
        pass
    try:
        cipher = ssl_object.cipher()
        if cipher:
            cipher_name = cipher[0]
            if len(cipher) >= 3:
                cipher_bits = cipher[2]
    except Exception:  # noqa: BLE001
        pass
    try:
        alpn = ssl_object.selected_alpn_protocol()
    except Exception:  # noqa: BLE001
        pass

    try:
        cert_dict = ssl_object.getpeercert() or None
    except Exception:  # noqa: BLE001
        cert_dict = None

    ders = _der_chain_from_ssl_object(ssl_object)
    peer_certificate = _cert_from_der(ders[0], cert_dict) if ders else None
    chain_out: list[dict[str, Any]] = []
    for idx, der in enumerate(ders):
        chain_out.append(_cert_from_der(der, cert_dict if idx == 0 else None))

    return {
        "version": version,
        "cipher": cipher_name,
        "cipher_bits": cipher_bits,
        "alpn": alpn,
        "sni": sni,
        "peer_ip": peer_ip,
        "peer_port": peer_port,
        "verified": verified,
        "peer_certificate": peer_certificate,
        "peer_certificate_chain": chain_out,
    }


def make_ssl_context(verify_tls: bool) -> ssl.SSLContext:
    if verify_tls:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except Exception:  # noqa: BLE001
        pass
    return ctx


class TlsCapturingStream:
    """Wrap a network stream and capture TLS metadata after start_tls succeeds."""

    def __init__(
        self,
        stream: httpcore.AsyncNetworkStream,
        holder: dict[str, Any],
        *,
        peer_ip: str,
        peer_port: int,
    ) -> None:
        self._stream = stream
        self._holder = holder
        self._peer_ip = peer_ip
        self._peer_port = peer_port

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        tls_stream = await self._stream.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=timeout,
        )
        ssl_object = tls_stream.get_extra_info("ssl_object")
        if ssl_object is not None:
            verified = (
                ssl_context.verify_mode != ssl.CERT_NONE and bool(ssl_context.check_hostname)
            )
            self._holder["tls"] = build_tls_info(
                ssl_object,
                sni=server_hostname,
                peer_ip=self._peer_ip,
                peer_port=self._peer_port,
                verified=verified,
            )
            self._holder["peer_ip"] = self._peer_ip
            self._holder["peer_port"] = self._peer_port
            self._holder["sni"] = server_hostname
        return tls_stream


async def probe_tls(
    *,
    host: str,
    port: int,
    server_hostname: str,
    verify_tls: bool,
    dns_override: dict[str, str] | None,
    allow_private_network: bool,
    timeout: float,
) -> dict[str, Any]:
    """TCP connect + TLS handshake only; return structured tls info.

    Imports GuardedNetworkBackend lazily to avoid circular imports with dns.py.
    """
    from fetch_url_raw.dns import GuardedNetworkBackend

    backend = GuardedNetworkBackend(
        dns_override,
        allow_private_network=allow_private_network,
    )
    stream = await backend.connect_tcp(host, port, timeout=timeout)
    peer = stream.get_extra_info("server_addr")
    if isinstance(peer, tuple) and peer:
        peer_ip = str(peer[0])
        peer_port = int(peer[1]) if len(peer) > 1 else port
    else:
        peer_ip = None
        peer_port = port

    ctx = make_ssl_context(verify_tls)
    try:
        tls_stream = await stream.start_tls(
            ctx,
            server_hostname=server_hostname,
            timeout=timeout,
        )
    except Exception:
        await stream.aclose()
        raise

    try:
        ssl_object = tls_stream.get_extra_info("ssl_object")
        if ssl_object is None:
            raise httpcore.ConnectError("TLS handshake completed without SSL object")
        return build_tls_info(
            ssl_object,
            sni=server_hostname,
            peer_ip=peer_ip,
            peer_port=peer_port,
            verified=bool(verify_tls),
        )
    finally:
        await tls_stream.aclose()
