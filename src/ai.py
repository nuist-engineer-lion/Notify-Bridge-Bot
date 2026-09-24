"""AI 回复建议：根据客户最近对话生成客服回复草稿。

在提醒合并转发发送前调用，建议附在第一层节点；LLM 未启用、失败或超时
时返回 None，调用方回退为不含建议的现状，绝不阻塞提醒发送。
配置读取自 cfg.AI_SUGGESTION（OpenAI 兼容接口），每次调用时读取，
可通过 .reload cfg 在线开关或更换模型。

图片输入（vision）：消息段中带 http(s) / data:image URL 的图片会按
OpenAI 兼容 image_url 送入支持视觉的模型（DeepSeek deepseek-flash 等）；
无 URL 或请求失败时回退纯文本 [图片] 占位。
"""

import asyncio
import time
from typing import Any

import aiohttp

from . import config as cfg
from .config import log, client

_SYSTEM_PROMPT = (
    "你是一家维修客服的助手。请根据以下客服与客户的最近对话，以客服身份草拟下一条发给客户的回复。"
    "要求：只输出回复内容本身，不要任何解释、前缀或引号；语气友好专业、简洁；"
    "如果客户诉求还不明确，先回应已知信息并礼貌追问。"
)

_PER_MESSAGE_MAX_CHARS = 200
_DEFAULT_TIMEOUT = 12.0
_DEFAULT_MAX_CONTEXT = 20
_DEFAULT_MAX_SUGGESTION_CHARS = 300
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_MAX_IMAGES = 3
_DEFAULT_IMAGE_DETAIL = "low"
_VALID_IMAGE_DETAILS = {"low", "high", "original", "auto"}


def _settings() -> dict:
    """每次调用时读取配置，保证 .reload cfg 热更新立即生效。"""
    return cfg.AI_SUGGESTION if isinstance(cfg.AI_SUGGESTION, dict) else {}


def is_enabled() -> bool:
    s = _settings()
    return bool(s.get("enabled")) and bool(s.get("base_url")) and bool(s.get("api_key")) and bool(s.get("model"))


def vision_enabled() -> bool:
    """是否把聊天中的图片 URL 送入模型。默认开启；模型不支持视觉时会自动回退纯文本。"""
    s = _settings()
    raw = s.get("vision_enabled")
    return True if raw is None else bool(raw)


def _image_detail() -> str:
    s = _settings()
    detail = str(s.get("image_detail") or _DEFAULT_IMAGE_DETAIL).strip().lower()
    return detail if detail in _VALID_IMAGE_DETAILS else _DEFAULT_IMAGE_DETAIL


def _max_images() -> int:
    s = _settings()
    try:
        n = int(s.get("max_images_per_suggestion", _DEFAULT_MAX_IMAGES))
    except (TypeError, ValueError):
        n = _DEFAULT_MAX_IMAGES
    return max(0, n)


def _extract_image_urls(data: dict) -> list[str]:
    """从单条 image 消息段提取可用的图片 URL（http(s) 或 data:image）。"""
    urls: list[str] = []
    for key in ("url", "file"):
        val = str(data.get(key) or "").strip()
        if not val:
            continue
        if val.startswith("http://") or val.startswith("https://") or val.startswith("data:image/"):
            if val not in urls:
                urls.append(val)
    return urls


def extract_content_from_segments(segments, *, allow_images: bool) -> tuple[str, list[str]]:
    """从 OB11 消息段提取文本与图片 URL。

    返回 (文本（含 [图片] 等占位）, image_url 列表)。
    allow_images=False 时只出占位符，不收集 URL。
    """
    parts: list[str] = []
    image_urls: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", ""))
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "image":
            parts.append("[图片]")
            if allow_images:
                for u in _extract_image_urls(data):
                    if u not in image_urls:
                        image_urls.append(u)
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()[:_PER_MESSAGE_MAX_CHARS], image_urls


