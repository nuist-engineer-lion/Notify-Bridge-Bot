"""通知 PR：监控通知群白名单成员的消息，自动转换为主页仓库的通知 PR。

通知群与客服群（INTERNAL_GROUP_ID）相互独立：本模块只处理 notice_pr.groups
中配置的群，且仅当发送者在 notice_pr.auto_pr_senders 白名单内时触发。
每条消息生成 notices/entries/YYYY-MM-DD-auto-<hash8>.md，通过 GitHub API
创建分支、提交文件并开 PR；合并由人工在 GitHub 上审核完成，结果不回显到任何群。
"""

import asyncio
import base64
import hashlib
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp

from napcat import GroupMessageEvent, Text

from . import config as cfg
from .config import log

GITHUB_API = "https://api.github.com"
# 国内业务按东八区取通知日期
CST = timezone(timedelta(hours=8))
_RETRYABLE_STATUS = {500, 502, 503, 504}

# 同一消息只处理一次（事件重复投递/并发保护）
_processed_msg_ids: dict[int, float] = {}
_PROCESSED_TTL = 3600
_PROCESSED_MAX = 512


def _notice_cfg() -> dict[str, Any]:
    ncfg = cfg.NOTICE_PR
    return ncfg if isinstance(ncfg, dict) else {}


# ======================= 消息过滤与内容提取 =======================

def extract_notice_text(message: list) -> str | None:
    """提取纯文本内容；含任何非 Text 段（图片/@/引用/表情等）时返回 None。

    QQ 图片 URL 是临时的，无法长期托管在站点上，V1 只支持纯文本通知。
    """
    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, Text):
            return None
        parts.append(seg.text)
    return "\n".join(parts).strip()


def _yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_notice_markdown(text: str, max_title_chars: int, msg_time: float) -> tuple[str, str, str, str]:
    """把群消息文本转换为通知 md 文件。

    返回 (md内容, 标题, YYYY-MM-DD日期, 内容hash前8位)。
    首行作标题，其余作摘要与正文，遵循主页仓库 notices/README.md 的约定。
    """
    stripped = [ln.strip() for ln in text.splitlines()]
    nonempty = [ln for ln in stripped if ln]
    if not nonempty:
        raise ValueError("消息文本为空")

    title = nonempty[0]
    if len(title) > max_title_chars:
        title = title[: max_title_chars - 1] + "…"

    first_idx = stripped.index(nonempty[0])
    body = "\n".join(stripped[first_idx + 1:]).strip()

    date_str = datetime.fromtimestamp(msg_time, CST).date().isoformat()
    hash8 = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]

    if not body:
        md = (
            "---\n"
            f"title: {_yaml_quote(title)}\n"
            f"date: {date_str}\n"
            "---\n"
        )
    else:
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        summary = paragraphs[0] if paragraphs else body
        md = (
            "---\n"
            f"title: {_yaml_quote(title)}\n"
            f"date: {date_str}\n"
            "---\n\n"
            f"{summary}\n\n"
            "<!-- more -->\n\n"
            f"{body}\n"
        )
    return md, title, date_str, hash8


# ======================= GitHub API =======================

async def _gh_request(session: aiohttp.ClientSession, method: str, path: str, token: str, *,
                      json_body: dict | None = None) -> tuple[int, Any]:
    """GitHub API 请求；5xx 与网络错误按指数退避重试，返回 (status, data)。"""
    url = f"{GITHUB_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Notify-Bridge-Bot",
    }
    last_err: str | None = None
    for attempt in range(3):
        try:
            async with session.request(method, url, headers=headers, json=json_body) as resp:
                data = await resp.json(content_type=None)
                if resp.status in _RETRYABLE_STATUS:
                    last_err = f"HTTP {resp.status}"
                else:
                    return resp.status, data
        except aiohttp.ClientError as e:
            last_err = str(e)
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GitHub API 重试后仍失败: {method} {path}: {last_err}")


async def _has_open_pr(session: aiohttp.ClientSession, repo: str, token: str, branch: str) -> bool:
    status, prs = await _gh_request(
        session, "GET", f"/repos/{repo}/pulls?head={repo}:{branch}&state=open&per_page=1", token,
    )
    return status == 200 and bool(prs)


def _build_pr_body(*, group_id: int, user_id: int, message_id: int, msg_time: float,
                   hash8: str, md: str) -> str:
    ts_str = datetime.fromtimestamp(msg_time, CST).strftime("%Y-%m-%d %H:%M:%S")
    return (
        "## 自动通知 PR\n\n"
        "由 Notify-Bridge-Bot 从通知群消息自动创建。\n\n"
        "| 字段 | 值 |\n"
        "|---|---|\n"
        f"| 来源群 | {group_id} |\n"
        f"| 发送者 | {user_id} |\n"
        f"| 消息 ID | {message_id} |\n"
        f"| 消息时间 | {ts_str} |\n"
        f"| 内容哈希 | `{hash8}` |\n\n"
        "### 通知文件预览\n\n"
        "````markdown\n" + md + "\n````\n\n"
        "### 审核说明\n\n"
        "- 合并即发布：自动触发 Pages 部署，首页气泡与通知中心更新\n"
        "- 驳回：直接关闭本 PR；同一内容再次发送会创建新 PR\n"
        "- 可直接在本 PR 中编辑文件修正标题/摘要后再合并\n"
    )


