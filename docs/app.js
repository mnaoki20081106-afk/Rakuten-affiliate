/*
 * SNS自動運用 管理画面（GitHub Pages / Vanilla JS）
 *
 * 【設計方針】
 * このリポジトリは公開（Public）前提のテンプレートである。
 * したがって機密情報（Threadsトークン・APIキー）は、リポジトリ内のファイルへ
 * 一切保存しない。管理画面で入力された機密情報は、その場でブラウザ内において
 * libsodium の sealed box でリポジトリの公開鍵により暗号化し、
 * GitHub Actions Secrets API へ直接送信する。平文はネットワークにもJSONにも残らない。
 *
 * 非機密のメタデータ（テーマ・キーワード等）だけを Contents API 経由で
 * config/accounts.json へコミットする。
 *
 * 認証は利用者が入力した Personal Access Token を localStorage に保存して使う。
 * トークンをこのソースへ直書きしてはならない。
 */

"use strict";

// ======================================================================
// 定数
// ======================================================================
const STORAGE_KEY = "sns-admin-auth";
const API_ROOT = "https://api.github.com";
const THREADS_TOKEN_PREFIX = "THREADS_TOKEN_";

/** Threads の OAuth 連携で使う値 */
const THREADS_AUTHORIZE_URL = "https://threads.net/oauth/authorize";
const THREADS_SCOPES = "threads_basic,threads_content_publish,threads_manage_insights";
const THREADS_APP_SECRET_SECRET = "THREADS_APP_SECRET";
const THREADS_OAUTH_CODE_SECRET = "THREADS_OAUTH_CODE";
const CONNECT_WORKFLOW = "threads_connect.yml";
const OAUTH_STATE_KEY = "sns-admin-oauth";

/**
 * この管理画面が置かれているディレクトリのURL。
 * index.html を直接開いた場合でも同じ値になるよう、末尾のファイル名は落とす。
 * Meta に登録するリダイレクトURLは完全一致で照合されるため、ここが揺れてはいけない。
 */
function baseUrl() {
  return `${location.origin}${location.pathname.replace(/[^/]*$/, "")}`;
}

/** Meta のアプリに登録するリダイレクトURL。 */
function redirectUri() {
  return baseUrl();
}

/** GitHubのトークン作成画面（必要な権限にチェックが入った状態で開く） */
const TOKEN_NEW_URL = {
  admin: "https://github.com/settings/tokens/new?scopes=repo&description=sns-admin",
  workflow: "https://github.com/settings/tokens/new?scopes=repo,workflow&description=sns-workflow",
};

/** 接続中のリポジトリに対応する各種GitHub画面へのリンク */
function repoLinks() {
  const { owner = "", repo = "" } = state.auth || {};
  const base = `https://github.com/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
  return {
    base,
    actions: `${base}/actions`,
    batch: `${base}/actions/workflows/batch.yml`,
    publisher: `${base}/actions/workflows/publisher.yml`,
    reposter: `${base}/actions/workflows/reposter.yml`,
    tokenRefresh: `${base}/actions/workflows/token_refresh.yml`,
    secrets: `${base}/settings/secrets/actions`,
    pages: `${base}/settings/pages`,
    actionsSettings: `${base}/settings/actions`,
  };
}

/** 外部リンク（新しいタブで開く） */
function link(href, text) {
  return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(text)}</a>`;
}

const PATHS = {
  accounts: "config/accounts.json",
  settings: "config/settings.json",
  queue: "data/queue.json",
  history: "data/post_history.json",
  used: "data/used_items.json",
  runLog: "data/run_log.json",
  tokenStatus: "data/token_status.json",
  prompt: "prompts/Claude×アフィリエイト投稿作成プロンプト.txt",
};

/** 全アカウント共通で必要になるシークレット。管理画面から登録できる。 */
const GLOBAL_SECRETS = [
  {
    name: "ANTHROPIC_API_KEY",
    label: "Claude APIキー",
    required: true,
    help: "投稿文と「ターゲットの悩み」の生成に使います。",
    link: "https://console.anthropic.com/",
    linkLabel: "Anthropic Console",
    placeholder: "sk-ant-...",
    steps: [
      "Anthropic Console でアカウントを作る",
      "Billing から少額（$5〜10程度）のクレジットを購入する（これをしないとキーが使えません）",
      "API Keys → Create Key で発行し、sk-ant- で始まる文字列をコピーする",
    ],
    caution: "1アカウント7投稿で1日14回の呼び出しです。まず1アカウントで運用し、Console の Usage で日額を確認してから増やしてください。",
  },
  {
    name: "RAKUTEN_APP_ID",
    label: "楽天アプリID",
    required: true,
    help: "楽天市場の商品検索に使います。",
    link: "https://webservice.rakuten.co.jp/",
    linkLabel: "楽天ウェブサービス",
    placeholder: "10000000000000000000",
    steps: [
      "楽天ウェブサービスに楽天会員でログインする",
      "「アプリID発行」を押す",
      "アプリ名（例: sns-auto）とURL（このリポジトリのURLでOK）を入力する",
      "発行された数字の羅列をコピーする",
    ],
  },
  {
    name: "RAKUTEN_AFFILIATE_ID",
    label: "楽天アフィリエイトID",
    required: true,
    help: "投稿に貼るアフィリエイトリンクの生成に使います。",
    link: "https://affiliate.rakuten.co.jp/",
    linkLabel: "楽天アフィリエイト",
    placeholder: "0123abcd.4567efgh...",
    steps: [
      "楽天アフィリエイトにログインする",
      "表示されているアフィリエイトIDをコピーする",
    ],
  },
  {
    name: THREADS_APP_SECRET_SECRET,
    label: "Threads アプリシークレット",
    required: true,
    help: "トークンの発行に使います。上の「Threads連携の準備」からも登録できます。",
    link: "https://developers.facebook.com/",
    linkLabel: "Meta for Developers",
    placeholder: "（Threads API の設定画面にある Client Secret）",
    steps: [
      "アプリのダッシュボードから Threads API の設定画面（Settings）を開く",
      "「アプリシークレット（Client Secret）」の表示を押して値をコピーする",
    ],
  },
  {
    name: "WORKFLOW_TOKEN",
    label: "GitHub PAT（workflow権限つき）",
    required: true,
    help:
      "翌日分の配信スケジュール（ワークフローファイル）を書き換えるために必要です。" +
      "repo と workflow の両方にチェックを入れて発行したトークンを登録してください。",
    link: TOKEN_NEW_URL.workflow,
    linkLabel: "この設定でトークンを作る",
    placeholder: "ghp_...",
    steps: [
      "上のリンクを開く（repo と workflow にチェックが入った状態で開きます）",
      "画面下部の Generate token を押す",
      "表示された ghp_ で始まる文字列をコピーする",
    ],
    caution: "これが無いと翌日の配信予約を作れず、自動投稿が始まりません。",
  },
];

// src/config.py の DEFAULT_SETTINGS と対応させる
const DEFAULT_SETTINGS = {
  timezone: "Asia/Tokyo",
  posts_per_day: 7,
  active_hours: { start: "07:00", end: "23:00" },
  jitter_minutes: { min: 15, max: 30 },
  golden_time_ranges: [["07:00", "09:00"], ["20:00", "23:00"]],
  min_gap_minutes: 20,
  duplicate_exclusion_days: 14,
  rakuten: { fetch_hits: 50, sort: "-reviewCount", min_price: 0, max_price: 0, ng_keywords: [] },
  claude: { model: "claude-3-5-sonnet-latest", max_tokens: 2000, temperature: 1.0, worry_max_tokens: 400 },
  publisher: {
    max_cron_per_file: 60, window_before_minutes: 5, window_after_minutes: 60,
    workflow_basename: "publisher", python_version: "3.11",
  },
  threads: { app_id: "" },
  repost: { lookback_days: 7, top_n: 3, cooldown_days: 14, weekday_rank_map: { 0: 0, 2: 1, 4: 2 } },
  pr_text: "※PR",
};

/**
 * config/accounts.json に保存する項目（すべて非機密）。
 * src/config.py の Account dataclass と 1 対 1 で対応させること。
 * ここに機密情報のフィールドを追加してはならない。
 */
const ACCOUNT_DEFAULTS = {
  id: "", name: "", enabled: true,
  genre: "", worldview: "", strength: "", tone: "", target: "",
  search_keywords: [],
  threads_user_id: "", threads_username: "",
  rakuten_site_registered: false,
  posts_per_day: 7, rakuten_affiliate_id: "", note: "",
};

const STATUS_LABEL = { pending: "予約中", sent: "送信済み", failed: "失敗", expired: "期限切れ" };

// ======================================================================
// 状態
// ======================================================================
const state = {
  auth: null,            // { token, owner, repo, branch }
  accounts: [],
  settings: {},
  queue: {},
  history: { posts: [] },
  used: { accounts: {} },
  runLog: {},
  tokenStatus: { accounts: {} },
  prompt: "",
  secrets: new Map(),    // 登録済みシークレット名 → { updated_at }
  secretsReadable: true, // 一覧を取得できたか（権限不足なら false）
  publicKey: null,       // { key_id, key }
  shas: {},
  view: "dashboard",
};

