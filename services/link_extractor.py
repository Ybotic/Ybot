"""Bounded HTML metadata extraction with SSRF-resistant URL handling."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 1_500_000
MAX_REDIRECTS = 5
MAX_EXTRACTION_SECONDS = 20
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_read=5)
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
    ".test",
    ".invalid",
    ".example",
    ".arpa",
    ".onion",
)


class LinkValidationError(ValueError):
    """Raised for malformed URLs and known non-public destinations."""


class LinkExtractionError(RuntimeError):
    """Raised when metadata cannot be safely fetched or parsed."""


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    url: str
    normalized_url: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class LinkMetadata:
    title: str | None
    description: str | None
    extracted_text: str | None
    normalized_url: str


class PublicDNSResolver(aiohttp.abc.AbstractResolver):
    """Resolve hosts to public IPs only; the connector connects to these IPs."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host,
            port,
            family=family,
            type=socket.SOCK_STREAM,
        )
        resolved: dict[tuple[int, str], dict[str, Any]] = {}
        for address_family, socket_type, proto, _, sockaddr in infos:
            address = sockaddr[0].split("%", 1)[0]
            ip = ipaddress.ip_address(address)
            if not _is_public_address(ip):
                raise LinkValidationError(
                    "This URL resolves to a private or non-public network destination."
                )
            key = (address_family, str(ip))
            resolved[key] = {
                "hostname": host,
                "host": str(ip),
                "port": port,
                "family": address_family,
                "proto": proto or socket_type,
                "flags": 0,
            }
        if not resolved:
            raise socket.gaierror("No public addresses were returned")
        return list(resolved.values())

    async def close(self) -> None:
        return None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self._in_title = False
        self._skip_depth = 0
        self._text_length = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg", "iframe", "template"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = attributes.get("property", "").casefold()
            if not key:
                key = attributes.get("name", "").casefold()
            content = attributes.get("content", "")
            if key and content:
                self.meta[key] = content
        elif tag == "link":
            rel = {part.casefold() for part in attributes.get("rel", "").split()}
            href = attributes.get("href", "")
            if "canonical" in rel and href:
                self.canonical = href

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg", "iframe", "template"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        remaining = 30_000 - self._text_length
        if remaining > 0:
            part = data[:remaining]
            self.text_parts.append(part)
            self._text_length += len(part)


class LinkMetadataExtractor:
    def __init__(self) -> None:
        self._resolver = PublicDNSResolver()

    async def validate_url(self, value: str) -> ValidatedURL:
        """Validate the scheme, host, port, and any available DNS answers."""
        target = _normalize_url(value)
        try:
            ip = ipaddress.ip_address(target.host)
        except ValueError:
            if _is_internal_hostname(target.host):
                raise LinkValidationError(
                    "Localhost and internal network URLs cannot be saved."
                ) from None
            try:
                await asyncio.wait_for(
                    self._resolver.resolve(
                        target.host,
                        target.port,
                        family=socket.AF_UNSPEC,
                    ),
                    timeout=3,
                )
            except LinkValidationError:
                raise
            except (socket.gaierror, asyncio.TimeoutError):
                # A syntactically valid public hostname may be temporarily offline.
                # The connector will still enforce public-only DNS before connecting.
                pass
        else:
            if not _is_public_address(ip):
                raise LinkValidationError(
                    "Private, loopback, and non-public IP addresses cannot be saved."
                )
        return target

    async def extract(self, value: str) -> LinkMetadata:
        try:
            return await asyncio.wait_for(
                self._extract_bounded(value), timeout=MAX_EXTRACTION_SECONDS
            )
        except asyncio.TimeoutError:
            raise LinkExtractionError("Metadata extraction timed out.") from None

    async def _extract_bounded(self, value: str) -> LinkMetadata:
        target = await self.validate_url(value)
        connector = aiohttp.TCPConnector(
            resolver=self._resolver,
            use_dns_cache=False,
            limit=4,
            limit_per_host=2,
            enable_cleanup_closed=True,
        )
        headers = {
            "User-Agent": "YBOT-LinkMetadata/1.0 (+local personal assistant)",
            "Accept": "text/html,application/xhtml+xml;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        current_url = target.url
        last_target = target
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=REQUEST_TIMEOUT,
            trust_env=False,
            headers=headers,
            raise_for_status=False,
        ) as session:
            for redirect_count in range(MAX_REDIRECTS + 1):
                last_target = await self.validate_url(current_url)
                try:
                    async with session.get(
                        last_target.url,
                        allow_redirects=False,
                        max_redirects=0,
                    ) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location or redirect_count >= MAX_REDIRECTS:
                                raise LinkExtractionError("The page redirected too many times.")
                            current_url = urljoin(last_target.url, location)
                            # Validate the destination before the following request.
                            await self.validate_url(current_url)
                            continue
                        if response.status < 200 or response.status >= 300:
                            raise LinkExtractionError("The webpage returned an error response.")
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                        if content_type.casefold().strip() not in {
                            "text/html",
                            "application/xhtml+xml",
                        }:
                            raise LinkExtractionError("The URL does not serve an HTML webpage.")
                        if (
                            response.content_length is not None
                            and response.content_length > MAX_RESPONSE_BYTES
                        ):
                            raise LinkExtractionError("The webpage exceeds the size limit.")
                        body = await _read_bounded(response)
                        encoding = response.charset or "utf-8"
                        html = body.decode(encoding, errors="replace")
                        metadata = await asyncio.to_thread(_parse_html, html)
                        final_url = last_target.normalized_url
                        canonical_url = metadata.canonical
                        if canonical_url:
                            try:
                                canonical = await self.validate_url(
                                    urljoin(last_target.url, canonical_url)
                                )
                            except (LinkValidationError, ValueError):
                                canonical = None
                            if canonical is not None:
                                final_url = canonical.normalized_url
                        return LinkMetadata(
                            title=metadata.title,
                            description=metadata.description,
                            extracted_text=metadata.extracted_text,
                            normalized_url=final_url,
                        )
                except LinkValidationError:
                    raise
                except LinkExtractionError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                    raise LinkExtractionError(
                        f"Page metadata could not be fetched ({type(exc).__name__})."
                    ) from exc
        raise LinkExtractionError("The webpage could not be fetched.")


