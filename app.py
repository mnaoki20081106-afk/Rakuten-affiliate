"""Streamlit 管理画面。

複数アカウントの「テーマ（発信ジャンル + 世界観 + 強み）」と
「Threads API トークン」を ``config/accounts.json`` に登録・保存する。
あわせて配信キュー・投稿履歴の確認と、投稿作成プロンプトの編集も行える。
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from src.accounts import (
    DEFAULT_SCHEDULE,
    default_token_env,
    load_accounts,
    normalize_account,
    resolve_threads_token,
    save_accounts,
    slugify,
)
from src.store import likes_of, load_history, load_queue, queue_items
from src.utils import ACCOUNTS_FILE, POST_PROMPT_FILE, iso, now_jst

st.set_page_config(page_title="Threads 自動運用ダッシュボード", page_icon="🧵", layout="wide")

SORT_OPTIONS = {
    "-reviewCount": "レビュー件数が多い順（売れ筋の目安）",
    "-reviewAverage": "レビュー評価が高い順",
    "+itemPrice": "価格が安い順",
    "-itemPrice": "価格が高い順",
    "standard": "楽天標準の並び順",
}


def _load_state() -> list[dict[str, Any]]:
    """アカウント一覧をセッションに読み込む。"""
    if "accounts" not in st.session_state:
        st.session_state.accounts = load_accounts()
    return st.session_state.accounts


def _persist(accounts: list[dict[str, Any]], message: str) -> None:
    """アカウント一覧を保存して結果を表示する。"""
    save_accounts(accounts)
    st.session_state.accounts = load_accounts()
    st.success(message)


def _blank_account(index: int) -> dict[str, Any]:
    return normalize_account({"name": f"新規アカウント{index + 1}", "id": f"account_{index + 1}"}, index)


# --- サイドバー -----------------------------------------------------------
def render_sidebar() -> None:
    with st.sidebar:
        st.header("🧵 Threads 自動運用")
        st.caption("楽天アフィリエイト × Claude × Threads")
        st.markdown(
            f"""
