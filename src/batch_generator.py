"""【前日バッチ処理】リサーチ・生成・スケジュール予約。

毎日 JST 23:00（UTC 14:00）に GitHub Actions から起動される想定。
登録された全アカウントに対して、以下をループ実行する。

1. リサーチ : 楽天 API でテーマに沿った売れ筋商品を必ず 7 件取得（1〜7 位）
2. 悩み生成 : Claude API でターゲットの具体的な悩みを推測
3. 投稿生成 : 投稿作成プロンプトを差し込み、伸びる確率が最大の本文を抽出
4. 予約     : 翌日の 7 枠にゆらぎを付けて割り当て、data/queue.json に保存
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta
from typing import Any

from .accounts import load_accounts
from .claude_api import (
    ClaudeAPIError,
    generate_post,
    generate_worry,
    get_client,
    load_post_prompt_template,
)
from .rakuten_api import RakutenAPIError, search_products
from .scheduler import build_schedule
from .store import load_queue, save_queue
from .utils import iso, now_jst, setup_logging

logger = setup_logging("batch_generator")

PRODUCT_COUNT = 7


def build_search_keyword(account: dict[str, Any]) -> str:
    """楽天リサーチ用のキーワードを決める。未設定ならテーマのジャンルを使う。"""
    keyword = str((account.get("rakuten") or {}).get("keyword") or "").strip()
    return keyword or str((account.get("theme") or {}).get("genre") or "").strip()


def research_products(account: dict[str, Any]) -> list[dict[str, Any]]:
    """アカウントテーマに沿った売れ筋商品を 7 件取得する。"""
    rakuten = account.get("rakuten") or {}
    products = search_products(
        keyword=build_search_keyword(account),
        hits=PRODUCT_COUNT,
        genre_id=str(rakuten.get("genre_id") or ""),
        min_price=rakuten.get("min_price"),
        max_price=rakuten.get("max_price"),
        sort=str(rakuten.get("sort") or "-reviewCount"),
    )
    logger.info("[%s] 売れ筋商品を %s 件取得しました。", account["id"], len(products))
    return products


def generate_contents(
    account: dict[str, Any], products: list[dict[str, Any]], template: str
) -> list[dict[str, Any]]:
    """7 件すべての商品について、悩みと投稿本文を生成する。"""
    client = get_client()
    theme = account.get("theme") or {}
    genre = str(theme.get("genre") or "")
    tone = str(theme.get("tone") or "")

    contents: list[dict[str, Any]] = []
    for product in products:
        try:
            worry = generate_worry(client, theme, product)
            post = generate_post(
                client,
                template=template,
                genre=genre,
                worry=worry,
                product_name=str(product.get("name") or ""),
                tone=tone,
            )
        except ClaudeAPIError as exc:
            logger.error(
                "[%s] ランキング %s位「%s」の生成に失敗しました: %s",
                account["id"],
                product.get("rank"),
                str(product.get("name"))[:30],
                exc,
            )
            continue

        contents.append(
            {
                "product": product,
                "worry": worry,
                "body": post["body"],
                "probability": post["probability"],
            }
        )
        logger.info(
            "[%s] ランキング %s位 生成完了（伸びる確率 %s%%）",
            account["id"],
            product.get("rank"),
            post["probability"],
        )
    return contents


def build_queue_entries(
    account: dict[str, Any], contents: list[dict[str, Any]], rng: random.Random | None = None
) -> list[dict[str, Any]]:
    """生成済みコンテンツを翌日のスケジュールに割り当て、キュー形式にする。"""
    target_day = (now_jst() + timedelta(days=1)).date()
    # scheduler には「rank を持つ商品」の並びを渡すので、生成結果を rank で引けるようにする
    by_rank = {content["product"]["rank"]: content for content in contents}
    products = [content["product"] for content in contents]
    assignments = build_schedule(target_day, account.get("schedule") or {}, products, rng=rng)

    entries: list[dict[str, Any]] = []
    for index, assignment in enumerate(assignments, start=1):
        content = by_rank[assignment["product"]["rank"]]
        product = content["product"]
        entries.append(
            {
                "id": f"{account['id']}-{target_day.isoformat()}-{index:02d}",
                "account_id": account["id"],
                "scheduled_at": iso(assignment["scheduled_at"]),
                "slot_type": assignment["slot_type"],
                "rank": product.get("rank"),
                "worry": content["worry"],
                "body": content["body"],
                "probability": content["probability"],
                "product": product,
                "affiliate_url": product.get("affiliate_url", ""),
                "status": "pending",
                "attempts": 0,
                "post_id": "",
                "reply_id": "",
                "published_at": None,
                "error": "",
                "source": "queue",
            }
        )
    return entries


def process_account(account: dict[str, Any], template: str) -> list[dict[str, Any]]:
    """1 アカウント分のリサーチ〜スケジュール予約を実行する。"""
    logger.info("=== [%s] %s の処理を開始します ===", account["id"], account["name"])
    products = research_products(account)
    contents = generate_contents(account, products, template)
    if not contents:
        raise ClaudeAPIError("投稿本文を 1 件も生成できませんでした。")
    if len(contents) < PRODUCT_COUNT:
        logger.warning(
            "[%s] %s 件中 %s 件のみ生成できました。生成できた分だけ予約します。",
            account["id"],
            PRODUCT_COUNT,
            len(contents),
        )
    entries = build_queue_entries(account, contents)
    for entry in entries:
        logger.info(
            "[%s] %s (%s枠) ランキング%s位 %s",
            account["id"],
            entry["scheduled_at"],
            "ゴールデン" if entry["slot_type"] == "golden" else "通常",
            entry["rank"],
            str(entry["product"].get("name"))[:30],
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="翌日分の投稿をリサーチ・生成して予約する")
    parser.add_argument("--account", help="指定した ID のアカウントだけ処理する")
    parser.add_argument(
        "--keep-pending",
        action="store_true",
        help="既存キューの未配信分を残したまま追記する（既定は当該アカウント分を置き換え）",
    )
    args = parser.parse_args(argv)

    accounts = load_accounts(enabled_only=True)
    if args.account:
        accounts = [a for a in accounts if a["id"] == args.account]
    if not accounts:
        logger.error("有効なアカウントがありません。config/accounts.json を確認してください。")
        return 1

    template = load_post_prompt_template()
    if not template.strip():
        logger.error(
            "prompts/Claude×アフィリエイト投稿作成プロンプト.txt が空です。"
            " 投稿作成プロンプトを記入してから実行してください。"
        )
        return 1

    queue = load_queue()
    target_day = (now_jst() + timedelta(days=1)).date()
    failures = 0

    for account in accounts:
        try:
            entries = process_account(account, template)
        except (RakutenAPIError, ClaudeAPIError, RuntimeError) as exc:
            failures += 1
            logger.error("[%s] 処理に失敗しました: %s", account["id"], exc)
            continue

        existing = queue["accounts"].get(account["id"], []) if args.keep_pending else []
        queue["accounts"][account["id"]] = existing + entries

    queue["generated_at"] = iso(now_jst())
    queue["target_date"] = target_day.isoformat()
    save_queue(queue)
    logger.info("data/queue.json を更新しました（対象日: %s）。", target_day.isoformat())

    if failures:
        logger.error("%s 件のアカウントで処理に失敗しました。", failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
