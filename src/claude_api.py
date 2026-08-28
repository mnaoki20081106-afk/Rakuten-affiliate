"""Claude API クライアント（悩み生成・投稿本文生成・パターン解析）。"""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from typing import Any

from src.config import ENV_ANTHROPIC_API_KEY, POST_PROMPT_FILE, Account
from src.storage import read_text

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-3-5-sonnet-latest"

# 「ターゲットの具体的な悩み」を推測させるプロンプト
WORRY_PROMPT = """あなたはSNSマーケティングのプロです。
以下のアカウントテーマと、これから紹介する商品の情報をもとに、
その商品を買う可能性が高いターゲット層が「実際に抱えている具体的な悩み」を1つ推測してください。

# アカウントのテーマ
{theme}

# 商品情報
商品名: {item_name}
価格: {price}円
ショップ: {shop_name}
レビュー件数: {review_count}件（平均{review_average}）
商品説明: {caption}

# 出力ルール
- 悩みは「日常のワンシーンが目に浮かぶ」レベルまで具体的に書く
- 商品名は使わず、悩みそのものだけを書く
- 60〜120文字の日本語1文で書く
- 前置き・見出し・箇条書き・引用符は書かず、悩みの本文だけを出力する
"""

# prompts/ のテンプレートが空のときに使う既定の投稿生成プロンプト
DEFAULT_POST_PROMPT = """あなたは日本語のThreads投稿を書くプロのコピーライターです。
以下の条件でThreadsの投稿本文を「3パターン」作成してください。

# 条件
- ジャンル: {ジャンル}
- ターゲットの悩み: {ターゲットの悩み}
- 商品名: {商品名}
- 口調: {口調}

# 執筆ルール
- 1投稿あたり全角400文字以内
- 1行目で悩みに刺さるフックを作る
- 商品リンクやURLは本文に含めない（リンクはコメント欄に貼るため）
- ハッシュタグは使わない
- 誇大広告・医薬品的な効能表現は避ける

# 出力フォーマット（厳守）
【パターン1】
本文：
（ここに投稿本文）
伸びる確率：〇〇％

【パターン2】
本文：
（ここに投稿本文）
伸びる確率：〇〇％

【パターン3】
本文：
（ここに投稿本文）
伸びる確率：〇〇％
"""

# 「伸びる確率：85％」等を検出する
PROBABILITY_RE = re.compile(
    r"伸びる確率\s*[:：]?\s*(?:約)?\s*([0-9０-９]{1,3}(?:[.．][0-9０-９]+)?)\s*[%％]"
)
# 「【パターン1】」「パターン2:」「案3」等の見出し
PATTERN_HEADER_RE = re.compile(
    r"^[ \t]*[#*\-–—>]*[ \t]*[【\[(（]?[ \t]*"
    r"(?:パターン|案|プラン|Pattern|PATTERN)[ \t]*[0-9０-９]{1,2}[ \t]*"
    r"[】\])）:：.、]?[ \t]*.*$",
    re.M,
)
# 本文以外のラベル行（これ以降はメタ情報として捨てる）
META_LABEL_RE = re.compile(
    r"^[ \t]*[#*\-–—【\[]*[ \t]*"
    r"(?:理由|解説|ポイント|狙い|補足|分析|評価|コメント|説明|meta|Reason)"
    r"[ \t]*[】\]]*[ \t]*[:：]",
    re.M,
)
BODY_LABEL_RE = re.compile(
    r"^[ \t]*[#*【\[]*[ \t]*(?:本文|投稿文|投稿本文|Body|text)[ \t]*[】\]]*[ \t]*[:：]?[ \t]*$",
    re.M,
)
INLINE_BODY_LABEL_RE = re.compile(
    r"^[ \t]*[#*【\[]*[ \t]*(?:本文|投稿文|投稿本文|Body|text)[ \t]*[】\]]*[ \t]*[:：][ \t]*",
    re.M,
)
CODE_FENCE_RE = re.compile(r"^[ \t]*```[a-zA-Z]*[ \t]*$", re.M)


