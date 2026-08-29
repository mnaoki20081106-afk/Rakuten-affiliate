"""Claude API クライアント。

役割は 2 つ。

1. 商品情報とアカウントテーマから「ターゲットの具体的な悩み」を生成する
2. ``prompts/Claude×アフィリエイト投稿作成プロンプト.txt`` を差し込んで投稿本文を生成し、
   「伸びる確率：〇〇％」が最も高いパターンの本文だけを取り出す
"""

from __future__ import annotations

import re
from typing import Any

import anthropic

from .utils import POST_PROMPT_FILE, env, require_env, setup_logging

logger = setup_logging(__name__)

# システム仕様書で指定されたモデル。CLAUDE_MODEL 環境変数で差し替えられる。
DEFAULT_MODEL = "claude-3-5-sonnet-latest"

WORRY_MAX_TOKENS = 1000
POST_MAX_TOKENS = 4000

# プロンプトテンプレートに差し込むプレースホルダの別名（日本語 / 英語の両対応）
PLACEHOLDER_ALIASES: dict[str, tuple[str, ...]] = {
    "genre": ("ジャンル", "genre", "GENRE"),
    "worry": ("ターゲットの悩み", "悩み", "worry", "WORRY"),
    "product": ("商品名", "product", "PRODUCT"),
    "tone": ("口調", "tone", "TONE"),
}

# ``{ジャンル}`` ``{{ジャンル}}`` ``【ジャンル】`` などの書き方を許容する
# 長い記法から先に試す（``{{X}}`` を ``{X}`` より先に置換するため）
PLACEHOLDER_FORMATS = ("{{{{{0}}}}}", "{{{0}}}", "[{0}]", "【{0}】", "<{0}>", "＜{0}＞", "${0}")

PROBABILITY_PATTERN = re.compile(r"伸びる確率[\s：:＝=]*([0-9]{1,3}(?:\.[0-9]+)?)\s*[%％]")
PATTERN_HEADING = re.compile(
    r"^\s*(?:[#*\->\s]*)?(?:【)?\s*(?:パターン|案|プラン|Pattern|PATTERN)\s*[0-9０-９①-⑩A-Za-zＡ-Ｚ一二三四五六七八九十]",
)

OUTPUT_RULE = """

# 出力ルール（システムが自動でパースするため厳守）
- 投稿案を 3 パターン作成してください。
- 各パターンは必ず「パターン1」「パターン2」…という見出しから始めてください。
- 各パターンの見出しの直後の行に「伸びる確率：〇〇％」を数値で記載してください。
- その次の行から、Threads にそのまま投稿できる本文だけを書いてください（説明や注釈は書かない）。
"""


class ClaudeAPIError(RuntimeError):
    """Claude API 呼び出し、または生成結果のパースに失敗したときの例外。"""


def get_client() -> anthropic.Anthropic:
    """API キーを確認した上で Anthropic クライアントを返す。"""
    require_env("ANTHROPIC_API_KEY")
    return anthropic.Anthropic()


def get_model() -> str:
    """使用するモデル ID を返す。"""
    return env("CLAUDE_MODEL", DEFAULT_MODEL)


