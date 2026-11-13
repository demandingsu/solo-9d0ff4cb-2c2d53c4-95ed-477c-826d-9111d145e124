"""A tiny, fully local notice board used for the offline demo."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import patrol_data
from .data import NOTICE_BY_SLUG, PAGE_SIZE, TYPE_LABELS, notices_on_page, page_count

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="采购公告示例站", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse(url="/notices?page=1", status_code=307)


@app.get("/notices")
async def notice_list(request: Request, page: int = 1):
    total_pages = page_count()
    if page < 1 or page > total_pages:
        raise HTTPException(status_code=404, detail="公告列表页不存在")

    notices = notices_on_page(page)
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "notices": notices,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": total_pages,
            "total_count": sum(1 for _ in NOTICE_BY_SLUG.values()),
            "type_labels": TYPE_LABELS,
            "site_name": os.getenv("DEMO_SITE_NAME", "采购公告示例站"),
        },
    )


@app.get("/notices/detail/{slug}")
async def notice_detail(request: Request, slug: str):
    notice = NOTICE_BY_SLUG.get(slug)
    if notice is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"type_labels": TYPE_LABELS},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"notice": notice, "type_labels": TYPE_LABELS},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "demo-site"}


# ---------------------------------------------------------------------------
# Switchable directory used by the patrol demo
# ---------------------------------------------------------------------------


@app.get("/patrol")
async def patrol_root() -> RedirectResponse:
    return RedirectResponse(url="/patrol/notices?page=1", status_code=307)


@app.get("/patrol/scenario/{key}")
async def patrol_set_scenario(key: str, page: int = 1):
    if not patrol_data.set_scenario(key):
        raise HTTPException(status_code=404, detail="未知的演示场景")
    return RedirectResponse(url=f"/patrol/notices?page={page}", status_code=303)


@app.post("/patrol/scenario/{key}")
async def patrol_set_scenario_post(key: str, page: int = 1):
    return await patrol_set_scenario(key, page)


@app.get("/patrol/notices")
async def patrol_notice_list(request: Request, page: int = 1):
    total_pages = patrol_data.total_pages()
    if patrol_data.page_fails(page):
        return Response(
            "<h1>503 演示故障页</h1>"
            "<p>第 2 页公告目录被模拟为暂时不可用。"
            "本轮巡检应显示失败地址，且不报新增、不改基准。</p>",
            status_code=503,
            media_type="text/html; charset=utf-8",
        )
    if page < 1 or page > total_pages:
        raise HTTPException(status_code=404, detail="公告列表页不存在")

    notices = patrol_data.notices_on_page(page)
    return templates.TemplateResponse(
        request,
        "patrol_list.html",
        patrol_data.scenario_context()
        | {
            "notices": notices,
            "page": page,
            "page_size": patrol_data.PAGE_SIZE,
            "total_pages": total_pages,
            "total_count": patrol_data.total_count(),
            "site_name": os.getenv("DEMO_SITE_NAME", "采购公告示例站"),
        },
    )


@app.get("/patrol/notices/detail/{slug}")
async def patrol_notice_detail(request: Request, slug: str):
    notice = patrol_data.ALL_NOTICES.get(slug)
    if notice is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            patrol_data.scenario_context(),
            status_code=404,
        )
    if patrol_data.detail_fails(slug):
        return Response(
            "<h1>503 演示故障页</h1>"
            "<p>该公告详情被模拟为暂时不可用。"
            "本轮巡检应显示这条失败地址，且不报新增、不改基准。</p>",
            status_code=503,
            media_type="text/html; charset=utf-8",
        )
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"notice": notice, "type_labels": TYPE_LABELS},
    )