class ClaudeAPIError(RuntimeError):
    """Claude API 呼び出しの失敗。"""


def _parse_number(text: str) -> float:
    """全角数字を含む数値表記を float へ変換する。"""
    try:
        return float(unicodedata.normalize("NFKC", text or "").replace("．", "."))
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------
# 出力パース
# ----------------------------------------------------------------------
def _clean_body(block: str) -> str:
    """1 パターン分のブロックから投稿本文だけを取り出す。"""
    text = block

    # 伸びる確率の行を削除
    text = PROBABILITY_RE.sub("", text)
    # パターン見出しを削除
    text = PATTERN_HEADER_RE.sub("", text)
    # 「理由：」以降のメタ情報を切り落とす
    meta = META_LABEL_RE.search(text)
    if meta:
        text = text[: meta.start()]
    # 「本文：」ラベルを削除
    text = BODY_LABEL_RE.sub("", text)
    text = INLINE_BODY_LABEL_RE.sub("", text)
    # コードフェンスを削除
    text = CODE_FENCE_RE.sub("", text)

    lines = [ln.rstrip() for ln in text.splitlines()]
    # 先頭・末尾の空行や記号だけの行を落とす
    while lines and not lines[0].strip(" 　-–—*#>「」『』\"'"):
        lines.pop(0)
    while lines and not lines[-1].strip(" 　-–—*#>「」『』\"'"):
        lines.pop()
    body = "\n".join(lines).strip()

    # 全体が引用符で囲まれている場合は外す
    if len(body) >= 2 and body[0] in "「『\"“'" and body[-1] in "」』\"”'":
        body = body[1:-1].strip()
    return body


def _split_blocks(text: str) -> list[str]:
    """レスポンスをパターン単位のブロックへ分割する。"""
    headers = [m.start() for m in PATTERN_HEADER_RE.finditer(text)]
    if len(headers) >= 2:
        bounds = headers + [len(text)]
        return [text[bounds[i] : bounds[i + 1]] for i in range(len(headers))]

    # 見出しが無い場合は「伸びる確率」の出現位置で区切る
    probs = [m.end() for m in PROBABILITY_RE.finditer(text)]
    if probs:
        blocks = []
        start = 0
        for end in probs:
            blocks.append(text[start:end])
            start = end
        return blocks
    return [text]


def parse_best_pattern(response_text: str) -> dict[str, Any]:
    """「伸びる確率」が最も高いパターンの本文だけを抽出する。

    戻り値: ``{"body": str, "probability": float | None, "pattern_count": int}``
    """
    # NFKC 正規化すると「？」「！」「〜」まで半角化され投稿文が壊れるため、
    # 本文は原文のまま扱い、確率の数値だけを半角化して解釈する。
    source = (response_text or "").replace("\r\n", "\n")

    blocks = _split_blocks(source)
    candidates: list[tuple[float, str]] = []
    for block in blocks:
        match = PROBABILITY_RE.search(block)
        if not match:
            continue
        body = _clean_body(block)
        if body:
            candidates.append((_parse_number(match.group(1)), body))

    if candidates:
        best = max(candidates, key=lambda c: c[0])
        return {"body": best[1], "probability": best[0], "pattern_count": len(candidates)}

    # 確率表記が無い場合は全体を本文として扱う（フォールバック）
    body = _clean_body(source)
    return {"body": body, "probability": None, "pattern_count": 0}


# ----------------------------------------------------------------------
# プロンプトテンプレート
# ----------------------------------------------------------------------
def load_post_prompt_template(path=POST_PROMPT_FILE) -> str:
    """投稿生成プロンプトを読み込む。空ファイルなら既定プロンプトを使う。"""
    template = read_text(path, "")
    if template.strip():
        return template
    logger.info("プロンプトファイルが空のため既定テンプレートを使用します: %s", path)
    return DEFAULT_POST_PROMPT


def render_prompt(template: str, variables: dict[str, str]) -> str:
    """``{key}`` ``{{key}}`` ``[key]`` ``【key】`` 形式のプレースホルダを置換する。"""
    rendered = template
    for key, value in variables.items():
        value = "" if value is None else str(value)
        for token in (f"{{{{{key}}}}}", f"{{{key}}}", f"[{key}]", f"【{key}】", f"<{key}>"):
            rendered = rendered.replace(token, value)
    return rendered


