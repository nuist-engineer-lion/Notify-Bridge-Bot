"""通知图片下载、WebP 压缩和图文顺序转换；不使用协议端的本地文件路径。"""

import asyncio
import base64
import binascii
import hashlib
import io
import warnings
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError

from . import config as cfg
from .messages import message_segments

_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_MAX_IMAGES = 50
_MAX_PIXELS = 40_000_000
_ASSET_DIR = "public/assets/notices"


def _image_source(data: dict) -> str | None:
    for key in ("url", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("https://", "http://", "base64://", "data:image/")):
            return value
    return None


async def _download_image(session: aiohttp.ClientSession, source: str) -> bytes:
    if source.startswith(("base64://", "data:image/")):
        if source.startswith("data:image/"):
            header, _, encoded = source.partition(",")
            if not header.endswith(";base64"):
                raise ValueError("图片 data URL 必须使用 base64 编码")
        else:
            encoded = source[len("base64://"):]
        if len(encoded) > ((_MAX_DOWNLOAD_BYTES + 2) // 3) * 4:
            raise ValueError("通知图片超过 20 MiB 下载限制")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("图片 base64 编码无效") from exc
    else:
        if urlsplit(source).scheme not in {"http", "https"}:
            raise ValueError("图片缺少可下载的 HTTP 地址")
        async with session.get(source) as response:
            response.raise_for_status()
            if response.content_length and response.content_length > _MAX_DOWNLOAD_BYTES:
                raise ValueError("通知图片超过 20 MiB 下载限制")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                raw.extend(chunk)
                if len(raw) > _MAX_DOWNLOAD_BYTES:
                    raise ValueError("通知图片超过 20 MiB 下载限制")
            raw = bytes(raw)
    if not raw or len(raw) > _MAX_DOWNLOAD_BYTES:
        raise ValueError("通知图片为空或超过 20 MiB 下载限制")
    return raw


def _compress_webp(raw: bytes, *, quality: int, max_width: int) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > _MAX_PIXELS:
                    raise ValueError("通知图片超过 4000 万像素限制")
                # 静态通知使用动图首帧；先按 EXIF 修正方向，再缩小，避免手机照片旋转。
                image.seek(0)
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail((max_width, 16383), Image.Resampling.LANCZOS)
                mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                converted = oriented.convert(mode)
                output = io.BytesIO()
                converted.save(output, format="WEBP", quality=quality, method=6)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError("图片损坏、格式不支持或像素过大，无法转换为 WebP") from exc


async def prepare_notice_content(
    messages: list[dict], settings: dict[str, Any],
) -> tuple[str, dict[str, bytes]]:
    """按消息和消息段顺序生成 Markdown，返回要随正文提交的仓库图片文件。"""
    image_settings = settings.get("images") or {}
    quality = int(image_settings.get("quality", 82))
    max_width = int(image_settings.get("max_width", 1920))
    if not 1 <= quality <= 100 or not 1 <= max_width <= 16383:
        raise ValueError("notice_pr.images 配置无效：quality 需为 1–100，max_width 需为 1–16383")

    assets: dict[str, bytes] = {}
    cache: dict[str, str] = {}
    total_bytes = 0
    image_count = 0
    texts = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for index, message in enumerate(messages, 1):
            parts = []
            for segment in message_segments(message["message"]):
                kind, data = segment["type"], segment["data"]
                if kind == "text":
                    parts.append(data["text"])
                elif kind == "at" and str(data.get("qq")).strip().lower() in {"all", "全体成员"}:
                    parts.append("@全体成员")
                elif kind == "image":
                    image_count += 1
                    if image_count > _MAX_IMAGES:
                        raise ValueError("单份通知最多支持 50 张图片")
                    try:
                        source = _image_source(data)
                        # 某些协议端历史只提供图片 file 标识，通过 get_image 换取地址。
                        if not source:
                            if not data.get("file"):
                                raise ValueError("缺少图片地址和文件标识")
                            info = await cfg.client.call_api("get_image", file=str(data["file"]))
                            source = _image_source(info)
                        if not source:
                            raise ValueError("协议端未返回可下载的图片地址")
                        if source not in cache:
                            raw = await _download_image(session, source)
                            webp = await asyncio.to_thread(
                                _compress_webp, raw, quality=quality, max_width=max_width,
                            )
                            digest = hashlib.sha256(webp).hexdigest()
                            path = f"{_ASSET_DIR}/{digest}.webp"
                            if path not in assets:
                                total_bytes += len(webp)
                                if total_bytes > _MAX_TOTAL_BYTES:
                                    raise ValueError("通知压缩后图片总大小超过 50 MiB")
                                assets[path] = webp
                            cache[source] = path
                        public_path = cache[source].removeprefix("public")
                        parts.append(f"\n\n![通知图片]({public_path})\n\n")
                    except Exception as exc:
                        # 不将含临时鉴权参数的 QQ 图片 URL 放入反馈或日志。
                        raise ValueError(f"第 {index} 条消息的图片下载或 WebP 转换失败，未创建 PR") from exc
                else:
                    raise ValueError(
                        f"选中的第 {index} 条消息含引用、表情等不支持的内容，未创建 PR；"
                        "目前支持文本、图片及 @全体成员"
                    )
            text = "".join(parts).strip()
            if not text:
                raise ValueError(f"选中的第 {index} 条消息为空，未创建 PR")
            texts.append(text)
    return "\n\n".join(texts), assets
