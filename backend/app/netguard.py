"""Защита от SSRF: запрещаем ходить во внутренние сети, если это явно не разрешено."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from .config import get_settings


async def check_url(url: str) -> None:
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Допустимы только http(s)-адреса")
    if get_settings().allow_private_urls:
        return
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, p.hostname, p.port or 443)
    except socket.gaierror as e:
        raise ValueError(f"Не удалось определить адрес {p.hostname}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Адрес во внутренней сети запрещён (ALLOW_PRIVATE_URLS=false)")
