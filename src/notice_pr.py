"""通知 PR：监控通知群白名单成员的消息，自动转换为主页仓库的通知 PR。

通知群与客服群（INTERNAL_GROUP_ID）相互独立：本模块只处理 notice_pr.groups
中配置的群，且仅当发送者在 notice_pr.auto_pr_senders 白名单内时触发。
每条消息生成 notices/entries/YYYY-MM-DD-auto-<hash8>.md，通过 GitHub API
创建分支、提交文件并开 PR；合并由人工在 GitHub 上审核完成，结果不回显到任何群。
"""

import asyncio
import base64
import hashlib
import json
import os
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

# ack 重试队列：PR 已创建但内部群 ack 发送失败时落盘，连接恢复/重启后重发
_ACK_RETRY_INTERVAL = 30
_ACK_MAX_RETRIES = 10
_ack_queue_loaded = False
_ack_queue: list[dict] = []
_ack_retry_task: asyncio.Task | None = None


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
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = str(e)
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GitHub API 重试后仍失败: {method} {path}: {last_err}")


async def _has_open_pr(session: aiohttp.ClientSession, repo: str, token: str, branch: str) -> bool:
    status, prs = await _gh_request(
        session, "GET", f"/repos/{repo}/pulls?head={repo}:{branch}&state=open&per_page=1", token,
    )
    return status == 200 and bool(prs)


async def _delete_branch(session: aiohttp.ClientSession, repo: str, token: str, branch: str) -> None:
    """删除本次创建后未完成 PR 的分支，避免孤儿分支累积。"""
    try:
        status, resp = await _gh_request(
            session, "DELETE", f"/repos/{repo}/git/refs/heads/{branch}", token,
        )
        if status == 204:
            log.info("已清理未完成 PR 的分支 %s", branch)
        else:
            log.warning("清理分支 %s 失败: HTTP %s %s", branch, status, resp)
    except Exception as e:
        log.warning("清理分支 %s 异常: %s", branch, e)


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
                    session, "POST", f"/repos/{repo}/git/refs", token,
                    json_body={"ref": f"refs/heads/{candidate}", "sha": base_sha},
                )
                if status == 201:
                    branch_name = candidate
                    break
                if status == 422:
                    # 并发场景：另一请求可能刚建好分支还未开完 PR，
                    # 轮询确认后再决定是否启用后缀分支，避免产生重复 PR
                    for attempt in range(3):
                        if await _has_open_pr(session, repo, token, candidate):
                            log.info("通知内容已存在待审 PR（分支 %s），跳过", candidate)
                            return
                        if attempt < 2:
                            await asyncio.sleep(2)
                    continue
                log.error("创建分支 %s 失败: HTTP %s %s", candidate, status, resp)
                return
            if branch_name is None:
                log.error("通知分支创建失败：候选分支均已存在（%s…）", branch)
                return

            # 分支由本次创建：提交文件或开 PR 任一步失败都删除分支，
            # 重试会从头走确定性流程，不遗留孤儿分支
            try:
                content_b64 = base64.b64encode(md.encode("utf-8")).decode("ascii")
                status, resp = await _gh_request(
                    session, "PUT", f"/repos/{repo}/contents/{path}", token,
                    json_body={"message": commit_msg, "content": content_b64, "branch": branch_name},
                )
                if status != 201:
                    raise RuntimeError(f"提交通知文件 {path} 失败: HTTP {status}: {resp}")

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
                    raise RuntimeError(f"创建 PR 失败: HTTP {status}: {resp}")
                pr_number = resp.get("number")
                pr_url = resp.get("html_url")
            except Exception:
                await _delete_branch(session, repo, token, branch_name)
                raise
    except Exception as e:
        log.error("通知 PR 创建失败: group=%d user=%d msg=%s: %s",
                  group_id, user_id, message_id, e, exc_info=True)
        return

    log.info("通知 PR 已创建: #%s %s", pr_number, pr_url)
    await _ack_pr_created(f"📬 已为通知群消息创建 PR #{pr_number}：{title}\n{pr_url}")


