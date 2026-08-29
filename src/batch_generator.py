"""【前日バッチ処理】リサーチ → 悩み生成 → 投稿生成 → 時間予約 → 動的 YAML 生成。

JST 23:00 (UTC 14:00) に 1 日 1 回起動し、登録済みの全アカウントを 1 件ずつ順番に処理する。
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from src import config as cfg
from src.claude_api import ClaudeClient, load_post_prompt_template
from src.config import Account, load_accounts, load_settings
from src.rakuten_api import RakutenClient
from src.scheduler import (
    JST,
    assign_items_to_slots,
    build_time_slots,
    cron_expression,
    is_golden_time,
    now_jst,
    parse_iso,
    to_utc,
)
from src.storage import read_json, write_json
from src.workflow_generator import (
    generate_publisher_workflows,
    generate_reposter_workflow,
    generate_token_refresh_workflow,
)

logger = logging.getLogger(__name__)

USED_ITEM_RETENTION_DAYS = 90  # used_items.json の保持期間（肥大化防止）


# ----------------------------------------------------------------------
# 履歴（重複防止）
# ----------------------------------------------------------------------
def load_used_items(path: Path) -> dict[str, Any]:
    data = read_json(path, {"accounts": {}}) or {}
    data.setdefault("accounts", {})
    return data


def recently_used_codes(
    used: dict[str, Any], account_id: str, days: int, reference: datetime
) -> set[str]:
    """過去 ``days`` 日以内にそのアカウントで紹介済みの itemCode 集合。"""
    cutoff = reference - timedelta(days=max(0, int(days)))
    codes: set[str] = set()
    for entry in (used.get("accounts", {}) or {}).get(account_id, []) or []:
        code = entry.get("item_code")
        if not code:
            continue
        try:
            used_at = parse_iso(entry.get("used_at", ""))
        except (ValueError, TypeError):
            codes.add(code)  # 日時が壊れている場合は安全側（除外）に倒す
            continue
        if used_at >= cutoff:
            codes.add(code)
    return codes


def record_used_items(
    used: dict[str, Any],
    account_id: str,
    items: Sequence[dict[str, Any]],
    target_date: str,
    reference: datetime,
) -> None:
    """選択した商品を履歴へ記録し、古い履歴を削除する。"""
    entries = list((used.setdefault("accounts", {})).get(account_id, []) or [])
    stamp = reference.astimezone(timezone.utc).isoformat()
    for item in items:
        entries.append(
            {
                "item_code": item.get("item_code", ""),
                "item_name": item.get("item_name", ""),
                "rank": item.get("rank", 0),
                "used_at": stamp,
                "target_date": target_date,
            }
        )
    cutoff = reference - timedelta(days=USED_ITEM_RETENTION_DAYS)
    kept = []
    for entry in entries:
        try:
            if parse_iso(entry.get("used_at", "")) >= cutoff:
                kept.append(entry)
        except (ValueError, TypeError):
            kept.append(entry)
    used["accounts"][account_id] = kept


# ----------------------------------------------------------------------
# スタブ（--dry-run 用）
# ----------------------------------------------------------------------
def stub_items(account: Account, count: int, rng: random.Random) -> list[dict[str, Any]]:
    items = []
    for i in range(count):
        suffix = rng.randint(1000, 9999)
        items.append(
            {
                "item_code": f"dryrun:{account.id}:{suffix}:{i}",
                "item_name": f"[ダミー] {account.genre or 'サンプル'}商品 {i + 1}",
                "catch_copy": "",
                "caption": "ダミーの商品説明です。",
                "price": 1980 + i * 500,
                "shop_name": "ダミーショップ",
                "review_count": 100 - i,
                "review_average": 4.5,
                "item_url": "https://item.rakuten.co.jp/dryrun/",
                "affiliate_url": "https://hb.afl.rakuten.co.jp/dryrun/",
                "image_url": "",
                "genre_id": "",
                "keyword": (account.keywords or ["ダミー"])[0],
                "rank_source": "dry-run",
                "rank": i + 1,
            }
        )
    return items


# ----------------------------------------------------------------------
# バッチ本体
# ----------------------------------------------------------------------
class BatchGenerator:
    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        rakuten: RakutenClient | None = None,
        claude: ClaudeClient | None = None,
        data_dir: Path = cfg.DATA_DIR,
        workflow_dir: Path = cfg.WORKFLOW_DIR,
        prompt_path: Path = cfg.POST_PROMPT_FILE,
        dry_run: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.dry_run = dry_run
        self.data_dir = Path(data_dir)
        self.workflow_dir = Path(workflow_dir)
        self.prompt_path = Path(prompt_path)
        self.rng = rng or random.Random()
        self._rakuten = rakuten
        self._claude = claude
        self.template = load_post_prompt_template(self.prompt_path)

    # -- クライアント ---------------------------------------------------
    @property
    def rakuten(self) -> RakutenClient:
        if self._rakuten is None:
            self._rakuten = RakutenClient()
        return self._rakuten

    @property
    def claude(self) -> ClaudeClient:
        if self._claude is None:
            claude_cfg = self.settings.get("claude", {})
            self._claude = ClaudeClient(
                model=claude_cfg.get("model", "claude-3-5-sonnet-latest"),
                max_tokens=int(claude_cfg.get("max_tokens", 2000)),
                temperature=float(claude_cfg.get("temperature", 1.0)),
            )
        return self._claude

    # -- リサーチ -------------------------------------------------------
    def research_candidates(self, account: Account) -> list[dict[str, Any]]:
        """アカウントの全キーワードで検索し、itemCode 単位で重複排除した候補を返す。"""
        rakuten_cfg = self.settings.get("rakuten", {})
        hits = int(rakuten_cfg.get("fetch_hits", 50))
        if self.dry_run:
            return stub_items(account, hits, self.rng)

        candidates: dict[str, dict[str, Any]] = {}
        for keyword in account.keywords:
            try:
                items = self.rakuten.search_items(
                    keyword=keyword,
                    hits=hits,
                    sort=rakuten_cfg.get("sort", "-reviewCount"),
                    min_price=int(rakuten_cfg.get("min_price", 0) or 0),
                    max_price=int(rakuten_cfg.get("max_price", 0) or 0),
                    ng_keywords=rakuten_cfg.get("ng_keywords", []),
                    affiliate_id=account.resolve_affiliate_id(),
                )
            except Exception as exc:  # noqa: BLE001 - 1 キーワードの失敗で全体を止めない
                logger.error("楽天検索に失敗しました account=%s keyword=%s: %s", account.id, keyword, exc)
                continue
            for item in items:
                code = item["item_code"]
                existing = candidates.get(code)
                # 複数キーワードで重複した場合は上位の順位を採用する
                if existing is None or item["rank"] < existing["rank"]:
                    candidates[code] = item
        return sorted(candidates.values(), key=lambda it: it["rank"])

    def select_items(
        self,
        account: Account,
        candidates: Sequence[dict[str, Any]],
        excluded_codes: set[str],
        count: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """紹介済みを除外した候補から必ず ``count`` 件をランダム選択する。

        戻り値: (選択した商品, 予備候補, 警告メッセージ)
        """
        warnings: list[str] = []
        fresh = [it for it in candidates if it["item_code"] not in excluded_codes]

        if len(fresh) >= count:
            # 通常経路: フレッシュな候補の中からランダムに count 件
            pool = fresh
        else:
            # 候補が足りない場合のみ、紹介済み商品を順位順で補充して count 件を確保する
            warnings.append(
                f"新規候補が {len(fresh)} 件のため、紹介済み商品を含めて {count} 件を確保します"
            )
            used_items = [it for it in candidates if it["item_code"] in excluded_codes]
            pool = fresh + used_items

        if len(pool) < count:
            warnings.append(f"候補が {len(pool)} 件しかなく {count} 件に届きませんでした")
            return list(pool), [], warnings

        selected = self.rng.sample(pool, count)
        selected_codes = {it["item_code"] for it in selected}
        # 予備はフレッシュな候補を優先する
        spare = [it for it in fresh if it["item_code"] not in selected_codes]
        spare += [it for it in pool if it["item_code"] not in selected_codes and it not in spare]
        return selected, spare, warnings

    # -- 生成 -----------------------------------------------------------
    def generate_content(self, account: Account, item: dict[str, Any]) -> dict[str, Any]:
        """1 商品分の「悩み」と「投稿本文」を生成する。"""
        if self.dry_run:
            worry = f"[ダミー] {account.genre or 'ジャンル'}に関する具体的な悩み"
            return {
                "worry": worry,
                "body": (
                    f"[ダミー投稿] {worry}\n"
                    f"{item.get('item_name', '')} を使ってみた話。\n"
                    f"（口調: {account.tone}）"
                ),
                "probability": 88.0,
                "pattern_count": 3,
                "raw_response": "",
            }

        worry_tokens = int(self.settings.get("claude", {}).get("worry_max_tokens", 400))
        worry = self.claude.generate_worry(account, item, max_tokens=worry_tokens)
        result = self.claude.generate_post(account, item, worry, template=self.template)
        if not result.get("body"):
            raise RuntimeError("Claude のレスポンスから投稿本文を抽出できませんでした")
        result["worry"] = worry
        return result

    # -- アカウント単位の処理 -------------------------------------------
    def process_account(
        self,
        account: Account,
        target_date: date,
        used: dict[str, Any],
        reference: datetime,
    ) -> dict[str, Any]:
        """1 アカウント分の投稿キューを作る。"""
        posts_per_day = int(account.posts_per_day or self.settings.get("posts_per_day", 7))
        exclusion_days = int(self.settings.get("duplicate_exclusion_days", 14))
        warnings: list[str] = []

        candidates = self.research_candidates(account)
        if not candidates:
            raise RuntimeError("楽天 API から商品候補を取得できませんでした")

        excluded = recently_used_codes(used, account.id, exclusion_days, reference)
        selected, spare, select_warnings = self.select_items(
            account, candidates, excluded, posts_per_day
        )
        warnings.extend(select_warnings)
        logger.info(
            "アカウント %s: 候補 %s 件 / 除外 %s 件 / 選択 %s 件",
            account.id,
            len(candidates),
            len(excluded),
            len(selected),
        )

        generated: list[dict[str, Any]] = []
        spare_queue = list(spare)
        for item in selected:
            current = item
            while True:
                try:
                    content = self.generate_content(account, current)
                    generated.append({"item": current, "content": content})
                    break
                except Exception as exc:  # noqa: BLE001 - 1 商品の失敗で全体を止めない
                    logger.error(
                        "投稿生成に失敗しました account=%s item=%s: %s",
                        account.id,
                        current.get("item_code"),
                        exc,
                    )
                    warnings.append(f"生成失敗: {current.get('item_code')} ({exc})")
                    if not spare_queue:
                        break
                    current = spare_queue.pop(0)  # 予備候補へ差し替えて再挑戦

        # スケジュール割り当て
        jitter = self.settings.get("jitter_minutes", {})
        active = self.settings.get("active_hours", {})
        slots = build_time_slots(
            target_date=target_date,
            count=len(generated) or posts_per_day,
            start=active.get("start", "07:00"),
            end=active.get("end", "23:00"),
            jitter_min=int(jitter.get("min", 15)),
            jitter_max=int(jitter.get("max", 30)),
            min_gap=int(self.settings.get("min_gap_minutes", 20)),
            rng=self.rng,
        )
        golden_ranges = self.settings.get("golden_time_ranges", [])
        pairs = assign_items_to_slots(
            [entry["item"] for entry in generated], slots, golden_ranges
        )
        content_by_code = {entry["item"]["item_code"]: entry["content"] for entry in generated}

        pr_text = self.settings.get("pr_text", "※PR")
        posts: list[dict[str, Any]] = []
        for index, (item, scheduled_jst) in enumerate(pairs):
            content = content_by_code.get(item["item_code"], {})
            scheduled_utc = to_utc(scheduled_jst)
            posts.append(
                {
                    "id": f"{account.id}-{target_date.isoformat()}-{index + 1}-{uuid.uuid4().hex[:6]}",
                    "account_id": account.id,
                    "account_name": account.name,
                    "slot_index": index + 1,
                    "scheduled_at_jst": scheduled_jst.isoformat(),
                    "scheduled_at_utc": scheduled_utc.isoformat(),
                    "cron": cron_expression(scheduled_utc),
                    "is_golden_time": is_golden_time(scheduled_jst, golden_ranges),
                    "status": "pending",
                    "attempts": 0,
                    "body": content.get("body", ""),
                    "worry": content.get("worry", ""),
                    "probability": content.get("probability"),
                    "pattern_count": content.get("pattern_count"),
                    "pr_text": pr_text,
                    "affiliate_url": item.get("affiliate_url", ""),
                    "item": {
                        "item_code": item.get("item_code", ""),
                        "item_name": item.get("item_name", ""),
                        "price": item.get("price", 0),
                        "shop_name": item.get("shop_name", ""),
                        "review_count": item.get("review_count", 0),
                        "review_average": item.get("review_average", 0),
                        "rank": item.get("rank", 0),
                        "rank_source": item.get("rank_source", ""),
                        "keyword": item.get("keyword", ""),
                        "item_url": item.get("item_url", ""),
                        "affiliate_url": item.get("affiliate_url", ""),
                        "image_url": item.get("image_url", ""),
                    },
                    "created_at": reference.astimezone(timezone.utc).isoformat(),
                }
            )

        record_used_items(
            used, account.id, [p["item"] for p in posts], target_date.isoformat(), reference
        )
        return {
            "account_id": account.id,
            "account_name": account.name,
            "theme": account.theme,
            "posts": posts,
            "warnings": warnings,
            "candidate_count": len(candidates),
            "excluded_count": len(excluded),
        }

    # -- 全体実行 -------------------------------------------------------
    def run(
        self,
        accounts: Sequence[Account],
        target_date: date | None = None,
        reference: datetime | None = None,
        write_workflows: bool = True,
    ) -> dict[str, Any]:
        reference = reference or now_jst()
        target_date = target_date or (reference.astimezone(JST).date() + timedelta(days=1))

        queue_path = self.data_dir / cfg.QUEUE_FILE.name
        used_path = self.data_dir / cfg.USED_ITEMS_FILE.name
        used = load_used_items(used_path)

        queue: dict[str, Any] = {
            "generated_at": reference.astimezone(timezone.utc).isoformat(),
            "target_date": target_date.isoformat(),
            "timezone": self.settings.get("timezone", "Asia/Tokyo"),
            "accounts": {},
        }
        summary: dict[str, Any] = {
            "target_date": target_date.isoformat(),
            "generated_at": queue["generated_at"],
            "accounts": [],
            "errors": [],
            "total_posts": 0,
        }

        # 「1 アカウントずつ順番に」処理する
        for account in accounts:
            logger.info("=== アカウント処理開始: %s (%s) ===", account.name, account.id)
            try:
                result = self.process_account(account, target_date, used, reference)
            except Exception as exc:  # noqa: BLE001 - 1 アカウントの失敗で全体を止めない
                logger.exception("アカウント処理に失敗しました: %s", account.id)
                summary["errors"].append({"account_id": account.id, "error": str(exc)})
                continue
            queue["accounts"][account.id] = {
                "account_name": result["account_name"],
                "theme": result["theme"],
                "posts": result["posts"],
            }
            summary["accounts"].append(
                {
                    "account_id": result["account_id"],
                    "account_name": result["account_name"],
                    "post_count": len(result["posts"]),
                    "candidate_count": result["candidate_count"],
                    "excluded_count": result["excluded_count"],
                    "warnings": result["warnings"],
                    "scheduled_at": [p["scheduled_at_jst"] for p in result["posts"]],
                }
            )
            summary["total_posts"] += len(result["posts"])

        write_json(queue_path, queue)
        write_json(used_path, used)

        if write_workflows:
            publisher_cfg = self.settings.get("publisher", {})
            python_version = str(publisher_cfg.get("python_version", "3.11"))
            # シークレット名はアカウント ID から動的に決まるため、
            # 実行するワークフロー側の参照もアカウント一覧から作り直す
            workflow_result = generate_publisher_workflows(
                schedule_times=[
                    parse_iso(post["scheduled_at_utc"])
                    for entry in queue["accounts"].values()
                    for post in entry["posts"]
                ],
                workflow_dir=self.workflow_dir,
                accounts=accounts,
                basename=publisher_cfg.get("workflow_basename", "publisher"),
                max_cron_per_file=int(publisher_cfg.get("max_cron_per_file", 60)),
                python_version=python_version,
                target_date=target_date.isoformat(),
                generated_at=queue["generated_at"],
            )
            workflow_result["reposter"] = generate_reposter_workflow(
                accounts=accounts,
                workflow_dir=self.workflow_dir,
                python_version=python_version,
                generated_at=queue["generated_at"],
            )
            workflow_result["token_refresh"] = generate_token_refresh_workflow(
                accounts=accounts,
                workflow_dir=self.workflow_dir,
                python_version=python_version,
                generated_at=queue["generated_at"],
            )
            summary["workflows"] = workflow_result

        write_json(self.data_dir / cfg.RUN_LOG_FILE.name, summary)
        return summary


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="前日バッチ処理（投稿生成・予約・YAML 生成）")
    parser.add_argument("--dry-run", action="store_true", help="外部 API を呼ばずスタブデータで実行")
    parser.add_argument("--target-date", default="", help="対象日 (YYYY-MM-DD, 既定は翌日)")
    parser.add_argument("--accounts", default="", help="対象アカウント ID をカンマ区切りで指定")
    parser.add_argument("--accounts-file", default=str(cfg.ACCOUNTS_FILE))
    parser.add_argument("--settings-file", default=str(cfg.SETTINGS_FILE))
    parser.add_argument("--data-dir", default=str(cfg.DATA_DIR))
    parser.add_argument("--workflow-dir", default=str(cfg.WORKFLOW_DIR))
    parser.add_argument("--prompt-file", default=str(cfg.POST_PROMPT_FILE))
    parser.add_argument("--skip-workflows", action="store_true", help="ワークフロー YAML を生成しない")
    parser.add_argument("--seed", type=int, default=None, help="乱数シード（再現用）")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = load_settings(args.settings_file)
    accounts = load_accounts(args.accounts_file)
    accounts = [a for a in accounts if a.enabled]
    if args.accounts:
        wanted = {a.strip() for a in args.accounts.split(",") if a.strip()}
        accounts = [a for a in accounts if a.id in wanted]

    if not accounts:
        logger.warning("有効なアカウントが登録されていません（config/accounts.json）")

    target_date = None
    if args.target_date:
        target_date = date.fromisoformat(args.target_date)

    generator = BatchGenerator(
        settings=settings,
        data_dir=Path(args.data_dir),
        workflow_dir=Path(args.workflow_dir),
        prompt_path=Path(args.prompt_file),
        dry_run=args.dry_run,
        rng=random.Random(args.seed) if args.seed is not None else random.Random(),
    )
    summary = generator.run(
        accounts, target_date=target_date, write_workflows=not args.skip_workflows
    )

    logger.info(
        "バッチ完了: 投稿 %s 件 / アカウント %s 件 / エラー %s 件",
        summary["total_posts"],
        len(summary["accounts"]),
        len(summary["errors"]),
    )
    for entry in summary["accounts"]:
        for warning in entry["warnings"]:
            logger.warning("[%s] %s", entry["account_id"], warning)
    return 1 if summary["errors"] and not summary["accounts"] else 0


if __name__ == "__main__":
    sys.exit(main())
