"""Защита от SSRF: запрещаем ходить во внутренние сети, если это явно не разрешено.

GuardedAsyncTransport подставляет уже проверенный IP в соединение (Host/SNI = исходный
hostname), чтобы исключить DNS-rebinding между проверкой и connect (TOCTOU).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from .config import get_settings


async def check_url(url: str) -> tuple[str, list[str]]:
    """Проверяет URL и возвращает (url, resolved_ips).

    resolved_ips — уже провалидированные адреса (пусто, если ALLOW_PRIVATE_URLS=true).
    Для исходящих запросов предпочтите guarded_client() / GuardedAsyncTransport —
    они сами закрепляют IP и выставляют Host/SNI.
    """
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Допустимы только http(s)-адреса")
    if get_settings().allow_private_urls:
        return url, []
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = await asyncio.to_thread(socket.getaddrinfo, p.hostname, port)
    except socket.gaierror as e:
        raise ValueError(f"Не удалось определить адрес {p.hostname}: {e}")
    resolved_ips: list[str] = []
    for info in infos:
        addr_str = info[4][0]
        ip = ipaddress.ip_address(addr_str)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Адрес во внутренней сети запрещён (ALLOW_PRIVATE_URLS=false)")
        resolved_ips.append(addr_str)
    if not resolved_ips:
        raise ValueError(f"Не удалось определить адрес {p.hostname}")
    return url, resolved_ips


def _pick_ip(ips: list[str]) -> str:
    """Предпочитаем IPv4 — проще для Host/URL и совместимее с прокси."""
    for ip in ips:
        if ipaddress.ip_address(ip).version == 4:
            return ip
    return ips[0]


def _host_header(url: httpx.URL) -> str:
    host = url.host
    port = url.port
    if port is None:
        return host
    default = 443 if url.scheme == "https" else 80
    if port == default:
        return host
    return f"{host}:{port}"


class GuardedAsyncTransport(httpx.AsyncBaseTransport):
    """Async-транспорт: check_url на каждый запрос, connect к pinned IP."""

    def __init__(self, **transport_kwargs):
        self._inner = httpx.AsyncHTTPTransport(**transport_kwargs)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _, ips = await check_url(str(request.url))
        if not ips:
            return await self._inner.handle_async_request(request)

        hostname = request.url.host
        if not hostname:
            raise ValueError("Допустимы только http(s)-адреса")

        ip = _pick_ip(ips)
        pinned_url = request.url.copy_with(host=ip)
        headers = request.headers.copy()
        headers["Host"] = _host_header(request.url)
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = hostname

        pinned = httpx.Request(
            method=request.method,
            url=pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient с GuardedAsyncTransport (SSRF + pin IP)."""
    if "transport" in kwargs:
        raise TypeError("guarded_client задаёт transport сам; не передавайте transport=")
    return httpx.AsyncClient(transport=GuardedAsyncTransport(), **kwargs)
