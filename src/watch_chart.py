"""Render the stored friend and group counts as a OneBot image segment."""

import asyncio
import base64
import io
import os
from datetime import date, timedelta
from math import ceil
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image, ImageDraw, ImageFont

from . import config as cfg, storage

_AVERAGES = ((3, "#aa7b13"), (5, "#8952cf"), (10, "#287dbc"))


def _chart_font_path() -> Path:
    if custom_font := os.environ.get("WATCH_CHART_FONT"):
        return Path(custom_font).expanduser()

    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = (
        windows_fonts / "msyh.ttc",
        windows_fonts / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    )
    for path in candidates:
        if path.is_file():
            return path
    raise OSError("找不到中文字体，请安装 Noto Sans CJK 或设置 WATCH_CHART_FONT")


def _moving_averages(records: dict[date, tuple[int | None, int]]) -> dict[int, dict[date, float]]:
    ordered = sorted(records)
    return {
        period: {
            day: sum(records[item][1] for item in ordered[index - period + 1:index + 1]) / period
            for index, day in enumerate(ordered) if index + 1 >= period
        }
        for period, _ in _AVERAGES
    }


def _render_chart(
    days: list[date],
    panels: list[tuple[str, dict[date, tuple[int | None, int]]]],
) -> str:
    width = max(820, 104 + 28 * len(days))
    panel_height = 248
    background = "#f9fbfd"
    image = Image.new("RGB", (width, 90 + panel_height * len(panels)), background)
    draw = ImageDraw.Draw(image)
    font_path = _chart_font_path()
    title = ImageFont.truetype(font_path, size=30)
    heading = ImageFont.truetype(font_path, size=20)
    small = ImageFont.truetype(font_path, size=14)
    ink, muted = "#132238", "#475569"
    green, red, neutral = "#00a77b", "#e54c61", "#64748b"

    draw.text((26, 36), "综合人数报表", anchor="lm", fill=ink, font=title)
    draw.text((width - 28, 36), f"{days[0]:%Y-%m-%d} 至 {days[-1]:%Y-%m-%d}", anchor="rm", fill=muted, font=small)
    legend = [("增加", green), ("减少", red), ("持平／首次记录", neutral)]
    legend_x = 28
    for label, color in legend:
        if color == neutral:
            draw.line((legend_x, 70, legend_x + 10, 70), fill=color, width=2)
        else:
            draw.rectangle((legend_x, 65, legend_x + 10, 75), fill=None if color == red else color, outline=color)
        draw.text((legend_x + 17, 70), label, anchor="lm", fill=muted, font=small)
        legend_x += 17 + round(draw.textlength(label, font=small)) + 26
    for period, color in _AVERAGES:
        draw.line((legend_x, 70, legend_x + 15, 70), fill=color, width=2)
        label = f"{period} 次均线"
        draw.text((legend_x + 21, 70), label, anchor="lm", fill=muted, font=small)
        legend_x += 21 + round(draw.textlength(label, font=small)) + 26

    left, right = 28, width - 78
    step = (right - left) / len(days)
    body_width = min(18, step * .62)
    tick_interval = 2 if len(days) > 14 else 1
    for panel_index, (label, records) in enumerate(panels):
        top = 90 + panel_index * panel_height
        draw.line((left, top, width - 28, top), fill="#dce5ee")
        draw.text((left, top + 24), label, anchor="lm", fill=ink, font=heading)
        visible = {day: records[day] for day in days if day in records}
        averages = _moving_averages(records)
        if visible:
            last_day = max(visible)
            draw.text((width - 28, top + 24), f"{last_day:%m-%d}　{visible[last_day][1]:,} 人", anchor="rm", fill=ink, font=heading)
        else:
            draw.text(((left + right) / 2, top + 121), "暂无采样记录", anchor="mm", fill=muted, font=small)

        values = [value for pair in visible.values() for value in pair if value is not None]
        values.extend(average[day] for average in averages.values() for day in visible if day in average)
        minimum, maximum = min(values, default=0), max(values, default=1)
        grid_step = max(1, ceil((maximum - minimum) / 4))
        low = max(0, (int(minimum) // grid_step - 1) * grid_step)
        high = (ceil(maximum / grid_step) + 1) * grid_step
        plot_top, bottom = top + 47, top + 211

        def y_at(value: float) -> float:
            return bottom - (value - low) / (high - low) * (bottom - plot_top)

        for value in range(low, high + 1, grid_step):
            y = y_at(value)
            draw.line((left, y, right, y), fill="#e4ecf3")
            draw.text((right + 8, y), f"{value:,}", anchor="lm", fill=muted, font=small)

        for index, day in enumerate(days):
            x = left + step * (index + .5)
            if (len(days) - 1 - index) % tick_interval == 0:
                draw.line((x, plot_top, x, bottom), fill="#eaf0f5")
                draw.text((x, bottom + 9), f"{day:%m-%d}", anchor="mt", fill=muted, font=small)
            if day not in visible:
                continue
            previous, current = visible[day]
            close_y = y_at(current)
            if previous is None or previous == current:
                draw.line((x - body_width / 2, close_y, x + body_width / 2, close_y), fill=neutral, width=2)
                continue
            color, fill = (green, green) if current > previous else (red, background)
            upper, lower = sorted((y_at(previous), close_y))
            draw.rectangle((x - body_width / 2, upper, x + body_width / 2, max(upper + 2, lower)), fill=fill, outline=color)

        for period, color in _AVERAGES:
            previous_point = None
            for index, day in enumerate(days):
                average = averages[period].get(day)
                if average is None:
                    previous_point = None
                    continue
                point = left + step * (index + .5), y_at(average)
                if previous_point is not None:
                    draw.line((*previous_point, *point), fill=color, width=2)
                previous_point = point

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


async def build_watch_chart(limit: int) -> MessageSegment | None:
    history_limit = limit + _AVERAGES[-1][0] - 1
    friends = await storage.recent_friend_stats(history_limit)
    panels: list[tuple[str, dict[date, tuple[int | None, int]]]] = [("好友人数", {
        date.fromisoformat(day): (total - added + removed, total)
        for day, added, removed, total in friends
    })]
    series = [*cfg.MONITORED_GROUPS.items(), *(
        (name, storage.cascade_key(group_ids)) for name, group_ids in cfg.MONITORED_GROUP_CASCADES.items()
    )]
    for name, group_id in series:
        rows = await storage.recent_group_stats(group_id, history_limit)
        panels.append((name, {
            date.fromisoformat(day): (total - change if change is not None else None, total)
            for day, total, change, _, _ in rows
        }))
    latest = max((day for _, records in panels for day in records), default=None)
    if latest is None:
        return None
    days = [latest - timedelta(days=offset) for offset in reversed(range(limit))]
    return MessageSegment.image(await asyncio.to_thread(_render_chart, days, panels))