def extract_text_from_segments(segments) -> str:
    """从 OB11 消息段列表提取可读文本，非文本段用占位符表示。"""
    text, _ = extract_content_from_segments(segments, allow_images=False)
    return text


async def build_transcript(uid: int, limit: int, *, allow_images: bool = False) -> list[dict[str, Any]]:
    """拉取该客户最近的双方消息，转为 [{role, text, images}] 序列。"""
    resp = await client.get_friend_msg_history(
        user_id=str(uid),
        count=limit,
        parse_mult_msg=True,
    )
    messages = resp.get("messages", []) or []
    messages.sort(key=lambda m: m.get("time", 0))
    transcript: list[dict[str, Any]] = []
    for msg in messages[-limit:]:
        text, images = extract_content_from_segments(msg.get("message", []), allow_images=allow_images)
        if not text and not images:
            continue
        sender = (msg.get("sender", {}) or {}).get("user_id")
        role = "customer" if sender is not None and int(sender) == uid else "staff"
        # 仅有图片时用占位文本，保证对话行可读
        line = text or ("[图片]" if images else "")
        if not line:
            continue
        transcript.append({"role": role, "text": line, "images": images})
    return transcript


def _build_prompt(transcript: list[dict[str, Any]]) -> str | None:
    if not transcript:
        return None
    lines = [f"{'客户' if item.get('role') == 'customer' else '客服'}：{item.get('text', '')}" for item in transcript]
    return "最近对话：\n" + "\n".join(lines) + "\n请给出客服的下一条回复。"


def _collect_image_urls(transcript: list[dict[str, Any]], max_images: int) -> list[str]:
    """优先收集客户侧图片，再补客服侧；去重后截断到 max_images。"""
    if max_images <= 0:
        return []
    urls: list[str] = []
    for want_customer in (True, False):
        for item in transcript:
            is_customer = item.get("role") == "customer"
            if is_customer != want_customer:
                continue
            for u in item.get("images") or []:
                if u not in urls:
                    urls.append(u)
                if len(urls) >= max_images:
                    return urls
    return urls


def _build_user_content(prompt: str, image_urls: list[str], detail: str) -> str | list[dict]:
    """无图返回纯文本；有图返回 OpenAI 兼容 content 数组（image_url 仅允许出现在 user 消息）。"""
    if not image_urls:
        return prompt
    content: list[dict] = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({
            "type": "image_url",
            "image_url": {"url": url, "detail": detail},
        })
    return content


