"""スケジュール割り当て（7 枠・ゆらぎ・ゴールデンタイム優先）のテスト。"""

import random
from datetime import date, datetime

from src.scheduler import (
    JST,
    assign_items_to_slots,
    build_time_slots,
    cron_expression,
    is_golden_time,
    parse_hhmm,
    to_utc,
)

GOLDEN = [["07:00", "09:00"], ["20:00", "23:00"]]


def _slots(seed, **kwargs):
    return build_time_slots(date(2026, 8, 29), rng=random.Random(seed), **kwargs)


def test_必ず指定件数の枠が活動時間帯の中に作られる():
    for seed in range(50):
        slots = _slots(seed)
        assert len(slots) == 7
        for slot in slots:
            minutes = slot.hour * 60 + slot.minute
            assert parse_hhmm("07:00") <= minutes <= parse_hhmm("23:00")


def test_枠は昇順で最小間隔が保たれる():
    for seed in range(50):
        slots = _slots(seed, min_gap=20)
        assert slots == sorted(slots)
        gaps = [
            (b - a).total_seconds() / 60 for a, b in zip(slots, slots[1:])
        ]
        assert min(gaps) >= 20


def test_ゆらぎが枠ごとに適用される():
    # 同じベース時刻でも seed が違えば分単位でばらける
    patterns = {tuple(s.strftime("%H:%M") for s in _slots(seed)) for seed in range(20)}
    assert len(patterns) > 15


def test_ゆらぎ幅は指定範囲内に収まる():
    span = parse_hhmm("23:00") - parse_hhmm("07:00")
    window = span / 7
    for seed in range(30):
        slots = _slots(seed, jitter_min=15, jitter_max=30, min_gap=0)
        for index, slot in enumerate(slots):
            base = parse_hhmm("07:00") + window * (index + 0.5)
            delta = abs((slot.hour * 60 + slot.minute) - base)
            assert 15 - 1 <= delta <= 30 + 1


def test_ゴールデンタイム判定():
    assert is_golden_time(datetime(2026, 8, 29, 7, 30), GOLDEN)
    assert is_golden_time(datetime(2026, 8, 29, 8, 59), GOLDEN)
    assert not is_golden_time(datetime(2026, 8, 29, 9, 0), GOLDEN)
    assert is_golden_time(datetime(2026, 8, 29, 22, 59), GOLDEN)
    assert not is_golden_time(datetime(2026, 8, 29, 12, 0), GOLDEN)


def test_ランキング上位がゴールデンタイムへ優先的に割り当てられる():
    items = [{"item_code": f"c{i}", "rank": i} for i in range(1, 8)]
    slots = _slots(3)
    pairs = assign_items_to_slots(items, slots, GOLDEN)

    assert [dt for _, dt in pairs] == sorted(slots)  # 戻り値は時刻順
    golden_ranks = sorted(it["rank"] for it, dt in pairs if is_golden_time(dt, GOLDEN))
    normal_ranks = sorted(it["rank"] for it, dt in pairs if not is_golden_time(dt, GOLDEN))
    assert golden_ranks  # ゴールデン枠が存在する
    assert max(golden_ranks) < min(normal_ranks)  # 上位ほどゴールデンへ


def test_cron式はUTCへ変換され日付が固定される():
    # JST 08/29 07:47 は UTC 08/28 22:47
    dt = datetime(2026, 8, 29, 7, 47, tzinfo=JST)
    assert cron_expression(to_utc(dt)) == "47 22 28 8 *"
    assert cron_expression(to_utc(dt), pin_date=False) == "47 22 * * *"
