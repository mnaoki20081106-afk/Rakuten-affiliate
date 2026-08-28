"""バッチ処理（重複防止・7 件選択・キュー生成）のテスト。"""

import random
from datetime import date, datetime, timedelta, timezone

from src.batch_generator import (
    BatchGenerator,
    recently_used_codes,
    record_used_items,
)
from src.config import Account

NOW = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def _account(**kwargs):
    defaults = dict(
        id="a1", name="テスト", genre="美容", worldview="夜", strength="自腹",
        tone="丁寧語", search_keywords=["化粧水"], posts_per_day=7,
    )
    defaults.update(kwargs)
    return Account(**defaults)


def _candidates(count=50, prefix="shop:item"):
    return [
        {
            "item_code": f"{prefix}{i}",
            "item_name": f"商品{i}",
            "rank": i + 1,
            "price": 1000 + i,
            "affiliate_url": f"https://hb.afl.rakuten.co.jp/{i}",
            "shop_name": "s",
            "review_count": 10,
            "review_average": 4.0,
            "item_url": "",
            "image_url": "",
            "keyword": "化粧水",
            "rank_source": "-reviewCount",
        }
        for i in range(count)
    ]


def _generator(tmp_path, **kwargs):
    return BatchGenerator(
        data_dir=tmp_path / "data",
        workflow_dir=tmp_path / "wf",
        dry_run=True,
        rng=random.Random(kwargs.pop("seed", 42)),
        **kwargs,
    )


# ----------------------------------------------------------------------
def test_過去14日以内に使った商品コードだけが除外対象になる():
    used = {
        "accounts": {
            "a1": [
                {"item_code": "recent", "used_at": (NOW - timedelta(days=3)).isoformat()},
                {"item_code": "old", "used_at": (NOW - timedelta(days=20)).isoformat()},
            ]
        }
    }
    codes = recently_used_codes(used, "a1", 14, NOW)
    assert codes == {"recent"}


def test_他アカウントの履歴は除外対象にならない():
    used = {"accounts": {"a2": [{"item_code": "x", "used_at": NOW.isoformat()}]}}
    assert recently_used_codes(used, "a1", 14, NOW) == set()


def test_選択した商品が履歴に記録され古い履歴は削除される():
    used = {"accounts": {"a1": [{"item_code": "ancient", "used_at": (NOW - timedelta(days=200)).isoformat()}]}}
    record_used_items(used, "a1", [{"item_code": "new", "item_name": "n", "rank": 1}], "2026-08-29", NOW)
    codes = {e["item_code"] for e in used["accounts"]["a1"]}
    assert codes == {"new"}  # 200 日前の履歴は保持期間外


def test_紹介済み商品を除外して必ず7件を選ぶ(tmp_path):
    generator = _generator(tmp_path)
    candidates = _candidates(50)
    excluded = {c["item_code"] for c in candidates[:40]}
    selected, spare, warnings = generator.select_items(_account(), candidates, excluded, 7)

    assert len(selected) == 7
    assert not warnings
    assert {c["item_code"] for c in selected} & excluded == set()
    assert len(spare) == 3  # 残ったフレッシュ候補


def test_候補が足りない場合は紹介済みを補充して7件を確保する(tmp_path):
    generator = _generator(tmp_path)
    candidates = _candidates(10)
    excluded = {c["item_code"] for c in candidates[:6]}  # フレッシュは 4 件のみ
    selected, _, warnings = generator.select_items(_account(), candidates, excluded, 7)

    assert len(selected) == 7
    assert warnings and "紹介済み" in warnings[0]


def test_選択はランダムで固定順ではない(tmp_path):
    candidates = _candidates(50)
    picks = set()
    for seed in range(10):
        generator = _generator(tmp_path, seed=seed)
        selected, _, _ = generator.select_items(_account(), candidates, set(), 7)
        picks.add(tuple(sorted(c["item_code"] for c in selected)))
    assert len(picks) > 5


def test_バッチ実行で7件のキューとcronが生成される(tmp_path):
    generator = _generator(tmp_path)
    accounts = [_account(id=f"a{i}", name=f"アカウント{i}") for i in range(3)]
    summary = generator.run(accounts, target_date=date(2026, 8, 29), reference=NOW)

    assert summary["total_posts"] == 21
    assert not summary["errors"]
    assert summary["workflows"]["file_count"] == 1

    import json

    queue = json.loads((tmp_path / "data" / "queue.json").read_text(encoding="utf-8"))
    assert set(queue["accounts"]) == {"a0", "a1", "a2"}
    posts = queue["accounts"]["a0"]["posts"]
    assert len(posts) == 7
    assert [p["slot_index"] for p in posts] == list(range(1, 8))
    assert all(p["status"] == "pending" for p in posts)
    assert all(p["body"] for p in posts)
    assert all(p["cron"] for p in posts)
    assert all(p["scheduled_at_jst"].startswith("2026-08-29") for p in posts)
    # 売れ筋ランキング順位が保持されている
    assert all(p["item"]["rank"] > 0 for p in posts)


def test_連続実行で同じ商品が選ばれない(tmp_path):
    import json

    accounts = [_account(id="a1")]
    _generator(tmp_path, seed=1).run(accounts, target_date=date(2026, 8, 29), reference=NOW)
    day1 = {
        p["item"]["item_code"]
        for p in json.loads((tmp_path / "data" / "queue.json").read_text(encoding="utf-8"))["accounts"]["a1"]["posts"]
    }
    _generator(tmp_path, seed=2).run(
        accounts, target_date=date(2026, 8, 30), reference=NOW + timedelta(days=1)
    )
    day2 = {
        p["item"]["item_code"]
        for p in json.loads((tmp_path / "data" / "queue.json").read_text(encoding="utf-8"))["accounts"]["a1"]["posts"]
    }
    assert len(day1) == len(day2) == 7
    assert day1 & day2 == set()


def test_1アカウントが失敗しても他のアカウントは処理される(tmp_path):
    generator = _generator(tmp_path)

    original = generator.research_candidates

    def flaky(account):
        if account.id == "bad":
            raise RuntimeError("楽天 API エラー")
        return original(account)

    generator.research_candidates = flaky
    summary = generator.run(
        [_account(id="bad"), _account(id="good")], target_date=date(2026, 8, 29), reference=NOW
    )
    assert [e["account_id"] for e in summary["errors"]] == ["bad"]
    assert [a["account_id"] for a in summary["accounts"]] == ["good"]
    assert summary["total_posts"] == 7
