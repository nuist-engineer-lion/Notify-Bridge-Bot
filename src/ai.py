"""AI 回复建议：根据客户最近对话生成客服回复草稿。

在提醒合并转发发送前调用，建议附在第一层节点；LLM 未启用、失败或超时
时返回 None，调用方回退为不含建议的现状，绝不阻塞提醒发送。
配置读取自 cfg.AI_SUGGESTION（OpenAI 兼容接口），每次调用时读取，
可通过 .reload cfg 在线开关或更换模型。
"""

import asyncio

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


def _settings() -> dict:
    """每次调用时读取配置，保证 .reload cfg 热更新立即生效。"""
    return cfg.AI_SUGGESTION if isinstance(cfg.AI_SUGGESTION, dict) else {}


def is_enabled() -> bool:
    s = _settings()
    return bool(s.get("enabled")) and bool(s.get("base_url")) and bool(s.get("api_key")) and bool(s.get("model"))


def extract_text_from_segments(segments) -> str:
    """从 OB11 消息段列表提取可读文本，非文本段用占位符表示。"""
    parts: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", ""))
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()[:_PER_MESSAGE_MAX_CHARS]


async def build_transcript(uid: int, limit: int) -> list[tuple[str, str]]:
    """拉取该客户最近的双方消息，转为 (customer/staff, 文本) 序列。"""
    resp = await client.get_friend_msg_history(
        user_id=str(uid),
        count=limit,
        parse_mult_msg=True,
    )
    messages = resp.get("messages", []) or []
    messages.sort(key=lambda m: m.get("time", 0))
    self_id = int(client.self_id)
    transcript: list[tuple[str, str]] = []
    for msg in messages[-limit:]:
        text = extract_text_from_segments(msg.get("message", []))
        if not text:
            continue
        sender = (msg.get("sender", {}) or {}).get("user_id")
        role = "customer" if sender is not None and int(sender) == uid else "staff"
        transcript.append((role, text))
    return transcript


def _build_prompt(transcript: list[tuple[str, str]]) -> str | None:
    if not transcript:
        return None
    lines = [f"{'客户' if role == 'customer' else '客服'}：{text}" for role, text in transcript]
    return "最近对话：\n" + "\n".join(lines) + "\n请给出客服的下一条回复。"


async def _call_llm(base_url: str, api_key: str, model: str, prompt: str) -> str | None:
    url = base_url.rstrip("/") + "/chat/completions"
    s = _settings()
    # system 提示词与温度均可在配置中覆盖；留空/非法时回退默认
    try:
        temperature = float(s.get("temperature", _DEFAULT_TEMPERATURE))
    except (TypeError, ValueError):
        temperature = _DEFAULT_TEMPERATURE
    system_prompt = str(s.get("system_prompt") or "").strip() or _SYSTEM_PROMPT
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=float(s.get("timeout_seconds", _DEFAULT_TIMEOUT)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("AI建议: LLM 接口返回 %s: %s", resp.status, body[:200])
                return None
            data = await resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
        return str(content).strip() or None
    except (KeyError, IndexError, TypeError):
        log.warning("AI建议: LLM 响应格式异常: %s", str(data)[:200])
        return None


async def generate_reply_suggestion(uid: int) -> str | None:
    """为单个客户生成建议回复；未启用/无对话/失败/超时均返回 None。"""
    s = _settings()
    if not is_enabled():
        return None
    timeout = float(s.get("timeout_seconds", _DEFAULT_TIMEOUT))
    try:
        transcript = await asyncio.wait_for(
            build_transcript(uid, int(s.get("max_context_messages", _DEFAULT_MAX_CONTEXT))),
            timeout=timeout,
        )
        prompt = _build_prompt(transcript)
        if prompt is None:
            log.debug("AI建议: 客户 %d 窗口内无可读对话，跳过", uid)
            return None
        suggestion = await _call_llm(
            str(s.get("base_url")), str(s.get("api_key")), str(s.get("model")), prompt,
        )
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
    if not is_enabled():
        return {}
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
    if suggestions:
        log.info("AI建议已生成: %d/%d 名客户", len(suggestions), len(customer_list))
    return suggestions
