"""Regression tests for the notice-directory patrol.

Run with:  python -m unittest discover -s tests -v

The tests use httpx's in-process mock transport and a temporary SQLite file,
so no network, demo container, or shared state is required.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

import httpx

from app.patrol import scan_directory
from app.patrol_service import run_patrol
from app.store import PatrolStore


def page_key(request: httpx.Request) -> str:
    """Identify a mocked page by path plus any query string."""

    return request.url.path + ("?" + request.url.query.decode() if request.url.query else "")


def listing(links: list[tuple[str, str]], next_href: str | None = None) -> str:
    """A same-site directory page linking to ``links`` as (href, title)."""

    rows = "".join(
        f'<li><a class="notice-link" href="{href}">{title}</a></li>'
        for href, title in links
    )
    pager = f'<a rel="next" href="{next_href}">下一页</a>' if next_href else ""
    return f"<html><body><main><ul>{rows}</ul>{pager}</main></body></html>"


def detail(title: str) -> str:
    return (
        "<html><body><article>"
        f"<h1>{title}</h1><p>{title} 正文。</p>"
        "</article></body></html>"
    )


class PatrolTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "patrol.sqlite3")
        self.store = PatrolStore(self.db_path)
        self.routes: dict[str, str] = {}
        self.failing: set[str] = set()

    def tearDown(self) -> None:
        self.store = None  # type: ignore[assignment]
        self.tempdir.cleanup()

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = page_key(request)
        if key in self.failing:
            return httpx.Response(503, text="service unavailable")
        if key not in self.routes:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200, text=self.routes[key], headers={"content-type": "text/html"}
        )

    def factory(self):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), follow_redirects=True
        )

    async def patrol(self, url: str = "http://test/list"):
        return await run_patrol(url, self.store, client_factory=self.factory)



class BaselineTests(PatrolTestBase):
    async def test_first_complete_patrol_only_builds_baseline(self):
        self.routes = {
            "/list": listing(
                [("/d/a", "公告 A"), ("/d/b", "公告 B")], "/list?page=2"
            ),
            "/list?page=2": listing([("/d/c", "公告 C")]),
            "/d/a": detail("公告 A"),
            "/d/b": detail("公告 B"),
            "/d/c": detail("公告 C"),
        }
        outcome = await self.patrol()
        self.assertEqual(outcome.status, "baseline")
        self.assertTrue(outcome.first_patrol)
        self.assertEqual(outcome.baseline_count, 3)
        self.assertEqual(outcome.new_notices, [])

        # A second patrol without changes must not repeat the baseline.
        again = await self.patrol()
        self.assertEqual(again.status, "no_new")
        self.assertEqual(again.new_notices, [])

    async def test_new_notice_is_reported_with_title_and_source(self):
        self.routes = {
            "/list": listing([("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
        }
        first = await self.patrol()
        self.assertEqual(first.status, "baseline")

        self.routes = {
            "/list": listing([("/d/new", "新公告"), ("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
            "/d/new": detail("新公告"),
        }
        second = await self.patrol()
        self.assertEqual(second.status, "new")
        self.assertEqual(len(second.new_notices), 1)
        self.assertEqual(second.new_notices[0]["title"], "新公告")
        self.assertEqual(second.new_notices[0]["url"], "http://test/d/new")

        # Nothing new on the third patrol.
        third = await self.patrol()
        self.assertEqual(third.status, "no_new")

    async def test_baseline_survives_a_new_store_instance(self):
        self.routes = {
            "/list": listing([("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
        }
        await self.patrol()
        # A fresh instance points at the same SQLite file, like a container
        # restart re-opening the mounted volume.
        reopened = PatrolStore(self.db_path)
        self.routes = {
            "/list": listing([("/d/b", "公告 B"), ("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
            "/d/b": detail("公告 B"),
        }
        outcome = await run_patrol(
            "http://test/list", reopened, client_factory=self.factory
        )
        self.assertEqual(outcome.status, "new")
        self.assertEqual([item["title"] for item in outcome.new_notices], ["公告 B"])


class ReorderingTests(PatrolTestBase):
    async def test_insertions_that_shift_old_notices_do_not_report_old_ones(self):
        old = [("/d/a", "旧公告 A"), ("/d/b", "旧公告 B")]
        self.routes = {
            "/list": listing(old, "/list?page=2"),
            "/list?page=2": listing([("/d/c", "旧公告 C")]),
            "/d/a": detail("旧公告 A"),
            "/d/b": detail("旧公告 B"),
            "/d/c": detail("旧公告 C"),
        }
        await self.patrol()

        # Three insertions push every old notice past a page boundary.
        self.routes = {
            "/list": listing(
                [
                    ("/d/n1", "新公告 1"),
                    ("/d/n2", "新公告 2"),
                    ("/d/n3", "新公告 3"),
                ],
                "/list?page=2",
            ),
            "/list?page=2": listing(
                [("/d/n4", "新公告 4"), *old], "/list?page=3"
            ),
            "/list?page=3": listing([("/d/c", "旧公告 C")]),
            "/d/n1": detail("新公告 1"),
            "/d/n2": detail("新公告 2"),
            "/d/n3": detail("新公告 3"),
            "/d/n4": detail("新公告 4"),
            "/d/a": detail("旧公告 A"),
            "/d/b": detail("旧公告 B"),
            "/d/c": detail("旧公告 C"),
        }
        outcome = await self.patrol()
        self.assertEqual(outcome.status, "new")
        self.assertEqual(
            sorted(item["title"] for item in outcome.new_notices),
            ["新公告 1", "新公告 2", "新公告 3", "新公告 4"],
        )
        self.assertNotIn("旧公告 A", [item["title"] for item in outcome.new_notices])

    async def test_same_notice_linked_from_two_pages_is_reported_once(self):
        self.routes = {
            "/list": listing([("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
        }
        await self.patrol()

        self.routes = {
            "/list": listing([("/d/dup", "重复公告"), ("/d/a", "公告 A")], "/list?page=2"),
            "/list?page=2": listing([("/d/dup", "重复公告"), ("/d/b", "公告 B")]),
            "/d/dup": detail("重复公告"),
            "/d/a": detail("公告 A"),
            "/d/b": detail("公告 B"),
        }
        outcome = await self.patrol()
        self.assertEqual(outcome.status, "new")
        titles = sorted(item["title"] for item in outcome.new_notices)
        self.assertEqual(titles, ["公告 B", "重复公告"])

        again = await self.patrol()
        self.assertEqual(again.status, "no_new")
        self.assertEqual(again.new_notices, [])

    async def test_normalised_url_deduplicates_query_and_fragment_variants(self):
        self.routes = {
            "/list": listing(
                [("/d/a?b=2&a=1", "公告 A"), ("/d/a?a=1&b=2#top", "公告 A 重复链接")]
            ),
            "/d/a?b=2&a=1": detail("公告 A"),
            "/d/a?a=1&b=2": detail("公告 A 重复链接"),
        }
        outcome = await self.patrol()
        self.assertEqual(outcome.status, "baseline")
        self.assertEqual(outcome.baseline_count, 1)
        self.assertEqual(outcome.duplicate_links, 1)


class FailureTests(PatrolTestBase):
    async def test_pagination_failure_reports_url_and_leaves_baseline_untouched(self):
        self.routes = {
            "/list": listing([("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
        }
        await self.patrol()

        self.routes = {
            "/list": listing([("/d/new", "新公告"), ("/d/a", "公告 A")], "/list?page=2"),
            "/d/a": detail("公告 A"),
            "/d/new": detail("新公告"),
            "/d/only-on-page-2": detail("只在新页的公告"),
        }
        self.failing = {"/list?page=2"}
        outcome = await self.patrol()
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.new_notices, [])
        failed_urls = [failure["url"] for failure in outcome.failures]
        self.assertIn("http://test/list?page=2", failed_urls)
        self.assertEqual(outcome.failures[0]["stage"], "pagination")

        # The new notice from page 1 is not baselined yet: retrying after the
        # failure is fixed must report it.
        self.failing = set()
        self.routes["/list?page=2"] = listing([("/d/b", "公告 B")])
        self.routes["/d/b"] = detail("公告 B")
        retry = await self.patrol()
        self.assertEqual(retry.status, "new")
        titles = sorted(item["title"] for item in retry.new_notices)
        self.assertEqual(titles, ["公告 B", "新公告"])

    async def test_detail_failure_reports_url_and_retry_reports_it(self):
        self.routes = {
            "/list": listing([("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
        }
        await self.patrol()

        self.routes = {
            "/list": listing([("/d/new", "新公告"), ("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
            "/d/new": detail("新公告"),
        }
        self.failing = {"/d/new"}
        outcome = await self.patrol()
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.new_notices, [])
        self.assertEqual(outcome.failures[0]["stage"], "detail")
        self.assertEqual(outcome.failures[0]["url"], "http://test/d/new")

        self.failing = set()
        retry = await self.patrol()
        self.assertEqual(retry.status, "new")
        self.assertEqual([item["title"] for item in retry.new_notices], ["新公告"])

    async def test_failure_on_first_ever_patrol_does_not_create_baseline(self):
        self.routes = {
            "/list": listing([("/d/a", "公告 A"), ("/d/b", "公告 B")], "/list?page=2"),
            "/list?page=2": listing([("/d/b", "公告 B")]),
            "/d/a": detail("公告 A"),
            "/d/b": detail("公告 B"),
        }
        self.failing = {"/d/a"}
        outcome = await self.patrol()
        self.assertEqual(outcome.status, "failed")
        state = self.store.get_state("http://test/list")
        self.assertFalse(state.baseline_complete)
        self.assertEqual(state.baseline_count, 0)

        self.failing = set()
        retry = await self.patrol()
        self.assertEqual(retry.status, "baseline")
        self.assertEqual(retry.baseline_count, 2)

    async def test_invalid_directory_address_is_reported(self):
        outcome = await run_patrol("ftp://example.com/list", self.store)
        self.assertEqual(outcome.status, "invalid")
        self.assertEqual(outcome.failures[0]["stage"], "validation")


class ConcurrencyTests(PatrolTestBase):
    async def test_concurrent_patrols_report_a_notice_only_once(self):
        self.routes = {
            "/list": listing([("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
        }
        await self.patrol()

        self.routes = {
            "/list": listing([("/d/new", "新公告"), ("/d/a", "公告 A")]),
            "/d/a": detail("公告 A"),
            "/d/new": detail("新公告"),
        }

        async def one():
            async with self.factory() as client:
                return await run_patrol(
                    "http://test/list", self.store, client_factory=lambda: client
                )

        outcomes = await asyncio.gather(one(), one(), one(), one())
        newly_reported = [
            (item["url"], item["title"])
            for outcome in outcomes
            for item in outcome.new_notices
        ]
        self.assertEqual(newly_reported, [("http://test/d/new", "新公告")])
        statuses = sorted(outcome.status for outcome in outcomes)
        self.assertEqual(statuses, ["new", "no_new", "no_new", "no_new"])


class ScanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.routes: dict[str, str] = {}
        self.failing: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = page_key(request)
        if key in self.failing:
            return httpx.Response(503, text="down")
        if key not in self.routes:
            return httpx.Response(404, text="missing")
        return httpx.Response(
            200, text=self.routes[key], headers={"content-type": "text/html"}
        )

    def factory(self):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), follow_redirects=True
        )

    async def test_scan_reads_same_site_pagination_only(self):
        self.routes = {
            "/list": listing(
                [("/d/a", "公告 A"), ("https://other.example/d/x", "站外公告")],
                "/list?page=2",
            ),
            "/list?page=2": listing([("/d/b", "公告 B")]),
            "/d/a": detail("公告 A"),
            "/d/b": detail("公告 B"),
        }
        scan = await scan_directory("http://test/list", client_factory=self.factory)
        self.assertTrue(scan.ok)
        self.assertEqual(len(scan.notices), 2)
        self.assertEqual(scan.details_read, 2)
        self.assertEqual(len(scan.pages_read), 2)
        self.assertNotIn("other.example", " ".join(row.url for row in scan.notices))


if __name__ == "__main__":
    unittest.main()