def _complete(client: anthropic.Anthropic, system: str, user: str, max_tokens: int) -> str:
    """1 往復のテキスト生成を実行し、本文を結合して返す。"""
    try:
        response = client.messages.create(
            model=get_model(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIStatusError as exc:
        raise ClaudeAPIError(f"Claude API がエラーを返しました: {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeAPIError(f"Claude API に接続できませんでした: {exc}") from exc

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise ClaudeAPIError("Claude API が空の応答を返しました。")
    return text


def _theme_summary(theme: dict[str, Any]) -> str:
    """アカウントテーマを Claude に渡す 1 ブロックのテキストに整形する。"""
    lines = [
        f"- 発信ジャンル: {theme.get('genre', '')}",
        f"- 世界観: {theme.get('worldview', '')}",
        f"- このアカウントの強み: {theme.get('strength', '')}",
        f"- 口調: {theme.get('tone', '')}",
    ]
    if theme.get("target"):
        lines.append(f"- 想定ターゲット: {theme['target']}")
    return "\n".join(lines)


# --- 1. 悩みの生成 --------------------------------------------------------
def generate_worry(client: anthropic.Anthropic, theme: dict[str, Any], product: dict[str, Any]) -> str:
    """商品とテーマから「ターゲットの具体的な悩み」を 1 文で生成する。"""
    system = (
        "あなたは SNS マーケティングのリサーチャーです。"
        "与えられたアカウントテーマと商品情報から、その商品を必要とする人が"
        "日常で実際に口にするレベルの具体的な悩みを推測します。"
    )
    user = f"""以下のアカウントで紹介する商品について、ターゲットが抱えている「具体的な悩み」を推測してください。

## アカウントテーマ
{_theme_summary(theme)}

## 商品情報
- 商品名: {product.get('name', '')}
- 価格: {product.get('price', '')}円
- ショップ: {product.get('shop', '')}
- レビュー件数: {product.get('review_count', 0)}件（売れ筋ランキング {product.get('rank', '-')}位）
- キャッチコピー: {product.get('catch_copy', '')}

## 出力条件
- 「〜で困っている」「〜がうまくいかない」のように、状況が目に浮かぶ具体的な悩みを 1 つだけ。
- 60〜100 文字程度の日本語 1 文。
- 前置き・見出し・箇条書き・鉤括弧は付けず、悩みの文だけを出力する。
"""
    worry = _complete(client, system, user, WORRY_MAX_TOKENS)
    # 念のため、複数行で返ってきた場合は最初の意味のある行を採用する
    for line in worry.splitlines():
        line = line.strip().lstrip("-・*").strip()
        if line:
            return line.strip("「」")
    return worry


# --- 2. 投稿本文の生成 ----------------------------------------------------
def load_post_prompt_template() -> str:
    """投稿作成プロンプトのテンプレートを読み込む。"""
    try:
        return POST_PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ClaudeAPIError(
            f"プロンプトファイルが見つかりません: {POST_PROMPT_FILE}"
        ) from exc


def render_prompt(template: str, values: dict[str, str]) -> str:
    """テンプレート内のプレースホルダを実際の値で置換する。

    ``{ジャンル}`` ``【商品名】`` ``{{tone}}`` など複数の記法に対応する。
    どのプレースホルダも見つからない場合は、テンプレート末尾に
    入力情報ブロックを追記して情報が欠落しないようにする。
    """
    rendered = template
    replaced_any = False
    for key, value in values.items():
        for alias in PLACEHOLDER_ALIASES[key]:
            for fmt in PLACEHOLDER_FORMATS:
                token = fmt.format(alias)
                if token in rendered:
                    rendered = rendered.replace(token, value)
                    replaced_any = True

    if not replaced_any:
        rendered = rendered.rstrip() + f"""

# 入力情報
- ジャンル: {values['genre']}
- ターゲットの悩み: {values['worry']}
- 商品名: {values['product']}
- 口調: {values['tone']}
"""
    return rendered


def build_post_prompt(
    template: str, genre: str, worry: str, product_name: str, tone: str
) -> str:
    """投稿生成用のプロンプト全文を組み立てる。"""
    prompt = render_prompt(
        template,
        {"genre": genre, "worry": worry, "product": product_name, "tone": tone},
    ).strip()
    if not prompt:
        raise ClaudeAPIError(
            f"{POST_PROMPT_FILE.name} が空です。投稿作成プロンプトを記入してください。"
        )
    # テンプレート側に「伸びる確率」の指示が無い場合だけ、パース用のルールを補う
    if "伸びる確率" not in prompt:
        prompt += OUTPUT_RULE
    return prompt


def _clean_body(block: str) -> str:
    """1 パターン分のテキストから、見出しや確率表記を取り除いて本文だけにする。"""
    lines: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if PROBABILITY_PATTERN.search(stripped):
            continue
        if PATTERN_HEADING.match(stripped):
            continue
        if stripped in {"---", "***", "___"}:
            continue
        if re.match(r"^\s*(?:【)?\s*(?:本文|投稿文|投稿本文)\s*(?:】)?\s*[:：]?\s*$", stripped):
            continue
        lines.append(line.rstrip())

    body = "\n".join(lines).strip()
    # 前後を囲む引用記号やコードフェンスを外す
    body = re.sub(r"^```[a-zA-Z]*\n?|```$", "", body).strip()
    if len(body) >= 2 and body[0] in "「『\"" and body[-1] in "」』\"":
        body = body[1:-1].strip()
    return body


def parse_best_post(response_text: str) -> tuple[str, float]:
    """「伸びる確率」が最も高いパターンの本文と確率を返す。"""
    matches = list(PROBABILITY_PATTERN.finditer(response_text))
    if not matches:
        # 確率表記が無い場合は、応答全体を 1 案として扱う
        body = _clean_body(response_text)
        if not body:
            raise ClaudeAPIError("Claude の応答から投稿本文を抽出できませんでした。")
        logger.warning("「伸びる確率」の表記が見つからないため、応答全体を本文として採用します。")
        return body, 0.0

    # 各確率表記の直前の見出しから、次の確率表記の直前までを 1 パターンとみなす
    boundaries: list[int] = []
    lines = response_text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    for match in matches:
        start = match.start()
        # 確率表記の行から遡り、直近のパターン見出し行を開始位置にする
        line_index = max(i for i, off in enumerate(offsets) if off <= start)
        block_start = offsets[line_index]
        for i in range(line_index, max(-1, line_index - 4), -1):
            if PATTERN_HEADING.match(lines[i].strip()):
                block_start = offsets[i]
                break
        boundaries.append(block_start)

    candidates: list[tuple[float, str]] = []
    for i, match in enumerate(matches):
        block_start = boundaries[i]
        block_end = boundaries[i + 1] if i + 1 < len(boundaries) else len(response_text)
        body = _clean_body(response_text[block_start:block_end])
        if body:
            candidates.append((float(match.group(1)), body))

    if not candidates:
        raise ClaudeAPIError("Claude の応答から投稿本文を抽出できませんでした。")

    probability, body = max(candidates, key=lambda item: item[0])
    return body, probability


def generate_post(
    client: anthropic.Anthropic,
    template: str,
    genre: str,
    worry: str,
    product_name: str,
    tone: str,
) -> dict[str, Any]:
    """投稿本文を生成し、最も伸びる確率が高いパターンを返す。"""
    prompt = build_post_prompt(template, genre, worry, product_name, tone)
    system = (
        "あなたは Threads で伸びる投稿を作るコピーライターです。"
        "指示されたフォーマットを厳密に守って出力してください。"
    )
    raw = _complete(client, system, prompt, POST_MAX_TOKENS)
    body, probability = parse_best_post(raw)
    return {"body": body, "probability": probability, "raw_response": raw}