**設定ファイル**
- `{ACCOUNTS_FILE.relative_to(ACCOUNTS_FILE.parent.parent)}`
- `{POST_PROMPT_FILE.relative_to(POST_PROMPT_FILE.parent.parent)}`
"""
        )
        st.info(
            "Streamlit Community Cloud はファイルシステムが一時的です。"
            "クラウド上で保存した内容はアプリ再起動で消えるため、"
            "「JSON をダウンロード」からファイルを取得して"
            "リポジトリの `config/accounts.json` にコミットしてください。",
            icon="⚠️",
        )
        st.divider()
        st.caption(f"現在時刻（JST）: {iso(now_jst())}")


# --- アカウント設定タブ ---------------------------------------------------
def render_account_form(account: dict[str, Any], index: int, accounts: list[dict[str, Any]]) -> None:
    theme = account["theme"]
    rakuten = account["rakuten"]
    threads = account["threads"]
    schedule = account["schedule"]

    with st.form(key=f"account_form_{index}"):
        col1, col2, col3 = st.columns([2, 2, 1])
        name = col1.text_input("アカウント名", value=account["name"], key=f"name_{index}")
        account_id = col2.text_input(
            "アカウント ID（英数字。ファイル・Secrets のキーに使います）",
            value=account["id"],
            key=f"id_{index}",
        )
        enabled = col3.checkbox("有効", value=account["enabled"], key=f"enabled_{index}")

        st.subheader("テーマ設定")
        genre = st.text_input(
            "発信ジャンル", value=theme["genre"], placeholder="例: 20〜30代女性向けのスキンケア", key=f"genre_{index}"
        )
        worldview = st.text_area(
            "世界観", value=theme["worldview"], placeholder="例: 頑張りすぎない、それでも自分を大事にする日常", height=80, key=f"world_{index}"
        )
        strength = st.text_area(
            "このアカウントの強み", value=theme["strength"], placeholder="例: 成分で選んだ実力派だけを紹介", height=80, key=f"strength_{index}"
        )
        col4, col5 = st.columns(2)
        tone = col4.text_input("口調", value=theme["tone"], key=f"tone_{index}")
        target = col5.text_input("想定ターゲット（任意）", value=theme.get("target", ""), key=f"target_{index}")

        st.subheader("Threads API 設定")
        col6, col7 = st.columns(2)
        user_id = col6.text_input(
            "Threads ユーザー ID（空欄ならトークンから自動取得）", value=threads["user_id"], key=f"uid_{index}"
        )
        token_env = col7.text_input(
            "トークンの環境変数名",
            value=threads["token_env"] or default_token_env(account["id"]),
            help="GitHub Secrets / Streamlit Secrets に登録した環境変数名。こちらを推奨します。",
            key=f"tokenenv_{index}",
        )
        token = st.text_input(
            "Threads API アクセストークン（直接保存する場合のみ）",
            value=threads["token"],
            type="password",
            help="ここに入力すると config/accounts.json に平文で保存されます。"
            "公開リポジトリでは環境変数を使ってください。",
            key=f"token_{index}",
        )

        st.subheader("楽天リサーチ設定")
        col8, col9 = st.columns([3, 1])
        keyword = col8.text_input(
            "検索キーワード（空欄なら発信ジャンルを使用）", value=rakuten["keyword"], key=f"kw_{index}"
        )
        genre_id = col9.text_input("楽天ジャンル ID（任意）", value=rakuten["genre_id"], key=f"gid_{index}")
        col10, col11, col12 = st.columns(3)
        min_price = col10.number_input(
            "最低価格", min_value=0, value=int(rakuten["min_price"] or 0), step=100, key=f"minp_{index}"
        )
        max_price = col11.number_input(
            "最高価格（0 で指定なし）", min_value=0, value=int(rakuten["max_price"] or 0), step=100, key=f"maxp_{index}"
        )
        sort_keys = list(SORT_OPTIONS)
        sort_value = col12.selectbox(
            "並び順",
            options=sort_keys,
            index=sort_keys.index(rakuten["sort"]) if rakuten["sort"] in sort_keys else 0,
            format_func=lambda key: SORT_OPTIONS[key],
            key=f"sort_{index}",
        )

        st.subheader("配信スケジュール設定")
        col13, col14, col15, col16 = st.columns(4)
        active_start = col13.text_input("活動開始（HH:MM）", value=schedule["active_start"], key=f"as_{index}")
        active_end = col14.text_input("活動終了（HH:MM）", value=schedule["active_end"], key=f"ae_{index}")
        slot_count = col15.number_input(
            "1 日の投稿枠数", min_value=1, max_value=20, value=int(schedule["slot_count"]), key=f"slots_{index}"
        )
        golden_slot_count = col16.number_input(
            "ゴールデンタイム枠数",
            min_value=0,
            max_value=20,
            value=int(schedule["golden_slot_count"]),
            key=f"gslots_{index}",
        )
        golden_windows = st.text_input(
            "ゴールデンタイム（カンマ区切り。例: 07:00-09:00, 20:00-23:00）",
            value=", ".join(f"{w[0]}-{w[1]}" for w in schedule["golden_windows"]),
            key=f"gw_{index}",
        )
        col17, col18, col19 = st.columns(3)
        jitter_min = col17.number_input(
            "ゆらぎ最小（分）", min_value=0, max_value=120, value=int(schedule["jitter_min_minutes"]), key=f"jmin_{index}"
        )
        jitter_max = col18.number_input(
            "ゆらぎ最大（分）", min_value=0, max_value=120, value=int(schedule["jitter_max_minutes"]), key=f"jmax_{index}"
        )
        min_gap = col19.number_input(
            "最小間隔（分）", min_value=0, max_value=240, value=int(schedule["min_gap_minutes"]), key=f"gap_{index}"
        )

        saved = st.form_submit_button("💾 このアカウントを保存", type="primary")

    if saved:
        windows: list[list[str]] = []
        for chunk in golden_windows.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            start, _, end = chunk.partition("-")
            if start.strip() and end.strip():
                windows.append([start.strip(), end.strip()])

        accounts[index] = normalize_account(
            {
                "id": slugify(account_id or name, fallback=f"account_{index + 1}"),
                "name": name,
                "enabled": enabled,
                "theme": {
                    "genre": genre,
                    "worldview": worldview,
                    "strength": strength,
                    "tone": tone,
                    "target": target,
                },
                "rakuten": {
                    "keyword": keyword,
                    "genre_id": genre_id,
                    "min_price": int(min_price) or None,
                    "max_price": int(max_price) or None,
                    "sort": sort_value,
                },
                "threads": {"user_id": user_id, "token": token, "token_env": token_env},
                "schedule": {
                    "active_start": active_start,
                    "active_end": active_end,
                    "slot_count": int(slot_count),
                    "golden_windows": windows or DEFAULT_SCHEDULE["golden_windows"],
                    "golden_slot_count": int(golden_slot_count),
                    "jitter_min_minutes": int(jitter_min),
                    "jitter_max_minutes": int(jitter_max),
                    "min_gap_minutes": int(min_gap),
                },
                "created_at": account.get("created_at"),
                "updated_at": iso(now_jst()),
            },
            index,
        )
        _persist(accounts, f"「{name}」を保存しました。")

    col_a, col_b = st.columns([1, 5])
    if col_a.button("🔌 接続テスト", key=f"test_{index}"):
        _connection_test(accounts[index])
    if col_b.button("🗑 このアカウントを削除", key=f"delete_{index}"):
        removed = accounts.pop(index)
        _persist(accounts, f"「{removed['name']}」を削除しました。")
        st.rerun()


def _connection_test(account: dict[str, Any]) -> None:
    """Threads API に接続できるかを確認する。"""
    from src.threads_api import ThreadsAPIError, ThreadsClient

    token = resolve_threads_token(account)
    if not token:
        st.error("アクセストークンが見つかりません。環境変数か直接入力で設定してください。")
        return
    try:
        client = ThreadsClient(token, user_id=account["threads"]["user_id"])
        profile = client.get_profile()
    except ThreadsAPIError as exc:
        st.error(f"接続に失敗しました: {exc}")
        return
    st.success(f"接続成功: @{profile.get('username', '(不明)')} / user_id={profile.get('id', '')}")


def render_accounts_tab() -> None:
    accounts = _load_state()
    st.header("アカウント設定")
    st.caption("複数アカウント（美容系・生活用品系など）のテーマと Threads API トークンを登録します。")

    col1, col2 = st.columns([1, 4])
    if col1.button("➕ アカウントを追加"):
        accounts.append(_blank_account(len(accounts)))
        _persist(accounts, "アカウントを追加しました。")
        st.rerun()
    col2.download_button(
        "⬇️ accounts.json をダウンロード",
        data=json.dumps(
            {"updated_at": iso(now_jst()), "accounts": accounts}, ensure_ascii=False, indent=2
        ),
        file_name="accounts.json",
        mime="application/json",
    )

    if not accounts:
        st.info("まだアカウントがありません。「アカウントを追加」から登録してください。")
        return

    for index, account in enumerate(list(accounts)):
        label = f"{'🟢' if account['enabled'] else '⚪️'} {account['name']}（{account['id']}）"
        with st.expander(label, expanded=len(accounts) == 1):
            render_account_form(account, index, accounts)


# --- プロンプトタブ -------------------------------------------------------
def render_prompt_tab() -> None:
    st.header("投稿作成プロンプト")
    st.caption(
        "`prompts/Claude×アフィリエイト投稿作成プロンプト.txt` の内容です。"
        "`{ジャンル}` `{ターゲットの悩み}` `{商品名}` `{口調}` がバッチ処理で置換されます。"
    )
    try:
        current = POST_PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""

    edited = st.text_area("プロンプト本文", value=current, height=420, label_visibility="collapsed")
    col1, col2 = st.columns([1, 4])
    if col1.button("💾 プロンプトを保存", type="primary"):
        POST_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        POST_PROMPT_FILE.write_text(edited, encoding="utf-8")
        st.success("保存しました。（Streamlit Cloud の場合はリポジトリへのコミットも忘れずに）")
    col2.download_button(
        "⬇️ プロンプトをダウンロード",
        data=edited,
        file_name=POST_PROMPT_FILE.name,
        mime="text/plain",
    )
    if "伸びる確率" not in edited:
        st.warning(
            "プロンプトに「伸びる確率：〇〇％」の出力指示が含まれていません。"
            "その場合はシステムが標準の出力ルールを自動で追記します。",
            icon="ℹ️",
        )


# --- キュー / 履歴タブ ----------------------------------------------------
def render_queue_tab() -> None:
    st.header("配信キュー")
    queue = load_queue()
    st.caption(
        f"対象日: {queue.get('target_date') or '-'} ／ 生成日時: {queue.get('generated_at') or '-'}"
    )
    items = queue_items(queue)
    if not items:
        st.info("キューは空です。前日バッチ（batch_generator）が実行されると作成されます。")
        return

    status_labels = {"pending": "未配信", "published": "配信済み", "failed": "失敗", "skipped": "スキップ"}
    counts = {key: sum(1 for i in items if i.get("status") == key) for key in status_labels}
    cols = st.columns(len(status_labels))
    for col, (key, label) in zip(cols, status_labels.items()):
        col.metric(label, counts[key])

    rows = [
        {
            "予約時刻": item.get("scheduled_at", ""),
            "アカウント": item.get("account_id", ""),
            "枠": "ゴールデン" if item.get("slot_type") == "golden" else "通常",
            "順位": item.get("rank"),
            "伸びる確率": item.get("probability"),
            "状態": status_labels.get(item.get("status", ""), item.get("status", "")),
            "商品名": (item.get("product") or {}).get("name", ""),
            "本文": item.get("body", ""),
        }
        for item in sorted(items, key=lambda i: i.get("scheduled_at") or "")
    ]
    st.dataframe(rows, width="stretch", hide_index=True)


def render_history_tab() -> None:
    st.header("投稿履歴")
    history = load_history()
    posts = history.get("posts", [])
    st.caption(f"最終更新: {history.get('updated_at') or '-'} ／ 総投稿数: {len(posts)}")
    if not posts:
        st.info("まだ投稿履歴がありません。")
        return

    rows = [
        {
            "投稿日時": post.get("published_at", ""),
            "アカウント": post.get("account_id", ""),
            "種別": "再投稿" if post.get("source") == "repost" else "通常",
            "いいね": likes_of(post),
            "表示": (post.get("metrics") or {}).get("views", ""),
            "投稿ID": post.get("post_id", ""),
            "商品名": (post.get("product") or {}).get("name", ""),
            "本文": post.get("body", ""),
        }
        for post in sorted(posts, key=lambda p: p.get("published_at") or "", reverse=True)
    ]
    st.dataframe(rows, width="stretch", hide_index=True)


def main() -> None:
    render_sidebar()
    st.title("Threads SNS 自動運用ダッシュボード")
    tab1, tab2, tab3, tab4 = st.tabs(["⚙️ アカウント設定", "📝 投稿プロンプト", "📅 配信キュー", "📊 投稿履歴"])
    with tab1:
        render_accounts_tab()
    with tab2:
        render_prompt_tab()
    with tab3:
        render_queue_tab()
    with tab4:
        render_history_tab()


if __name__ == "__main__":
    main()
