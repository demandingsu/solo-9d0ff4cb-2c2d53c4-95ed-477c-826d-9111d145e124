"""Read a notice directory and collect every notice's normalised URL.

This module only *reads*: it never touches the baseline.  The request layer
hands the collected rows to :mod:`app.store`, which performs the atomic
baseline comparison.  Any failure -- a listing page, a detail page, or an
incomplete crawl because a safety limit was reached -- makes the whole patrol
incomplete so the caller can leave the baseline untouched.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .crawler import (
    FETCH_TIMEOUT,
    USER_AGENT,
    _fetch,
    _same_origin,
    extract_links,
    normalize_url,
    parse_notice_html,
)

MAX_DIRECTORY_PAGES = int(os.getenv("PATROL_MAX_PAGES", "60"))
MAX_DIRECTORY_DETAILS = int(os.getenv("PATROL_MAX_DETAILS", "1500"))

# Trackers that never carry notice identity.  Dropping them keeps the same
# notice linked as /d/123 and /d/123?utm_source=wechat on one baseline key.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "fbclid", "msclkid", "spm", "from_source", "share_token",
}
DEFAULT_PORTS = {"http": "80", "https": "443"}


def directory_key(url: str) -> str:
    """Normalised form of a directory address, used as the baseline key."""

    return normalize_detail_url(url)


def normalize_detail_url(url: str) -> str:
    """Canonical detail URL used for baseline comparison.

    Scheme and host are lower-cased, the default port and fragment are
    dropped, a trailing slash is removed, tracking parameters are ignored,
    and the remaining query parameters are sorted.  The same notice linked
    twice from different pages, or through a cosmetic tracking variant,
    therefore maps to one key while id-bearing query values are preserved.
    """

    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(params), doseq=True)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))



@dataclass(frozen=True)
class PatrolFailure:
    url: str
    reason: str
    stage: str  # pagination | detail | limit | validation

    @property
    def stage_label(self) -> str:
        return {
            "pagination": "分页读取失败",
            "detail": "公告详情读取失败",
            "limit": "巡检未完成",
            "validation": "目录地址不可用",
        }.get(self.stage, "读取失败")


@dataclass(frozen=True)
class PatrolNoticeRow:
    url_key: str
    url: str
    title: str


@dataclass
class PatrolScan:
    list_url: str
    key: str
    notices: list[PatrolNoticeRow] = field(default_factory=list)
    failures: list[PatrolFailure] = field(default_factory=list)
    pages_read: list[str] = field(default_factory=list)
    details_read: int = 0
    duplicate_links: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _client_factory_default():
    timeout = httpx.Timeout(FETCH_TIMEOUT, connect=min(8.0, FETCH_TIMEOUT))
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    return httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers=headers, limits=limits
    )


async def scan_directory(list_url: str, *, client_factory=None) -> PatrolScan:
    """Walk the same-site pagination and read every linked detail page."""

    start_url = normalize_url(list_url)
    scan = PatrolScan(list_url=start_url, key=directory_key(start_url))

    queue: list[str] = [start_url]
    seen_pages: set[str] = set()
    seen_details: set[str] = set()
    notice_keys: set[str] = set()

    client = _client_factory_default() if client_factory is None else client_factory()
    owns_client = client_factory is None

    try:
        while queue:
            if len(scan.pages_read) >= MAX_DIRECTORY_PAGES:
                scan.failures.append(
                    PatrolFailure(
                        start_url,
                        f"公告目录超过 {MAX_DIRECTORY_PAGES} 页，本轮未完整巡检",
                        "limit",
                    )
                )
                break

            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            scan.pages_read.append(page_url)

            html, error, final_page_url = await _fetch(client, page_url, "list")
            if html is None:
                scan.failures.append(
                    PatrolFailure(page_url, f"公告目录页读取失败：{error}", "pagination")
                )
                continue
            if not _same_origin(final_page_url, start_url):
                scan.failures.append(
                    PatrolFailure(
                        page_url,
                        "公告目录页跳转到站外地址，已停止读取该页",
                        "pagination",
                    )
                )
                continue
            if final_page_url != page_url:
                seen_pages.add(final_page_url)

            try:
                links = extract_links(html, final_page_url)
            except Exception as exc:
                scan.failures.append(
                    PatrolFailure(
                        page_url,
                        f"公告目录页解析失败：{exc.__class__.__name__}",
                        "pagination",
                    )
                )
                continue

            for next_url in links.pagination:
                if not _same_origin(next_url, start_url):
                    continue
                if next_url in seen_pages or next_url in queue:
                    continue
                queue.append(next_url)

            if page_url == start_url and not links.detail_urls and not queue:
                scan.failures.append(
                    PatrolFailure(
                        start_url,
                        "目录首页未发现公告链接，请确认地址是否为公告目录页",
                        "validation",
                    )
                )
                continue

            candidates: list[str] = []
            for detail_url in links.detail_urls:
                key = normalize_detail_url(detail_url)
                if key in seen_details:
                    scan.duplicate_links += 1
                    continue
                seen_details.add(key)
                candidates.append(detail_url)

            remaining = MAX_DIRECTORY_DETAILS - scan.details_read
            if len(candidates) > remaining:
                candidates = candidates[: max(remaining, 0)]
                queue.clear()
                scan.failures.append(
                    PatrolFailure(
                        start_url,
                        f"公告详情超过 {MAX_DIRECTORY_DETAILS} 页，本轮未完整巡检",
                        "limit",
                    )
                )
                if not candidates:
                    break

            async def fetch_detail(url: str):
                detail_html, detail_error, final_detail_url = await _fetch(
                    client, url, "detail"
                )
                return url, detail_html, detail_error, final_detail_url

            results = await asyncio.gather(
                *(fetch_detail(url) for url in candidates)
            )

            for requested_url, detail_html, detail_error, final_detail_url in results:
                scan.details_read += 1
                if detail_html is None:
                    scan.failures.append(
                        PatrolFailure(
                            requested_url,
                            f"公告详情页读取失败：{detail_error}",
                            "detail",
                        )
                    )
                    continue
                if not _same_origin(final_detail_url, start_url):
                    scan.failures.append(
                        PatrolFailure(
                            requested_url,
                            "公告详情页跳转到站外地址，已停止读取该页",
                            "detail",
                        )
                    )
                    continue
                try:
                    parsed = parse_notice_html(detail_html, final_detail_url)
                except Exception as exc:
                    scan.failures.append(
                        PatrolFailure(
                            requested_url,
                            f"公告详情页解析失败：{exc.__class__.__name__}",
                            "detail",
                        )
                    )
                    continue

                title = parsed.title.strip()
                if not title:
                    scan.failures.append(
                        PatrolFailure(
                            requested_url,
                            "公告详情页未读取到标题，无法列入新增清单",
                            "detail",
                        )
                    )
                    continue

                try:
                    canonical = normalize_detail_url(final_detail_url)
                except ValueError:
                    scan.failures.append(
                        PatrolFailure(
                            requested_url,
                            "公告详情页地址无效，无法登记",
                            "detail",
                        )
                    )
                    continue
                if canonical in notice_keys:
                    scan.duplicate_links += 1
                    continue
                notice_keys.add(canonical)
                scan.notices.append(
                    PatrolNoticeRow(
                        url_key=canonical,
                        url=normalize_url(final_detail_url),
                        title=title,
                    )
                )

            if any(failure.stage == "limit" for failure in scan.failures):
                break
    finally:
        if owns_client:
            await client.aclose()

    return scan
