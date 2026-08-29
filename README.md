# Threads × 楽天アフィリエイト 自動運用システム

Threads API・Claude API・楽天商品検索 API を組み合わせ、複数アカウントの SNS 運用を
自動化するサーバーレス構成のプロジェクトです。実行は **GitHub Actions**、管理画面は
**Streamlit Community Cloud** を想定しています。

状態はすべて **リポジトリ内の JSON ファイル**（`data/queue.json` / `data/post_history.json`）で
管理し、各ワークフローの最後に差分をコミット・プッシュして保存します。データベースは不要です。

---

## 1. ディレクトリ構成

```
.
├── .github/
│   ├── scripts/
│   │   └── commit_data.sh   # data/ config/ の差分をコミット・プッシュする共通処理
│   └── workflows/
│       ├── batch_job.yml    # 前日 23:00 リサーチ・生成・キュー作成
│       ├── publish_job.yml  # 当日 7:00〜23:00 の 15 分毎に配信
│       └── repost_job.yml   # 月水金 19:00 に過去バズ投稿を再配信
├── app.py                   # Streamlit ダッシュボード
├── config/
│   ├── accounts.json        # アカウントごとのテーマ・トークン設定（本番）
│   └── accounts.example.json # 記入例（美容系・生活用品系）
├── data/
│   ├── queue.json           # 翌日の配信スケジュール（7 件/アカウント）
│   └── post_history.json    # 投稿履歴とエンゲージメント記録
├── prompts/
│   └── Claude×アフィリエイト投稿作成プロンプト.txt  # ★空ファイル。ご自身のプロンプトを記入
├── src/
│   ├── accounts.py          # アカウント設定の読み書き
│   ├── batch_generator.py   # 7 件リサーチ・Claude 生成・スケジュール割当
│   ├── claude_api.py        # 悩み生成 / 投稿生成 / 「伸びる確率」のパース
│   ├── publisher.py         # 親投稿と PR リプライの実行
│   ├── rakuten_api.py       # 楽天商品検索 API
│   ├── reposter.py          # 上位 3 件抽出・再投稿
│   ├── scheduler.py         # 7 枠の生成・ゆらぎ・ゴールデンタイム割当
│   ├── store.py             # queue.json / post_history.json の操作
│   └── threads_api.py       # Threads Graph API
└── requirements.txt
```

---

## 2. 処理の流れ

### 前日バッチ（`batch_job.yml` / JST 23:00 = UTC 14:00）
1. **リサーチ** — 楽天商品検索 API でテーマに沿った売れ筋商品を**必ず 7 件**取得（1〜7 位）
2. **悩み生成** — Claude API が商品とテーマからターゲットの具体的な悩みを推測
3. **投稿生成** — `prompts/Claude×アフィリエイト投稿作成プロンプト.txt` の
   `{ジャンル}` `{ターゲットの悩み}` `{商品名}` `{口調}` を置換して送信し、
   **「伸びる確率：〇〇％」が最大のパターンの本文だけ**を抽出（7 件すべてに対して実行）
4. **スケジュール予約** — 翌日 7:00〜23:00 を 7 枠に分割し、各枠に ±15〜30 分のゆらぎを付与。
   ゴールデンタイム（7〜8 時台・20〜22 時台の計 4 枠）に売れ筋上位の商品を優先割当し、
   `data/queue.json` にアカウント別に保存

### 当日配信（`publish_job.yml` / JST 7:00〜23:00 の 15 分毎）
1. `data/queue.json` から予約時刻を過ぎた未配信データを取得
2. Threads API で**親投稿**を送信
3. 返ってきた投稿 ID を `reply_to_id` に指定し、コメント欄へ
   **「楽天アフィリエイト URL + ※PR」の子投稿（リプライ）**を送信
4. ステータスを更新し、`data/post_history.json` に記録

### 再投稿（`repost_job.yml` / 月水金 JST 19:00 = UTC 10:00）
1. `data/post_history.json` の過去 1 週間分について、いいね数を Threads API から取り直す
2. 各アカウントの上位 3 件を抽出
3. **元の本文と全く同じ内容**で「8 件目の投稿」として再投稿（親投稿 + PR リプライ）

