# Changelog

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 風。 バージョン番号は付けず、 直近変更を上、
段階的な節 (history milestone) を下に並べる。 細かい diff は `git log` が一次資料。

## Unreleased

### Added
- `server.py` — lispy を HTTP で叩ける常駐プロセス。 env をプロセス内に持ち、 Claude / nl REPL / 別 bash から **同じ env を共有** できる
  - endpoints: `GET /` (healthz) / `GET /bindings` / `GET /recall?q=` / `POST /eval` / `POST /load` / `POST /reset`
  - CLI: `--host` `--port` `--yolo` `--session <id>` (既存 session に append) `--stdin` (server と並列の REPL)
  - evaluation は `threading.Lock` で serialize、 `(print ...)` の stdout は HTTP response の `stdout` field に捕捉
- `lispy.build_default_env(record, sid)` に `sid` 引数を追加 — server の `--session` 引き継ぎ用。 既存 session に turns を append できる (bindings 復元は無し)
- README に `server` セクション (起動方法 / endpoint 一覧 / 例 / 仕様メモ) を追記
- `CHANGELOG.md` を新規作成 (このファイル)

## 2026-05-21 — nl REPL + ds4 default + auth 整備

### Added
- `nl.py` — 日本語 → S 式 翻訳の REPL (lispy sidecar)。 入力は `(`/`!`/`@`/`"""`/平文 で分岐
  - degenerate loop 防御として `max_tokens=2048` を強制
  - `lispy.eval_` への直接ルート (`@文`)、 NL 翻訳ルート、 S 式直叩きを 1 つの REPL に統合
- `nl.SYSTEM_PROMPT.md` (翻訳器 addendum)、 `lispy.SYSTEM_PROMPT.md` (共有 system prompt)

### Changed
- `.env.example` を ds4 default に並べ替え — ローカル ds4 (DeepSeek V4 Flash) を先頭でアクティブ化、 他プロバイダはコメント選択肢として残す
- README を現状に同期 — macro / try / box / recur / fork-env / shell / yolo / `.env` 必須

### Removed
- `host.py` から LLM 設定の default 値 (`anthropic/claude-opus-4.7`, openrouter URL 等) を除去 — provider 選択は config (`.env`) の責務

## 2026-05-21 — lispy core 確立

### Added
- `lispy.py` — evaluator 本体: macro (`defmacro` + quasiquote) / try-catch / box / recur (明示 TCO) / set! / `&rest` / 短絡 `and`/`or` / 型述語
- `edit.py` — 副作用 tool (`shell` / `write-file` / `edit-file` / `append-file`) を allow-list + y/N 確認で
- `init.lispy` — 起動時 auto-load される seed (`agent-step` / `compose` の元定義)
- `extras.lispy` — list ops / numeric / combinator / control / robust / agent idiom の派生定義集
- `fork-env` (env を first-class に複製) / logprob 観測 / `match` macro (extras)
- `--yolo` フラグ + `(set-yolo #t/#f)` で session 中切替

### Changed
- shell tool の metacharacter 検出 (`;` `&&` `|` backtick `$(` `>` `<`) で allow-list bypass を強制 confirm に倒す
