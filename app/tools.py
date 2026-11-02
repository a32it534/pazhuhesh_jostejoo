"""Research tools: web search (with fallback chain) and safe page reading.

Tools never raise: they always return a dict {"ok": bool, ...} so the pipeline can continue.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import Settings

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PazhuheshJostejoo/1.0"
)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_CACHED_CHARS = 20000
MAX_REDIRECTS = 4
SKIP_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".rar", ".mp4", ".mp3")

ToolResult = dict[str, Any]


def _ok(**data: Any) -> ToolResult:
    return {"ok": True, **data}


def _fail(error: str, code: str = "error") -> ToolResult:
    return {"ok": False, "error": error, "code": code}


# ------------------------------------------------------------------ SSRF guard

def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def check_url_safety(url: str, resolve: bool = True) -> tuple[bool, str]:
    """Return (is_safe, reason). Rejects non-http(s), localhost and private/reserved IPs.

    With resolve=True the hostname is resolved and *every* returned address must be public.
    Note: a DNS-rebinding race between this check and the actual connection is still
    theoretically possible; run the app on a machine without sensitive internal services.
    """
    try:
        parts = urlparse(url.strip())
        port = parts.port
    except ValueError:
        return False, "آدرس نامعتبر است."
    if parts.scheme not in ("http", "https"):
        return False, "فقط آدرس‌های http و https مجاز هستند."
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False, "آدرس نامعتبر است."
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return False, "آدرس‌های محلی مجاز نیستند."

    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:  # catches obfuscated IPv4 forms such as 2130706433, 0x7f.1, 127.1
            literal = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
        except (OSError, ValueError):
            literal = None
    if literal is not None:
        if not _ip_is_public(literal):
            return False, "آدرس‌های خصوصی یا محلی مجاز نیستند."
        return True, ""

    if resolve:
        try:
            infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return False, "دامنه پیدا نشد."
        if not infos:
            return False, "دامنه پیدا نشد."
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False, "آدرس نامعتبر است."
            if not _ip_is_public(ip):
                return False, "این دامنه به یک آدرس خصوصی اشاره می‌کند و مجاز نیست."
    return True, ""


# ------------------------------------------------------------------ parsing helpers

def clean_ddg_url(href: str) -> str | None:
    """Unwrap DuckDuckGo redirect links (//duckduckgo.com/l/?uddg=...)."""
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        if parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            href = unquote(target)
        else:
            return None  # ads / internal links
    if urlparse(href).scheme not in ("http", "https"):
        return None
    return href


def _url_key(url: str) -> str:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/')}"


def dedupe_results(results: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in results:
        key = _url_key(item["url"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def decode_html(body: bytes, content_type: str = "") -> str:
    """Decode bytes to str, handling Persian pages (UTF-8 first, then declared charset, then cp1256)."""
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    declared: list[str] = []
    match = re.search(r"charset=([\w\-]+)", content_type, re.IGNORECASE)
    if match:
        declared.append(match.group(1))
    head = body[:4096].decode("ascii", errors="ignore")
    meta = re.search(r"<meta[^>]+charset=[\"']?\s*([\w\-]+)", head, re.IGNORECASE)
    if meta:
        declared.append(meta.group(1))
    for enc in [*declared, "windows-1256"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def extract_text(html: str, max_chars: int = MAX_CACHED_CHARS) -> tuple[str, str]:
    """Return (title, clean_text) with boilerplate tags removed."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "noscript", "form", "svg", "iframe", "template"]):
        tag.decompose()

    def _clean(root: Any) -> str:
        raw = root.get_text("\n")
        lines: list[str] = []
        for line in raw.replace("\u00a0", " ").splitlines():
            line = re.sub(r"[ \t]+", " ", line).strip()
            if len(line) >= 20:  # drop menu fragments; ZWNJ is preserved on purpose
                lines.append(line)
        return "\n".join(lines)

    text = ""
    for root in (soup.find("article"), soup.find("main"), soup.body, soup):
        if root is None:
            continue
        text = _clean(root)
        if len(text) >= 300:
            break
    if len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars)
        text = text[: cut if cut > max_chars * 0.8 else max_chars]
    return title[:200], text


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind(" ", 0, max_chars)
    return text[: cut if cut > max_chars * 0.8 else max_chars], True


# ------------------------------------------------------------------ tools

class WebTools:
    """Per-job tool set. Each job creates its own instance, so the page cache is never shared."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, ToolResult] = {}

    # ---------------- search

    async def search_web(self, query: str, max_results: int = 6) -> ToolResult:
        try:
            providers: list[tuple[str, Any]] = [
                ("DuckDuckGo", self._search_ddg),
                ("DuckDuckGo-Lite", self._search_ddg_lite),
            ]
            if self.settings.search_fallback_url:
                providers.append(("SearXNG", self._search_searxng))
            providers += [
                ("Wikipedia-fa", lambda q: self._search_wikipedia(q, "fa")),
                ("Wikipedia-en", lambda q: self._search_wikipedia(q, "en")),
            ]
            failed: list[str] = []
            for name, func in providers:
                try:
                    results = dedupe_results(await func(query))[:max_results]
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{name}: {type(exc).__name__}")
                    continue
                if results:
                    return _ok(results=results, provider=name, query=query)
                failed.append(f"{name}: بدون نتیجه")
            if all("بدون نتیجه" not in f for f in failed):
                return _fail("اتصال به هیچ موتور جستجویی برقرار نشد (اینترنت/ را بررسی کنید): "
                             + "، ".join(failed), "network")
            return _fail("نتیجه‌ای برای این جستجو پیدا نشد. (" + "، ".join(failed) + ")", "no_results")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"خطای غیرمنتظره در جستجو: {type(exc).__name__}")

    async def _search_ddg(self, query: str) -> list[dict[str, str]]:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8",
                   "Referer": "https://html.duckduckgo.com/"}
        async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
            resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
            resp.raise_for_status()
        if resp.status_code == 202 or "anomaly" in resp.text[:5000]:
            raise RuntimeError("blocked by anti-bot page")
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict[str, str]] = []
        for block in soup.select("div.result"):
            if "result--ad" in (block.get("class") or []):
                continue
            link = block.select_one("a.result__a")
            if link is None:
                continue
            url = clean_ddg_url(link.get("href", ""))
            if not url:
                continue
            snippet = block.select_one(".result__snippet")
            results.append({
                "title": link.get_text(" ", strip=True),
                "url": url,
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
            })
        return results

    async def _search_ddg_lite(self, query: str) -> list[dict[str, str]]:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8"}
        async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
            resp = await client.post("https://lite.duckduckgo.com/lite/", data={"q": query})
            resp.raise_for_status()
        if resp.status_code == 202 or "anomaly" in resp.text[:5000]:
            raise RuntimeError("blocked by anti-bot page")
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict[str, str]] = []
        for link in soup.select("a.result-link"):
            url = clean_ddg_url(link.get("href", ""))
            if url:
                results.append({"title": link.get_text(" ", strip=True), "url": url, "snippet": ""})
        return results

    async def _search_wikipedia(self, query: str, lang: str) -> list[dict[str, str]]:
        """Keyless, rarely-blocked fallback. Returns encyclopedia articles."""
        params = {"action": "query", "list": "search", "srsearch": query,
                  "format": "json", "srlimit": "8", "utf8": "1"}
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(f"https://{lang}.wikipedia.org/w/api.php", params=params)
            resp.raise_for_status()
            payload = resp.json()
        results: list[dict[str, str]] = []
        for item in payload.get("query", {}).get("search", []):
            title = item.get("title", "")
            if not title:
                continue
            snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text(" ", strip=True)
            results.append({
                "title": f"{title} — ویکی‌پدیا",
                "url": f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                "snippet": snippet,
            })
        return results

    async def _search_searxng(self, query: str) -> list[dict[str, str]]:
        base = self.settings.search_fallback_url
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(f"{base}/search", params={"q": query, "format": "json"})
            resp.raise_for_status()
            payload = resp.json()
        results: list[dict[str, str]] = []
        for item in payload.get("results", []):
            url = item.get("url", "")
            if urlparse(url).scheme in ("http", "https"):
                results.append({"title": item.get("title", "") or url, "url": url,
                                "snippet": item.get("content", "") or ""})
        return results

    # ---------------- read

    async def read_webpage(self, url: str, max_chars: int = 4000) -> ToolResult:
        try:
            cached = self._cache.get(url)
            if cached is None:
                cached = await asyncio.wait_for(self._download(url), timeout=30)
                if cached["ok"]:
                    self._cache[url] = cached
            if not cached["ok"]:
                return cached
            text, truncated = _truncate(cached["text"], max_chars)
            return _ok(url=cached["url"], title=cached["title"], text=text, truncated=truncated)
        except asyncio.TimeoutError:
            return _fail("دانلود صفحه بیش از حد طول کشید.", "timeout")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"خطای غیرمنتظره در خواندن صفحه: {type(exc).__name__}")

    async def _download(self, url: str) -> ToolResult:
        current = url.strip()
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8",
                   "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers=headers,
                                         follow_redirects=False) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    safe, reason = await asyncio.to_thread(check_url_safety, current)
                    if not safe:
                        return _fail(reason, "blocked")
                    async with client.stream("GET", current) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            location = resp.headers.get("location")
                            if not location:
                                return _fail("ریدایرکت نامعتبر.", "http")
                            current = urljoin(current, location)
                            continue
                        if resp.status_code >= 400:
                            return _fail(f"سرور خطای {resp.status_code} برگرداند.", "http")
                        ctype = resp.headers.get("content-type", "").lower()
                        if ctype and not any(t in ctype for t in ("text/html", "application/xhtml", "text/plain")):
                            return _fail("نوع محتوای صفحه پشتیبانی نمی‌شود.", "unsupported")
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in resp.aiter_bytes():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size >= MAX_DOWNLOAD_BYTES:
                                break
                        body = b"".join(chunks)[:MAX_DOWNLOAD_BYTES]
                        final_url = current
                        break
                else:
                    return _fail("تعداد ریدایرکت‌ها بیش از حد است.", "http")
        except httpx.TimeoutException:
            return _fail("زمان دریافت صفحه تمام شد.", "timeout")
        except httpx.HTTPError:
            return _fail("دریافت صفحه ناموفق بود (اتصال).", "network")

        html = decode_html(body, ctype)
        title, text = await asyncio.to_thread(extract_text, html, MAX_CACHED_CHARS)
        if len(text) < 100:
            return _fail("صفحه متن قابل‌استفاده‌ای نداشت.", "empty")
        return _ok(url=final_url, title=title, text=text)