---

## 3. セットアップ手順（初心者向け）

### 手順 1: 必要な API キーを用意する

| キー | 取得場所 | 用途 |
|---|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ → API Keys | 悩み・投稿本文の生成 |
| `RAKUTEN_APP_ID` | https://webservice.rakuten.co.jp/ → アプリ ID 発行 | 商品検索 |
| `RAKUTEN_AFFILIATE_ID` | https://affiliate.rakuten.co.jp/ | アフィリエイト URL の付与 |
| `THREADS_TOKEN_<アカウントID大文字>` | https://developers.facebook.com/ → Threads API | 投稿の送信 |

### 手順 2: GitHub Secrets に登録する

1. GitHub でリポジトリを開く
2. **Settings** タブ → 左メニューの **Secrets and variables** → **Actions**
3. **New repository secret** を押す
4. Name と Secret を入力して **Add secret**。以下をすべて登録します。

```
ANTHROPIC_API_KEY        ← Claude の API キー
RAKUTEN_APP_ID           ← 楽天のアプリ ID
RAKUTEN_AFFILIATE_ID     ← 楽天アフィリエイト ID
THREADS_TOKEN_BEAUTY     ← 美容系アカウントの Threads トークン
THREADS_TOKEN_LIVING     ← 生活用品系アカウントの Threads トークン
```

> **アカウントを増やしたとき**
> - 方法 A: `THREADS_TOKEN_<アカウントID大文字>` の Secret を追加し、
>   `publish_job.yml` / `repost_job.yml` の `env:` に同名の行を 1 行足す
> - 方法 B: `THREADS_TOKENS_JSON` という Secret 1 つに
>   `{"beauty": "THAA...", "living": "THAA..."}` の形式でまとめる（ワークフローの編集不要）

任意で **Variables** タブに以下を登録できます（Secrets ではなく Variables です）。

```
CLAUDE_MODEL        ← 使用モデルの上書き（未設定なら claude-3-5-sonnet-latest）
PR_REPLY_TEMPLATE   ← PR リプライの文面。{url} が URL に置換されます
```

### 手順 3: GitHub へ初回プッシュする

```bash
# 1) このプロジェクトのフォルダへ移動
cd threads_github_system

# 2) Git リポジトリとして初期化（すでに git clone した場合は不要）
git init
git branch -M main

# 3) すべてのファイルを追加してコミット
git add .
git commit -m "初回コミット: Threads 自動運用システム"

# 4) GitHub 上で作成した空のリポジトリと接続（URL はご自身のものに置き換え）
git remote add origin https://github.com/<ユーザー名>/<リポジトリ名>.git

# 5) プッシュ
git push -u origin main
```

> **重要**: `config/accounts.json` に Threads トークンを直接書くと、リポジトリに平文で
> 保存されます。**公開リポジトリでは必ず GitHub Secrets（環境変数）を使ってください。**
> 管理画面のトークン欄は空のままにし、「トークンの環境変数名」だけを設定するのが安全です。

### 手順 4: Actions の書き込み権限を有効にする

ワークフローは JSON の更新結果をリポジトリにコミットします。以下を必ず確認してください。

1. **Settings** → **Actions** → **General**
2. 一番下の **Workflow permissions** で **Read and write permissions** を選択
3. **Save** を押す

### 手順 5: Streamlit Community Cloud にデプロイする

1. https://share.streamlit.io/ にアクセスし、GitHub アカウントでサインイン
2. **Create app**（または **New app**）→ **Deploy a public app from GitHub** を選択
3. 以下を入力
   - **Repository**: `<ユーザー名>/<リポジトリ名>`
   - **Branch**: `main`
   - **Main file path**: `app.py`
