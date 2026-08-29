"""配信スケジュールの生成。

翌日の活動時間（既定 7:00〜23:00）を 7 つの時間枠に分割し、
各枠に ±15〜30 分のランダムなゆらぎを付けて「不規則な等間隔」にする。
そのうちゴールデンタイム（通勤時間帯 7〜8 時台 / 帰宅後 20〜22 時台）に
入る枠を 4 つ確保し、売れ筋ランキング上位の商品を優先的に割り当てる。
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any

from .utils import parse_hhmm


def _window_bounds(window: Any, base_day: date) -> tuple[datetime, datetime] | None:
    """``["07:00", "09:00"]`` 形式の設定を datetime の組にする。"""
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return None
    start = parse_hhmm(str(window[0]), base_day)
    end = parse_hhmm(str(window[1]), base_day)
    if end <= start:
        return None
    return start, end


def _even_points(start: datetime, end: datetime, count: int) -> list[datetime]:
    """区間 ``[start, end]`` の内側に ``count`` 個の点を等間隔で置く。

    両端ちょうどに置かないよう、区間を ``count`` 等分した各区画の中央を使う。
    """
    if count <= 0:
        return []
    span = (end - start).total_seconds()
    step = span / count
    return [start + timedelta(seconds=step * (i + 0.5)) for i in range(count)]


def _allocate(total: int, weights: list[float]) -> list[int]:
    """重み（区間の長さ）に応じて ``total`` 個を最大剰余法で配分する。"""
    if not weights or total <= 0:
        return [0] * len(weights)
    weight_sum = sum(weights) or 1.0
    exact = [total * w / weight_sum for w in weights]
    counts = [int(x) for x in exact]
    remainder = total - sum(counts)
    order = sorted(range(len(weights)), key=lambda i: exact[i] - counts[i], reverse=True)
    for i in range(remainder):
        counts[order[i % len(order)]] += 1
    return counts


def build_base_slots(base_day: date, schedule: dict[str, Any]) -> list[dict[str, Any]]:
    """ゆらぎを付ける前のベース時間枠を作る。

    ゴールデンタイムに ``golden_slot_count`` 枠、残りを非ゴールデンの時間帯に
    均等配置し、合計が ``slot_count`` 枠になるようにする。
    """
    active_start = parse_hhmm(str(schedule.get("active_start", "07:00")), base_day)
    active_end = parse_hhmm(str(schedule.get("active_end", "23:00")), base_day)
    if active_end <= active_start:
        raise ValueError("活動終了時刻は開始時刻より後にしてください。")

    slot_count = max(1, int(schedule.get("slot_count", 7)))
    golden_target = max(0, min(slot_count, int(schedule.get("golden_slot_count", 4))))

    # 活動時間と重なる部分だけをゴールデンタイムとして採用する
    golden_windows: list[tuple[datetime, datetime]] = []
    for window in schedule.get("golden_windows") or []:
        bounds = _window_bounds(window, base_day)
        if not bounds:
            continue
        start = max(bounds[0], active_start)
        end = min(bounds[1], active_end)
        if end > start:
            golden_windows.append((start, end))
    golden_windows.sort()

    # 重なり合うゴールデンタイム指定はマージする
    merged: list[list[datetime]] = []
    for start, end in golden_windows:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    golden_windows = [(s, e) for s, e in merged]

    if not golden_windows:
        golden_target = 0

    # ゴールデンタイム以外の区間（活動時間からゴールデンタイムを引いた残り）
    normal_windows: list[tuple[datetime, datetime]] = []
    cursor = active_start
    for start, end in golden_windows:
        if start > cursor:
            normal_windows.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < active_end:
        normal_windows.append((cursor, active_end))

    normal_target = slot_count - golden_target
    if not normal_windows:
        # 活動時間がすべてゴールデンタイムなら、全枠をゴールデン扱いにする
        golden_target, normal_target = slot_count, 0

    slots: list[dict[str, Any]] = []
    for windows, count, slot_type in (
        (golden_windows, golden_target, "golden"),
        (normal_windows, normal_target, "normal"),
    ):
        if count <= 0 or not windows:
            continue
        weights = [(end - start).total_seconds() for start, end in windows]
        for (start, end), n in zip(windows, _allocate(count, weights)):
            for point in _even_points(start, end, n):
                slots.append({"base_at": point, "slot_type": slot_type})

    slots.sort(key=lambda s: s["base_at"])
    return slots


def apply_jitter(
    slots: list[dict[str, Any]],
    schedule: dict[str, Any],
    base_day: date,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """各枠に ±15〜30 分のゆらぎを付け、不規則な等間隔にする。

    活動時間からはみ出さないよう丸め、枠同士が近づきすぎないよう
    ``min_gap_minutes`` 以上の間隔を確保する。
    """
    rng = rng or random.Random()
    active_start = parse_hhmm(str(schedule.get("active_start", "07:00")), base_day)
    active_end = parse_hhmm(str(schedule.get("active_end", "23:00")), base_day)
    jitter_min = int(schedule.get("jitter_min_minutes", 15))
    jitter_max = max(jitter_min, int(schedule.get("jitter_max_minutes", 30)))
    min_gap = timedelta(minutes=int(schedule.get("min_gap_minutes", 30)))

    jittered: list[dict[str, Any]] = []
    previous: datetime | None = None
    for slot in sorted(slots, key=lambda s: s["base_at"]):
        offset = rng.uniform(jitter_min, jitter_max) * rng.choice([-1, 1])
        moment = slot["base_at"] + timedelta(minutes=offset)
        moment = moment.replace(second=0, microsecond=0)
        moment = min(max(moment, active_start), active_end)
        if previous is not None and moment - previous < min_gap:
            moment = min(previous + min_gap, active_end)
        previous = moment
        jittered.append({**slot, "scheduled_at": moment})
    return jittered


def assign_products(
    slots: list[dict[str, Any]], products: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """売れ筋ランキング上位の商品をゴールデンタイムの枠に優先的に割り当てる。

    ゴールデン枠を時間順に、ランキング 1 位から順に埋め、
    残りの商品を非ゴールデン枠に時間順で割り当てる。
    """
    ranked = sorted(products, key=lambda p: p.get("rank", 999))
    golden = [s for s in slots if s["slot_type"] == "golden"]
    normal = [s for s in slots if s["slot_type"] != "golden"]

    assignments: list[dict[str, Any]] = []
    cursor = 0
    for group in (golden, normal):
        for slot in sorted(group, key=lambda s: s["scheduled_at"]):
            if cursor >= len(ranked):
                break
            assignments.append({**slot, "product": ranked[cursor]})
            cursor += 1

    assignments.sort(key=lambda a: a["scheduled_at"])
    return assignments


def build_schedule(
    base_day: date,
    schedule: dict[str, Any],
    products: list[dict[str, Any]],
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """ベース枠の生成 → ゆらぎ付与 → 商品割り当てまでを一括で行う。"""
    base_slots = build_base_slots(base_day, schedule)
    jittered = apply_jitter(base_slots, schedule, base_day, rng=rng)
    return assign_products(jittered, products)
