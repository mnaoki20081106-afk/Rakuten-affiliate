"""投稿スケジュール（7 枠 + ゆらぎ + ゴールデンタイム優先）の割り当て。"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc


def parse_hhmm(value: str) -> int:
    """``"07:00"`` を 0 時からの分数へ変換する。"""
    hours, _, minutes = str(value).partition(":")
    return int(hours) * 60 + int(minutes or 0)


def minutes_to_time(total_minutes: int) -> time:
    total_minutes = max(0, min(24 * 60 - 1, int(total_minutes)))
    return time(hour=total_minutes // 60, minute=total_minutes % 60)


def build_time_slots(
    target_date: date,
    count: int = 7,
    start: str = "07:00",
    end: str = "23:00",
    jitter_min: int = 15,
    jitter_max: int = 30,
    min_gap: int = 20,
    rng: random.Random | None = None,
    tz: ZoneInfo = JST,
) -> list[datetime]:
    """活動時間帯を ``count`` 個の等間隔な枠に分割し、±ゆらぎを加えた時刻を返す。

    - 各枠の中心時刻をベースとし、±(jitter_min〜jitter_max) 分のランダムなゆらぎを加える
    - 活動時間帯からはみ出さないようクランプし、``min_gap`` 分以上の間隔を保証する
    """
    rng = rng or random.Random()
    count = max(1, int(count))
    start_min = parse_hhmm(start)
    end_min = parse_hhmm(end)
    if end_min <= start_min:
        raise ValueError("活動時間帯の終了時刻は開始時刻より後である必要があります")

    span = end_min - start_min
    window = span / count
    # 枠数に対して活動時間帯が狭いと最小間隔を満たせないため、収まる値まで詰める
    min_gap = min(int(min_gap), span // max(1, count - 1)) if count > 1 else 0

    slots: list[int] = []
    for index in range(count):
        base = start_min + window * (index + 0.5)
        magnitude = rng.randint(int(jitter_min), int(jitter_max))
        jitter = magnitude * rng.choice((-1, 1))
        slots.append(int(round(base + jitter)))

    slots.sort()
    # 前方向: 下限と最小間隔を担保する
    for i, value in enumerate(slots):
        lower = start_min if i == 0 else slots[i - 1] + min_gap
        slots[i] = max(value, lower)
    # 後方向: 上限を超えた分を押し戻す（min_gap を詰めてあるので下限は割らない）
    for i in range(len(slots) - 1, -1, -1):
        upper = end_min if i == len(slots) - 1 else slots[i + 1] - min_gap
        slots[i] = min(slots[i], upper)

    return [datetime.combine(target_date, minutes_to_time(m), tzinfo=tz) for m in slots]


def is_golden_time(dt: datetime, golden_ranges: Iterable[Sequence[str]]) -> bool:
    """ゴールデンタイム（朝 7〜8 時台 / 夜 20〜22 時台など）に入るか。"""
    minutes = dt.hour * 60 + dt.minute
    for entry in golden_ranges or []:
        if len(entry) < 2:
            continue
        low, high = parse_hhmm(entry[0]), parse_hhmm(entry[1])
        if low <= minutes < high:
            return True
    return False


def assign_items_to_slots(
    items: Sequence[dict[str, Any]],
    slots: Sequence[datetime],
    golden_ranges: Iterable[Sequence[str]] = (),
) -> list[tuple[dict[str, Any], datetime]]:
    """売れ筋ランキング上位の商品をゴールデンタイム枠へ優先的に割り当てる。

    戻り値は投稿時刻の昇順。
    """
    golden_ranges = list(golden_ranges or [])
    sorted_slots = sorted(slots)
    golden = [s for s in sorted_slots if is_golden_time(s, golden_ranges)]
    normal = [s for s in sorted_slots if not is_golden_time(s, golden_ranges)]
    priority_slots = golden + normal

    ranked = sorted(items, key=lambda it: (int(it.get("rank") or 10**6), it.get("item_code", "")))
    pairs = list(zip(ranked, priority_slots))
    return sorted(pairs, key=lambda pair: pair[1])


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(UTC)


def to_jst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(JST)


def cron_expression(dt_utc: datetime, pin_date: bool = True) -> str:
    """UTC の日時を GitHub Actions の cron 式へ変換する。

    ``pin_date=True`` では日・月を固定し、その日だけ起動するようにする。
    """
    dt_utc = to_utc(dt_utc)
    if pin_date:
        return f"{dt_utc.minute} {dt_utc.hour} {dt_utc.day} {dt_utc.month} *"
    return f"{dt_utc.minute} {dt_utc.hour} * * *"


def now_jst() -> datetime:
    return datetime.now(tz=JST)


def parse_iso(value: str) -> datetime:
    """ISO8601 文字列を timezone 付き datetime へ変換する（Z 表記に対応）。"""
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