def build_post_variables(account: Account, item: dict[str, Any], worry: str) -> dict[str, str]:
    """プロンプトへ渡す変数（ジャンル・ターゲットの悩み・商品名・口調 ほか）。"""
    values = {
        "genre": account.genre,
        "ジャンル": account.genre,
        "target_worry": worry,
        "悩み": worry,
        "ターゲットの悩み": worry,
        "item_name": item.get("item_name", ""),
        "商品名": item.get("item_name", ""),
        "tone": account.tone,
        "口調": account.tone,
        # 補助変数
        "worldview": account.worldview,
        "世界観": account.worldview,
        "strength": account.strength,
        "強み": account.strength,
        "target": account.target,
        "ターゲット": account.target,
        "theme": account.theme,
        "テーマ": account.theme,
        "account_name": account.name,
        "アカウント名": account.name,
        "price": str(item.get("price", "")),
        "価格": str(item.get("price", "")),
        "shop_name": item.get("shop_name", ""),
        "ショップ名": item.get("shop_name", ""),
        "review_count": str(item.get("review_count", "")),
        "レビュー件数": str(item.get("review_count", "")),
        "rank": str(item.get("rank", "")),
        "ランキング順位": str(item.get("rank", "")),
        "caption": item.get("caption", ""),
        "商品説明": item.get("caption", ""),
    }
    return values


# ----------------------------------------------------------------------
# クライアント
# ----------------------------------------------------------------------
class ClaudeClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2000,
        temperature: float = 1.0,
        client: Any = None,
    ) -> None:
        self.api_key = (api_key or os.environ.get(ENV_ANTHROPIC_API_KEY, "")).strip()
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise ClaudeAPIError(
                    f"Claude API キーが未設定です（環境変数 {ENV_ANTHROPIC_API_KEY}）"
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - 依存未導入時のみ
                raise ClaudeAPIError("anthropic パッケージが導入されていません") from exc
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete(
        self, prompt: str, max_tokens: int | None = None, retries: int = 3
    ) -> str:
        """メッセージを 1 往復送り、テキストを返す。"""
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=self.temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
                parts = []
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text is None and isinstance(block, dict):
                        text = block.get("text")
                    if text:
                        parts.append(text)
                return "\n".join(parts).strip()
            except Exception as exc:  # noqa: BLE001 - SDK 例外を横断的に再試行
                last_error = exc
                logger.warning("Claude API 呼び出し失敗 (%s/%s): %s", attempt + 1, retries, exc)
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise ClaudeAPIError(f"Claude API 呼び出しに失敗しました: {last_error}")

    # -- 高レベル API ---------------------------------------------------
    def generate_worry(
        self, account: Account, item: dict[str, Any], max_tokens: int = 400
    ) -> str:
        """商品情報とテーマからターゲットの具体的な悩みを生成する。"""
        prompt = WORRY_PROMPT.format(
            theme=account.theme or account.genre,
            item_name=item.get("item_name", ""),
            price=item.get("price", ""),
            shop_name=item.get("shop_name", ""),
            review_count=item.get("review_count", ""),
            review_average=item.get("review_average", ""),
            caption=(item.get("caption", "") or "")[:400],
        )
        text = self.complete(prompt, max_tokens=max_tokens)
        # 念のため 1 文に整える
        return text.strip().strip("「」『』\"'").split("\n")[0].strip()

    def generate_post(
        self,
        account: Account,
        item: dict[str, Any],
        worry: str,
        template: str | None = None,
    ) -> dict[str, Any]:
        """投稿生成プロンプトを送り、最も伸びる確率が高い本文を返す。"""
        template = template if template is not None else load_post_prompt_template()
        prompt = render_prompt(template, build_post_variables(account, item, worry))
        raw = self.complete(prompt)
        parsed = parse_best_pattern(raw)
        parsed["raw_response"] = raw
        return parsed
