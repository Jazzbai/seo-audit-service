"""Public-only, DNS-pinned HTTP access shared by observations and connectors."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


def _effective_port(url):
    """Return the port that is part of an HTTP(S) URL's authority."""

    if url.port is not None:
        return url.port
    return 443 if url.scheme == 'https' else 80


async def public_addresses(host, port):
    rows = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(r[4][0] for r in rows))
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise ValueError('The URL resolves to a private or reserved network')
    return addresses


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self, origin=None, **kwargs):
        self.inner = httpx.AsyncHTTPTransport(retries=0,trust_env=False)
        self.origin = urlsplit(origin) if origin else None

    async def handle_async_request(self, request):
        url = request.url
        if url.scheme not in ('https', 'http') or url.port not in (None, 80, 443) or url.username or url.password:
            raise ValueError('Only public HTTP(S) ports are supported')
        if self.origin and (
            url.host != self.origin.hostname
            or url.scheme != self.origin.scheme
            or _effective_port(url) != _effective_port(self.origin)
        ):
            raise ValueError('Connector requests cannot change authority')
        addresses = await public_addresses(url.host, url.port or (443 if url.scheme == 'https' else 80))
        extensions = dict(request.extensions)
        extensions['sni_hostname'] = url.host
        pinned = httpx.Request(request.method, url.copy_with(host=addresses[0]),
                               headers=request.headers, stream=request.stream, extensions=extensions)
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()


async def fetch(url, transport=None, max_bytes=2_000_000):
    origin = urlsplit(url)
    async with httpx.AsyncClient(transport=transport or PublicTransport(), trust_env=False,
                                timeout=20, follow_redirects=False,
                                headers={'User-Agent':'ForgeSEOPlatform/1.0 (+site-owner monitoring)'}) as client:
        current = url
        for _ in range(6):
            async with client.stream('GET', current) as response:
                if response.is_redirect:
                    next_url = response.url.join(response.headers.get('location',''))
                    if (
                        next_url.host != origin.hostname
                        or next_url.scheme != origin.scheme
                        or _effective_port(next_url) != _effective_port(origin)
                    ):
                        raise ValueError('Unexpected redirect outside the selected site')
                    current = str(next_url)
                    continue
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError('Page exceeds observation size limit')
                return {'url':current,'status_code':response.status_code,
                        'html':body.decode(response.encoding or 'utf-8', errors='replace'),
                        'headers':dict(response.headers)}
        raise ValueError('Too many redirects')
