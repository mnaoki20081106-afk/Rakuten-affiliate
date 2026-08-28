"""Claude 出力のパース（伸びる確率が最も高いパターンの抽出）。"""

from src.claude_api import build_post_variables, parse_best_pattern, render_prompt
from src.config import Account


def test_最も伸びる確率が高いパターンの本文だけを抽出する():
    response = """【パターン1】
本文：
低い方の本文
伸びる確率：62％

【パターン2】
本文：
高い方の本文
2行目もある
伸びる確率：88％
理由：共感フックが強いため

【パターン3】
本文：
真ん中の本文
伸びる確率：70％
"""
    result = parse_best_pattern(response)
    assert result["probability"] == 88.0
    assert result["pattern_count"] == 3
    assert result["body"] == "高い方の本文\n2行目もある"


def test_全角数字とパターン見出しに対応する():
    response = """【パターン１】
本文：
一つ目
伸びる確率：６２％

【パターン２】
本文：
二つ目
伸びる確率：９１％
"""
    result = parse_best_pattern(response)
    assert result["probability"] == 91.0
    assert result["body"] == "二つ目"


def test_見出しが無くても確率で区切れる():
    response = "前半の本文\n伸びる確率: 55%\n\n後半の本文\n伸びる確率: 77%\n"
    result = parse_best_pattern(response)
    assert result["probability"] == 77.0
    assert result["body"] == "後半の本文"


def test_全角記号は半角化されない():
    response = "本文：\nほんとに疲れてない？　無理しないで〜！（実話）\n伸びる確率：62％"
    assert parse_best_pattern(response)["body"] == "ほんとに疲れてない？　無理しないで〜！（実話）"


def test_確率表記が無い場合は全体を本文として返す():
    result = parse_best_pattern("```\nそのままの本文\n```")
    assert result["body"] == "そのままの本文"
    assert result["probability"] is None
    assert result["pattern_count"] == 0


def test_プロンプト変数が置換される():
    account = Account(
        id="a1", name="テスト", genre="美容", worldview="夜の独白", strength="自腹",
        tone="やさしい丁寧語",
    )
    item = {"item_name": "テスト化粧水", "price": 1980, "rank": 3}
    template = "ジャンル={ジャンル} / 悩み={ターゲットの悩み} / 商品={商品名} / 口調={口調} / 英語={genre}"
    rendered = render_prompt(template, build_post_variables(account, item, "肌荒れが治らない"))
    assert rendered == (
        "ジャンル=美容 / 悩み=肌荒れが治らない / 商品=テスト化粧水 / 口調=やさしい丁寧語 / 英語=美容"
    )


def test_二重波括弧と角括弧のプレースホルダにも対応する():
    rendered = render_prompt("{{商品名}} / [商品名] / 【商品名】", {"商品名": "X"})
    assert rendered == "X / X / X"