4. **Advanced settings** → **Secrets** に、以下を貼り付ける（TOML 形式）

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
RAKUTEN_APP_ID = "10000000..."
RAKUTEN_AFFILIATE_ID = "xxxxxxxx.xxxxxxxx"
THREADS_TOKEN_BEAUTY = "THAA..."
THREADS_TOKEN_LIVING = "THAA..."
```

5. **Deploy** を押す（初回は 2〜3 分かかります）

> **Streamlit Cloud での保存について**
> Streamlit Community Cloud のファイルシステムは一時的なので、画面上で保存した
> `accounts.json` はアプリ再起動で消えます。画面の
> 「⬇️ accounts.json をダウンロード」でファイルを取得し、リポジトリの
> `config/accounts.json` に置き換えてコミット・プッシュしてください。
> （手元の PC で `streamlit run app.py` を実行して編集し、そのままコミットするのが最も簡単です）

### 手順 6: 投稿作成プロンプトを記入する

`prompts/Claude×アフィリエイト投稿作成プロンプト.txt` は**空ファイル**です。
ご自身のプロンプトを記入してください。以下のプレースホルダが自動で置換されます。

- `{ジャンル}` / `{ターゲットの悩み}` / `{商品名}` / `{口調}`
- `{{商品名}}` `【商品名】` `[商品名]` `<商品名>` の記法にも対応しています

プロンプト内に「伸びる確率：〇〇％」を出力させる指示が無い場合は、
システムが標準の出力ルール（3 パターン + 各パターンの伸びる確率）を自動で追記します。

### 手順 7: 動作確認する

GitHub の **Actions** タブから手動実行できます。

1. `publish_job` → **Run workflow** → `dry_run` に ✅ を入れて実行（送信されません）
2. `batch_job` → **Run workflow** で翌日分のキューを生成
3. `data/queue.json` が更新されコミットされていれば成功です

---

## 4. ローカルでの実行

```bash
pip install -r requirements.txt

# 管理画面
streamlit run app.py

# 各バッチ（環境変数を設定してから実行）
export ANTHROPIC_API_KEY=...
export RAKUTEN_APP_ID=...
export RAKUTEN_AFFILIATE_ID=...
export THREADS_TOKEN_BEAUTY=...

python -m src.batch_generator              # 翌日分の生成・予約
python -m src.publisher --dry-run          # 配信対象の確認のみ
python -m src.publisher                    # 実際に配信
python -m src.reposter --dry-run           # 再投稿候補の確認のみ
python -m src.reposter                     # 実際に再投稿
```

主なオプション:

| コマンド | オプション | 説明 |
|---|---|---|
| `batch_generator` | `--account <ID>` | 指定アカウントだけ処理 |
| `batch_generator` | `--keep-pending` | 既存キューを残して追記 |
| `publisher` | `--limit <N>` | 1 回の配信件数を制限 |
| `publisher` / `reposter` | `--dry-run` | 送信せず対象を表示 |

---

## 5. スケジュール一覧（cron は UTC 表記）

| ワークフロー | JST | cron (UTC) |
|---|---|---|
| `batch_job.yml` | 毎日 23:00 | `0 14 * * *` |
| `publish_job.yml` | 毎日 7:00〜23:00 の 15 分毎 | `*/15 0-14,22-23 * * *` |
| `repost_job.yml` | 月・水・金 19:00 | `0 10 * * 1,3,5` |

> GitHub Actions の cron は UTC で動作し、混雑状況によって数分〜十数分遅れて起動する
> ことがあります。配信処理は「予約時刻を過ぎた未配信データ」を対象にするため、
> 多少の遅延があっても投稿が飛ばされることはありません。

---

## 6. 運用上の注意

- **アフィリエイト表記**: PR リプライには必ず `※PR` が入ります（`PR_REPLY_TEMPLATE` を
  変更しても自動で付加されます）。ステマ規制対応として削除しないでください。
- **トークンの有効期限**: Threads の長期アクセストークンは約 60 日で失効します。
  期限が切れたら Secrets を更新してください。
- **API 利用料**: Claude API は 1 アカウントあたり 1 日 14 リクエスト（悩み 7 + 投稿 7）
  を消費します。
- **モデル**: 既定は `claude-3-5-sonnet-latest` です。より新しいモデルへ切り替える場合は
  Variables の `CLAUDE_MODEL` を設定してください。
