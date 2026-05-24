# Changelog

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 風。 バージョン番号は付けず、 直近変更を上、
段階的な節 (history milestone) を下に並べる。 細かい diff は `git log` が一次資料。

## 2026-05-24 — RDD (Requirement-Don't-Die) と server.py 主経路化

**RDD = Requirement-Don't-Die** — lispy の SDD 方式の呼称を導入。 R は append-only、
`'replaces` で lineage は動くが原本は消えない。 SDD の形式張った workflow との対比で、
RDD は opportunistic / LLM-callable / ledger-resident。

### Added
- `commit-R` に LLM auto-judge (a/b/c) — RDD curator
  - `'replaces N` 無しで `commit-R` を打つと、 LLM が現 session の既存 R 群と照合して
    a (= 無関係追加) / b (= refines R#N) / c (= contradicts R#N) を判定
  - payload に `@judge=*` / `@judge-target=N` / `@judge-reason` / `@judge-impact` を残す
  - `@replaces=` は自動付与しない (= advisory only; ledger は append-only 維持、
    上流 LLM が同意するなら次 turn で explicit `'replaces` 付きで commit し直す)
  - LLM 失敗時は `@judge-error` を残して通常 commit (= 致命傷にならない)
- `lispy-server` を `pyproject.toml` の CLI entry に昇格 — `server.py --stdin` を主経路化
  - `lispy-server --stdin --yolo` で REPL + HTTP server + `/spec` (R/K/S/artifact 1 枚 HTML) が同時に立つ

### Changed
- `README.md` を `docs/reference.md` に rename — ルートを軽くする (新 README は workflow が固まってから書く)

## 2026-05-22 — R/K event ledger + ds4 拡張

### Added
- R/K event ledger の primitive 12 個 — session を跨ぐ要求 / 知識 / 仕様の append-only 記録
  - 刻む: `session-intent` / `commit-R` (`'replaces N` で lineage) / `commit-K` / `commit-S` / `commit-artifact`
  - 観測: `rk-log` (intent/R/K/S/artifact/replay/test-S-R/restore-S を時系列 + lineage 付き)
  - lineage / 復元 / 比較: `S-history` / `restore-S` (id 省略で最新、 lisp/llm 両 kind) / `diff-S` (unified diff) / `diff-K` (env 比較)
  - 動的 check: `test-S-against-R` (LLM に R 群と S の整合性を判定させ ledger に記録) / `replay-with-K` (過去 turn を 現 K で再評価)
- `'extra` plist passthrough を `llm-call` の option に追加 — `(llm-call env 'extra (list 'foo 1 'bar 2))` で OpenAI SDK の `extra_body=` に流す。 kebab-case → snake_case 自動変換、 provider 固有 field を lispy.py 改変なしで通せる
- `ds4.lispy` — ds4-server 接続時のみ load する派生 idiom 集
  - thinking mode 制御: `think-on` / `think-off` / `(think-effort "max")`
  - directional steering: `steering` / `steering+` / `steer-call` / `steer-sweep` / `steer-debate` / `steer-entropy-curve`
- README に 2 つの大セクション追加 — `ds4-server 接続時の拡張` (言語拡張 配下) と `R/K event ledger — 終わらない実装、 でも区切りはある`
- REPL help に R/K event / S lineage / `'extra` plist の行追加

### Removed
- `nl.py` (309 行) / `nl.SYSTEM_PROMPT.md` (39 行) — 日本語 → S 式 翻訳 REPL の役割が main REPL + agent-step + `(set-mode <translator>)` に吸収。 翻訳機能を復活させたい場合は 15 行の λ + `set-mode` で `.lispy` ファイル化可能 (README の言語拡張 / R/K event ledger 参照)
- README の `## nl: 日本語 → S 式 翻訳 REPL` セクション全体

## 2026-05-21 — server (HTTP daemon)

### Added
- `server.py` — lispy を HTTP で叩ける常駐プロセス。 env をプロセス内に持ち、 Claude / 別 REPL / 別 bash から **同じ env を共有** できる
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
