"""``data/queue.json`` の配信時刻から配信用ワークフロー YAML を動的生成する。

GitHub Actions は 1 つの YAML につき cron を 60 個までしか登録できないため、
1 日の配信時刻が 60 件を超える場合はファイルを自動的に分割する。

アカウントの Threads トークンはリポジトリに保存せず GitHub Secrets にのみ置く。
シークレット名はアカウント ID から動的に決まる（``THREADS_TOKEN_<ID大文字>``）ため、
生成する YAML では ``${{ secrets['...'] }}`` の索引記法で参照する。
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
REPOSTER_CRON = "30 10 * * 1,3,5"  # JST 月・水・金 19:30
REPOSTER_FILENAME = "reposter.yml"
TOKEN_REFRESH_CRON = "0 19 * * 0"  # 毎週月曜 JST 04:00
TOKEN_REFRESH_FILENAME = "token_refresh.yml"
GENERATED_HEADER = "# このファイルは src/batch_generator.py によって自動生成されます。手動で編集しないでください。"


# ----------------------------------------------------------------------
# ファイル名 / キュー
# ----------------------------------------------------------------------
def workflow_filename(basename: str, index: int) -> str:
    """1 つ目は ``publisher.yml``、2 つ目以降は ``publisher_2.yml`` とする。"""
    return f"{basename}.yml" if index == 0 else f"{basename}_{index + 1}.yml"


def managed_filename_pattern(basename: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(basename)}(?:_[0-9]+)?\.ya?ml$")


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


# ----------------------------------------------------------------------
# YAML の部品
# ----------------------------------------------------------------------
def secret_name_for(account: Any) -> str:
    """アカウントから Threads トークンのシークレット名を求める。"""
    name = getattr(account, "token_secret_name", None)
    return str(name if name else account)


def threads_secret_env(accounts: Sequence[Any], indent: str = "          ") -> list[str]:
    """アカウントごとのトークンを env へ注入する行を作る。

    アカウント数と ID は運用中に変わるため、シークレット名をアカウント定義から
    組み立てて ``${{ secrets['NAME'] }}`` の索引記法で参照する。
    値は YAML には現れず、実行時に GitHub が解決する。
    """
    lines: list[str] = []
    for account in accounts or []:
        name = secret_name_for(account)
        lines.append(f"{indent}{name}: \"${{{{ secrets['{name}'] }}}}\"")
    return lines


def _setup_steps(python_version: str) -> list[str]:
    return [
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
    ]


def _job_lines(
    job_id: str,
    step_name: str,
    run_lines: Sequence[str],
    accounts: Sequence[Any],
    python_version: str,
    commit_message: str,
    extra_env: Sequence[str] = (),
    extra_steps: Sequence[str] = (),
    commit_always: bool = False,
) -> list[str]:
    """3 種類のワークフローで共通のジョブ本体を組み立てる。"""
    lines = [
        "jobs:",
        f"  {job_id}:",
        "    runs-on: ubuntu-latest",
        "    permissions:",
        "      contents: write",
        "    steps:",
        *_setup_steps(python_version),
        *extra_steps,
        f"      - name: {step_name}",
        "        env:",
        '          PYTHONUNBUFFERED: "1"',
        *extra_env,
        "          # トークンはリポジトリに保存せず、実行時に GitHub Secrets から注入する",
        *threads_secret_env(accounts),
        *run_lines,
        "      - name: Commit & push state",
    ]
    if commit_always:
        lines.append("        if: always()")
    lines.append(f"        run: 'bash scripts/commit_and_push.sh \"{commit_message}\"'")
    lines.append("")
    return lines


# ----------------------------------------------------------------------
# 配信用ワークフロー
# ----------------------------------------------------------------------
def render_workflow(
    workflow_name: str,
    schedule_times: Sequence[datetime],
    part_index: int,
    part_total: int,
    accounts: Sequence[Any] = (),
    target_date: str = "",
    python_version: str = "3.11",
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
        *_job_lines(
            job_id="publish",
            step_name="Publish scheduled posts",
            run_lines=["        run: python -m src.publisher"],
            accounts=accounts,
            python_version=python_version,
            commit_message="chore: publish scheduled posts",
        ),
    ]
    return "\n".join(lines)


def generate_publisher_workflows(
    schedule_times: Iterable[datetime],
    workflow_dir: Path,
    accounts: Sequence[Any] = (),
    basename: str = "publisher",
    max_cron_per_file: int = MAX_CRON_PER_FILE,
    python_version: str = "3.11",
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
            accounts=accounts,
            target_date=target_date,
            python_version=python_version,
            generated_at=generated_at,
        )
        (workflow_dir / filename).write_text(content, encoding="utf-8")
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
        "accounts": [secret_name_for(a) for a in accounts or []],
    }


# ----------------------------------------------------------------------
# 再投稿用ワークフロー
# ----------------------------------------------------------------------
def render_reposter_workflow(
    accounts: Sequence[Any] = (),
    cron: str = REPOSTER_CRON,
    python_version: str = "3.11",
    generated_at: str = "",
) -> str:
    """再投稿用ワークフロー YAML を組み立てる。

    アカウントが増減してもシークレット参照が追随するよう、配信用と同様に生成する。
    """
    return "\n".join([
        GENERATED_HEADER,
        "# 【再投稿機能】毎週 月・水・金 の JST 19:30 (UTC 10:30) に起動する。",
        "# 過去 1 週間でいいね数が高かった上位 3 件を、月=1 位 / 水=2 位 / 金=3 位 の順に",
        "# その日の「8 件目の投稿」として再投稿する。",
        f"# 生成日時 (UTC): {generated_at or '-'}",
        "name: Reposter",
        "",
        "on:",
        "  schedule:",
        f'    - cron: "{cron}"  # JST 月・水・金 19:30',
        "  workflow_dispatch:",
        "    inputs:",
        "      dry_run:",
        '        description: "Threads API を呼ばずに動作確認する"',
        "        type: boolean",
        "        default: false",
        "      rank_index:",
        '        description: "使用する順位 (0 始まり, 空なら曜日から自動判定)"',
        "        type: string",
        '        default: ""',
        "",
        "concurrency:",
        "  group: reposter",
        "  cancel-in-progress: false",
        "",
        *_job_lines(
            job_id="repost",
            step_name="Repost top posts",
            run_lines=[
                "        run: |",
                '          ARGS=""',
                '          if [ "${{ inputs.dry_run }}" = "true" ]; then ARGS="$ARGS --dry-run"; fi',
                '          if [ -n "${{ inputs.rank_index }}" ]; then ARGS="$ARGS --rank-index ${{ inputs.rank_index }}"; fi',
                "          python -m src.reposter $ARGS",
            ],
            accounts=accounts,
            python_version=python_version,
            commit_message="chore: repost top posts",
        ),
    ])


def generate_reposter_workflow(
    accounts: Sequence[Any],
    workflow_dir: Path,
    filename: str = REPOSTER_FILENAME,
    python_version: str = "3.11",
    generated_at: str = "",
) -> str:
    """再投稿ワークフローを生成・上書きする。"""
    workflow_dir = Path(workflow_dir)
    workflow_dir.mkdir(parents=True, exist_ok=True)
    content = render_reposter_workflow(
        accounts=accounts, python_version=python_version, generated_at=generated_at
    )
    (workflow_dir / filename).write_text(content, encoding="utf-8")
    logger.info("再投稿ワークフローを生成しました: %s (アカウント %s 件)", filename, len(accounts or []))
    return filename


# ----------------------------------------------------------------------
# トークン自動更新ワークフロー
# ----------------------------------------------------------------------
def render_token_refresh_workflow(
    accounts: Sequence[Any] = (),
    cron: str = TOKEN_REFRESH_CRON,
    python_version: str = "3.11",
    generated_at: str = "",
) -> str:
    """Threads の長寿命トークンを延長するワークフローを組み立てる。

    更新には「現在のトークン」が必要なため、配信用と同じくアカウントごとの
    シークレットを実行時に注入する。更新後の値は Secrets API で上書きされる。
    """
    return "\n".join([
        GENERATED_HEADER,
        "# 【トークン自動更新】Threads の長寿命アクセストークン（有効期限 60 日）を延長する。",
        "# 毎週実行するため、失効までに 8 回以上やり直しの機会がある。",
        "# 更新後のトークンは GitHub Secrets へ暗号化して保存し直されるので手作業は不要。",
        f"# 生成日時 (UTC): {generated_at or '-'}",
        "name: Token Refresh",
        "",
        "on:",
        "  schedule:",
        f'    - cron: "{cron}"  # 毎週月曜 JST 04:00（日曜 UTC 19:00）',
        "  workflow_dispatch:",
        "    inputs:",
        "      dry_run:",
        '        description: "外部 API を呼ばずに動作確認する"',
        "        type: boolean",
        "        default: false",
        "      force:",
        '        description: "直近に更新済みでも強制的に更新する"',
        "        type: boolean",
        "        default: false",
        "",
        "concurrency:",
        "  group: token-refresh",
        "  cancel-in-progress: false",
        "",
        *_job_lines(
            job_id="refresh",
            step_name="Refresh Threads long-lived tokens",
            run_lines=[
                "        run: |",
                '          ARGS=""',
                '          if [ "${{ inputs.dry_run }}" = "true" ]; then ARGS="$ARGS --dry-run"; fi',
                '          if [ "${{ inputs.force }}" = "true" ]; then ARGS="$ARGS --force"; fi',
                "          python -m src.token_refresher $ARGS",
            ],
            accounts=accounts,
            python_version=python_version,
            commit_message="chore: refresh threads tokens",
            # Secrets の上書きには repo 権限を持つ PAT が必要（GITHUB_TOKEN では不可）
            extra_env=[
                "          WORKFLOW_TOKEN: ${{ secrets.WORKFLOW_TOKEN }}",
                "          GITHUB_REPOSITORY: ${{ github.repository }}",
            ],
            extra_steps=[
                "      - name: Check WORKFLOW_TOKEN",
                "        run: |",
                '          if [ -z "${{ secrets.WORKFLOW_TOKEN }}" ]; then',
                '            echo "::error::WORKFLOW_TOKEN が未設定です。Secrets を書き換えられないため更新できません。"',
                "            exit 1",
                "          fi",
            ],
            commit_always=True,
        ),
    ])


def generate_token_refresh_workflow(
    accounts: Sequence[Any],
    workflow_dir: Path,
    filename: str = TOKEN_REFRESH_FILENAME,
    python_version: str = "3.11",
    generated_at: str = "",
) -> str:
    """トークン更新ワークフローを生成・上書きする。"""
    workflow_dir = Path(workflow_dir)
    workflow_dir.mkdir(parents=True, exist_ok=True)
    content = render_token_refresh_workflow(
        accounts=accounts, python_version=python_version, generated_at=generated_at
    )
    (workflow_dir / filename).write_text(content, encoding="utf-8")
    logger.info("トークン更新ワークフローを生成しました: %s (アカウント %s 件)", filename, len(accounts or []))
    return filename
