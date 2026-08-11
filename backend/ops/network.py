from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse, urlunsplit

import requests
import urllib3
from requests.structures import CaseInsensitiveDict


class NetworkPolicyError(ValueError):
    pass


_DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class WorkflowHttpClient:
    def __init__(
        self,
        allowed_urls: list[str],
        *,
        allow_private_networks: bool = False,
        timeout: float = 30,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        self.allow_private_networks = allow_private_networks
        self.timeout = timeout
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self._resolver = resolver
        self._origins = {_origin(url.replace("{{external_id}}", "challenge-id")) for url in allowed_urls if url}
        for url in allowed_urls:
            if url:
                self.validate_url(url.replace("{{external_id}}", "challenge-id"))

    def validate_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise NetworkPolicyError("workflow requests require an http or https URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise NetworkPolicyError("workflow URLs cannot contain credentials or fragments")
        if parsed.scheme == "http" and not self.allow_private_networks:
            raise NetworkPolicyError("public workflow endpoints must use https")
        if _origin(url) not in self._origins:
            raise NetworkPolicyError(f"URL origin was not present in the confirmed workflow: {_origin(url)}")
        if not self.allow_private_networks:
            self._reject_non_public_host(parsed.hostname, parsed.port, parsed.scheme)
        return url

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any):
        self.validate_url(url)
        if method.upper() not in {"GET", "POST", "PUT"}:
            raise NetworkPolicyError(f"HTTP method is not allowed: {method}")
        address = self._resolved_address(urlparse(url))
        timeout = kwargs.pop("timeout", self.timeout)
        kwargs.pop("max_response_bytes", None)
        kwargs.pop("allow_redirects", None)
        response = _pinned_request(
            method.upper(),
            url,
            address=address,
            timeout=timeout,
            max_response_bytes=self.max_response_bytes,
            **kwargs,
        )
        status = int(getattr(response, "status_code", 200))
        if 300 <= status < 400:
            location = getattr(response, "headers", {}).get("location", "")
            target = urljoin(url, location) if location else ""
            if target:
                self.validate_url(target)
            raise NetworkPolicyError("redirects are disabled for confirmed workflows")
        return response

    def _reject_non_public_host(self, hostname: str, port: int | None, scheme: str) -> None:
        addresses = self._resolve_addresses(hostname, port, scheme)
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise NetworkPolicyError(
                    f"workflow hostname resolves to a non-public address: {hostname}"
                )

    def _resolved_address(self, parsed) -> str:
        addresses = self._resolve_addresses(parsed.hostname, parsed.port, parsed.scheme)
        if not self.allow_private_networks:
            for address in addresses:
                if not ipaddress.ip_address(address).is_global:
                    raise NetworkPolicyError(
                        f"workflow hostname resolves to a non-public address: {parsed.hostname}"
                    )
        return sorted(addresses)[0]

    def _resolve_addresses(self, hostname: str, port: int | None, scheme: str) -> set[str]:
        effective_port = port or (443 if scheme == "https" else 80)
        try:
            records = self._resolver(hostname, effective_port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise NetworkPolicyError(f"cannot resolve workflow hostname: {hostname}") from exc
        addresses = {record[4][0].split("%", 1)[0] for record in records}
        if not addresses:
            raise NetworkPolicyError(f"cannot resolve workflow hostname: {hostname}")
        return addresses


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise NetworkPolicyError("invalid workflow URL")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise NetworkPolicyError("workflow URL contains an invalid port") from exc
    if parsed_port is not None and parsed_port < 1:
        raise NetworkPolicyError("workflow URL contains an invalid port")
    port = parsed_port if parsed_port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port


def _pinned_request(
    method: str,
    url: str,
    *,
    address: str,
    timeout: float,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    headers: dict[str, str] | None = None,
    json: Any = None,
    data: Any = None,
    stream: bool = False,
) -> requests.Response:
    parsed = urlparse(url)
    parsed_port = parsed.port
    port = parsed_port if parsed_port is not None else (443 if parsed.scheme == "https" else 80)
    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    request_headers = dict(headers or {})
    request_headers.setdefault("Host", parsed.netloc)
    try:
        if parsed.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(
                address,
                port,
                timeout=timeout,
                retries=False,
                cert_reqs=ssl.CERT_REQUIRED,
                ca_certs=requests.certs.where(),
                assert_hostname=parsed.hostname,
                server_hostname=parsed.hostname,
            )
        else:
            pool = urllib3.HTTPConnectionPool(address, port, timeout=timeout, retries=False)
        raw = pool.request(
            method,
            request_target,
            headers=request_headers,
            json=json,
            body=data,
            redirect=False,
            preload_content=False,
            retries=False,
            timeout=timeout,
        )
    except urllib3.exceptions.HTTPError as exc:
        raise requests.ConnectionError(str(exc)) from exc

    response = requests.Response()
    response.status_code = raw.status
    response.headers = CaseInsensitiveDict(raw.headers)
    response.raw = raw
    response.url = url
    response.reason = raw.reason
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    if not stream:
        response._content = _read_bounded_content(raw, max_response_bytes)
        response._content_consumed = True
    return response


def _read_bounded_content(raw: Any, max_bytes: int) -> bytes:
    length_value = getattr(raw, "headers", {}).get("Content-Length")
    if length_value is not None:
        try:
            content_length = int(length_value)
        except (TypeError, ValueError):
            content_length = None
        if content_length is not None and content_length > max_bytes:
            close = getattr(raw, "close", None)
            if callable(close):
                close()
            raise NetworkPolicyError(
                f"workflow response exceeds {max_bytes} byte limit"
            )
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = raw.read(min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise NetworkPolicyError(
                    f"workflow response exceeds {max_bytes} byte limit"
                )
            chunks.append(chunk)
    except urllib3.exceptions.HTTPError as exc:
        raise requests.ConnectionError(str(exc)) from exc
    finally:
        close = getattr(raw, "close", None)
        if callable(close):
            close()
    return b"".join(chunks)
