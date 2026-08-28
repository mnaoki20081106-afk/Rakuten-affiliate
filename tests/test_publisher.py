"""配信処理（起動時刻に対応する投稿の抽出・親投稿＋PR リプライ）のテスト。"""

import json
from datetime import datetime, timedelta, timezone

from src.publisher import publish_post, run, select_due_posts
from src.threads_api import ThreadsAPIError, build_pr_reply

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)


def _post(post_id, offset_minutes, status="pending", account_id="a1"):
    return {
        "id": post_id,
        "account_id": account_id,
        "account_name": "テスト",
        "status": status,
        "body": f"{post_id} の本文",
        "affiliate_url": f"https://hb.afl.rakuten.co.jp/{post_id}",
        "pr_text": "※PR",
        "item": {"item_code": post_id, "item_name": "商品"},
        "scheduled_at_utc": (NOW + timedelta(minutes=offset_minutes)).isoformat(),
        "scheduled_at_jst": "",
        "attempts": 0,
    }


def _queue(posts):
    return {"accounts": {"a1": {"account_name": "テスト", "posts": posts}}}


class FakeClient:
    """Threads クライアントの差し替え用。"""

    def __init__(self, fail_on_reply=False, fail_on_parent=False):
        self.calls = []
        self.fail_on_reply = fail_on_reply
        self.fail_on_parent = fail_on_parent

    def post_text(self, text, reply_to_id=None, link_attachment=None):
        if reply_to_id is None and self.fail_on_parent:
            raise ThreadsAPIError("親投稿に失敗")
        if reply_to_id is not None and self.fail_on_reply:
            raise ThreadsAPIError("リプライに失敗")
        self.calls.append((text, reply_to_id))
        return f"media-{len(self.calls)}"


# ----------------------------------------------------------------------
def test_起動時刻に対応する投稿だけを抽出する():
    queue = _queue([_post("past", -10), _post("now", 0), _post("future", 30)])
    due, expired = select_due_posts(queue, NOW, window_before_minutes=5, window_after_minutes=60)
    assert [p["id"] for p in due] == ["past", "now"]
    assert expired == []


def test_窓を過ぎた投稿は期限切れになる():
    queue = _queue([_post("stale", -120), _post("now", 0)])
    due, expired = select_due_posts(queue, NOW, window_after_minutes=60)
    assert [p["id"] for p in due] == ["now"]
    assert [p["id"] for p in expired] == ["stale"]


def test_送信済みの投稿は再送されない():
    queue = _queue([_post("done", 0, status="sent"), _post("todo", 0)])
    due, _ = select_due_posts(queue, NOW)
    assert [p["id"] for p in due] == ["todo"]


def test_同一時刻の複数投稿は予約時刻順に並ぶ():
    queue = _queue([_post("b", 0), _post("a", -1), _post("c", 0)])
    due, _ = select_due_posts(queue, NOW)
    assert [p["id"] for p in due] == ["a", "b", "c"]


def test_親投稿の直後にPRリプライが送られる():
    client = FakeClient()
    post = _post("p1", 0)
    result = publish_post(client, post)

    assert result["media_id"] == "media-1"
    assert result["reply_media_id"] == "media-2"
    parent_text, parent_reply_to = client.calls[0]
    reply_text, reply_to = client.calls[1]
    assert parent_reply_to is None
    assert parent_text == "p1 の本文"
    assert reply_to == "media-1"  # 親投稿の ID にぶら下げる
    assert "※PR" in reply_text
    assert "https://hb.afl.rakuten.co.jp/p1" in reply_text


def test_リプライが失敗しても親投稿は送信済みとして扱う():
    result = publish_post(FakeClient(fail_on_reply=True), _post("p1", 0))
    assert result["media_id"] == "media-1"
    assert result["reply_media_id"] == ""
    assert "リプライに失敗" in result["reply_error"]


def test_本文が空なら送信しない():
    post = _post("p1", 0)
    post["body"] = "  "
    try:
        publish_post(FakeClient(), post)
    except ThreadsAPIError as exc:
        assert "本文が空" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("例外が発生しませんでした")


def test_PRリプライの組み立て():
    assert build_pr_reply("https://x", "※PR") == "※PR\nhttps://x"
    assert build_pr_reply("", "※PR") == "※PR"


def test_ドライランで実行するとキューと履歴が更新される(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "queue.json").write_text(
        json.dumps(_queue([_post("p1", 0), _post("p2", -5), _post("p3", 120)]), ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "post_history.json").write_text('{"posts": []}', encoding="utf-8")
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps({"accounts": [{"id": "a1", "name": "テスト", "threads_access_token": "t"}]}),
        encoding="utf-8",
    )

    summary = run(now_utc=NOW, data_dir=data_dir, accounts_file=accounts_file, dry_run=True)

    assert len(summary["sent"]) == 2
    assert not summary["failed"]

    queue = json.loads((data_dir / "queue.json").read_text(encoding="utf-8"))
    statuses = {p["id"]: p["status"] for p in queue["accounts"]["a1"]["posts"]}
    assert statuses == {"p1": "sent", "p2": "sent", "p3": "pending"}

    history = json.loads((data_dir / "post_history.json").read_text(encoding="utf-8"))
    assert len(history["posts"]) == 2
    assert all(entry["media_id"] and entry["reply_media_id"] for entry in history["posts"])
    assert all(entry["is_repost"] is False for entry in history["posts"])
