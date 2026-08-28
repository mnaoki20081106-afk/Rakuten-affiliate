"""配信用ワークフローの動的生成（60 cron 制限による分割）のテスト。"""

from datetime import datetime, timedelta, timezone

import yaml

from src.workflow_generator import (
    collect_schedule_times,
    generate_publisher_workflows,
    iter_queue_posts,
    workflow_filename,
)

BASE = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)


def _times(count, step_minutes=7):
    return [BASE + timedelta(minutes=step_minutes * i) for i in range(count)]


def _crons(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [entry["cron"] for entry in data[True].get("schedule", [])]  # "on" は True になる


def test_ファイル名の規則(tmp_path):
    assert workflow_filename("publisher", 0) == "publisher.yml"
    assert workflow_filename("publisher", 1) == "publisher_2.yml"
    assert workflow_filename("publisher", 2) == "publisher_3.yml"


def test_60件以下なら1ファイル(tmp_path):
    result = generate_publisher_workflows(_times(60), tmp_path)
    assert result["files"] == ["publisher.yml"]
    assert len(_crons(tmp_path / "publisher.yml")) == 60


def test_60件を超えるとファイルが自動分割される(tmp_path):
    result = generate_publisher_workflows(_times(130), tmp_path)
    assert result["files"] == ["publisher.yml", "publisher_2.yml", "publisher_3.yml"]
    assert [len(_crons(tmp_path / f)) for f in result["files"]] == [60, 60, 10]
    # 全 cron が重複なく登録されている
    all_crons = [c for f in result["files"] for c in _crons(tmp_path / f)]
    assert len(all_crons) == len(set(all_crons)) == 130


def test_投稿が減ると余分な分割ファイルは削除される(tmp_path):
    generate_publisher_workflows(_times(130), tmp_path)
    result = generate_publisher_workflows(_times(5), tmp_path)
    assert result["removed"] == ["publisher_2.yml", "publisher_3.yml"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["publisher.yml"]


def test_配信予定が無くてもファイルは生成される(tmp_path):
    result = generate_publisher_workflows([], tmp_path)
    data = yaml.safe_load((tmp_path / "publisher.yml").read_text(encoding="utf-8"))
    assert result["cron_count"] == 0
    assert "schedule" not in data[True]
    assert "workflow_dispatch" in data[True]


def test_同一分の予定は1つのcronにまとめられる(tmp_path):
    times = [BASE, BASE, BASE + timedelta(seconds=30), BASE + timedelta(minutes=5)]
    result = generate_publisher_workflows(times, tmp_path)
    assert result["cron_count"] == 2


def test_アカウントのシークレット名が環境変数として埋め込まれる(tmp_path):
    generate_publisher_workflows(
        _times(2), tmp_path, secret_names=["THREADS_TOKEN_A", "THREADS_TOKEN_B"]
    )
    data = yaml.safe_load((tmp_path / "publisher.yml").read_text(encoding="utf-8"))
    env = data["jobs"]["publish"]["steps"][3]["env"]
    assert env["THREADS_TOKEN_A"] == "${{ secrets.THREADS_TOKEN_A }}"
    assert env["THREADS_TOKEN_B"] == "${{ secrets.THREADS_TOKEN_B }}"


def test_queueから未送信の配信時刻だけを集める():
    queue = {
        "accounts": {
            "a1": {
                "posts": [
                    {"scheduled_at_utc": "2026-08-28T22:00:00+00:00", "status": "pending"},
                    {"scheduled_at_utc": "2026-08-28T23:00:00+00:00", "status": "sent"},
                ]
            },
            "a2": {"posts": [{"scheduled_at_utc": "2026-08-28T22:00:00+00:00", "status": "pending"}]},
        }
    }
    assert len(iter_queue_posts(queue)) == 3
    times = collect_schedule_times(queue)
    assert [t.isoformat() for t in times] == ["2026-08-28T22:00:00+00:00"]