async def _call_llm(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    image_urls: list[str] | None = None,
) -> str | None:
    url = base_url.rstrip("/") + "/chat/completions"
    s = _settings()
    # system 提示词与温度均可在配置中覆盖；留空/非法时回退默认
    try:
        temperature = float(s.get("temperature", _DEFAULT_TEMPERATURE))
    except (TypeError, ValueError):
        temperature = _DEFAULT_TEMPERATURE
    system_prompt = str(s.get("system_prompt") or "").strip() or _SYSTEM_PROMPT
    image_urls = image_urls or []
    user_content = _build_user_content(prompt, image_urls, _image_detail())
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=float(s.get("timeout_seconds", _DEFAULT_TIMEOUT)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                img_note = f", images={len(image_urls)}" if image_urls else ""
                log.warning("AI建议: LLM 接口返回 %s%s: %s", resp.status, img_note, body[:200])
                return None
            data = await resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
        return str(content).strip() or None
    except (KeyError, IndexError, TypeError):
        log.warning("AI建议: LLM 响应格式异常: %s", str(data)[:200])
        return None


async def generate_reply_suggestion(uid: int) -> str | None:
    """为单个客户生成建议回复；未启用/无对话/失败/超时均返回 None。

    总超时按 timeout_seconds 计：拉历史 + 视觉请求 + 纯文本回退共享同一时间预算，
    视觉超时/失败不会吞掉纯文本重试机会。
    """
    s = _settings()
    if not is_enabled():
        return None
    timeout = float(s.get("timeout_seconds", _DEFAULT_TIMEOUT))
    use_vision = vision_enabled()
    max_images = _max_images() if use_vision else 0
    deadline = time.monotonic() + timeout
    base_url = str(s.get("base_url"))
    api_key = str(s.get("api_key"))
    model = str(s.get("model"))

    def _remaining() -> float:
        return max(0.05, deadline - time.monotonic())

    try:
        transcript = await asyncio.wait_for(
            build_transcript(uid, int(s.get("max_context_messages", _DEFAULT_MAX_CONTEXT)), allow_images=use_vision),
            timeout=_remaining(),
        )
        prompt = _build_prompt(transcript)
        if prompt is None:
            log.info("AI建议: 客户 %d 窗口内无可读对话，跳过", uid)
            return None
        image_urls = _collect_image_urls(transcript, max_images)
        if image_urls:
            log.debug("AI建议: 客户 %d 附带图片 %d 张", uid, len(image_urls))

        suggestion: str | None = None
        if image_urls:
            try:
                suggestion = await asyncio.wait_for(
                    _call_llm(base_url, api_key, model, prompt, image_urls=image_urls),
                    timeout=_remaining(),
                )
            except asyncio.TimeoutError:
                log.warning("AI建议: 客户 %d 含图片请求超时，回退纯文本重试", uid)
                suggestion = None
            except Exception as e:
                log.warning("AI建议: 客户 %d 含图片请求失败，回退纯文本重试: %s", uid, e)
                suggestion = None
            else:
                if not suggestion:
                    log.info("AI建议: 客户 %d 含图片请求未成功，回退纯文本重试", uid)

        if not suggestion:
            try:
                suggestion = await asyncio.wait_for(
                    _call_llm(base_url, api_key, model, prompt, image_urls=[]),
                    timeout=_remaining(),
                )
            except asyncio.TimeoutError:
                log.warning("AI建议: 为客户 %d 生成建议超时（%s 秒），跳过", uid, timeout)
                return None
    except asyncio.TimeoutError:
        log.warning("AI建议: 为客户 %d 生成建议超时（%s 秒），跳过", uid, timeout)
        return None
    except Exception as e:
        log.warning("AI建议: 为客户 %d 生成建议失败: %s", uid, e)
        return None

    if not suggestion:
        return None
    max_chars = int(s.get("max_suggestion_chars", _DEFAULT_MAX_SUGGESTION_CHARS))
    return suggestion[:max_chars]


async def suggest_for_customers(customer_list: list[tuple[int, dict]]) -> dict[int, str]:
    """为一批客户并发生成建议回复，返回 {uid: 建议}；失败/未启用的客户不在结果中。"""
    total = len(customer_list)
    if total == 0:
        return {}
    if not is_enabled():
        log.info("AI建议未启用，跳过 %d 名客户", total)
        return {}
    s = _settings()
    log.info(
        "AI建议开始生成: %d 名客户, model=%s, vision=%s, max_images=%d",
        total,
        s.get("model"),
        vision_enabled(),
        _max_images() if vision_enabled() else 0,
    )
    results = await asyncio.gather(
        *(generate_reply_suggestion(qq) for qq, _ in customer_list),
        return_exceptions=True,
    )
    suggestions: dict[int, str] = {}
    for (qq, _), result in zip(customer_list, results):
        if isinstance(result, BaseException):
            log.warning("AI建议: 客户 %d 生成异常: %s", qq, result)
            continue
        if result:
            suggestions[qq] = result
    ok = len(suggestions)
    if ok == 0:
        log.warning("AI建议: 0/%d 名客户生成成功（全部跳过或失败，详见上方日志）", total)
    else:
        log.info("AI建议已生成: %d/%d 名客户（未生成 %d）", ok, total, total - ok)
    return suggestions
