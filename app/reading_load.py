"""阅读负荷（字/秒）计算与热区扫描。

流程：
* 每条字幕的速率 = 去掉全部 Unicode 空白后的字符数 ÷ 显示秒数；
* 重叠显示的字幕，同一时刻的速率相加（各字幕在自身显示区间内速率恒定）；
* 以全部字幕的开始 / 结束事件把时间轴切成半开区间 ``[t, t+1)`` 逐段扫描，
  不做整秒采样，毫秒及更细粒度的时间变化都可识别；
* 找出合计速率严格超过阈值的连续区间：超阈区间天然相邻（端点相接即合并），
  仅当连续时长达下限（毫秒）才保留为一个热区；
* 每个热区返回起止时间、区间内合计速率峰值及参与字幕的输入顺序号
  （1 起步，按 :func:`app.srt.parse_srt` 的解析顺序）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .srt import Cue

# 浮点比较容差：合计速率由若干“字符数 / 秒数”累加而来，允许极小误差
_EPS = 1e-9

_UNICODE_WS_RE = re.compile(r"\s+", re.UNICODE)


@dataclass(frozen=True)
class CueLoad:
    """单条字幕的字数、时长与阅读速率。"""

    order: int
    chars: int
    duration_ms: int
    rate: float


@dataclass(frozen=True)
class HotZone:
    """一个阅读负荷热区。"""

    start: int
    end: int
    peak_rate: float
    cue_orders: list[int]


def count_chars(text: str) -> int:
    """去掉全部 Unicode 空白（含全角空格、不间断空格、制表符、换行等）后的字符数。"""
    return len(_UNICODE_WS_RE.sub("", text))


def cue_loads(cues: list[Cue]) -> list[CueLoad]:
    """按输入顺序计算每条字幕的阅读速率（字/秒）。"""
    loads: list[CueLoad] = []
    for order, cue in enumerate(cues, start=1):
        duration_ms = cue.end - cue.start  # 解析器已保证显示时长严格为正
        chars = count_chars(cue.text)
        rate = chars / (duration_ms / 1000.0)
        loads.append(
            CueLoad(order=order, chars=chars, duration_ms=duration_ms, rate=rate)
        )
    return loads


def _exceeds(total_rate: float, threshold: float) -> bool:
    """合计速率是否严格超过阈值（容忍浮点累加误差）。"""
    return total_rate > threshold + _EPS


def find_hot_zones(
    cues: list[Cue], threshold: float, min_duration_ms: int
) -> list[HotZone]:
    """扫描半开时间轴，找出合计速率严格超过阈值且连续时长达到下限的热区。

    ``threshold`` 为正数（字/秒），``min_duration_ms`` 为非负整数毫秒。
    超阈区段端点相接（前一段结束 = 下一段开始）即视为连续并合并，
    因此无需扫描结束后再做一次合并；被任意长度的非超阈区段隔开则保持独立。
    """
    loads = cue_loads(cues)

    # 事件流：(时间, 速率增量, 字幕输入顺序号)；开始 +rate，结束 -rate
    events: list[tuple[int, float, int]] = []
    for load, cue in zip(loads, cues):
        events.append((cue.start, load.rate, load.order))
        events.append((cue.end, -load.rate, load.order))
    events.sort(key=lambda e: e[0])

    zones: list[HotZone] = []
    active: dict[int, float] = {}
    total_rate = 0.0

    zone_start: int | None = None
    zone_peak = 0.0
    zone_cues: set[int] = set()

    prev_time: int | None = None
    i = 0
    n = len(events)
    while i < n:
        time = events[i][0]

        # 先评估上一批事件之后、当前时刻之前的半开区段 [prev_time, time)
        if prev_time is not None and time > prev_time:
            if _exceeds(total_rate, threshold):
                if zone_start is None:
                    zone_start = prev_time
                    zone_peak = total_rate
                    zone_cues = set(active)
                elif total_rate > zone_peak:
                    zone_peak = total_rate
                zone_cues.update(active)
            elif zone_start is not None:
                # 落入非超阈区段：相接的超阈区段此前已自然并入同一热区，
                # 到这里热区必然结束；时长达下限才保留
                if prev_time - zone_start >= min_duration_ms:
                    zones.append(
                        HotZone(
                            start=zone_start,
                            end=prev_time,
                            peak_rate=round(zone_peak, 6),
                            cue_orders=sorted(zone_cues),
                        )
                    )
                zone_start = None
                zone_peak = 0.0
                zone_cues = set()

        # 应用同一时刻的全部开始 / 结束事件，新状态对 [time, 下一时刻) 生效
        while i < n and events[i][0] == time:
            _, delta, order = events[i]
            total_rate += delta
            if delta > 0:
                active[order] = delta
            else:
                active.pop(order, None)
            i += 1

        prev_time = time

    # 末批事件必为各字幕结束（合计回到 0）；兜底关闭仍开启的热区
    if zone_start is not None and prev_time is not None:
        if prev_time - zone_start >= min_duration_ms:
            zones.append(
                HotZone(
                    start=zone_start,
                    end=prev_time,
                    peak_rate=round(zone_peak, 6),
                    cue_orders=sorted(zone_cues),
                )
            )

    return zones
