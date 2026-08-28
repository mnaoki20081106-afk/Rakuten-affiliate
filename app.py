"""Streamlit 管理画面: アカウント管理・キュー確認・履歴確認・設定編集。

Streamlit Community Cloud 上ではローカルの書き込みが再デプロイで失われるため、
GitHub Contents API 経由でリポジトリへコミットして状態を維持する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

from src import config as cfg
from src.config import (
    Account,
    load_accounts,
    load_settings,
    save_accounts,
    save_settings,
    slugify,
)
from src.github_sync import GitHubSync, GitHubSyncError
from src.scheduler import to_jst
from src.storage import read_json, read_text, write_text

st.set_page_config(page_title="SNS 自動運用 管理画面", page_icon="🤖", layout="wide")

REPO_RELATIVE = {
    "accounts": "config/accounts.json",
    "settings": "config/settings.json",
    "prompt": "prompts/Claude×アフィリエイト投稿作成プロンプト.txt",
}


# ----------------------------------------------------------------------
# GitHub 連携
# ----------------------------------------------------------------------
def _secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - secrets.toml が無い環境では例外になる
        return default
    return str(value or default)


def get_sync() -> GitHubSync | None:
    """secrets に GITHUB_TOKEN / GITHUB_REPO があれば同期クライアントを返す。"""
    token = _secret("GITHUB_TOKEN")
    repo = _secret("GITHUB_REPO")
    branch = _secret("GITHUB_BRANCH", "main")
    if not token or not repo:
        return None
    try:
        return GitHubSync(token=token, repo=repo, branch=branch)
    except GitHubSyncError as exc:
        st.sidebar.error(f"GitHub 連携の初期化に失敗しました: {exc}")
        return None


def persist(kind: str, data: Any, message: str) -> None:
    """ローカルへ保存し、GitHub 連携が有効ならコミットも行う。"""
    path = REPO_RELATIVE[kind]
    if kind == "accounts":
        save_accounts(data)
    elif kind == "settings":
        save_settings(data)
    else:
        write_text(cfg.ROOT / path, data)

    sync = get_sync()
    if sync is None:
        st.warning(
            "ローカルに保存しました。GitHub へ反映するには secrets に "
            "GITHUB_TOKEN / GITHUB_REPO を設定してください（未設定だと再デプロイで失われます）。"
        )
        return
    try:
        if kind == "prompt":
            sync.put_file(path, data, message)
        else:
            payload = (
                {"accounts": [a.to_dict() for a in data]} if kind == "accounts" else data
            )
            sync.put_json(path, payload, message)
    except GitHubSyncError as exc:
        st.error(f"GitHub へのコミットに失敗しました: {exc}")
        return
    st.success(f"保存し、GitHub へコミットしました: {path}")


# ----------------------------------------------------------------------
# データ読み込み
# ----------------------------------------------------------------------
def load_state() -> dict[str, Any]:
    return {
        "accounts": load_accounts(),
        "settings": load_settings(),
        "queue": read_json(cfg.QUEUE_FILE, {}) or {},
        "history": read_json(cfg.POST_HISTORY_FILE, {"posts": []}) or {},
        "used": read_json(cfg.USED_ITEMS_FILE, {"accounts": {}}) or {},
        "run_log": read_json(cfg.RUN_LOG_FILE, {}) or {},
    }


# ----------------------------------------------------------------------
# 画面: アカウント管理
# ----------------------------------------------------------------------
def render_accounts(accounts: list[Account]) -> None:
    st.header("アカウント管理")
    st.caption(
        "各アカウントの「テーマ（発信ジャンル＋世界観＋強み）」と Threads API トークンを登録します。"
        "10 アカウント程度の並行運用を想定しています。"
    )

    if accounts:
        st.dataframe(
            [
                {
                    "ID": a.id,
                    "アカウント名": a.name,
                    "有効": a.enabled,
                    "ジャンル": a.genre,
                    "1日の投稿数": a.posts_per_day,
                    "キーワード": " / ".join(a.keywords),
                    "トークン": "環境変数" if not a.threads_access_token else "accounts.json（非推奨）",
                }
                for a in accounts
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("アカウントが登録されていません。下のフォームから追加してください。")

    labels = ["＋ 新規追加"] + [f"{a.name or a.id} ({a.id})" for a in accounts]
    choice = st.selectbox("編集対象", labels, key="account_choice")
    index = labels.index(choice) - 1
    account = accounts[index] if index >= 0 else Account()

    with st.form("account_form"):
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("アカウント名", value=account.name)
            account_id = st.text_input(
                "アカウント ID（英数字。空なら自動採番）", value=account.id if index >= 0 else ""
            )
            enabled = st.checkbox("有効", value=account.enabled)
            posts_per_day = st.number_input(
                "1 日の投稿数", min_value=1, max_value=20, value=int(account.posts_per_day or 7)
            )
        with col2:
            threads_user_id = st.text_input("Threads ユーザー ID（空なら自動取得）", value=account.threads_user_id)
            token_env = st.text_input(
                "トークンの環境変数名（推奨）",
                value=account.threads_token_env or (account.default_token_env if index >= 0 else ""),
                help="GitHub Secrets 名。公開リポジトリではこちらを使ってください。",
            )
            token = st.text_input(
                "Threads アクセストークン（直接保存・非推奨）",
                value=account.threads_access_token,
                type="password",
                help="config/accounts.json に平文で保存されます。公開リポジトリでは使用しないでください。",
            )
            affiliate_id = st.text_input("楽天アフィリエイト ID（空なら共通設定）", value=account.rakuten_affiliate_id)

        st.subheader("テーマ")
        genre = st.text_input("発信ジャンル", value=account.genre)
        worldview = st.text_area("世界観", value=account.worldview, height=80)
        strength = st.text_area("強み", value=account.strength, height=80)
        target = st.text_input("ターゲット", value=account.target)
        tone = st.text_input("口調", value=account.tone)
        keywords = st.text_area(
            "楽天検索キーワード（改行またはカンマ区切り。空ならジャンルを使用）",
            value="\n".join(account.search_keywords),
            height=80,
        )
        note = st.text_area("メモ", value=account.note, height=60)

        col_save, col_delete = st.columns([3, 1])
        submitted = col_save.form_submit_button("保存", width="stretch")
        deleted = col_delete.form_submit_button(
            "削除", width="stretch", disabled=index < 0
        )

    if submitted:
        if not name.strip():
            st.error("アカウント名を入力してください。")
            return
        new_id = slugify(account_id) or (account.id if index >= 0 else "")
        updated = Account(
            id=new_id,
            name=name.strip(),
            enabled=enabled,
            genre=genre.strip(),
            worldview=worldview.strip(),
            strength=strength.strip(),
            tone=tone.strip(),
            target=target.strip(),
            search_keywords=keywords,
            threads_user_id=threads_user_id.strip(),
            threads_access_token=token.strip(),
            threads_token_env=token_env.strip(),
            posts_per_day=int(posts_per_day),
            rakuten_affiliate_id=affiliate_id.strip(),
            note=note.strip(),
        )
        others = [a for i, a in enumerate(accounts) if i != index]
        if any(a.id == updated.id for a in others):
            st.error(f"アカウント ID が重複しています: {updated.id}")
            return
        if index >= 0:
            accounts[index] = updated
        else:
            accounts.append(updated)
        persist("accounts", accounts, f"chore(accounts): update {updated.id}")
        st.rerun()

    if deleted and index >= 0:
        removed = accounts.pop(index)
        persist("accounts", accounts, f"chore(accounts): remove {removed.id}")
        st.rerun()

    st.divider()
    st.subheader("GitHub Secrets に登録するトークン名")
    st.caption("公開リポジトリを想定しているため、トークンは Secrets 側で管理することを推奨します。")
    st.code(
        "\n".join(
            f"{a.threads_token_env or a.default_token_env}  # {a.name or a.id}" for a in accounts
        )
        or "（アカウント未登録）",
        language="text",
    )


# ----------------------------------------------------------------------
# 画面: 投稿キュー
# ----------------------------------------------------------------------
def render_queue(queue: dict[str, Any], run_log: dict[str, Any]) -> None:
    st.header("投稿キュー")
    if not queue.get("accounts"):
        st.info("キューが空です。バッチ処理（Batch Generator）を実行してください。")
        return

    col1, col2, col3 = st.columns(3)
    posts = [p for entry in queue["accounts"].values() for p in entry.get("posts", [])]
    col1.metric("対象日 (JST)", queue.get("target_date", "-"))
    col2.metric("予約投稿数", len(posts))
    col3.metric("送信済み", sum(1 for p in posts if p.get("status") == "sent"))

    if run_log.get("workflows"):
        wf = run_log["workflows"]
        st.caption(
            f"生成された配信ワークフロー: {', '.join(wf.get('files', []))} "
            f"（cron {wf.get('cron_count', 0)} 件 / {wf.get('file_count', 0)} ファイル）"
        )

    for account_id, entry in queue["accounts"].items():
        with st.expander(f"{entry.get('account_name', account_id)} ({account_id}) — {len(entry.get('posts', []))} 件"):
            for post in entry.get("posts", []):
                jst = post.get("scheduled_at_jst", "")[11:16]
                badge = "🌟 ゴールデン" if post.get("is_golden_time") else ""
                status = {"pending": "予約中", "sent": "送信済み", "failed": "失敗", "expired": "期限切れ"}.get(
                    post.get("status", ""), post.get("status", "")
                )
                st.markdown(
                    f"**{jst}** {badge} / 状態: {status} / 順位: {post.get('item', {}).get('rank', '-')} "
                    f"/ 伸びる確率: {post.get('probability', '-')}%"
                )
                st.text(post.get("body", ""))
                st.caption(
                    f"{post.get('item', {}).get('item_name', '')} — {post.get('affiliate_url', '')}"
                )
                if post.get("error"):
                    st.warning(post["error"])
                st.divider()


# ----------------------------------------------------------------------
# 画面: 投稿履歴
# ----------------------------------------------------------------------
def render_history(history: dict[str, Any], accounts: list[Account]) -> None:
    st.header("投稿履歴")
    posts = history.get("posts", [])
    if not posts:
        st.info("履歴がありません。")
        return

    names = ["すべて"] + [f"{a.name or a.id} ({a.id})" for a in accounts]
    selected = st.selectbox("アカウント", names)
    account_id = selected.rsplit("(", 1)[-1].rstrip(")") if selected != "すべて" else ""

    rows = []
    for post in reversed(posts):
        if account_id and post.get("account_id") != account_id:
            continue
        rows.append(
            {
                "投稿日時(JST)": (post.get("published_at_jst") or post.get("published_at", ""))[:16].replace("T", " "),
                "アカウント": post.get("account_name", post.get("account_id", "")),
                "いいね": post.get("likes", 0),
                "再投稿": "○" if post.get("is_repost") else "",
                "本文": (post.get("body", "") or "").replace("\n", " ")[:60],
                "商品": (post.get("item", {}) or {}).get("item_name", ""),
                "media_id": post.get("media_id", ""),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)


# ----------------------------------------------------------------------
# 画面: 商品履歴
# ----------------------------------------------------------------------
def render_used_items(used: dict[str, Any], settings: dict[str, Any]) -> None:
    st.header("紹介済み商品（重複防止）")
    days = settings.get("duplicate_exclusion_days", 14)
    st.caption(f"過去 {days} 日以内に紹介した商品は、次回のリサーチ対象から除外されます。")
    accounts_data = used.get("accounts", {})
    if not accounts_data:
        st.info("記録がありません。")
        return
    for account_id, entries in accounts_data.items():
        with st.expander(f"{account_id} — {len(entries)} 件"):
            st.dataframe(
                [
                    {
                        "itemCode": e.get("item_code", ""),
                        "商品名": e.get("item_name", ""),
                        "順位": e.get("rank", ""),
                        "使用日": e.get("target_date", ""),
                    }
                    for e in reversed(entries)
                ],
                width="stretch",
                hide_index=True,
            )


# ----------------------------------------------------------------------
# 画面: 設定・プロンプト
# ----------------------------------------------------------------------
def render_settings(settings: dict[str, Any]) -> None:
    st.header("共通設定")
    col1, col2 = st.columns(2)
    with col1:
        posts_per_day = st.number_input(
            "1 日の投稿数（枠の数）", min_value=1, max_value=20, value=int(settings.get("posts_per_day", 7))
        )
        start = st.text_input("活動開始 (JST)", value=settings.get("active_hours", {}).get("start", "07:00"))
        end = st.text_input("活動終了 (JST)", value=settings.get("active_hours", {}).get("end", "23:00"))
        exclusion = st.number_input(
            "重複除外の日数", min_value=0, max_value=365, value=int(settings.get("duplicate_exclusion_days", 14))
        )
    with col2:
        jitter = settings.get("jitter_minutes", {})
        jitter_min = st.number_input("ゆらぎ 最小(分)", min_value=0, max_value=120, value=int(jitter.get("min", 15)))
        jitter_max = st.number_input("ゆらぎ 最大(分)", min_value=0, max_value=120, value=int(jitter.get("max", 30)))
        fetch_hits = st.number_input(
            "楽天 取得件数", min_value=10, max_value=300, value=int(settings.get("rakuten", {}).get("fetch_hits", 50))
        )
        model = st.text_input("Claude モデル", value=settings.get("claude", {}).get("model", "claude-3-5-sonnet-latest"))

    golden = st.text_area(
        "ゴールデンタイム（1 行 1 区間、`開始-終了`）",
        value="\n".join(f"{r[0]}-{r[1]}" for r in settings.get("golden_time_ranges", [])),
        height=80,
    )

    if st.button("設定を保存", width="stretch"):
        ranges = []
        for line in golden.splitlines():
            if "-" in line:
                low, _, high = line.partition("-")
                ranges.append([low.strip(), high.strip()])
        settings["posts_per_day"] = int(posts_per_day)
        settings["active_hours"] = {"start": start.strip(), "end": end.strip()}
        settings["duplicate_exclusion_days"] = int(exclusion)
        settings["jitter_minutes"] = {"min": int(jitter_min), "max": int(jitter_max)}
        settings.setdefault("rakuten", {})["fetch_hits"] = int(fetch_hits)
        settings.setdefault("claude", {})["model"] = model.strip()
        settings["golden_time_ranges"] = ranges
        persist("settings", settings, "chore(settings): update from admin UI")

    st.divider()
    st.subheader("投稿生成プロンプト")
    st.caption(
        "`{ジャンル}` `{ターゲットの悩み}` `{商品名}` `{口調}` が置換されます。"
        "空のままだと組み込みの既定プロンプトが使われます。"
    )
    current = read_text(cfg.POST_PROMPT_FILE, "")
    prompt = st.text_area("プロンプト本文", value=current, height=320)
    if st.button("プロンプトを保存", width="stretch"):
        persist("prompt", prompt, "chore(prompt): update from admin UI")


# ----------------------------------------------------------------------
def main() -> None:
    st.title("🤖 Threads × 楽天アフィリエイト 自動運用")
    state = load_state()

    sync = get_sync()
    st.sidebar.header("状態")
    st.sidebar.write("GitHub 連携: " + ("✅ 有効" if sync else "⚠️ 未設定（ローカル保存のみ）"))
    st.sidebar.write(f"登録アカウント: {len(state['accounts'])} 件")
    if state["run_log"].get("generated_at"):
        generated = state["run_log"]["generated_at"]
        try:
            generated = to_jst(datetime.fromisoformat(generated.replace("Z", "+00:00"))).strftime(
                "%Y-%m-%d %H:%M JST"
            )
        except ValueError:
            pass
        st.sidebar.write(f"最終バッチ: {generated}")
    if state["run_log"].get("errors"):
        st.sidebar.error(f"直近バッチのエラー: {len(state['run_log']['errors'])} 件")

    page = st.sidebar.radio(
        "メニュー",
        ["アカウント管理", "投稿キュー", "投稿履歴", "紹介済み商品", "共通設定"],
    )

    if page == "アカウント管理":
        render_accounts(state["accounts"])
    elif page == "投稿キュー":
        render_queue(state["queue"], state["run_log"])
    elif page == "投稿履歴":
        render_history(state["history"], state["accounts"])
    elif page == "紹介済み商品":
        render_used_items(state["used"], state["settings"])
    else:
        render_settings(state["settings"])

    with st.sidebar.expander("直近バッチのログ"):
        st.json(state["run_log"] or {"info": "未実行"})


if __name__ == "__main__":
    main()
