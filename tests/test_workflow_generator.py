"""配信用ワークフローの動的生成（60 cron 制限による分割）のテスト。"""

from datetime import datetime, timedelta, timezone

import yaml

from src.config import Account
from src.workflow_generator import (
    collect_schedule_times,
    generate_publisher_workflows,
    generate_reposter_workflow,
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


def _env_of(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    job = next(iter(data["jobs"].values()))
    return next(step for step in job["steps"] if "env" in step)["env"]


def test_アカウントごとのシークレットが索引記法で参照される(tmp_path):
    accounts = [Account(id="beauty_lab", name="a"), Account(id="gadget_note", name="b")]
    generate_publisher_workflows(_times(2), tmp_path, accounts=accounts)
    env = _env_of(tmp_path / "publisher.yml")
    # シークレット名はアカウントIDから動的に決まる
    assert env["THREADS_TOKEN_BEAUTY_LAB"] == "${{ secrets['THREADS_TOKEN_BEAUTY_LAB'] }}"
    assert env["THREADS_TOKEN_GADGET_NOTE"] == "${{ secrets['THREADS_TOKEN_GADGET_NOTE'] }}"


def test_生成YAMLにトークンの実体が現れない(tmp_path):
    accounts = [Account(id="beauty_lab", name="a")]
    generate_publisher_workflows(_times(2), tmp_path, accounts=accounts)
    generate_reposter_workflow(accounts, tmp_path)
    for name in ("publisher.yml", "reposter.yml"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        # 参照だけが書かれ、値は GitHub が実行時に解決する
        assert "secrets['THREADS_TOKEN_BEAUTY_LAB']" in text
        assert "THDS_" not in text and "ghp_" not in text


def test_再投稿ワークフローも同じシークレット参照で生成される(tmp_path):
    accounts = [Account(id="a1", name="a"), Account(id="a2", name="b")]
    generate_reposter_workflow(accounts, tmp_path)
    data = yaml.safe_load((tmp_path / "reposter.yml").read_text(encoding="utf-8"))
    assert data["name"] == "Reposter"
    assert data[True]["schedule"] == [{"cron": "30 10 * * 1,3,5"}]  # JST 月水金 19:30
    env = _env_of(tmp_path / "reposter.yml")
    assert set(env) >= {"THREADS_TOKEN_A1", "THREADS_TOKEN_A2"}


def test_アカウントが増減しても参照が追随する(tmp_path):
    generate_reposter_workflow([Account(id="a1", name="a")], tmp_path)
    assert "THREADS_TOKEN_A1" in _env_of(tmp_path / "reposter.yml")
    generate_reposter_workflow([Account(id="a2", name="b")], tmp_path)
    env = _env_of(tmp_path / "reposter.yml")
    assert "THREADS_TOKEN_A2" in env and "THREADS_TOKEN_A1" not in env


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
