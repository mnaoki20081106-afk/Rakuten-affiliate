"""再投稿処理（過去 1 週間の上位 3 件を月・水・金へ割り当て）のテスト。"""

import json
from datetime import datetime, timedelta, timezone

from src.reposter import pick_for_weekday, recent_reposted_media_ids, run, top_posts

MON = datetime(2026, 8, 31, 10, 30, tzinfo=timezone.utc)  # JST 月 19:30
WED = MON + timedelta(days=2)
FRI = MON + timedelta(days=4)
PUBLISHED = datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)
RANK_MAP = {"0": 0, "2": 1, "4": 2}


def _history(likes, account_id="a1"):
    return {
        "posts": [
            {
                "post_id": f"{account_id}-{i}",
                "account_id": account_id,
                "account_name": "テスト",
                "media_id": f"media-{i}",
                "body": f"過去投稿{i}の本文",
                "affiliate_url": f"https://hb.afl.rakuten.co.jp/{i}",
                "pr_text": "※PR",
                "item": {"item_code": f"c{i}"},
                "published_at": (PUBLISHED + timedelta(minutes=i)).isoformat(),
                "is_repost": False,
                "likes": like,
            }
            for i, like in enumerate(likes)
        ]
    }


def _setup(tmp_path, likes):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "post_history.json").write_text(
        json.dumps(_history(likes), ensure_ascii=False), encoding="utf-8"
    )
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps({"accounts": [{"id": "a1", "name": "テスト"}]}),
        encoding="utf-8",
    )
    return data_dir, accounts_file


# ----------------------------------------------------------------------
def test_いいね数上位3件が抽出される():
    history = _history([120, 480, 300, 55, 210, 390, 90])
    top = top_posts(history, "a1", PUBLISHED - timedelta(days=1), top_n=3)
    assert [p["media_id"] for p in top] == ["media-1", "media-5", "media-2"]


def test_再投稿分は集計対象から除かれる():
    history = _history([100, 200])
    history["posts"].append({**history["posts"][0], "media_id": "repost", "is_repost": True, "likes": 999})
    top = top_posts(history, "a1", PUBLISHED - timedelta(days=1), top_n=3)
    assert [p["media_id"] for p in top] == ["media-1", "media-0"]


def test_期間外の投稿は集計されない():
    history = _history([100, 200])
    top = top_posts(history, "a1", PUBLISHED + timedelta(days=1), top_n=3)
    assert top == []


def test_曜日ごとに1位2位3位が割り当てられる():
    candidates = [{"media_id": "1st"}, {"media_id": "2nd"}, {"media_id": "3rd"}]
    assert pick_for_weekday(candidates, 0, RANK_MAP)["media_id"] == "1st"  # 月
    assert pick_for_weekday(candidates, 2, RANK_MAP)["media_id"] == "2nd"  # 水
    assert pick_for_weekday(candidates, 4, RANK_MAP)["media_id"] == "3rd"  # 金


def test_再投稿済みのものは繰り上げて選ばれる():
    candidates = [{"media_id": "1st"}, {"media_id": "2nd"}, {"media_id": "3rd"}]
    picked = pick_for_weekday(candidates, 0, RANK_MAP, excluded_media_ids={"1st"})
    assert picked["media_id"] == "2nd"


def test_候補が全て再投稿済みなら選ばれない():
    candidates = [{"media_id": "1st"}]
    assert pick_for_weekday(candidates, 0, RANK_MAP, excluded_media_ids={"1st"}) is None


def test_クールダウン中の元投稿を検出する():
    history = {
        "posts": [
            {"account_id": "a1", "is_repost": True, "repost_of": "media-1",
             "published_at": MON.isoformat()},
            {"account_id": "a1", "is_repost": True, "repost_of": "media-9",
             "published_at": (MON - timedelta(days=30)).isoformat()},
        ]
    }
    assert recent_reposted_media_ids(history, "a1", MON - timedelta(days=14)) == {"media-1"}


def test_月水金で1位2位3位が順に再投稿される(tmp_path):
    data_dir, accounts_file = _setup(tmp_path, [120, 480, 300, 55, 210, 390, 90])
    reposted = []
    for now in (MON, WED, FRI):
        summary = run(
            now_utc=now,
            data_dir=data_dir,
            accounts_file=accounts_file,
            skip_insights=True,
            dry_run=True,
        )
        assert len(summary["reposted"]) == 1
        reposted.append(summary["reposted"][0]["source_media_id"])
    assert reposted == ["media-1", "media-5", "media-2"]  # 480 → 390 → 300


def test_再投稿は元の本文とまったく同じ内容で送られる(tmp_path):
    data_dir, accounts_file = _setup(tmp_path, [100, 500])
    run(now_utc=MON, data_dir=data_dir, accounts_file=accounts_file, skip_insights=True, dry_run=True)

    history = json.loads((data_dir / "post_history.json").read_text(encoding="utf-8"))
    original = next(p for p in history["posts"] if p["media_id"] == "media-1")
    repost = next(p for p in history["posts"] if p.get("is_repost"))
    assert repost["body"] == original["body"]
    assert repost["affiliate_url"] == original["affiliate_url"]
    assert repost["repost_of"] == "media-1"
    assert repost["source_likes"] == 500
    assert repost["reply_media_id"]  # PR リプライも送信されている


def test_候補が無ければスキップされる(tmp_path):
    data_dir, accounts_file = _setup(tmp_path, [])
    summary = run(
        now_utc=MON, data_dir=data_dir, accounts_file=accounts_file, skip_insights=True, dry_run=True
    )
    assert summary["reposted"] == []
    assert summary["skipped"] == [{"account_id": "a1", "reason": "候補なし"}]
