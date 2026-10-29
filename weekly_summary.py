#!/usr/bin/env python3
"""GLaDOS 周报：汇总最近 7 天的签到情况、积分变化和套餐剩余天数，推送到已配置的后端。

由 .github/workflows/weeklySummary.yml 在每周一 09:30（Asia/Shanghai）调用，
也可以在 Actions 页面手动 workflow_dispatch 运行。

数据来源：
  1. 本周每次 Actions 运行日志里的「签到总结」行（需要 GITHUB_TOKEN 读 actions:read）；
  2. GLaDOS 接口的实时积分 / 剩余天数（复用 checkin.py 的 API 与 Config）。

推送复用 checkin.py 的 PushService（PushDeer / Server酱），密钥沿用现有 Secrets。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from checkin import API, Config, LogEmoji, PushService, logger

CST = timezone(timedelta(hours=8))
LOOKBACK_DAYS = 7

# "#1   ✅ 签到成功    glados.cloud   获得 0 积分 · 剩余 4 天 · 总 62 积分 · ..."
ROW_RE = re.compile(
    r"#(\d+)\s+(✅|🔄|❌)\s+(\S+)\s+(\S+)\s+"
    r"获得\s+(\d+)\s+积分\s+·\s+剩余\s+(\S+)\s+天\s+·\s+总\s+(\d+)\s+积分"
)

STATUS_EMOJI = {"success": "✅", "repeat": "🔄", "fail": "❌"}


# ---------------------------------------------------------------- GitHub API


def _gh_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def list_runs(repo: str, since: datetime) -> List[dict]:
    """列出 since 之后创建的 workflow 运行（只取第一页，7 天内不会超过 100 条）。"""
    url = f"https://api.github.com/repos/{repo}/actions/runs"
    try:
        resp = requests.get(
            url,
            headers=_gh_headers(),
            params={"created": f">={since:%Y-%m-%d}", "per_page": 100},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("workflow_runs", [])
    except Exception as exc:  # 网络/权限问题不该让周报整体失败
        logger.warning(f"{LogEmoji.WARNING} 读取运行列表失败: {exc}")
        return []


def run_log_text(repo: str, run_id: int) -> str:
    """下载单次运行的完整日志并拼成文本。"""
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=90, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            return "\n".join(
                zf.read(name).decode("utf-8", "replace")
                for name in zf.namelist()
                if name.endswith(".txt")
            )
    except Exception as exc:
        logger.warning(f"{LogEmoji.WARNING} 读取运行 {run_id} 日志失败: {exc}")
        return ""


def parse_rows(text: str) -> List[dict]:
    rows = []
    for match in ROW_RE.finditer(text):
        rows.append(
            {
                "emoji": match.group(2),
                "status": match.group(3),
                "domain": match.group(4),
                "points": int(match.group(5)),
                "days": match.group(6),
                "total": int(match.group(7)),
            }
        )
    return rows


def collect_history(repo: str) -> Dict[str, dict]:
    """按天汇总最近 7 天的签到记录：{MM-DD: {runs, success, repeat, fail, total, left}}"""
    since = datetime.now(CST) - timedelta(days=LOOKBACK_DAYS - 1)
    days: Dict[str, dict] = {}

    for run in list_runs(repo, since):
        if run.get("status") != "completed":
            continue
        try:
            created = datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            continue
        created = created.replace(tzinfo=timezone.utc).astimezone(CST)
        if created < since:
            continue

        text = run_log_text(repo, run["id"])
        if "签到总结" not in text:
            continue  # 周报自己的运行、失败的构建等都不含总结行

        key = created.strftime("%m-%d")
        entry = days.setdefault(
            key,
            {"runs": 0, "success": 0, "repeat": 0, "fail": 0, "total": None, "left": None, "at": None},
        )
        entry["runs"] += 1

        for row in parse_rows(text):
            if row["emoji"] == "✅":
                entry["success"] += 1
            elif row["emoji"] == "🔄":
                entry["repeat"] += 1
            else:
                entry["fail"] += 1
            # 同一天可能有多次运行（补签），以最晚一次的总积分 / 剩余天数为准
            if entry["at"] is None or created >= entry["at"]:
                entry["at"] = created
                entry["total"] = row["total"]
                entry["left"] = int(row["days"]) if row["days"].isdigit() else None

    return days


# ---------------------------------------------------------------- GLaDOS API


def current_status(config: Config) -> Dict[str, str]:
    """实时积分与剩余天数；多域名/多 Cookie 时取第一个成功返回的结果。"""
    for cookie in config.cookies_list:
        for domain in config.DOMAINS:
            api = API(domain)
            try:
                days_str, code = api.get_status(cookie)
                points_str, points_num = api.get_points(cookie)
            except Exception as exc:
                logger.warning(f"{LogEmoji.WARNING} 查询 {domain} 失败: {exc}")
                continue
            finally:
                api.close()
            if code == 0 and points_num:
                return {"days": days_str, "points": points_str, "num": str(points_num)}
    return {"days": "未知", "points": "未知", "num": ""}


# ---------------------------------------------------------------- 渲染与推送


def build_message(days: Dict[str, dict], current: Dict[str, str], plan: str) -> tuple[str, str]:
    today = datetime.now(CST)
    start = today - timedelta(days=LOOKBACK_DAYS - 1)
    title = f"GLaDOS 周报 {start:%m-%d}~{today:%m-%d}"

    ordered = sorted(days.items())
    covered = len(ordered)
    ok_days = sum(1 for _, d in ordered if d["success"] or d["repeat"])
    fail_days = sum(1 for _, d in ordered if not d["success"] and not d["repeat"] and d["fail"])
    runs_total = sum(d["runs"] for _, d in ordered)

    lines: List[str] = []
    lines.append(
        f"本周 {covered}/{LOOKBACK_DAYS} 天有记录 · 签到成功 {ok_days} 天 · 失败 {fail_days} 天 · 共 {runs_total} 次运行"
    )
    lines.append(f"当前：{current['points']} · 套餐剩余 {current['days']}")

    # 积分变化（用本周最早/最晚一次记录的总积分）
    totals = [(d["at"], d["total"]) for _, d in ordered if d["total"] is not None and d["at"]]
    if len(totals) >= 2:
        totals.sort()
        delta = totals[-1][1] - totals[0][1]
        lines.append(f"积分变化：{totals[0][1]} → {totals[-1][1]}（{delta:+d}）")

    lines.append("")  # 空行（推送里渲染成段落分隔）
    for key, d in ordered:
        mark = STATUS_EMOJI["success"] if (d["success"] or d["repeat"]) else STATUS_EMOJI["fail"]
        detail = []
        if d["total"] is not None:
            detail.append(f"{d['total']} 分")
        if d["left"] is not None:
            detail.append(f"剩 {d['left']} 天")
        if d["runs"] > 1:
            detail.append(f"{d['runs']} 次运行")
        lines.append(f"{key}  {mark}  " + " · ".join(detail) if detail else f"{key}  {mark}")

    # 漏签提示：只统计已经有记录之后的空档，避免新仓库/新装那几天全是噪音
    first_day = None
    if ordered:
        first_day = datetime.strptime(f"{today.year}-{ordered[0][0]}", "%Y-%m-%d").replace(tzinfo=CST)
        if first_day > today:  # 跨年保护
            first_day = first_day.replace(year=first_day.year - 1)
    window_start = start if first_day is None else max(start, first_day)
    missing = []
    cursor = window_start
    while cursor <= today:
        key = cursor.strftime("%m-%d")
        if key not in days:
            missing.append(key)
        cursor += timedelta(days=1)
    if missing:
        lines.append("")
        lines.append(f"⚠️ 没有记录：{'、'.join(missing)}（Actions 丢跑或本机没开机）")

    # 到期 / 兑换提醒
    left = None
    if current["days"].replace(" 天", "").isdigit():
        left = int(current["days"].replace(" 天", ""))
    else:
        for _, d in ordered:
            if d["left"] is not None:
                left = d["left"]
    if left is not None and left <= 5:
        lines.append("")
        lines.append(f"🔔 套餐只剩 {left} 天，记得续费或攒积分（100 分可换 10 天）")

    try:
        points_num = int(current["num"]) if current["num"] else None
    except ValueError:
        points_num = None
    if points_num is not None and points_num >= 100 and plan in ("none", ""):
        lines.append("")
        lines.append(f"💡 积分已够 {points_num}，可在仓库 Secrets 设 GLADOS_EXCHANGE_PLAN 自动兑换")

    return title, "  \n".join(lines)  # 行尾两空格 = Markdown 硬换行（Server酱），PushDeer 里不可见


def main() -> None:
    logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
    config = Config()

    logger.info(f"{LogEmoji.START} 步骤 2: 汇总最近 {LOOKBACK_DAYS} 天记录")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    days = collect_history(repo) if repo else {}
    logger.info(f"{LogEmoji.INFO} 共 {len(days)} 天有签到记录")

    logger.info(f"{LogEmoji.START} 步骤 3: 查询当前状态")
    current = current_status(config)
    logger.info(f"{LogEmoji.INFO} 当前 {current['points']} · 剩余 {current['days']}")

    logger.info(f"{LogEmoji.START} 步骤 4: 生成周报")
    title, body = build_message(days, current, getattr(config, "exchange_plan", "none"))
    logger.info(f"\n{LogEmoji.END} ========== 周报 ==========\n{title}\n{body}")

    logger.info(f"{LogEmoji.START} 步骤 5: 发送推送")
    push_service = PushService(config)
    ok = push_service.send(title, {"title": title, "rows": [], "note": body})
    if not ok:
        logger.warning(f"{LogEmoji.WARNING} 推送未成功（可能未配置密钥）")
    logger.info(f"{LogEmoji.END} 周报完成")


if __name__ == "__main__":
    main()
