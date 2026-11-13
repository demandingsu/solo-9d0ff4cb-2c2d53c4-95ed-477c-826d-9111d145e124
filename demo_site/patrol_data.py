"""Switchable fixture for the notice-directory patrol demo.

The checker demo at ``/notices`` is intentionally left untouched.  This
separate directory at ``/patrol/notices`` lets a browser user flip between
scenarios between two patrols:

* ``baseline``   - first patrol, establishes the SQLite baseline only
* ``new``        - one notice appended at the top of page 1
* ``shift``      - four notices inserted, pushing every old notice to later pages
* ``duplicate``  - one new notice linked from both page 1 and page 2
* ``fail_page2`` - same data as ``new`` but listing page 2 returns 503
* ``fail_detail``- same data as ``new`` but the new notice's detail returns 503

Switching a failure scenario back to ``new`` is the "retry" step: the patrol
must then report the notice that the failed run could not safely baseline.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .data import Notice, TYPE_LABELS

PAGE_SIZE = 3


def _notice(
    slug: str,
    project_no: str,
    section_no: str,
    notice_type: str,
    title: str,
    published_at: str,
    deadline: str | None,
    deadline_label: str | None,
    body: tuple[str, ...],
    project_name: str,
) -> Notice:
    return Notice(
        slug=slug,
        project_no=project_no,
        section_no=section_no,
        notice_type=notice_type,
        title=title,
        published_at=published_at,
        deadline=deadline,
        deadline_label=deadline_label,
        body=body,
        project_name=project_name,
    )


# Newest first.  Three per page.
BASE: tuple[Notice, ...] = (
    _notice(
        "patrol-101-original", "PRJ-2026-101", "01", "original",
        "东港污水处理站运维服务（01包）采购公告", "2026-11-09 09:00",
        "2026-12-01 09:30", "投标截止时间",
        (
            "东港污水处理站运维服务（01包）进行公开招标。",
            "项目编号：PRJ-2026-101；包号：01。",
            "投标截止时间：2026年12月01日 09:30。",
        ),
        "东港污水处理站运维服务",
    ),
    _notice(
        "patrol-102-original", "PRJ-2026-102", "A", "original",
        "南湖学校食堂设备采购项目（A包）招标公告", "2026-11-08 10:00",
        "2026-12-02 14:00", "投标截止时间",
        (
            "南湖学校食堂设备采购项目（A包）进行公开招标。",
            "项目编号：PRJ-2026-102；包号：A。",
            "投标截止时间：2026年12月02日 14:00。",
        ),
        "南湖学校食堂设备采购项目",
    ),
    _notice(
        "patrol-103-original", "PRJ-2026-103", "B", "original",
        "滨河智慧园区绿化养护项目（B包）询价公告", "2026-11-07 15:00",
        "2026-12-03 10:00", "报价截止时间",
        (
            "滨河智慧园区绿化养护项目（B包）进行询价采购。",
            "项目编号：PRJ-2026-103；包号：B。",
            "报价截止时间：2026年12月03日 10:00。",
        ),
        "滨河智慧园区绿化养护项目",
    ),
    _notice(
        "patrol-104-original", "PRJ-2026-104", "C", "original",
        "北岸道路照明提升项目（C包）竞争性磋商公告", "2026-11-06 09:30",
        "2026-12-04 14:30", "响应文件提交截止时间",
        (
            "北岸道路照明提升项目（C包）采用竞争性磋商方式采购。",
            "项目编号：PRJ-2026-104；包号：C。",
            "响应文件提交截止时间：2026年12月04日 14:30。",
        ),
        "北岸道路照明提升项目",
    ),
    _notice(
        "patrol-105-original", "PRJ-2026-105", "D", "original",
        "西岭街道办公家具采购项目（D包）招标公告", "2026-11-05 11:00",
        "2026-12-05 09:30", "投标截止时间",
        (
            "西岭街道办公家具采购项目（D包）进行公开招标。",
            "项目编号：PRJ-2026-105；包号：D。",
            "投标截止时间：2026年12月05日 09:30。",
        ),
        "西岭街道办公家具采购项目",
    ),
    _notice(
        "patrol-106-extension", "PRJ-2026-101", "01", "extension",
        "东港污水处理站运维服务（01包）延期公告", "2026-11-04 16:00",
        "2026-12-08 09:30", "投标截止时间（延期后）",
        (
            "东港污水处理站运维服务（01包）投标截止时间延期。",
            "项目编号：PRJ-2026-101；包号：01。",
            "原投标截止时间为 2026年12月01日 09:30，现延期至 2026年12月08日 09:30。",
        ),
        "东港污水处理站运维服务",
    ),
    _notice(
        "patrol-107-extension", "PRJ-2026-102", "A", "extension",
        "南湖学校食堂设备采购项目（A包）延期公告", "2026-11-03 09:00",
        "2026-12-09 14:00", "投标截止时间（变更后）",
        (
            "南湖学校食堂设备采购项目（A包）投标截止时间变更。",
            "项目编号：PRJ-2026-102；包号：A。",
            "原投标截止时间为 2026年12月02日 14:00，现变更为 2026年12月09日 14:00。",
        ),
        "南湖学校食堂设备采购项目",
    ),
    _notice(
        "patrol-108-result", "PRJ-2026-103", "B", "result",
        "滨河智慧园区绿化养护项目（B包）成交结果公告", "2026-11-02 10:00",
        None, None,
        (
            "滨河智慧园区绿化养护项目（B包）成交结果公告。",
            "项目编号：PRJ-2026-103；包号：B。",
            "本公告不改变报价截止时间。",
        ),
        "滨河智慧园区绿化养护项目",
    ),
)

NEW_NOTICE = _notice(
    "patrol-109-original", "PRJ-2026-109", "E", "original",
    "西岭街道办公家具采购项目（E包）招标公告", "2026-11-10 10:00",
    "2026-12-12 09:30", "投标截止时间",
    (
        "西岭街道办公家具采购项目（E包）进行公开招标。",
        "项目编号：PRJ-2026-109；包号：E。",
        "投标截止时间：2026年12月12日 09:30。",
    ),
    "西岭街道办公家具采购项目",
)

SHIFT_NOTICES = (
    _notice(
        "patrol-110-original", "PRJ-2026-110", "F", "original",
        "北岸道路照明提升项目（F包）招标公告", "2026-11-10 14:00",
        "2026-12-14 09:30", "投标截止时间",
        (
            "北岸道路照明提升项目（F包）进行公开招标。",
            "项目编号：PRJ-2026-110；包号：F。",
            "投标截止时间：2026年12月14日 09:30。",
        ),
        "北岸道路照明提升项目",
    ),
    _notice(
        "patrol-111-original", "PRJ-2026-111", "G", "original",
        "南湖学校食堂设备采购项目（G包）招标公告", "2026-11-10 13:00",
        "2026-12-15 14:00", "投标截止时间",
        (
            "南湖学校食堂设备采购项目（G包）进行公开招标。",
            "项目编号：PRJ-2026-111；包号：G。",
            "投标截止时间：2026年12月15日 14:00。",
        ),
        "南湖学校食堂设备采购项目",
    ),
    _notice(
        "patrol-112-original", "PRJ-2026-112", "H", "original",
        "东港污水处理站运维服务（02包）招标公告", "2026-11-10 12:00",
        "2026-12-16 10:00", "投标截止时间",
        (
            "东港污水处理站运维服务（02包）进行公开招标。",
            "项目编号：PRJ-2026-112；包号：02。",
            "投标截止时间：2026年12月16日 10:00。",
        ),
        "东港污水处理站运维服务",
    ),
    _notice(
        "patrol-113-original", "PRJ-2026-113", "I", "original",
        "西岭街道办公家具采购项目（I包）询价公告", "2026-11-10 11:00",
        "2026-12-17 10:00", "报价截止时间",
        (
            "西岭街道办公家具采购项目（I包）进行询价采购。",
            "项目编号：PRJ-2026-113；包号：I。",
            "报价截止时间：2026年12月17日 10:00。",
        ),
        "西岭街道办公家具采购项目",
    ),
)

DUPLICATE_NOTICE = NEW_NOTICE


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    detail: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("baseline", "初始基线", "先在本场景巡检一次，只建立基准、不报新增。"),
    Scenario("new", "新增 1 条", "目录顶部多出 1 条公告；再巡检应只报这条。"),
    Scenario(
        "shift", "新增 4 条 · 旧公告换页",
        "顶部插入 4 条，旧公告全部被挤到后面的页；不得把旧公告当成新增。",
    ),
    Scenario(
        "duplicate", "同公告跨页重复",
        "1 条新公告同时出现在第 1、2 页；只应报出一次。",
    ),
    Scenario(
        "fail_page2", "第 2 页读取失败",
        "数据和“新增 1 条”相同，但第 2 页返回 503；本轮不报新增、不改基准。",
    ),
    Scenario(
        "fail_detail", "详情读取失败",
        "数据和“新增 1 条”相同，但新公告详情返回 503；本轮不报新增、不改基准。",
    ),
)

SCENARIO_KEYS = {scenario.key for scenario in SCENARIOS}
SCENARIO_LABELS = {scenario.key: scenario.label for scenario in SCENARIOS}
SCENARIO_DETAILS = {scenario.key: scenario.detail for scenario in SCENARIOS}

_state_lock = threading.Lock()
_current = "baseline"


def current_scenario() -> str:
    with _state_lock:
        return _current


def set_scenario(key: str) -> bool:
    global _current
    if key not in SCENARIO_KEYS:
        return False
    with _state_lock:
        _current = key
    return True


def reset_scenario() -> None:
    set_scenario("baseline")


ALL_NOTICES: dict[str, Notice] = {
    notice.slug: notice
    for notice in (*BASE, NEW_NOTICE, *SHIFT_NOTICES)
}


def _entries() -> list[Notice]:
    scenario = current_scenario()
    if scenario == "baseline":
        return list(BASE)
    if scenario == "new" or scenario.startswith("fail_"):
        return [NEW_NOTICE, *BASE]
    if scenario == "shift":
        return [*SHIFT_NOTICES, *BASE]
    if scenario == "duplicate":
        # New notice at the top of page 1 and repeated at the top of page 2.
        # The repeated link is the same slug, so the crawler sees one URL.
        return [DUPLICATE_NOTICE, *BASE[:2], DUPLICATE_NOTICE, *BASE[2:]]
    return list(BASE)


def total_pages() -> int:
    return (len(_entries()) + PAGE_SIZE - 1) // PAGE_SIZE


def total_count() -> int:
    return len(_entries())


def notices_on_page(page: int) -> list[Notice]:
    entries = _entries()
    start = (page - 1) * PAGE_SIZE
    return entries[start : start + PAGE_SIZE]


def page_fails(page: int) -> bool:
    return current_scenario() == "fail_page2" and page == 2


def detail_fails(slug: str) -> bool:
    return current_scenario() == "fail_detail" and slug == NEW_NOTICE.slug


def scenario_context() -> dict:
    return {
        "scenario": current_scenario(),
        "scenario_label": SCENARIO_LABELS[current_scenario()],
        "scenario_detail": SCENARIO_DETAILS[current_scenario()],
        "scenarios": SCENARIOS,
        "scenario_keys": SCENARIO_KEYS,
        "type_labels": TYPE_LABELS,
    }