// ======================================================================
// 小さなユーティリティ
// ======================================================================
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** src/config.py の slugify() と同じ規則でIDを正規化する。 */
function slugify(text) {
  return String(text ?? "")
    .normalize("NFKC")
    .replace(/[^0-9A-Za-z]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}

/** 楽天アフィリエイトへ登録する、そのアカウントの公開URL。 */
function threadsUrl(username) {
  const name = String(username || "").replace(/^@/, "").trim();
  return name ? `https://www.threads.com/@${name}` : "";
}

/** アカウントIDからシークレット名を求める（Account.token_secret_name と同じ規則）。 */
function tokenSecretName(accountId) {
  return `${THREADS_TOKEN_PREFIX}${slugify(accountId).toUpperCase()}`;
}

function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast toast-${type}`;
  node.textContent = message;
  const area = $("#toast-area");
  area.appendChild(node);
  // 続けて操作すると積み上がって画面を覆うため、古いものから消す
  while (area.children.length > 3) area.firstElementChild.remove();
  setTimeout(() => {
    node.style.transition = "opacity .3s";
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 300);
  }, type === "error" ? 7000 : 3500);
}

function formatDateTime(iso, withDate = true) {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const parts = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t)?.value ?? "";
  const time = `${get("hour")}:${get("minute")}`;
  return withDate ? `${get("month")}/${get("day")} ${time}` : time;
}

// ---- UTF-8 対応の Base64 変換 ----------------------------------------
// btoa/atob は Latin-1 しか扱えないため、日本語を含む内容では必ずここを通す。
function encodeBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function decodeBase64(base64) {
  const binary = atob(String(base64).replace(/\s/g, ""));
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder("utf-8").decode(bytes);
}

// ======================================================================
// 認証情報の保存
// ======================================================================
function loadAuth() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY) || sessionStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveAuth(auth, remember) {
  const store = remember ? localStorage : sessionStorage;
  const other = remember ? sessionStorage : localStorage;
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(auth));
    other.removeItem(STORAGE_KEY);
  } catch {
    toast("ブラウザにトークンを保存できませんでした", "error");
  }
}

function clearAuth() {
  localStorage.removeItem(STORAGE_KEY);
  sessionStorage.removeItem(STORAGE_KEY);
}

// ======================================================================
// GitHub REST API（共通）
// ======================================================================
function repoUrl(suffix = "") {
  const { owner, repo } = state.auth;
  return `${API_ROOT}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}${suffix}`;
}

function contentsUrl(path) {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return repoUrl(`/contents/${encoded}`);
}

async function ghFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    cache: "no-store",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${state.auth.token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });

  if (response.status === 204) return null;
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const error = new Error(payload?.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

// ---- ファイル（Contents API） ----------------------------------------
async function getFile(path) {
  const url = `${contentsUrl(path)}?ref=${encodeURIComponent(state.auth.branch)}&t=${Date.now()}`;
  try {
    const payload = await ghFetch(url);
    state.shas[path] = payload.sha;
    return decodeBase64(payload.content || "");
  } catch (error) {
    if (error.status === 404) {
      state.shas[path] = null;
      return null;
    }
    throw error;
  }
}

async function getJson(path, fallback) {
  const text = await getFile(path);
  if (text === null || text.trim() === "") return fallback;
  try {
    return JSON.parse(text);
  } catch {
    toast(`${path} のJSONを解釈できませんでした`, "error");
    return fallback;
  }
}

/** ファイルを上書きコミットする。SHAが古い場合は取り直して1度だけ再試行する。 */
async function putFile(path, text, message) {
  const body = { message, content: encodeBase64(text), branch: state.auth.branch };
  if (state.shas[path]) body.sha = state.shas[path];

  try {
    const payload = await ghFetch(contentsUrl(path), { method: "PUT", body: JSON.stringify(body) });
    state.shas[path] = payload.content.sha;
    return payload;
  } catch (error) {
    // 409/422 は GitHub Actions などから同時に更新されSHAがずれた場合
    if (error.status === 409 || error.status === 422) {
      await getFile(path);
      body.sha = state.shas[path] || undefined;
      const payload = await ghFetch(contentsUrl(path), { method: "PUT", body: JSON.stringify(body) });
      state.shas[path] = payload.content.sha;
      toast("他の処理と競合したため、最新の状態に対して保存し直しました", "info");
      return payload;
    }
    throw error;
  }
}

function putJson(path, data, message) {
  return putFile(path, `${JSON.stringify(data, null, 2)}\n`, message);
}

// ======================================================================
// GitHub Actions Secrets（暗号化して保存）
// ======================================================================
/** libsodium の準備を待つ。読み込めていなければ分かりやすい例外を投げる。 */
async function ensureSodium() {
  if (typeof window.sodium === "undefined") {
    throw new Error(
      "暗号化ライブラリ(libsodium)を読み込めませんでした。ページを再読み込みしてください。"
    );
  }
  await window.sodium.ready;
  return window.sodium;
}

/** リポジトリの公開鍵を取得する（1セッション1回だけ）。 */
async function getPublicKey() {
  if (!state.publicKey) {
    state.publicKey = await ghFetch(repoUrl("/actions/secrets/public-key"));
  }
  return state.publicKey;
}

/**
 * 値をリポジトリの公開鍵で暗号化する（GitHubが要求する sealed box 方式）。
 * 復号できるのはGitHubだけで、暗号文からは元の値を復元できない。
 */
async function encryptSecretValue(value, publicKeyBase64) {
  const sodium = await ensureSodium();
  const key = sodium.from_base64(publicKeyBase64, sodium.base64_variants.ORIGINAL);
  const sealed = sodium.crypto_box_seal(sodium.from_string(value), key);
  return sodium.to_base64(sealed, sodium.base64_variants.ORIGINAL);
}

/** 登録済みシークレットの一覧（名前と更新日時のみ。値は取得できない）。 */
async function loadSecrets() {
  try {
    const payload = await ghFetch(repoUrl("/actions/secrets?per_page=100"));
    state.secrets = new Map((payload.secrets || []).map((s) => [s.name, s]));
    state.secretsReadable = true;
  } catch (error) {
    state.secrets = new Map();
    state.secretsReadable = false;
    if (error.status !== 403 && error.status !== 404) throw error;
  }
}

/** シークレットを暗号化して保存（新規作成・上書きの両方）。 */
async function putSecret(name, value) {
  const publicKey = await getPublicKey();
  const encrypted = await encryptSecretValue(value, publicKey.key);
  await ghFetch(repoUrl(`/actions/secrets/${encodeURIComponent(name)}`), {
    method: "PUT",
    body: JSON.stringify({ encrypted_value: encrypted, key_id: publicKey.key_id }),
  });
  state.secrets.set(name, { name, updated_at: new Date().toISOString() });
}

async function deleteSecret(name) {
  try {
    await ghFetch(repoUrl(`/actions/secrets/${encodeURIComponent(name)}`), { method: "DELETE" });
  } catch (error) {
    if (error.status !== 404) throw error;
  }
  state.secrets.delete(name);
}

function secretStatus(name) {
  if (!state.secretsReadable) return { known: false, registered: false };
  const entry = state.secrets.get(name);
  return { known: true, registered: Boolean(entry), updatedAt: entry?.updated_at };
}

/** data/token_status.json から、そのアカウントのトークン有効期限の状況を求める。 */
function tokenExpiry(accountId) {
  const entry = state.tokenStatus?.accounts?.[accountId];
  if (!entry?.expires_at) return null;
  const expires = new Date(entry.expires_at);
  if (Number.isNaN(expires.getTime())) return null;
  const days = Math.floor((expires - Date.now()) / 86400000);
  return { days, expiresAt: entry.expires_at, status: entry.status, error: entry.error || "" };
}

/** 残り日数に応じた注意バッジ。トークン更新が止まっていることに気付けるようにする。 */
function expiryBadge(accountId) {
  const expiry = tokenExpiry(accountId);
  if (!expiry) return "";
  if (expiry.days <= 0) {
    return '<span class="badge badge-failed">期限切れ</span>';
  }
  const cls = expiry.days <= 14 ? "badge-expired" : "badge-pending";
  return `<span class="badge ${cls}">残り ${escapeHtml(expiry.days)} 日</span>`;
}

function secretBadge(name) {
  const status = secretStatus(name);
  if (!status.known) return '<span class="badge badge-pending">状態不明</span>';
  if (!status.registered) return '<span class="badge badge-expired">未登録</span>';
  return `<span class="badge badge-sent">登録済み</span>
    <span class="tiny sub">${escapeHtml(formatDateTime(status.updatedAt))} 更新</span>`;
}

// ======================================================================
// Threads との連携（OAuth）
// ======================================================================
/** アプリID・アプリシークレットが両方登録されていれば連携できる。 */
function threadsAppId() {
  return (state.settings?.threads?.app_id || "").trim();
}

function canConnectThreads() {
  return Boolean(threadsAppId()) && secretStatus(THREADS_APP_SECRET_SECRET).registered;
}

/**
 * Threads の認可画面へ移動する。
 * 利用者は Threads 自身のページでログインするため、パスワードはここには渡らない。
 */
async function startThreadsConnect(accountId) {
  if (!canConnectThreads()) {
    toast("先に「APIキー」タブで Threads アプリID とアプリシークレットを登録してください", "error");
    return;
  }
  // 戻ってきたときに、どのアカウントの連携だったかを判別するための控え
  const nonce = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  try {
    sessionStorage.setItem(OAUTH_STATE_KEY, JSON.stringify({ nonce, accountId }));
  } catch {
    toast("ブラウザの記憶領域を使えないため連携を開始できません", "error");
    return;
  }

  const params = new URLSearchParams({
    client_id: threadsAppId(),
    redirect_uri: redirectUri(),
    scope: THREADS_SCOPES,
    response_type: "code",
    state: nonce,
  });
  location.href = `${THREADS_AUTHORIZE_URL}?${params}`;
}

/** 連携用ワークフローを起動する。 */
async function dispatchConnectWorkflow(accountId) {
  await ghFetch(repoUrl(`/actions/workflows/${CONNECT_WORKFLOW}/dispatches`), {
    method: "POST",
    body: JSON.stringify({
      ref: state.auth.branch,
      inputs: { account_id: accountId, redirect_uri: redirectUri() },
    }),
  });
}

/** 起動したワークフローの結果を待つ。 */
async function waitForConnectWorkflow(startedAt, timeoutMs = 180000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5000));
    let runs;
    try {
      runs = await ghFetch(repoUrl(`/actions/workflows/${CONNECT_WORKFLOW}/runs?per_page=5`));
    } catch {
      continue;  // 一時的な失敗は待って再確認する
    }
    const run = (runs.workflow_runs || []).find(
      (r) => new Date(r.created_at).getTime() >= startedAt - 60000
    );
    if (!run) continue;
    if (run.status === "completed") {
      return { ok: run.conclusion === "success", url: run.html_url, conclusion: run.conclusion };
    }
  }
  return { ok: false, timedOut: true };
}

/**
 * 認可画面から戻ってきたときの処理。
 * 認可コードは実行ログに残さないため、暗号化して一時シークレットへ入れ、
 * ワークフロー側で交換・削除させる。
 */
async function handleOAuthCallback() {
  const params = new URLSearchParams(location.search);
  // Threads は戻り先URLの末尾に「#_」を付けることがある。
  // location.search には入らないが、念のため取り除いておく。
  const code = (params.get("code") || "").replace(/#_?$/, "") || null;
  const returnedState = params.get("state");
  const error = params.get("error_description") || params.get("error");

  if (!code && !error) return false;

  // URL からコードを消す（履歴や共有で漏れないように）
  history.replaceState(null, "", redirectUri());

  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(OAUTH_STATE_KEY) || "null");
  } catch {
    saved = null;
  }
  sessionStorage.removeItem(OAUTH_STATE_KEY);

  if (error) {
    toast(`Threads との連携が中断されました: ${error}`, "error");
    return false;
  }
  if (!saved || saved.nonce !== returnedState) {
    toast("連携の照合に失敗しました。お手数ですがもう一度お試しください。", "error");
    return false;
  }

  showConnectProgress(saved.accountId, "認可コードを安全に受け渡しています...");
  try {
    await putSecret(THREADS_OAUTH_CODE_SECRET, code);
    showConnectProgress(saved.accountId, "トークンを発行しています...（1〜2分かかります）");
    const startedAt = Date.now();
    await dispatchConnectWorkflow(saved.accountId);
    const result = await waitForConnectWorkflow(startedAt);

    if (result.ok) {
      showConnectProgress(saved.accountId, "", { done: true });
      toast("Threads と連携しました", "success");
    } else if (result.timedOut) {
      showConnectProgress(saved.accountId, "", { failed: true, message: "時間内に完了しませんでした。Actions の実行結果を確認してください。" });
    } else {
      showConnectProgress(saved.accountId, "", { failed: true, message: "連携に失敗しました。Actions の実行ログを確認してください。", url: result.url });
    }
  } catch (exc) {
    showConnectProgress(saved.accountId, "", { failed: true, message: exc.message });
  }
  return true;
}

/** 連携中の状態を画面に出す。 */
function showConnectProgress(accountId, message, options = {}) {
  const box = $("#connect-progress");
  const account = state.accounts.find((a) => a.id === accountId);
  const name = account ? (account.name || account.id) : accountId;
  box.classList.remove("hidden");

  if (options.done) {
    box.innerHTML = `<div class="alert-info">
      <strong>${escapeHtml(name)} と Threads の連携が完了しました。</strong>
      トークンは暗号化して保存され、以後は毎週自動で更新されます。
    </div>`;
    reloadAll();
    return;
  }
  if (options.failed) {
    box.innerHTML = `<div class="alert-error">
      <strong>${escapeHtml(name)} の連携に失敗しました。</strong>
      ${escapeHtml(options.message || "")}
      ${options.url ? `<span class="block mt-1">→ ${link(options.url, "実行ログを開く")}</span>` : ""}
    </div>`;
    return;
  }
  box.innerHTML = `<div class="alert-info">
    <strong>${escapeHtml(name)} を連携しています。</strong> ${escapeHtml(message)}
    <span class="block tiny">この画面を閉じずにお待ちください。</span>
  </div>`;
}

// ======================================================================
// ログイン
// ======================================================================
/**
 * このページのURLから接続先リポジトリを推測する。
 * GitHub Pages は https://<オーナー名>.github.io/<リポジトリ名>/ で配信されるため、
 * フォークした人それぞれの環境に自動で合う。
 */
function guessRepoFromUrl() {
  const match = location.hostname.match(/^([^.]+)\.github\.io$/i);
  if (!match) return null;
  const owner = match[1];
  const segment = location.pathname.split("/").filter(Boolean)[0];
  // ユーザーページ（<owner>.github.io）はリポジトリ名も同じ形になる
  return { owner, repo: segment || `${owner}.github.io` };
}

/** 接続先の表示と入力欄を同期する。 */
function syncRepoFields() {
  const owner = $("#login-owner").value.trim();
  const repo = $("#login-repo").value.trim();
  $("#login-repo-detected").textContent = owner && repo ? `${owner} / ${repo}` : "（未設定）";
}

function setupRepoDetection() {
  const guessed = guessRepoFromUrl();
  const fields = $("#login-repo-fields");
  const note = $("#login-repo-note");

  if (guessed) {
    $("#login-owner").value = guessed.owner;
    $("#login-repo").value = guessed.repo;
    note.textContent = "このページのURLから自動で判定しました。違う場合だけ「変更する」を押してください。";
  } else {
    // GitHub Pages 以外（ローカル確認など）では自分で入力してもらう
    fields.classList.remove("hidden");
    note.textContent = "このページのURLからは判定できませんでした。接続先を入力してください。";
  }
  syncRepoFields();

  $("#login-repo-toggle").addEventListener("click", () => {
    fields.classList.toggle("hidden");
    $("#login-repo-toggle").textContent = fields.classList.contains("hidden") ? "変更する" : "閉じる";
  });
  $("#login-owner").addEventListener("input", syncRepoFields);
  $("#login-repo").addEventListener("input", syncRepoFields);
}

async function verifyAndStart(auth, remember) {
  state.auth = auth;
  const repo = await ghFetch(
    `${API_ROOT}/repos/${encodeURIComponent(auth.owner)}/${encodeURIComponent(auth.repo)}`
  );
  if (!repo.permissions?.push) {
    toast("このトークンには書き込み権限がありません。閲覧のみ可能です。", "error");
  }
  if (remember !== undefined) saveAuth(auth, remember);

  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#repo-label").textContent = `${auth.owner}/${auth.repo} @ ${auth.branch}`;
  await reloadAll();
}

function setupLogin() {
  setupRepoDetection();

  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#login-submit");
    const errorBox = $("#login-error");
    errorBox.classList.add("hidden");
    button.disabled = true;
    button.textContent = "接続中...";

    const auth = {
      token: $("#login-token").value.trim(),
      owner: $("#login-owner").value.trim(),
      repo: $("#login-repo").value.trim(),
      branch: $("#login-branch").value.trim() || "main",
    };

    try {
      await verifyAndStart(auth, $("#login-remember").checked);
    } catch (error) {
      state.auth = null;
      const hint = error.status === 401
        ? "トークンが正しくないか、有効期限が切れています。"
        : error.status === 404
          ? "リポジトリが見つかりません。オーナー名・リポジトリ名と、トークンの repo 権限を確認してください。"
          : error.message;
      errorBox.textContent = `接続できませんでした: ${hint}`;
      errorBox.classList.remove("hidden");
    } finally {
      button.disabled = false;
      button.textContent = "接続する";
    }
  });
}

// ======================================================================
// データ読み込み
// ======================================================================
async function reloadAll() {
  $("#loading").classList.remove("hidden");
  $$(".view").forEach((el) => el.classList.add("hidden"));
  try {
    const [accountsRaw, settingsRaw, queue, history, used, runLog, tokenStatus, prompt] = await Promise.all([
      getJson(PATHS.accounts, { accounts: [] }),
      getJson(PATHS.settings, {}),
      getJson(PATHS.queue, {}),
      getJson(PATHS.history, { posts: [] }),
      getJson(PATHS.used, { accounts: {} }),
      getJson(PATHS.runLog, {}),
      getJson(PATHS.tokenStatus, { accounts: {} }),
      getFile(PATHS.prompt),
      loadSecrets(),
    ]);

    const list = Array.isArray(accountsRaw) ? accountsRaw : (accountsRaw.accounts || []);
    state.accounts = list.map((account) => sanitizeAccount(account));
    state.settings = mergeDeep(DEFAULT_SETTINGS, settingsRaw || {});
    state.queue = queue || {};
    state.history = history?.posts ? history : { posts: [] };
    state.used = used?.accounts ? used : { accounts: {} };
    state.runLog = runLog || {};
    state.tokenStatus = tokenStatus?.accounts ? tokenStatus : { accounts: {} };
    state.prompt = prompt ?? "";
  } catch (error) {
    toast(`読み込みに失敗しました: ${error.message}`, "error");
  } finally {
    $("#loading").classList.add("hidden");
    render();
  }
}

/**
 * 既知の非機密フィールドだけを取り込む。
 * 旧バージョンのファイルにトークン欄が残っていても、ここで確実に落とす。
 */
function sanitizeAccount(raw) {
  const account = { ...ACCOUNT_DEFAULTS };
  for (const key of Object.keys(ACCOUNT_DEFAULTS)) {
    if (raw && raw[key] !== undefined) account[key] = raw[key];
  }
  return account;
}

function mergeDeep(base, override) {
  const result = { ...base };
  for (const [key, value] of Object.entries(override || {})) {
    result[key] = (value && typeof value === "object" && !Array.isArray(value)
      && result[key] && typeof result[key] === "object" && !Array.isArray(result[key]))
      ? mergeDeep(result[key], value)
      : value;
  }
  return result;
}

async function saveAccounts(message) {
  await putJson(PATHS.accounts, { accounts: state.accounts }, message);
}

// ======================================================================
// 画面切り替え
// ======================================================================
function render() {
  $$("#tabs .tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === state.view);
  });
  $$(".view").forEach((el) => el.classList.add("hidden"));
  const target = $(`#view-${state.view}`);
  if (!target) return;
  target.classList.remove("hidden");

  const renderers = {
    dashboard: renderDashboard,
    accounts: renderAccounts,
    secrets: renderSecrets,
    settings: renderSettings,
    prompt: renderPrompt,
  };
  (renderers[state.view] || renderDashboard)(target);

  // 「→ 〇〇タブを開く」形式のリンクで画面を切り替える
  target.querySelectorAll("[data-goto]").forEach((anchor) => {
    anchor.addEventListener("click", (event) => {
      event.preventDefault();
      state.view = anchor.dataset.goto;
      render();
      window.scrollTo({ top: 0 });
    });
  });
}