async def _submit_notice_pr(*, text: str, group_id: int, user_id: int,
                            message_id: int, msg_time: float) -> None:
    ncfg = _notice_cfg()
    gh = ncfg.get("github") or {}
    repo = gh.get("repo")
    token = gh.get("token")
    base_branch = gh.get("base_branch", "main")
    max_title = int(ncfg.get("max_title_chars", 30) or 30)

    try:
        md, title, date_str, hash8 = build_notice_markdown(text, max_title, msg_time)
    except ValueError:
        return

    filename = f"{date_str}-auto-{hash8}.md"
    path = f"notices/entries/{filename}"
    branch = f"notice/auto-{date_str.replace('-', '')}-{hash8}"
    commit_msg = f"notice(bot): 通知群消息自动提交 {filename}"

    if ncfg.get("dry_run"):
        log.info("[notice_pr] dry_run：将提交 %s（分支 %s）\n%s", path, branch, md)
        return
    if not token or not repo:
        log.error("notice_pr.github.repo/token 未配置，无法开 PR")
        return

    pr_number: int | None = None
    pr_url: str | None = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            status, ref = await _gh_request(session, "GET", f"/repos/{repo}/git/ref/heads/{base_branch}", token)
            if status != 200:
                log.error("获取基准分支 %s 失败: HTTP %s %s", base_branch, status, ref)
                return
            base_sha = ref["object"]["sha"]

            # 内容 hash 使分支名确定化：已存在且仍有待审 PR 即视为重复
            branch_name: str | None = None
            for candidate in [branch] + [f"{branch}-{i}" for i in range(2, 6)]:
                status, resp = await _gh_request(
                    session, "POST", "/repos/{r}/git/refs".format(r=repo), token,
                    json_body={"ref": f"refs/heads/{candidate}", "sha": base_sha},
                )
                if status == 201:
                    branch_name = candidate
                    break
                if status == 422:
                    if await _has_open_pr(session, repo, token, candidate):
                        log.info("通知内容已存在待审 PR（分支 %s），跳过", candidate)
                        return
                    continue
                log.error("创建分支 %s 失败: HTTP %s %s", candidate, status, resp)
                return
            if branch_name is None:
                log.error("通知分支创建失败：候选分支均已存在（%s…）", branch)
                return

            content_b64 = base64.b64encode(md.encode("utf-8")).decode("ascii")
            status, resp = await _gh_request(
                session, "PUT", f"/repos/{repo}/contents/{path}", token,
                json_body={"message": commit_msg, "content": content_b64, "branch": branch_name},
            )
            if status != 201:
                log.error("提交通知文件 %s 失败: HTTP %s %s", path, status, resp)
                return

            status, resp = await _gh_request(
                session, "POST", f"/repos/{repo}/pulls", token,
                json_body={
                    "title": f"通知：{title}",
                    "head": branch_name,
                    "base": base_branch,
                    "body": _build_pr_body(
                        group_id=group_id, user_id=user_id, message_id=message_id,
                        msg_time=msg_time, hash8=hash8, md=md,
                    ),
                },
            )
            if status != 201:
                log.error("创建 PR 失败: HTTP %s %s", status, resp)
                return
            pr_number = resp.get("number")
            pr_url = resp.get("html_url")
    except Exception as e:
        log.error("通知 PR 创建失败: group=%d user=%d msg=%s: %s",
                  group_id, user_id, message_id, e, exc_info=True)
        return

    log.info("通知 PR 已创建: #%s %s", pr_number, pr_url)
    if ncfg.get("ack_to_internal", True) and cfg.INTERNAL_GROUP_ID:
        try:
            await cfg.client.send_group_msg(
                group_id=str(cfg.INTERNAL_GROUP_ID),
                message=f"📬 已为通知群消息创建 PR #{pr_number}：{title}\n{pr_url}",
            )
        except Exception as e:
            log.error("发送通知 PR ack 到内部群失败: %s", e)


# ======================= 事件入口 =======================

async def handle_notice_group_msg(event: GroupMessageEvent) -> bool:
    """处理通知群消息：白名单成员的纯文本消息自动转为主页仓库通知 PR。

    返回 True 表示事件属于通知群且已被本模块处理（或有意忽略）。
    """
    ncfg = _notice_cfg()
    if not ncfg.get("enabled"):
        return False

    try:
        groups = {int(g) for g in (ncfg.get("groups") or [])}
        senders = {int(s) for s in (ncfg.get("auto_pr_senders") or [])}
    except (TypeError, ValueError):
        log.error("notice_pr.groups / auto_pr_senders 配置格式错误，需为 QQ 号列表")
        return False

    if event.group_id not in groups:
        return False
    if event.user_id == cfg.client.self_id:
        return True
    if event.user_id not in senders:
        return True

    now = time.time()
    if event.message_id in _processed_msg_ids:
        return True
    _processed_msg_ids[event.message_id] = now
    if len(_processed_msg_ids) > _PROCESSED_MAX:
        cutoff = now - _PROCESSED_TTL
        for mid, ts in list(_processed_msg_ids.items()):
            if ts < cutoff:
                _processed_msg_ids.pop(mid, None)

    text = extract_notice_text(event.message)
    if not text:
        log.info("通知群消息为空或含非文本段，跳过自动开 PR: group=%d, user=%d, msg=%s",
                 event.group_id, event.user_id, event.message_id)
        return True

    msg_time = float(getattr(event, "time", 0) or now)
    # GitHub API 调用较慢，放后台任务避免阻塞事件循环
    asyncio.create_task(_submit_notice_pr(
        text=text, group_id=event.group_id, user_id=event.user_id,
        message_id=event.message_id, msg_time=msg_time,
    ))
    return True
