"""``data/queue.json`` の配信時刻から配信用ワークフロー YAML を動的生成する。

GitHub Actions は 1 つの YAML につき cron を 60 個までしか登録できないため、
1 日の配信時刻が 60 件を超える場合はファイルを自動的に分割する。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.scheduler import cron_expression, to_jst, to_utc

logger = logging.getLogger(__name__)

MAX_CRON_PER_FILE = 60
GENERATED_HEADER = "# このファイルは src/batch_generator.py によって自動生成されます。手動で編集しないでください。"


def workflow_filename(basename: str, index: int) -> str:
    """1 つ目は ``publisher.yml``、2 つ目以降は ``publisher_2.yml`` とする。"""
    return f"{basename}.yml" if index == 0 else f"{basename}_{index + 1}.yml"


def managed_filename_pattern(basename: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(basename)}(?:_[0-9]+)?\.ya?ml$")


def collect_schedule_times(queue: dict[str, Any]) -> list[datetime]:
    """queue から未送信投稿の配信時刻（UTC・分単位で重複排除）を集める。"""
    seen: dict[tuple[int, int, int, int], datetime] = {}
    for post in iter_queue_posts(queue):
        if post.get("status") not in (None, "", "pending"):
            continue
        raw = post.get("scheduled_at_utc") or post.get("scheduled_at")
        if not raw:
            continue
        dt = to_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        key = (dt.month, dt.day, dt.hour, dt.minute)
        seen.setdefault(key, dt.replace(second=0, microsecond=0))
    return sorted(seen.values())


def iter_queue_posts(queue: dict[str, Any]) -> list[dict[str, Any]]:
    """アカウント別 queue から全投稿をフラットに取り出す。"""
    posts: list[dict[str, Any]] = []
    accounts = (queue or {}).get("accounts") or {}
    if isinstance(accounts, dict):
        for account_id, entry in accounts.items():
            for post in (entry or {}).get("posts", []) or []:
                post.setdefault("account_id", account_id)
                posts.append(post)
    for post in (queue or {}).get("posts", []) or []:  # フラット形式の互換
        posts.append(post)
    return posts


def _env_block(secret_names: Sequence[str], indent: str) -> str:
    lines = []
    for name in secret_names:
        lines.append(f"{indent}{name}: ${{{{ secrets.{name} }}}}")
    return "\n".join(lines)


def render_workflow(
    workflow_name: str,
    schedule_times: Sequence[datetime],
    part_index: int,
    part_total: int,
    target_date: str = "",
    python_version: str = "3.11",
    secret_names: Sequence[str] = (),
    generated_at: str = "",
) -> str:
    """配信用ワークフロー YAML の中身を組み立てる。"""
    lines: list[str] = [
        GENERATED_HEADER,
        f"# 対象日 (JST): {target_date or '-'} / パート {part_index + 1}/{max(part_total, 1)}",
        f"# 生成日時 (UTC): {generated_at or '-'}",
        f"# cron 登録数: {len(schedule_times)} (上限 {MAX_CRON_PER_FILE}/ファイル)",
        f"name: {workflow_name}",
        "",
        "on:",
        "  workflow_dispatch:",
    ]
    if schedule_times:
        lines.append("  schedule:")
        for dt_utc in schedule_times:
            jst = to_jst(dt_utc)
            lines.append(
                f'    - cron: "{cron_expression(dt_utc)}"  # JST {jst.strftime("%Y-%m-%d %H:%M")}'
            )
    else:
        lines.append("  # 配信予定がないため schedule は登録されていません")

    lines += [
        "",
        "concurrency:",
        f"  group: {workflow_name.lower().replace(' ', '-')}",
        "  cancel-in-progress: false",
        "",
        "jobs:",
        "  publish:",
        "    runs-on: ubuntu-latest",
        "    permissions:",
        "      contents: write",
        "    steps:",
        "      - name: Checkout",
        "        uses: actions/checkout@v4",
        "        with:",
        "          token: ${{ secrets.WORKFLOW_TOKEN || secrets.GITHUB_TOKEN }}",
        "      - name: Setup Python",
        "        uses: actions/setup-python@v5",
        "        with:",
        f'          python-version: "{python_version}"',
        "          cache: pip",
        "      - name: Install dependencies",
        "        run: pip install -r requirements.txt",
        "      - name: Publish scheduled posts",
        "        env:",
        "          PYTHONUNBUFFERED: \"1\"",
    ]
    env_lines = _env_block(secret_names, "          ")
    if env_lines:
        lines.append(env_lines)
    lines += [
        "        run: python -m src.publisher",
        "      - name: Commit & push state",
        "        run: 'bash scripts/commit_and_push.sh \"chore: publish scheduled posts\"'",
        "",
    ]
    return "\n".join(lines)


def generate_publisher_workflows(
    schedule_times: Iterable[datetime],
    workflow_dir: Path,
    basename: str = "publisher",
    max_cron_per_file: int = MAX_CRON_PER_FILE,
    python_version: str = "3.11",
    secret_names: Sequence[str] = (),
    target_date: str = "",
    generated_at: str = "",
) -> dict[str, Any]:
    """配信用ワークフローを生成・上書きし、不要になった分割ファイルを削除する。"""
    workflow_dir = Path(workflow_dir)
    workflow_dir.mkdir(parents=True, exist_ok=True)
    max_cron_per_file = max(1, min(int(max_cron_per_file), MAX_CRON_PER_FILE))

    times = sorted({to_utc(dt).replace(second=0, microsecond=0) for dt in schedule_times})
    chunks = [times[i : i + max_cron_per_file] for i in range(0, len(times), max_cron_per_file)]
    if not chunks:
        chunks = [[]]

    written: list[str] = []
    for index, chunk in enumerate(chunks):
        filename = workflow_filename(basename, index)
        workflow_name = f"Publisher {index + 1}" if len(chunks) > 1 else "Publisher"
        content = render_workflow(
            workflow_name=workflow_name,
            schedule_times=chunk,
            part_index=index,
            part_total=len(chunks),
            target_date=target_date,
            python_version=python_version,
            secret_names=secret_names,
            generated_at=generated_at,
        )
        path = workflow_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(filename)
        logger.info("配信ワークフローを生成しました: %s (cron %s 件)", filename, len(chunk))

    # 前日より投稿数が減った場合に残る分割ファイルを削除する
    removed: list[str] = []
    pattern = managed_filename_pattern(basename)
    for path in sorted(workflow_dir.iterdir()):
        if path.is_file() and pattern.match(path.name) and path.name not in written:
            path.unlink()
            removed.append(path.name)
            logger.info("不要になった配信ワークフローを削除しました: %s", path.name)

    return {
        "files": written,
        "removed": removed,
        "cron_count": len(times),
        "file_count": len(chunks),
    }