// ======================================================================
// ダッシュボード
// ======================================================================
function queuePosts() {
  const accounts = state.queue?.accounts || {};
  return Object.entries(accounts).flatMap(([accountId, entry]) =>
    (entry?.posts || []).map((post) => ({ account_id: accountId, ...post }))
  );
}

function statCard(value, label) {
  return `<div class="stat">
    <div class="stat-value">${escapeHtml(value)}</div>
    <div class="stat-label">${escapeHtml(label)}</div>
  </div>`;
}

/** セットアップの進み具合を示すチェックリスト（初心者向けの道しるべ）。 */
function renderSetupChecklist() {
  const missingGlobal = GLOBAL_SECRETS.filter(
    (secret) => secret.required && !secretStatus(secret.name).registered
  );
  const accountsWithoutToken = state.accounts.filter(
    (account) => account.enabled && !secretStatus(tokenSecretName(account.id)).registered
  );

  const links = repoLinks();
  const steps = [
    {
      done: state.accounts.length > 0,
      label: "アカウントを登録する",
      hint: "「アカウント管理」タブの ＋追加 から登録します",
    },
    {
      done: state.secretsReadable && missingGlobal.length === 0,
      label: "APIキーを登録する",
      hint: state.secretsReadable
        ? `未登録: ${missingGlobal.map((s) => s.label).join(" / ") || "なし"}`
        : "トークンの権限が足りず状態を確認できません",
      action: { href: "#secrets", label: "「APIキー」タブを開く" },
    },
    {
      done: state.secretsReadable && state.accounts.length > 0 && accountsWithoutToken.length === 0,
      label: "各アカウントのThreadsトークンを登録する",
      hint: accountsWithoutToken.length
        ? `未登録: ${accountsWithoutToken.map((a) => a.name || a.id).join(" / ")}`
        : "すべて登録済みです",
      action: { href: "https://developers.facebook.com/", label: "Meta for Developers を開く" },
    },
    {
      done: state.accounts.length > 0
        && state.accounts.filter((a) => a.enabled).every((a) => a.rakuten_site_registered),
      label: "楽天アフィリエイトにサイトを登録する",
      hint: (() => {
        const missing = state.accounts
          .filter((a) => a.enabled && !a.rakuten_site_registered)
          .map((a) => a.name || a.id);
        return missing.length
          ? `未登録: ${missing.join(" / ")}（登録しないと成果が計上されません）`
          : "すべて登録済みです";
      })(),
      action: { href: "#accounts", label: "「アカウント管理」タブを開く" },
    },
    {
      done: Boolean(state.runLog?.generated_at),
      label: "バッチを1回実行する",
      hint: "Run workflow を押すと翌日分の投稿が生成されます",
      action: { href: links.batch, label: "Batch Generator を開く" },
    },
  ];

  if (steps.every((step) => step.done)) return "";

  return `<div class="card pad-lg">
    <h2 class="h2">セットアップの進み具合</h2>
    <p class="sub mb-3">すべて済みになれば自動運用が始まります。</p>
    <ol class="steps">
      ${steps.map((step, index) => `<li>
        <span class="step-no ${step.done ? "done" : ""}">${step.done ? "✓" : index + 1}</span>
        <span class="min-w-0">
          <span class="${step.done ? "sub" : "bold"}">${escapeHtml(step.label)}</span>
          <span class="block tiny">${escapeHtml(step.hint)}</span>
          ${step.done || !step.action ? "" :
            `<span class="block small mt-1">→ ${
              step.action.href.startsWith("#")
                ? `<a href="#" data-goto="${escapeHtml(step.action.href.slice(1))}">${escapeHtml(step.action.label)}</a>`
                : link(step.action.href, step.action.label)
            }</span>`}
        </span>
      </li>`).join("")}
    </ol>
  </div>`;
}