async def _read_bounded(response: aiohttp.ClientResponse) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(16_384):
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise LinkExtractionError("The webpage exceeds the size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _normalize_url(value: str) -> ValidatedURL:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise LinkValidationError("Provide a valid HTTP or HTTPS URL.")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise LinkValidationError("The URL contains invalid control characters.")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise LinkValidationError("Provide a valid HTTP or HTTPS URL.") from None
    if scheme not in {"http", "https"} or not host:
        raise LinkValidationError("Only complete HTTP and HTTPS URLs are supported.")
    if parsed.username is not None or parsed.password is not None:
        raise LinkValidationError("URLs containing usernames or passwords are not allowed.")
    host = host.rstrip(".").casefold()
    if not host or _is_internal_hostname(host):
        raise LinkValidationError("Localhost and internal network URLs cannot be saved.")
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            host_ascii = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise LinkValidationError("The URL hostname is invalid.") from None
    else:
        host_ascii = host_ip.compressed
    default_port = 80 if scheme == "http" else 443
    port = port or default_port
    if port != default_port:
        raise LinkValidationError("Only the standard HTTP and HTTPS ports are allowed.")
    try:
        ip = ipaddress.ip_address(host_ascii)
    except ValueError:
        ip = None
    if ip is None and "." not in host_ascii:
        raise LinkValidationError("Internal and single-label hostnames are not allowed.")
    netloc_host = f"[{host_ascii}]" if ":" in host_ascii else host_ascii
    netloc = netloc_host if port == default_port else f"{netloc_host}:{port}"
    path = parsed.path or "/"
    normalized = urlunsplit((scheme, netloc, path, parsed.query, ""))
    return ValidatedURL(normalized, normalized, host_ascii, port)


def _is_internal_hostname(host: str) -> bool:
    normalized = host.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(_BLOCKED_SUFFIXES):
        return True
    return False


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


@dataclass(frozen=True, slots=True)
class _ParsedPage:
    title: str | None
    description: str | None
    extracted_text: str | None
    canonical: str | None


def _parse_html(document: str) -> _ParsedPage:
    parser = _PageParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception:
        logger.info("HTML metadata parser rejected malformed markup")
    title = _clean_metadata(
        parser.meta.get("og:title") or " ".join(parser.title_parts), 300
    )
    description = _clean_metadata(
        parser.meta.get("og:description")
        or parser.meta.get("description")
        or parser.meta.get("twitter:description"),
        2000,
    )
    text = _clean_metadata(" ".join(parser.text_parts), 20_000)
    return _ParsedPage(title, description, text, parser.canonical)


def _clean_metadata(value: str | None, maximum_length: int) -> str | None:
    if not value:
        return None
    cleaned = "".join(
        char for char in value if unicodedata.category(char) not in {"Cc", "Cf"}
    )
    cleaned = re.sub(r"<[^>]{0,500}>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:maximum_length] or None
