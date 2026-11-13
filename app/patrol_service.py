"""Orchestration for a single notice-directory patrol."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .patrol import PatrolFailure, PatrolScan, directory_key, normalize_url, scan_directory
from .store import NoticeRow, PatrolState, PatrolStore


@dataclass
class PatrolOutcome:
    list_url: str
    key: str
    status: str  # baseline | new | no_new | failed | invalid
    first_patrol: bool
    baseline_count: int
    new_notices: list[dict[str, str | None]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    pages_read: list[str] = field(default_factory=list)
    detail_count: int = 0
    duplicate_links: int = 0
    warnings: list[str] = field(default_factory=list)
    state: PatrolState = field(default_factory=PatrolState)
    recent_additions: list[dict[str, str | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"baseline", "new", "no_new"}

    def as_dict(self, public_url_fn=None) -> dict:
        def pub(url):
            return public_url_fn(url) if public_url_fn and url else url

        return {
            "input_url": self.list_url,
            "status": self.status,
            "ok": self.ok,
            "first_patrol": self.first_patrol,
            "baseline_count": self.baseline_count,
            "new_count": len(self.new_notices),
            "new_notices": [
                {
                    "url": pub(item["url"]),
                    "raw_url": item["url"],
                    "title": item["title"],
                }
                for item in self.new_notices
            ],
            "failures": [
                {
                    "url": pub(item["url"]) or item["url"],
                    "raw_url": item["url"],
                    "reason": item["reason"],
                    "stage": item["stage"],
                    "stage_label": item["stage_label"],
                }
                for item in self.failures
            ],
            "pages_read": [pub(url) or url for url in self.pages_read],
            "detail_page_count": self.detail_count,
            "duplicate_links": self.duplicate_links,
            "warnings": self.warnings,
            "baseline": {
                "exists": self.state.exists,
                "complete": self.state.baseline_complete,
                "count": self.state.baseline_count,
                "first_patrol_at": self.state.first_patrol_at,
                "last_patrol_at": self.state.last_patrol_at,
                "last_success_at": self.state.last_success_at,
                "last_status": self.state.last_status,
            },
            "recent_additions": [
                {
                    "url": pub(item["url"]),
                    "title": item["title"],
                    "detected_at": item["detected_at"],
                }
                for item in self.recent_additions
            ],
        }


def _failure_dict(failure: PatrolFailure) -> dict[str, str]:
    return {
        "url": failure.url,
        "reason": failure.reason,
        "stage": failure.stage,
        "stage_label": failure.stage_label,
    }


async def run_patrol(
    list_url: str,
    store: PatrolStore,
    *,
    client_factory=None,
) -> PatrolOutcome:
    """Run one patrol and atomically update the persistent baseline.

    A scan with any failure is recorded as failed and the notice baseline is
    left untouched.  The next successful patrol therefore compares against the
    last complete baseline and reports everything missed in between.
    """

    run_id = uuid.uuid4().hex
    raw_url = list_url

    try:
        normalized = normalize_url(list_url)
    except ValueError as exc:
        failure = PatrolFailure(raw_url, str(exc), "validation")
        return PatrolOutcome(
            list_url=raw_url,
            key="",
            status="invalid",
            first_patrol=False,
            baseline_count=0,
            failures=[_failure_dict(failure)],
        )

    key = directory_key(normalized)

    scan: PatrolScan = await scan_directory(normalized, client_factory=client_factory)

    if scan.ok and not scan.notices:
        # An empty directory page is more likely a broken listing than a real
        # empty catalogue.  Treat it as an incomplete patrol so it can never
        # reset or silently advance the baseline.
        scan.failures.append(
            PatrolFailure(
                normalized,
                "目录中未读取到任何公告详情页，本轮未完成巡检",
                "validation",
            )
        )

    if not scan.ok:
        note = scan.failures[0].reason if scan.failures else "目录读取未完成"
        await store.mark_failure_async(
            key, normalized, len(scan.failures), note, run_id
        )
        state_after = await store.get_state_async(key)
        return PatrolOutcome(
            list_url=normalized,
            key=key,
            status="failed",
            first_patrol=False,
            baseline_count=state_after.baseline_count,
            failures=[_failure_dict(failure) for failure in scan.failures],
            pages_read=scan.pages_read,
            detail_count=scan.details_read,
            duplicate_links=scan.duplicate_links,
            warnings=scan.warnings,
            state=state_after,
            recent_additions=await store.recent_additions_async(key),
        )

    result = await store.commit_success_async(
        key,
        normalized,
        [
            NoticeRow(url_key=row.url_key, url=row.url, title=row.title)
            for row in scan.notices
        ],
        run_id,
    )
    state_after = await store.get_state_async(key)

    if result.first_patrol:
        status = "baseline"
    elif result.new_notices:
        status = "new"
    else:
        status = "no_new"

    warnings = list(scan.warnings)

    return PatrolOutcome(
        list_url=normalized,
        key=key,
        status=status,
        first_patrol=result.first_patrol,
        baseline_count=result.baseline_count,
        new_notices=[
            {"url": row.url, "title": row.title} for row in result.new_notices
        ],
        failures=[],
        pages_read=scan.pages_read,
        detail_count=scan.details_read,
        duplicate_links=scan.duplicate_links,
        warnings=warnings,
        state=state_after,
        recent_additions=await store.recent_additions_async(key),
    )