function renderDashboard(root) {
  const posts = queuePosts();
  const sent = posts.filter((p) => p.status === "sent").length;
  const failed = posts.filter((p) => p.status === "failed").length;
  const history = state.history.posts || [];
  const errors = state.runLog?.errors || [];
  const workflows = state.runLog?.workflows;

  const tokenWarnings = state.accounts
    .filter((account) => account.enabled)
    .map((account) => ({ account, expiry: tokenExpiry(account.id) }))
    .filter(({ expiry }) => expiry && (expiry.days <= 14 || expiry.status === "failed"));

  const tokenBanner = tokenWarnings.length
    ? `<div class="alert-error">
         <strong>Threadsトークンの期限が近づいています。</strong>
         ${escapeHtml(tokenWarnings.map(({ account, expiry }) =>
           expiry.days <= 0
             ? `${account.name || account.id}: 期限切れ`
             : `${account.name || account.id}: 残り${expiry.days}日`).join(" / "))}<br />
         <span class="block mt-1">
           → ${link(repoLinks().tokenRefresh, "Token Refresh を開いて Run workflow を押す")}
         </span>
         <span class="block tiny">
           それでも直らない場合は、アカウント編集画面の「トークンの取得のしかた」から取り直してください。
         </span>
       </div>`
    : "";

  const banner = errors.length
    ? `<div class="alert-error">直近のバッチで ${errors.length} 件のエラーが発生しました:
        ${escapeHtml(errors.map((e) => `${e.account_id}: ${e.error}`).join(" / "))}</div>`
    : "";

  const workflowInfo = workflows
    ? `<p class="tiny sub">配信ワークフロー: <code class="code">${escapeHtml((workflows.files || []).join(", "))}</code>
       （cron ${escapeHtml(workflows.cron_count ?? 0)} 件 / ${escapeHtml(workflows.file_count ?? 0)} ファイル）
       ${link(repoLinks().actions, "Actions を開く")}</p>`
    : "";

  root.innerHTML = `
    <h1 class="h1">ダッシュボード</h1>
    ${banner}
    ${tokenBanner}
    ${renderSetupChecklist()}
    <div class="grid-4">
      ${statCard(state.accounts.filter((a) => a.enabled).length, "有効なアカウント")}
      ${statCard(posts.length, `予約投稿（${state.queue?.target_date || "未生成"}）`)}
      ${statCard(sent, "送信済み")}
      ${statCard(history.length, "累計投稿数")}
    </div>

    <div class="card stack-xs">
      <div class="row wrap gap-sm gap-xs small">
        <span class="sub">最終バッチ:</span>
        <span>${escapeHtml(state.runLog?.generated_at ? formatDateTime(state.runLog.generated_at) + " JST" : "未実行")}</span>
        <span class="sub">配信対象日:</span>
        <span>${escapeHtml(state.queue?.target_date || "-")}</span>
        ${failed ? `<span class="badge badge-failed">失敗 ${failed} 件</span>` : ""}
      </div>
      ${workflowInfo}
    </div>

    <div class="card">
      <h2 class="section-title">予約キュー</h2>
      ${renderQueueSection()}
    </div>

    <div class="card">
      <h2 class="section-title">投稿履歴</h2>
      ${renderHistoryTable()}
    </div>

    <div class="card">
      <h2 class="section-title">紹介済み商品</h2>
      <p class="tiny sub mb-3">過去 ${escapeHtml(state.settings.duplicate_exclusion_days)} 日以内の商品は次回のリサーチから除外されます。</p>
      ${renderUsedItems()}
    </div>
  `;
}

function renderQueueSection() {
  const entries = Object.entries(state.queue?.accounts || {});
  if (!entries.length) {
    return `<p class="small sub">キューが空です。GitHub Actions の <code class="code">Batch Generator</code> を実行すると翌日分が生成されます。</p>`;
  }

  return entries.map(([accountId, entry]) => {
    const posts = entry.posts || [];
    const rows = posts.map((post) => {
      const status = post.status || "pending";
      const item = post.item || {};
      return `<div class="sep-top pt-3 mt-3 stack-xs">
        <div class="row wrap gap-xs small">
          <span class="mono bold">${escapeHtml(formatDateTime(post.scheduled_at_jst, false))}</span>
          <span class="badge badge-${escapeHtml(status)}">${escapeHtml(STATUS_LABEL[status] || status)}</span>
          ${post.is_golden_time ? '<span class="badge badge-golden">ゴールデン</span>' : ""}
          <span class="tiny sub">順位 ${escapeHtml(item.rank ?? "-")}</span>
          ${post.probability != null ? `<span class="tiny sub">伸びる確率 ${escapeHtml(post.probability)}%</span>` : ""}
        </div>
        <div class="post-body">${escapeHtml(post.body)}</div>
        <div class="tiny sub break-all">
          ${escapeHtml(item.item_name || "")}
          ${post.affiliate_url ? `<br /><a class="underline" href="${escapeHtml(post.affiliate_url)}" target="_blank" rel="noopener">${escapeHtml(post.affiliate_url)}</a>` : ""}
        </div>
        ${post.error ? `<div class="alert-error">${escapeHtml(post.error)}</div>` : ""}
      </div>`;
    }).join("");

    return `<details class="account-queue mb-2">
      <summary class="medium small">
        ${escapeHtml(entry.account_name || accountId)}
        <span class="sub normal">（${posts.length} 件）</span>
      </summary>
      <div class="pl-2">${rows}</div>
    </details>`;
  }).join("");
}