# ======================= ack 持久化重试队列 =======================

def _ack_queue_file() -> str:
    return os.path.join(cfg.ARCHIVE_DIR, "notice_ack_queue.json")


def _load_ack_queue() -> list[dict]:
    global _ack_queue_loaded, _ack_queue
    if _ack_queue_loaded:
        return _ack_queue
    try:
        with open(_ack_queue_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        _ack_queue = data if isinstance(data, list) else []
    except FileNotFoundError:
        _ack_queue = []
    except Exception as e:
        log.error("读取通知 ack 队列失败，按空队列处理: %s", e)
        _ack_queue = []
    _ack_queue_loaded = True
    return _ack_queue


def _save_ack_queue(acks: list[dict]) -> None:
    global _ack_queue
    _ack_queue = acks
    try:
        os.makedirs(cfg.ARCHIVE_DIR, exist_ok=True)
        path = _ack_queue_file()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(acks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.error("保存通知 ack 队列失败: %s", e)


def _ensure_ack_retry_task() -> None:
    global _ack_retry_task
    if _ack_retry_task is None or _ack_retry_task.done():
        _ack_retry_task = asyncio.create_task(_ack_retry_loop())


async def _ack_pr_created(text: str) -> None:
    """开 PR 后 ack 到客服内部群；发送失败进入持久化队列，恢复连接/重启后重发。"""
    ncfg = _notice_cfg()
    if not ncfg.get("ack_to_internal", True) or not cfg.INTERNAL_GROUP_ID:
        return
    try:
        await cfg.client.send_group_msg(group_id=str(cfg.INTERNAL_GROUP_ID), message=text)
        log.info("通知 PR ack 已发送至内部群")
    except Exception as e:
        log.error("发送通知 PR ack 到内部群失败，加入重试队列: %s", e)
        _ack_send_failed(text)


def _ack_send_failed(text: str) -> None:
    acks = _load_ack_queue()
    acks.append({"text": text, "queued_at": time.time(), "retries": 0})
    _save_ack_queue(acks)
    _ensure_ack_retry_task()


async def flush_pending_acks() -> None:
    """启动时冲刷上次未发出的 ack 队列；仍失败的转入后台重试。"""
    acks = _load_ack_queue()
    if not acks:
        return
    log.info("发现 %d 条待发送的通知 PR ack，尝试重发", len(acks))
    remaining = []
    for ack in acks:
        try:
            await cfg.client.send_group_msg(
                group_id=str(cfg.INTERNAL_GROUP_ID), message=ack["text"],
            )
            log.info("重发通知 PR ack 成功")
        except Exception as e:
            log.warning("启动重发通知 PR ack 失败，转入后台重试: %s", e)
            remaining.append(ack)
    _save_ack_queue(remaining)
    if remaining:
        _ensure_ack_retry_task()


async def _ack_retry_loop() -> None:
    """后台重发 ack 队列，连接恢复后每 30 秒一轮，单条最多重试 10 次。"""
    for _ in range(_ACK_MAX_RETRIES):
        await asyncio.sleep(_ACK_RETRY_INTERVAL)
        acks = _load_ack_queue()
        if not acks:
            return
        remaining = []
        for ack in acks:
            try:
                await cfg.client.send_group_msg(
                    group_id=str(cfg.INTERNAL_GROUP_ID), message=ack["text"],
                )
                log.info("重发通知 PR ack 成功")
            except Exception as e:
                ack["retries"] = ack.get("retries", 0) + 1
                if ack["retries"] >= _ACK_MAX_RETRIES:
                    log.error("通知 PR ack 重发 %d 次仍失败，放弃: %s (%s)",
                              _ACK_MAX_RETRIES, ack["text"], e)
                else:
                    remaining.append(ack)
        _save_ack_queue(remaining)
        if not remaining:
            return
    log.warning("通知 ack 后台重试轮次用尽，剩余 %d 条留待下次启动重发", len(_load_ack_queue()))


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