function renderHistoryTable() {
  const posts = [...(state.history.posts || [])].reverse().slice(0, 100);
  if (!posts.length) return `<p class="small sub">まだ投稿履歴がありません。</p>`;

  const rows = posts.map((post) => `<tr>
    <td class="nowrap">${escapeHtml(formatDateTime(post.published_at_jst || post.published_at))}</td>
    <td>${escapeHtml(post.account_name || post.account_id)}</td>
    <td class="right mono">${escapeHtml(post.likes ?? 0)}</td>
    <td>${post.is_repost ? '<span class="badge badge-on">再投稿</span>' : ""}</td>
    <td class="clip"><div class="truncate">${escapeHtml((post.body || "").replace(/\n/g, " "))}</div></td>
    <td class="clip"><div class="truncate sub">${escapeHtml(post.item?.item_name || "")}</div></td>
  </tr>`).join("");

  return `<div class="scroll-x"><table class="data">
    <thead><tr>
      <th>投稿日時(JST)</th><th>アカウント</th><th class="right">いいね</th>
      <th></th><th>本文</th><th>商品</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function renderUsedItems() {
  const entries = Object.entries(state.used?.accounts || {});
  if (!entries.length) return `<p class="small sub">記録がありません。</p>`;

  return entries.map(([accountId, items]) => {
    const rows = [...(items || [])].reverse().slice(0, 50).map((item) => `<tr>
      <td class="mono tiny">${escapeHtml(item.item_code)}</td>
      <td><div class="truncate clip">${escapeHtml(item.item_name)}</div></td>
      <td class="right">${escapeHtml(item.rank ?? "")}</td>
      <td class="nowrap">${escapeHtml(item.target_date || "")}</td>
    </tr>`).join("");

    return `<details class="account-queue mb-2">
      <summary class="medium small">
        ${escapeHtml(accountId)} <span class="sub normal">（${(items || []).length} 件）</span>
      </summary>
      <div class="scroll-x"><table class="data">
        <thead><tr><th>itemCode</th><th>商品名</th><th class="right">順位</th><th>使用日</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </details>`;
  }).join("");
}

// ======================================================================
// APIキー（GitHub Secrets）
// ======================================================================
/**
 * Threadsアプリの設定カード。
 * 「アプリを作る → リダイレクトURLを登録 → アプリIDとシークレットを保存」までを
 * 1つの流れとして見せる。リダイレクトURLはこの画面のURLから自動で決まる。
 */
/**
 * Threads連携の準備ウィザード。
 *
 * Metaの画面は項目名が似た横文字ばかりで迷いやすいため、
 * 1画面につき1つの作業だけを出し、「何をする画面か」「どこを見るか」
 * 「その値はどんな見た目か」を日本語で示す。
 */
const SETUP_STEPS = [
  {
    key: "create",
    title: "Metaでアプリを作る",
    lead: "Threadsと連携するための「アプリ」を1つ作ります。何アカウント運用しても、作るのはこの1つだけです。",
    body: () => `
      <ol class="ol">
        <li>Meta for Developers を開いてログインする</li>
        <li><strong>マイアプリ</strong> → <strong>アプリを作成</strong> を押す</li>
        <li>ユースケースの選択で <strong>「Threads APIの使用」</strong> を選ぶ</li>
        <li>アプリ名（何でも構いません。例：<code class="code">my-threads-bot</code>）と
            連絡先メールアドレスを入力して作成する</li>
      </ol>
      <p class="mt-3">${link("https://developers.facebook.com/", "Meta for Developers を開く")}</p>`,
  },
  {
    key: "callback",
    title: "コールバックURLを登録する",
    lead: "連携を許可したあと、この管理画面に戻ってくるためのURLです。Metaに登録しておかないと連携できません。",
    body: () => `
      <p class="small">
        アプリのダッシュボードから <strong>Threads API の設定画面（Settings）</strong> を開きます。
        その中の <strong>コールバックURL（Redirect URI）</strong> の欄に、下のURLを貼り付けて保存してください。
      </p>
      <div class="mt-3">
        <label class="label" for="wiz-redirect">貼り付けるURL</label>
        <input id="wiz-redirect" class="input mono" readonly value="${escapeHtml(redirectUri())}" />
        <p class="mt-2"><button type="button" id="wiz-copy-redirect" class="btn-ghost btn-sm">URLをコピー</button></p>
      </div>
      <div class="alert-warn mt-3">
        <strong>一字一句そのまま</strong>貼り付けてください。末尾の <code class="code">/</code> も必要です。
        1文字でも違うと連携が失敗します。
      </div>`,
    setup: (root) => {
      root.querySelector("#wiz-copy-redirect")?.addEventListener("click", () =>
        copyText(redirectUri(), root.querySelector("#wiz-redirect")));
    },
  },
  {
    key: "appid",
    title: "アプリIDを控える",
    lead: "アプリを見分けるための番号です。秘密ではないので、そのまま設定ファイルに保存します。",
    done: () => Boolean(threadsAppId()),
    body: () => `
      <p class="small">
        さきほどと<strong>同じ設定画面（Settings）</strong>に表示されている
        <strong>アプリID（Client ID）</strong> をコピーして、下に貼り付けてください。
      </p>
      <div class="looks mt-3">
        <strong>見た目の目安：</strong>数字だけが15〜17桁ほど並んだ値です。<br />
        例）<span class="mono">1234567890123456</span>
      </div>
      <div class="mt-3">
        <label class="label" for="wiz-app-id">アプリID</label>
        <input id="wiz-app-id" class="input mono" inputmode="numeric"
               value="${escapeHtml(threadsAppId())}" placeholder="1234567890123456" />
        <p class="mt-2"><button type="button" id="wiz-save-app-id" class="btn-primary btn-sm">保存する</button></p>
      </div>`,
    setup: (root) => {
      root.querySelector("#wiz-save-app-id")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const value = root.querySelector("#wiz-app-id").value.trim();
        if (!/^[0-9]+$/.test(value)) {
          toast("アプリIDは数字だけの値です。設定画面の「アプリID（Client ID）」を確認してください。", "error");
          return;
        }
        button.disabled = true;
        button.textContent = "保存中...";
        try {
          const updated = mergeDeep(state.settings, { threads: { app_id: value } });
          await putJson(PATHS.settings, updated, "chore(settings): set threads app id");
          state.settings = updated;
          toast("アプリIDを保存しました", "success");
          renderWizard();
        } catch (error) {
          toast(`保存に失敗しました: ${error.message}`, "error");
        } finally {
          button.disabled = false;
          button.textContent = "保存する";
        }
      });
    },
  },
  {
    key: "appsecret",
    title: "アプリシークレットを控える",
    lead: "トークンを発行するときに使う、パスワードにあたる値です。暗号化して保存されます。",
    done: () => secretStatus(THREADS_APP_SECRET_SECRET).registered,
    body: () => `
      <p class="small">
        <strong>同じ設定画面（Settings）</strong>にある
        <strong>アプリシークレット（Client Secret）</strong> をコピーして、下に貼り付けてください。
      </p>
      <div class="looks mt-3">
        <strong>見た目の目安：</strong>英数字が30文字ほど並んだ値です。<br />
        最初は伏せ字（●●●）になっているので、<strong>「表示」</strong>にあたるボタンを押すと読めます。
      </div>
      <div class="mt-3">
        <label class="label" for="wiz-app-secret">アプリシークレット</label>
        <input id="wiz-app-secret" type="password" class="input mono" autocomplete="off"
               placeholder="${secretStatus(THREADS_APP_SECRET_SECRET).registered ? "登録済み（変更するときだけ入力）" : ""}" />
        <p class="mt-2"><button type="button" id="wiz-save-secret" class="btn-primary btn-sm">暗号化して保存</button></p>
      </div>
      <div class="alert-warn mt-3">
        この値はブラウザの中で暗号化されてから送られ、GitHubの保管庫にだけ入ります。
        リポジトリのファイルには書き込まれません。
      </div>`,
    setup: (root) => {
      root.querySelector("#wiz-save-secret")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const input = root.querySelector("#wiz-app-secret");
        const value = input.value.trim();
        if (!value) {
          toast("アプリシークレットを入力してください", "error");
          return;
        }
        button.disabled = true;
        button.textContent = "保存中...";
        try {
          await putSecret(THREADS_APP_SECRET_SECRET, value);
          input.value = "";
          toast("アプリシークレットを暗号化して保存しました", "success");
          renderWizard();
        } catch (error) {
          toast(`保存に失敗しました: ${error.message}`, "error");
        } finally {
          button.disabled = false;
          button.textContent = "暗号化して保存";
        }
      });
    },
  },
  {
    key: "tester",
    title: "Threadsテスターを追加する",
    lead: "作ったばかりのアプリは試験中の状態です。投稿したいThreadsアカウントを、このアプリに登録します。",
    body: () => `
      <p class="small">
        <strong>同じ設定画面（Settings）</strong>にある
        <strong>「Threadsテスターの追加/削除」</strong> を開きます。
      </p>
      <ol class="ol mt-2">
        <li>連携したいThreadsアカウントの<strong>ユーザーネーム</strong>を入力して追加する</li>
        <li>追加した直後は <strong>Pending（保留中）</strong> と表示されます</li>
      </ol>
      <div class="alert-warn mt-3">
        <strong>ここではまだ終わりません。</strong>
        次の画面で、Threadsアプリ側から招待を承認する必要があります。
      </div>
      <p class="tiny mt-2">
        運用するアカウントが複数ある場合は、そのぶんユーザーネームを追加してください。
      </p>`,
  },
  {
    key: "approve",
    title: "スマホで招待を承認する",
    lead: "Pending（保留中）を解除します。この操作はパソコンではなく、スマートフォンのThreadsアプリで行います。",
    body: () => `
      <ol class="ol">
        <li>スマートフォンで <strong>Threadsアプリ</strong> を開く
            （追加したアカウントでログインしている状態にする）</li>
        <li><strong>設定</strong> → <strong>アカウント</strong> →
            <strong>ウェブサイトのアクセス許可</strong> の順に進む</li>
        <li>開発者からの<strong>招待を承認</strong>する</li>
      </ol>
      <div class="alert-info mt-3">
        承認すると Pending が解除され、連携できるようになります。
        複数アカウントある場合は、それぞれのアカウントで承認してください。
      </div>`,
  },
  {
    key: "done",
    title: "準備完了",
    lead: "これで下ごしらえは終わりです。あとはアカウントごとにボタンを押すだけです。",
    body: () => `
      <ol class="ol">
        <li><strong>「アカウント管理」タブ</strong>を開く</li>
        <li>連携したいアカウントの <strong>「Threadsでログイン」</strong> を押す</li>
        <li>Threadsのログイン画面が出るので、ログインして「許可」を押す</li>
      </ol>
      <p class="small mt-3">
        戻ってくると、トークンが自動で発行されて保存されます。
        有効期限（60日）は毎週自動で延長されるので、以後の手入れは不要です。
      </p>
      <div class="mt-3">
        <button type="button" id="wiz-goto-accounts" class="btn-primary">アカウント管理を開く</button>
      </div>`,
    setup: (root) => {
      root.querySelector("#wiz-goto-accounts")?.addEventListener("click", () => {
        closeSetupWizard();
        state.view = "accounts";
        render();
        window.scrollTo({ top: 0 });
      });
    },
  },
];

/**
 * 楽天アフィリエイトへのサイト登録の手順（アカウントごと）。
 *
 * 楽天では、アフィリエイトリンクを貼る場所（ここではThreadsのプロフィール）を
 * 「運営サイト」としてあらかじめ登録しておく必要がある。
 * 登録していないと、リンクを貼っても成果が計上されない。
 */
function rakutenSiteSteps(accountId) {
  const account = () => state.accounts.find((a) => a.id === accountId) || {};

  return [
    {
      key: "why",
      title: "なぜ登録が必要か",
      lead: "楽天では、アフィリエイトリンクを貼る場所を「運営サイト」として事前に登録しておく決まりです。",
      body: () => `
        <p class="small">
          この登録をしていないと、投稿にリンクを貼っても<strong>成果として計上されません</strong>。
          Threadsのプロフィールページを「運営サイト」として登録します。
        </p>
        <div class="alert-warn mt-3">
          登録は<strong>Threadsアカウントごと</strong>に必要です。
          複数アカウントを運用する場合は、それぞれのURLを登録してください。
        </div>
        <p class="small mt-3">
          いま設定しているアカウント：<strong>${escapeHtml(account().name || accountId)}</strong>
        </p>`,
    },
    {
      key: "url",
      title: "ThreadsのURLを調べる",
      lead: "登録するURLは、あなたのThreadsプロフィールのアドレスです。",
      done: () => Boolean(account().threads_username),
      body: () => `
        <p class="small bold">ユーザーネームの確認のしかた</p>
        <ol class="ol">
          <li>スマートフォンで <strong>Threadsアプリ</strong> を開く</li>
          <li>右下の<strong>プロフィール</strong>を開く</li>
          <li>名前の下にある <strong>@から始まる文字列</strong> がユーザーネームです</li>
        </ol>
        <p class="tiny mt-2">
          プロフィールの共有メニューから「リンクをコピー」しても同じURLが手に入ります。
        </p>

        <div class="mt-3">
          <label class="label" for="wiz-username">ユーザーネーム（@は不要）</label>
          <input id="wiz-username" class="input mono"
                 value="${escapeHtml(account().threads_username || "")}" placeholder="beauty_lab" />
          <p class="hint">入力すると、登録するURLが下に表示されます。</p>
        </div>

        <div class="mt-3">
          <label class="label" for="wiz-threads-url">登録するURL</label>
          <input id="wiz-threads-url" class="input mono" readonly
                 value="${escapeHtml(threadsUrl(account().threads_username))}" />
          <p class="mt-2">
            <button type="button" id="wiz-copy-url" class="btn-ghost btn-sm">URLをコピー</button>
            <button type="button" id="wiz-save-username" class="btn-primary btn-sm">保存する</button>
          </p>
        </div>`,
      setup: (root) => {
        const input = root.querySelector("#wiz-username");
        const preview = root.querySelector("#wiz-threads-url");
        const sync = () => { preview.value = threadsUrl(input.value); };
        input.addEventListener("input", sync);

        root.querySelector("#wiz-copy-url").addEventListener("click", () => {
          if (!preview.value) {
            toast("先にユーザーネームを入力してください", "error");
            return;
          }
          copyText(preview.value, preview);
        });

        root.querySelector("#wiz-save-username").addEventListener("click", async (event) => {
          const button = event.currentTarget;
          const value = input.value.replace(/^@/, "").trim();
          if (!value) {
            toast("ユーザーネームを入力してください", "error");
            return;
          }
          const index = state.accounts.findIndex((a) => a.id === accountId);
          if (index < 0) return;
          const backup = [...state.accounts];
          button.disabled = true;
          button.textContent = "保存中...";
          try {
            state.accounts[index] = { ...state.accounts[index], threads_username: value };
            await saveAccounts(`chore(accounts): set threads username for ${accountId}`);
            toast("保存しました", "success");
            renderWizard();
          } catch (error) {
            state.accounts = backup;
            toast(`保存に失敗しました: ${error.message}`, "error");
          } finally {
            button.disabled = false;
            button.textContent = "保存する";
          }
        });
      },
    },
    {
      key: "open",
      title: "登録画面を開く",
      lead: "楽天アフィリエイトの「サイト情報の登録」画面まで進みます。",
      body: () => `
        <ol class="ol">
          <li>楽天アフィリエイトを開く</li>
          <li>「<strong>メニュー</strong>」のアイコンを押す</li>
          <li>「<strong>マイページ</strong>」を押す</li>
          <li>「<strong>サイト情報の登録</strong>」を押す</li>
          <li>「<strong>サイト情報を追加登録する</strong>」を押す</li>
        </ol>
        <p class="small mt-3">→ ${link("https://affiliate.rakuten.co.jp/", "楽天アフィリエイトを開く")}</p>`,
    },
    {
      key: "form",
      title: "サイト情報を入力する",
      lead: "入力するのは実質2つだけです。ジャンルの欄は任意なので空のままで構いません。",
      body: () => `
        <table class="data">
          <thead><tr><th>入力欄</th><th>入れる内容</th></tr></thead>
          <tbody>
            <tr>
              <td class="bold">運営サイト名</td>
              <td>アカウント名で構いません<br />
                  <span class="mono">${escapeHtml(account().name || accountId)}</span></td>
            </tr>
            <tr>
              <td class="bold">運営サイトURL</td>
              <td><span class="mono break-all">${escapeHtml(threadsUrl(account().threads_username) || "（前の画面で入力してください）")}</span></td>
            </tr>
            <tr>
              <td class="bold">順位</td>
              <td>そのままで構いません</td>
            </tr>
            <tr>
              <td class="bold">運営サイトのジャンル</td>
              <td>任意（空でも登録できます）</td>
            </tr>
            <tr>
              <td class="bold">扱う商品ジャンル</td>
              <td>任意（空でも登録できます）</td>
            </tr>
          </tbody>
        </table>

        ${threadsUrl(account().threads_username) ? `<p class="mt-3">
          <button type="button" id="wiz-copy-url2" class="btn-ghost btn-sm">URLをコピー</button>
        </p>` : ""}

        <p class="small mt-3">
          入力できたら「<strong>サイト情報を登録する</strong>」を押してください。
        </p>`,
      setup: (root) => {
        root.querySelector("#wiz-copy-url2")?.addEventListener("click", () =>
          copyText(threadsUrl(account().threads_username)));
      },
    },
    {
      key: "finish",
      title: "登録を完了にする",
      lead: "楽天側での登録が終わったら、ここに印を付けておきます。",
      done: () => Boolean(account().rakuten_site_registered),
      body: () => `
        <p class="small">
          この印は管理画面の覚え書きです。どのアカウントの登録が済んでいるかを
          一覧で確認できるようになります。
        </p>
        <label class="check mt-3">
          <input type="checkbox" id="wiz-registered"
                 ${account().rakuten_site_registered ? "checked" : ""} />
          <span>楽天アフィリエイトへのサイト登録を済ませた</span>
        </label>
        <p class="mt-3">
          <button type="button" id="wiz-save-registered" class="btn-primary btn-sm">保存する</button>
        </p>
        <div class="alert-warn mt-3">
          楽天側の審査や反映に時間がかかる場合があります。
          登録直後にリンクが機能しなくても、しばらく待ってから確認してください。
        </div>`,
      setup: (root) => {
        root.querySelector("#wiz-save-registered").addEventListener("click", async (event) => {
          const button = event.currentTarget;
          const checked = root.querySelector("#wiz-registered").checked;
          const index = state.accounts.findIndex((a) => a.id === accountId);
          if (index < 0) return;
          const backup = [...state.accounts];
          button.disabled = true;
          button.textContent = "保存中...";
          try {
            state.accounts[index] = { ...state.accounts[index], rakuten_site_registered: checked };
            await saveAccounts(`chore(accounts): rakuten site registration for ${accountId}`);
            toast("保存しました", "success");
            renderWizard();
          } catch (error) {
            state.accounts = backup;
            toast(`保存に失敗しました: ${error.message}`, "error");
          } finally {
            button.disabled = false;
            button.textContent = "保存する";
          }
        });
      },
    },
  ];
}

/** クリップボードへコピーする。使えない環境では選択状態にして手動コピーを促す。 */
async function copyText(value, fallbackInput) {
  try {
    await navigator.clipboard.writeText(value);
    toast("URLをコピーしました", "success");
  } catch {
    fallbackInput?.select();
    toast("URLを選択しました。長押ししてコピーしてください。", "info");
  }
}

/** 手順の一覧を渡してウィザードを開く（Threadsアプリ設定／楽天サイト登録で使い回す）。 */
function openWizard(steps, index = 0) {
  state.wizSteps = steps;
  state.wizStep = index;
  $("#setup-modal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  renderWizard();
}

function openSetupWizard(index = 0) {
  openWizard(SETUP_STEPS, index);
}

function closeSetupWizard() {
  $("#setup-modal").classList.add("hidden");
  document.body.style.overflow = "";
  render();
}

function renderWizard() {
  const steps = state.wizSteps || SETUP_STEPS;
  const total = steps.length;
  const index = Math.max(0, Math.min(state.wizStep ?? 0, total - 1));
  state.wizStep = index;
  const step = steps[index];
  const body = $("#setup-body");

  // 上部: 進捗
  $("#setup-count").textContent =
    `${index + 1} / ${total}${step.done?.() ? "　✓ 入力済み" : ""}`;
  $("#setup-dots").innerHTML = steps.map((_, i) =>
    `<span class="wiz-dot ${i < index ? "on" : ""} ${i === index ? "now" : ""}"></span>`).join("");

  // 中央: この画面でやること
  body.innerHTML = `
    <h3 class="wiz-title">${escapeHtml(step.title)}</h3>
    <p class="small mt-2">${escapeHtml(step.lead)}</p>
    <div class="mt-3">${step.body()}</div>
  `;

  // 下部: 移動
  $("#setup-foot").innerHTML = `
    <button type="button" id="wiz-prev" class="btn-ghost" ${index === 0 ? "disabled" : ""}>戻る</button>
    <span class="grow"></span>
    ${index === total - 1
      ? `<button type="button" id="wiz-finish" class="btn-ghost">閉じる</button>`
      : `<button type="button" id="wiz-next" class="btn-primary">次へ</button>`}
  `;

  step.setup?.(body);
  $("#wiz-prev")?.addEventListener("click", () => { state.wizStep -= 1; renderWizard(); });
  $("#wiz-next")?.addEventListener("click", () => { state.wizStep += 1; renderWizard(); });
  $("#wiz-finish")?.addEventListener("click", closeSetupWizard);

  // 画面が変わったら先頭から読ませる
  $("#setup-scroll").scrollTop = 0;
}

function setupWizard() {
  $("#setup-close").addEventListener("click", closeSetupWizard);
  // 画面全体を使うため、外側をタップして閉じる動作は設けない（誤操作を防ぐ）
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#setup-modal").classList.contains("hidden")) {
      closeSetupWizard();
    }
  });
}

/** 「APIキー」タブに置く、Threads連携の準備状況カード。 */
function renderThreadsAppCard() {
  const appId = threadsAppId();
  const secretOk = secretStatus(THREADS_APP_SECRET_SECRET).registered;
  const ready = canConnectThreads();

  return `<div class="card pad-lg">
    <div class="between mb-2">
      <h3 class="h2">Threads連携の準備</h3>
      <span class="badge ${ready ? "badge-sent" : "badge-expired"}">${ready ? "準備できています" : "未設定"}</span>
    </div>
    <p class="small sub">
      Threadsに投稿するための下ごしらえです。最初に1回だけ行えば、
      以降は各アカウントの「Threadsでログイン」を押すだけで連携できます。
    </p>

    <ul class="steps mt-2">
      <li>
        <span class="step-no ${appId ? "done" : ""}">${appId ? "✓" : "1"}</span>
        <span><span class="bold">アプリID</span>
          <span class="block tiny">${appId ? `登録済み（${escapeHtml(appId)}）` : "未登録"}</span></span>
      </li>
      <li>
        <span class="step-no ${secretOk ? "done" : ""}">${secretOk ? "✓" : "2"}</span>
        <span><span class="bold">アプリシークレット</span>
          <span class="block tiny">${secretOk ? "登録済み" : "未登録"}</span></span>
      </li>
    </ul>

    <div class="row wrap gap-sm mt-3">
      <button type="button" id="open-setup" class="btn-primary">
        ${ready ? "設定を見直す" : "設定をはじめる"}
      </button>
      <span class="tiny">全7画面・1画面ずつ進みます</span>
    </div>

    ${renderAppModeGuide()}
  </div>`;
}

/**
 * 「テスターに追加」が必要なのは開発モードだからで、審査を通せば不要になる。
 * ただし自分のアカウントを動かすだけなら審査は不要なので、その旨を先に伝える。
 * 普段は畳んでおき、必要になったときだけ開いてもらう。
 */
function renderAppModeGuide() {
  const base = baseUrl();
  return `<details class="mt-3">
    <summary class="small bold" style="cursor: pointer;">「テスターに追加」をしなくて済む方法はある？</summary>

    <p class="small mt-2">
      あります。ただし <strong>Metaの審査（アプリレビュー）が必要</strong>です。
      <strong>自分のアカウントを運用するだけなら、開発モードのままで問題ありません。</strong>
      投稿もいいね数の取得も、通常どおり動きます。
    </p>

    <table class="data mt-2">
      <thead><tr><th></th><th>開発モード（最初はこちら）</th><th>ライブモード</th></tr></thead>
      <tbody>
        <tr>
          <td class="bold">連携できるアカウント</td>
          <td>テスターに追加したアカウントのみ</td>
          <td>制限なし</td>
        </tr>
        <tr>
          <td class="bold">必要な手続き</td>
          <td>なし</td>
          <td>Metaの審査（アプリレビュー）</td>
        </tr>
        <tr>
          <td class="bold">向いている場面</td>
          <td>自分のアカウントを運用する</td>
          <td>他の人にも使ってもらう</td>
        </tr>
      </tbody>
    </table>

    <p class="small mt-3 bold">審査に出すときに必要になるURL</p>
    <p class="tiny">
      プライバシーポリシーと利用規約のひな形を用意してあります（運営者名と連絡先の記入が必要です）。
    </p>
    <div class="mt-2">
      <label class="label" for="policy-privacy">プライバシーポリシー</label>
      <input id="policy-privacy" class="input mono" readonly value="${escapeHtml(base)}privacy.html" />
      <p class="tiny mt-1">
        ${link(`${base}privacy.html`, "内容を確認する")}
        ・<button type="button" class="btn-ghost btn-sm" data-copy="${escapeHtml(base)}privacy.html">URLをコピー</button>
      </p>
    </div>
    <div class="mt-2">
      <label class="label" for="policy-terms">利用規約</label>
      <input id="policy-terms" class="input mono" readonly value="${escapeHtml(base)}terms.html" />
      <p class="tiny mt-1">
        ${link(`${base}terms.html`, "内容を確認する")}
        ・<button type="button" class="btn-ghost btn-sm" data-copy="${escapeHtml(base)}terms.html">URLをコピー</button>
      </p>
    </div>
  </details>`;
}

function setupThreadsAppCard(root) {
  root.querySelector("#open-setup")?.addEventListener("click", () => openSetupWizard(0));
  root.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", () =>
      copyText(button.dataset.copy, button.closest("div")?.querySelector("input")));
  });
}


function renderSecrets(root) {
  const permissionNote = state.secretsReadable
    ? ""
    : `<div class="alert-warn">
         登録済みシークレットの一覧を取得できませんでした。トークンに <code class="code">repo</code> 権限
         （fine-grained なら <code class="code">Secrets: Read and write</code>）があるか確認してください。
         保存自体は試せますが、登録状況は表示できません。
       </div>`;

  const cards = GLOBAL_SECRETS.map((secret) => `
    <div class="card stack-sm">
      <div class="wrap between gap-xs">
        <div class="min-w-0">
          <h3 class="bold">${escapeHtml(secret.label)}</h3>
          <code class="code">${escapeHtml(secret.name)}</code>
        </div>
        <div class="row gap-xs">${secretBadge(secret.name)}</div>
      </div>
      <p class="small sub">${escapeHtml(secret.help)}</p>
      ${secret.caution ? `<div class="alert-warn">${escapeHtml(secret.caution)}</div>` : ""}
      <details>
        <summary class="small bold" style="cursor: pointer;">取得のしかた</summary>
        <ol class="ol small">
          ${(secret.steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("")}
        </ol>
        <p class="small mt-1">→ ${link(secret.link, secret.linkLabel + " を開く")}</p>
      </details>
      <div class="col row-sm gap-xs">
        <input type="password" class="input mono flex-1" autocomplete="off"
               data-secret-input="${escapeHtml(secret.name)}"
               placeholder="${escapeHtml(secret.placeholder)}" />
        <button class="btn-primary shrink-0" data-secret-save="${escapeHtml(secret.name)}">
          暗号化して保存
        </button>
      </div>
    </div>`).join("");

  root.innerHTML = `
    <div>
      <h2 class="h1">APIキー</h2>
      <p class="tiny sub">
        入力した値はこのブラウザの中で暗号化されてから送信され、GitHub Secrets に保存されます。
        リポジトリのファイルには一切書き込まれません。
      </p>
    </div>
    ${permissionNote}
    ${renderThreadsAppCard()}
    <div class="alert-warn">
      一度保存した値は GitHub 側でも読み出せません（表示できるのは名前と更新日時だけです）。
      変更したいときは新しい値を入力して上書き保存してください。
    </div>
    ${cards}
    <div class="card">
      <h3 class="section-title">各アカウントのThreadsトークン</h3>
      <p class="tiny sub mb-3">
        アカウントごとのトークンは「アカウント管理」タブの各アカウントの編集画面から登録します。<br />
        登録後は毎週日曜に自動で有効期限が延長されるため、通常は手作業での更新は不要です
        （Actions タブの「Token Refresh」から手動実行もできます）。
      </p>
      ${state.accounts.length ? `<div class="scroll-x"><table class="data">
        <thead><tr><th>アカウント</th><th>シークレット名</th><th>状態</th><th>有効期限</th></tr></thead>
        <tbody>${state.accounts.map((account) => `<tr>
          <td>${escapeHtml(account.name || account.id)}</td>
          <td><code class="code">${escapeHtml(tokenSecretName(account.id))}</code></td>
          <td>${secretBadge(tokenSecretName(account.id))}</td>
          <td>${expiryBadge(account.id) || '<span class="sub tiny">未更新</span>'}</td>
        </tr>`).join("")}</tbody>
      </table></div>` : '<p class="small sub">アカウントが未登録です。</p>'}
    </div>
  `;

  setupThreadsAppCard(root);

  root.querySelectorAll("[data-secret-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      const name = button.dataset.secretSave;
      const input = root.querySelector(`[data-secret-input="${name}"]`);
      const value = input.value.trim();
      if (!value) {
        toast("値を入力してください", "error");
        return;
      }
      button.disabled = true;
      button.textContent = "保存中...";
      try {
        await putSecret(name, value);
        input.value = "";  // 平文を画面に残さない
        toast(`${name} を暗号化して保存しました`, "success");
        render();
      } catch (error) {
        toast(`保存に失敗しました: ${error.message}`, "error");
      } finally {
        button.disabled = false;
        button.textContent = "暗号化して保存";
      }
    });
  });
}

// ======================================================================
// アカウント管理
// ======================================================================
function renderAccounts(root) {
  const cards = state.accounts.map((account, index) => {
    const secretName = tokenSecretName(account.id);
    return `<div class="card stack-sm">
      <div class="row start gap-sm">
        <div class="min-w-0 flex-1">
          <div class="row wrap gap-xs">
            <h3 class="bold truncate">${escapeHtml(account.name || account.id)}</h3>
            <span class="badge ${account.enabled ? "badge-on" : "badge-off"}">${account.enabled ? "有効" : "無効"}</span>
          </div>
          <p class="tiny sub mono">${escapeHtml(account.id)}</p>
        </div>
        <div class="row gap-xs shrink-0">
          <button class="btn-ghost" data-action="edit" data-index="${index}">編集</button>
          <button class="btn-danger" data-action="delete" data-index="${index}">削除</button>
        </div>
      </div>

      <dl class="grid-3 small">
        <div><dt class="stat-label">ジャンル</dt><dd>${escapeHtml(account.genre || "-")}</dd></div>
        <div><dt class="stat-label">世界観</dt><dd>${escapeHtml(account.worldview || "-")}</dd></div>
        <div><dt class="stat-label">強み</dt><dd>${escapeHtml(account.strength || "-")}</dd></div>
      </dl>

      <div class="row wrap gap-sm gap-xs tiny sub">
        <span>1日 ${escapeHtml(account.posts_per_day)} 投稿</span>
        <span>キーワード: ${escapeHtml((account.search_keywords || []).join(" / ") || account.genre || "-")}</span>
      </div>
      <div class="row wrap gap-xs tiny">
        <span class="sub">Threadsトークン:</span>
        ${secretBadge(secretName)}
        ${expiryBadge(account.id)}
        <code class="code">${escapeHtml(secretName)}</code>
      </div>
      <div class="row wrap gap-xs tiny">
        <span class="sub">楽天へのサイト登録:</span>
        <span class="badge ${account.rakuten_site_registered ? "badge-sent" : "badge-expired"}">
          ${account.rakuten_site_registered ? "登録済み" : "未登録"}
        </span>
        ${account.threads_username
          ? `<code class="code break-all">${escapeHtml(threadsUrl(account.threads_username))}</code>`
          : '<span class="sub">ユーザーネーム未設定</span>'}
      </div>

      <div class="row wrap gap-xs">
        <button class="btn-ghost btn-sm" data-rakuten="${escapeHtml(account.id)}">
          ${account.rakuten_site_registered ? "楽天の登録を見直す" : "楽天にサイト登録する"}
        </button>
        <button class="btn-primary btn-sm" data-connect="${escapeHtml(account.id)}"
                ${canConnectThreads() ? "" : "disabled"}>
          ${secretStatus(secretName).registered ? "Threadsで再連携" : "Threadsでログイン"}
        </button>
        ${canConnectThreads() ? "" :
          `<span class="tiny">先に <a href="#" data-goto="secrets">Threadsアプリの設定</a> を済ませてください</span>`}
      </div>
    </div>`;
  }).join("");

  root.innerHTML = `
    <div class="between gap-sm">
      <div>
        <h2 class="h1">アカウント</h2>
        <p class="tiny sub">
          テーマは <code class="code">config/accounts.json</code> に、トークンは暗号化して GitHub Secrets に保存されます。
        </p>
      </div>
      <button id="add-account" class="btn-primary shrink-0">＋ 追加</button>
    </div>

    ${cards || '<div class="card pad-lg center-text sub small">アカウントがまだ登録されていません。「＋ 追加」から登録してください。</div>'}
  `;

  $("#add-account")?.addEventListener("click", () => openAccountModal(-1));
  root.querySelectorAll("[data-rakuten]").forEach((button) => {
    button.addEventListener("click", () => openWizard(rakutenSiteSteps(button.dataset.rakuten)));
  });
  root.querySelectorAll("[data-connect]").forEach((button) => {
    button.addEventListener("click", () => startThreadsConnect(button.dataset.connect));
  });
  root.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.index);
      if (button.dataset.action === "edit") openAccountModal(index);
      else deleteAccount(index);
    });
  });
}

async function deleteAccount(index) {
  const account = state.accounts[index];
  if (!account) return;
  const secretName = tokenSecretName(account.id);
  const hasSecret = secretStatus(secretName).registered;
  const message = hasSecret
    ? `アカウント「${account.name || account.id}」を削除します。\n登録済みのシークレット ${secretName} も削除します。よろしいですか？`
    : `アカウント「${account.name || account.id}」を削除します。よろしいですか？`;
  if (!confirm(message)) return;

  const backup = [...state.accounts];
  state.accounts.splice(index, 1);
  try {
    await saveAccounts(`chore(accounts): remove ${account.id}`);
    if (hasSecret) {
      try {
        await deleteSecret(secretName);
      } catch (error) {
        toast(`シークレットの削除に失敗しました（手動で削除してください）: ${error.message}`, "error");
      }
    }
    toast("削除しました", "success");
    render();
  } catch (error) {
    state.accounts = backup;
    toast(`削除に失敗しました: ${error.message}`, "error");
  }
}

function openAccountModal(index) {
  const isNew = index < 0;
  const account = isNew ? { ...ACCOUNT_DEFAULTS } : state.accounts[index];

  $("#account-modal-title").textContent = isNew ? "アカウントを追加" : "アカウントを編集";
  $("#acc-index").value = String(index);
  $("#acc-original-id").value = isNew ? "" : account.id;
  $("#acc-name").value = account.name || "";
  $("#acc-id").value = account.id || "";
  $("#acc-genre").value = account.genre || "";
  $("#acc-worldview").value = account.worldview || "";
  $("#acc-strength").value = account.strength || "";
  $("#acc-target").value = account.target || "";
  $("#acc-tone").value = account.tone || "";
  $("#acc-keywords").value = (account.search_keywords || []).join("\n");
  $("#acc-posts").value = account.posts_per_day || 7;
  $("#acc-affiliate").value = account.rakuten_affiliate_id || "";
  $("#acc-username").value = account.threads_username || "";
  $("#acc-user-id").value = account.threads_user_id || "";
  syncThreadsUrlPreview();
  $("#acc-note").value = account.note || "";
  $("#acc-enabled").checked = account.enabled !== false;
  $("#acc-token").value = "";

  updateSecretPreview();
  $("#account-modal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeAccountModal() {
  // 平文をDOMに残さない
  $("#acc-token").value = "";
  $("#exchange-secret").value = "";
  $("#exchange-short").value = "";
  $("#exchange-link").removeAttribute("href");
  $("#account-modal").classList.add("hidden");
  document.body.style.overflow = "";
}

function updateSecretPreview() {
  const id = $("#acc-id").value.trim();
  const name = id ? tokenSecretName(id) : "";
  $("#acc-secret-name").textContent = name || "アカウントIDを入力してください";

  const statusBox = $("#acc-secret-status");
  if (!name) {
    statusBox.innerHTML = "";
    return;
  }
  const status = secretStatus(name);
  statusBox.innerHTML = status.registered
    ? `${secretBadge(name)} <span class="tiny sub">変更する場合のみ入力してください</span>`
    : `${secretBadge(name)} <span class="tiny sub">投稿するにはトークンの登録が必要です</span>`;
}

/**
 * 短命トークンを長寿命トークンへ交換するURLを組み立てる。
 * 値はこのブラウザの中だけで使い、どこへも送信しない。
 */
function setupTokenExchange() {
  const update = () => {
    const secret = $("#exchange-secret").value.trim();
    const short = $("#exchange-short").value.trim();
    const anchor = $("#exchange-link");
    if (!secret || !short) {
      anchor.removeAttribute("href");
      anchor.textContent = "→ 交換ページを開く（両方を入力してください）";
      return;
    }
    const url = new URL("https://graph.threads.net/access_token");
    url.searchParams.set("grant_type", "th_exchange_token");
    url.searchParams.set("client_secret", secret);
    url.searchParams.set("access_token", short);
    anchor.href = url.toString();
    anchor.textContent = "→ 交換ページを開く";
  };
  $("#exchange-secret").addEventListener("input", update);
  $("#exchange-short").addEventListener("input", update);
  update();
}

/** 入力されたユーザーネームから、登録するURLの見本を出す。 */
function syncThreadsUrlPreview() {
  const url = threadsUrl($("#acc-username").value);
  $("#acc-threads-url").innerHTML = url
    ? `<strong>登録するURL：</strong><span class="mono break-all">${escapeHtml(url)}</span>`
    : '<span class="sub">ユーザーネームを入れると、登録するURLがここに出ます</span>';
}

function setupAccountModal() {
  $("#acc-username").addEventListener("input", syncThreadsUrlPreview);
  setupTokenExchange();
  $("#account-modal-close").addEventListener("click", closeAccountModal);
  $("#account-cancel").addEventListener("click", closeAccountModal);
  $("#acc-id").addEventListener("input", updateSecretPreview);
  $("#account-modal").addEventListener("click", (event) => {
    if (event.target === $("#account-modal")) closeAccountModal();
  });

  $("#account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const index = Number($("#acc-index").value);
    const isNew = index < 0;
    const originalId = $("#acc-original-id").value;

    const id = slugify($("#acc-id").value);
    if (!id) {
      toast("アカウントIDには半角英数字を含めてください", "error");
      return;
    }
    if (state.accounts.some((a, i) => a.id === id && i !== index)) {
      toast(`アカウントIDが重複しています: ${id}`, "error");
      return;
    }

    const token = $("#acc-token").value.trim();
    const secretName = tokenSecretName(id);

    // IDを変えるとシークレット名も変わるが、既存の値は読み出せないため引き継げない
    if (!isNew && originalId && originalId !== id && !token) {
      const oldName = tokenSecretName(originalId);
      if (secretStatus(oldName).registered) {
        toast(
          `アカウントIDを変更するとシークレット名が ${secretName} に変わります。` +
          "トークンを入力し直してください。",
          "error"
        );
        return;
      }
    }

    const account = {
      ...ACCOUNT_DEFAULTS,
      id,
      name: $("#acc-name").value.trim(),
      enabled: $("#acc-enabled").checked,
      genre: $("#acc-genre").value.trim(),
      worldview: $("#acc-worldview").value.trim(),
      strength: $("#acc-strength").value.trim(),
      tone: $("#acc-tone").value.trim(),
      target: $("#acc-target").value.trim(),
      search_keywords: $("#acc-keywords").value
        .split(/[\n,、]+/).map((k) => k.trim()).filter(Boolean),
      threads_username: $("#acc-username").value.replace(/^@/, "").trim(),
      rakuten_site_registered: isNew
        ? false
        : Boolean(state.accounts[index]?.rakuten_site_registered),
      threads_user_id: $("#acc-user-id").value.trim(),
      posts_per_day: Number($("#acc-posts").value) || 7,
      rakuten_affiliate_id: $("#acc-affiliate").value.trim(),
      note: $("#acc-note").value.trim(),
    };

    const button = $("#account-form button[type=submit]");
    button.disabled = true;
    const backup = [...state.accounts];
    try {
      // トークンを先に保存する。失敗した場合はメタデータもコミットしない。
      if (token) {
        button.textContent = "トークンを暗号化中...";
        await putSecret(secretName, token);
      }
      button.textContent = "保存中...";
      if (isNew) state.accounts.push(account);
      else state.accounts[index] = account;
      await saveAccounts(`chore(accounts): ${isNew ? "add" : "update"} ${id}`);

      toast(token ? "保存し、トークンを暗号化して登録しました" : "保存しました", "success");
      closeAccountModal();
      render();
    } catch (error) {
      state.accounts = backup;
      toast(`保存に失敗しました: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存する";
    }
  });
}

// ======================================================================
// 共通設定
// ======================================================================
function renderSettings(root) {
  const s = state.settings;
  const golden = (s.golden_time_ranges || []).map((r) => `${r[0]}-${r[1]}`).join("\n");

  root.innerHTML = `
    <div class="card stack-sm">
      <div>
        <h2 class="h1">共通設定</h2>
        <p class="tiny sub"><code class="code">config/settings.json</code> を編集します。全アカウント共通の設定です。</p>
      </div>

      <div class="grid-2">
        <div>
          <label class="label" for="set-posts">1日の投稿数（時間枠の数）</label>
          <input id="set-posts" type="number" min="1" max="20" class="input" value="${escapeHtml(s.posts_per_day)}" />
        </div>
        <div>
          <label class="label" for="set-exclusion">重複除外の日数</label>
          <input id="set-exclusion" type="number" min="0" max="365" class="input" value="${escapeHtml(s.duplicate_exclusion_days)}" />
          <p class="hint">この日数以内に紹介した商品は再度選ばれません。</p>
        </div>
        <div>
          <label class="label" for="set-start">活動開始（JST）</label>
          <input id="set-start" class="input mono" value="${escapeHtml(s.active_hours?.start)}" placeholder="07:00" />
        </div>
        <div>
          <label class="label" for="set-end">活動終了（JST）</label>
          <input id="set-end" class="input mono" value="${escapeHtml(s.active_hours?.end)}" placeholder="23:00" />
        </div>
        <div>
          <label class="label" for="set-jitter-min">投稿時刻のゆらぎ 最小（分）</label>
          <input id="set-jitter-min" type="number" min="0" max="120" class="input" value="${escapeHtml(s.jitter_minutes?.min)}" />
        </div>
        <div>
          <label class="label" for="set-jitter-max">投稿時刻のゆらぎ 最大（分）</label>
          <input id="set-jitter-max" type="number" min="0" max="120" class="input" value="${escapeHtml(s.jitter_minutes?.max)}" />
        </div>
        <div>
          <label class="label" for="set-hits">楽天の取得件数</label>
          <input id="set-hits" type="number" min="10" max="300" class="input" value="${escapeHtml(s.rakuten?.fetch_hits)}" />
          <p class="hint">多いほど重複を避けやすくなります（既定 50）。</p>
        </div>
        <div>
          <label class="label" for="set-model">Claudeのモデル</label>
          <input id="set-model" class="input mono" value="${escapeHtml(s.claude?.model)}" />
        </div>
        <div>
          <label class="label" for="set-pr">PR表記</label>
          <input id="set-pr" class="input" value="${escapeHtml(s.pr_text)}" />
        </div>
        <div>
          <label class="label" for="set-window-after">配信の取りこぼし許容（分）</label>
          <input id="set-window-after" type="number" min="0" max="720" class="input" value="${escapeHtml(s.publisher?.window_after_minutes)}" />
          <p class="hint">cronの遅延で送れなかった投稿を、何分前まで遡って送るか。</p>
        </div>
      </div>

      <div>
        <label class="label" for="set-golden">ゴールデンタイム（1行1区間、<code class="code">開始-終了</code>）</label>
        <textarea id="set-golden" class="input mono" rows="3">${escapeHtml(golden)}</textarea>
        <p class="hint">売れ筋ランキング上位の商品がこの時間帯へ優先的に割り当てられます。</p>
      </div>

      <button id="save-settings" class="btn-primary full w-auto-sm">保存してコミット</button>
    </div>
  `;

  $("#save-settings").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const ranges = $("#set-golden").value.split("\n")
      .map((line) => line.trim()).filter((line) => line.includes("-"))
      .map((line) => {
        const [low, high] = line.split("-");
        return [low.trim(), high.trim()];
      });

    const updated = mergeDeep(state.settings, {
      posts_per_day: Number($("#set-posts").value) || 7,
      duplicate_exclusion_days: Number($("#set-exclusion").value) || 0,
      active_hours: { start: $("#set-start").value.trim(), end: $("#set-end").value.trim() },
      jitter_minutes: { min: Number($("#set-jitter-min").value) || 0, max: Number($("#set-jitter-max").value) || 0 },
      golden_time_ranges: ranges,
      rakuten: { fetch_hits: Number($("#set-hits").value) || 50 },
      claude: { model: $("#set-model").value.trim() },
      publisher: { window_after_minutes: Number($("#set-window-after").value) || 0 },
      pr_text: $("#set-pr").value,
    });

    button.disabled = true;
    button.textContent = "保存中...";
    try {
      await putJson(PATHS.settings, updated, "chore(settings): update from admin UI");
      state.settings = updated;
      toast("保存してGitHubへコミットしました", "success");
    } catch (error) {
      toast(`保存に失敗しました: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存してコミット";
    }
  });
}

// ======================================================================
// プロンプト
// ======================================================================
function renderPrompt(root) {
  root.innerHTML = `
    <div class="card stack-sm">
      <div>
        <h2 class="h1">プロンプト</h2>
        <p class="tiny sub"><code class="code">${escapeHtml(PATHS.prompt)}</code> を編集します。</p>
      </div>

      <div class="alert-warn">
        <code class="code">{ジャンル}</code> <code class="code">{ターゲットの悩み}</code>
        <code class="code">{商品名}</code> <code class="code">{口調}</code> が実際の値へ置換されます。<br />
        Claudeには複数パターンを出力させ、各パターンの末尾に
        <code class="code">伸びる確率：〇〇％</code> を書かせてください。最も高いパターンの本文だけが投稿されます。<br />
        空のまま保存すると、組み込みの既定プロンプトが使われます。
      </div>

      <textarea id="prompt-text" class="input mono" rows="20">${escapeHtml(state.prompt)}</textarea>

      <div class="row gap-xs">
        <button id="save-prompt" class="btn-primary">保存してコミット</button>
        <button id="reset-prompt" class="btn-ghost">編集を取り消す</button>
      </div>
    </div>
  `;

  $("#reset-prompt").addEventListener("click", () => {
    $("#prompt-text").value = state.prompt;
  });

  $("#save-prompt").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const text = $("#prompt-text").value;
    button.disabled = true;
    button.textContent = "保存中...";
    try {
      await putFile(PATHS.prompt, text, "chore(prompt): update from admin UI");
      state.prompt = text;
      toast("保存してGitHubへコミットしました", "success");
    } catch (error) {
      toast(`保存に失敗しました: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存してコミット";
    }
  });
}

// ======================================================================
// 起動
// ======================================================================
function setupChrome() {
  $$("#tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.view = tab.dataset.view;
      render();
    });
  });

  $("#reload-btn").addEventListener("click", reloadAll);
  $("#logout-btn").addEventListener("click", () => {
    if (!confirm("保存されたトークンを削除して切断します。よろしいですか？")) return;
    clearAuth();
    location.reload();
  });
}

async function main() {
  // 暗号化ライブラリはリポジトリに同梱しているが、念のため状態を見ておく
  if (typeof window.sodium === "undefined") {
    $("#sodium-error")?.classList.remove("hidden");
  }

  setupLogin();
  setupChrome();
  setupAccountModal();
  setupWizard();

  const saved = loadAuth();
  if (saved?.token && saved.owner && saved.repo) {
    try {
      await verifyAndStart({ branch: "main", ...saved });
      // 認可画面から戻ってきた場合はここで連携を仕上げる
      await handleOAuthCallback();
      return;
    } catch {
      clearAuth();
      toast("保存されていたトークンが使えませんでした。再度接続してください。", "error");
    }
  }
  $("#login-screen").classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", main);
