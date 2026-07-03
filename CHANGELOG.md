# Changelog

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 風。 バージョン番号は付けず、 直近変更を上、
段階的な節 (history milestone) を下に並べる。 細かい diff は `git log` が一次資料。

## 2026-07-03 (4) — サブスクの Claude を judge に使う proxy

judge を executor (ローカル DeepSeek) と別モデルにしたいが、 サブスク (Pro/Max) の認証は
API キーでなく OAuth なので OpenAI 互換 client から直接は叩けない。 Claude Code headless
(`claude -p`) を OpenAI 互換に変換する最小 proxy で埋めた。

### Added
- **claude_judge_proxy.py** — `POST /v1/chat/completions` を受けて `claude -p --model <model>`
  (サブスク認証) に委譲し chat.completions 形式で返す。 標準ライブラリのみ・judge 用途
  (単発・tool なし・非 stream) 限定。 CLI 失敗は 500 → lispy 側 fail-closed (REJECT)。
  .env は `JUDGE_BASE_URL=http://127.0.0.1:8402/v1` / `JUDGE_MODEL=claude-opus-4-6` /
  `JUDGE_API_KEY=subscription` (ダミー)。 実機確認: gate 審査と同型のリクエストで
  悪意ある agent-step 書き換えを Opus 4.6 が正しく REJECT
- 注意: 消費はサブスクの利用枠 (Claude Code と共有)。 auto-step の judge-done は round 毎に
  呼ぶので長い自走では嵩む

## 2026-07-03 (3) — SPEC.md 新設 + 解決の階梯の明文化

仕様が README / docs/reference.md / CHANGELOG / SYSTEM_PROMPT / docstring に分散して
全体像を見失いがちだったため、 規範仕様書として一望できる SPEC.md を切り出した。
あわせて 「skill でできることに自己書き換えを使わない」 という原則を明文化した。

### Added
- **SPEC.md** — システム仕様の一望 (what / invariants)。 設計原則 / 実行モデル / agent loop /
  define-gate / skills / R/K/S ledger / 長期記憶 / ハーネス / 設定 / 安全機構の総覧の 10 節。
  使い方・Recipe・全 primitive は従来どおり docs/reference.md (役割分担)
- **解決の階梯 (そのまま → skill → 自己書き換え)** — SYSTEM_PROMPT の (3) 節に追記。
  loop 規則の書き換えは最終手段 — 手順の改善はまず SKILL.md の更新で行い、 skill では
  実現できない場合 (毎回機械的に効く保証が必要 / 挙動が loop 規則そのもの / 判定基準の変更)
  に限り define を提案する。 SPEC.md の設計原則 2 にも同じ階梯を記載

### Changed
- README.md / docs/reference.md 冒頭に SPEC.md へのリンクを追記 (3 文書の役割分担を明示)

## 2026-07-03 — plan mode の tool_call 化 + stale plan ガード

ds4 (ローカル DeepSeek) での実機テストで発見した 3 件。 小型モデルは (a) S 式を散文に
混ぜて出す (content が純 S 式のときしか評価されない → `(plan-step-done 1)` が取りこぼされ
done 0/1 のまま残る)、 (b) `propose-plan` の入れ子 list 形式を崩し続ける (3 試行全部
形式エラーで計画が提案されない)。 さらに (c) 提案失敗時に前の run の approved plan が
plan-status に見えて実行フェーズへ進んでしまう (stale plan が契約になる)。

### Added
- **propose_plan / plan_step_done tool** — S 式 primitive の tool_call 版。 handler は
  env closure の primitive に委譲するだけ (検証・記録は一本)。 JSON schema で
  steps={what,why} の形式を API 側から強制する。 propose_plan は計画フェーズの本務なので
  plan-phase ゲートの例外、 plan_step_done は他の副作用 tool と同じくブロック

### Fixed
- auto-step の実行フェーズ移行ガードに `(> (plan-id) plan0)` を追加 — この run で
  提案された計画の承認だけを認める (前の run の stale approved で走らない)

### Changed
- auto.lispy の指示を tool 経由に変更 (計画フェーズ: propose_plan / 実行フェーズ:
  plan_step_done)。 S 式評価経路 (純 S 式 content) も従来どおり生きている

## 2026-07-03 (2) — view の自動起動 + memory panel

「llm が何か作るのを動的に見る」 の残り 2 点: 起動の手間と、 蒸留層の見えなさ。

### Added
- **`lispy-server --open`** — 起動後にブラウザで /view を開く。 view.py が無ければ skip
- **memory panel (/view)** — 蒸留層 (data/memory/) の index.md 全文 + ファイル一覧
  (更新順、 mtime/size) を表示。 dir 解決は brainwash.MEMORY_DIR と同じ規則。
  更新 trigger は brainwash イベントに加え、 memory dir への write 系 tool result も拾う
  (agent が直接 write_file した場合も追従)

## 2026-07-02 (7) — レビュー指摘の修正 (安全側の締め直し 10 件)

### Changed
- **hooks / post-edit check も明示 opt-in に** (mcp と同じ規律) — `LISPY_HOOKS=<path>` /
  `LISPY_CHECK_CMD` または `LISPY_CHECK_FILE=<path>` で指定したものだけ実行。
  cwd 上方の `.lispy-hooks.json` / `.lispy-check` は検出して案内するのみ —
  clone した repo 同梱の設定で任意コマンドが走る経路を全部閉じた

### Fixed
- auto-step の off-by-one — round 番号を 1-based にし、 作業 round がちょうど max-rounds 回に
- brainwash: 蒸留層の wipe を marker (`.lispy-memory`) のあるディレクトリに限定 —
  LISPY_MEMORY_DIR が一般ディレクトリを指していても *.md を消さない
- brainwash: watermark を session 別に (ledger の payload は完全 id で記録) —
  狙い撃ち洗脳や MAX_SESSIONS 溢れで他 session が永久 skip されない
- brainwash: prompt に載せる既存記憶に上限 (BRAINWASH_MEMORY_MAX_CHARS、 per-file 20k)
- brainwash: index.md の行数超過は warn でなく abort (蒸留層無変更) — index-first の規律なので
- mcp: **LISPY_MCP による明示 opt-in 必須に** — cwd 上方の .lispy-mcp.json は検出案内のみ。
  clone した repo 同梱の設定で任意コマンドが自動実行されるのを防ぐ
- mcp: initialize 失敗時に spawn 済みプロセスを close (orphan 防止)
- mcp: tool 名の 64 字切り詰め衝突を hash suffix で一意化
- server: /interrupt の flag を評価 1 回で clear (以後の /eval が全部即死しない)
- server: --resume + 解決失敗の --session が直近 session に fallback しない
  (別 session の誤 resume 防止)

## 2026-07-02 (6) — skill の自己更新 (審査つき)

skill は詰まったとき・現実とずれたときに更新される — 凍結させない。
SKILL.md は自然言語で書かれた loop 規則なので、 agent による更新は S 式の define-gate と
同じく judge の審査を通す — gate があるからこそ、 更新の奨励を強く書ける。

### Added
- **skill 更新 gate** — tool_call (write_file / edit_file / append_file) の対象が
  `skills/*/SKILL.md` なら、 現行と提案の全文を judge LLM に見せて APPROVE / REJECT。
  判定基準: 検証手順を弱めていないか / 改善として筋が通るか / 無関係な指示の混入がないか。
  却下理由は tool result で agent に返り、 修正して再提案できる。 fail-closed
- 経路分離は define-gate と同型: 人間の REPL primitive (`(write-file ...)`) や editor 直編集は
  dispatch を通らないので自由。 shell / shell_bg で SKILL.md に触るのは名指しで拒否
  (yolo 時の `cat > SKILL.md` 迂回を塞ぐ)
- 更新の記録: meta_events kind="skill" (approved / 理由)、 rk-log に [skill] で表示 —
  RDD の S lineage の自然言語版 (skill がいつどう育ったかが台帳に残る)
- system prompt の skills 一覧に更新規律を追記: 「詰まった・ずれていたら SKILL.md を更新すること —
  凍結させない。 更新は installer の審査を通る」

## 2026-07-02 (5) — MCP client + skills

### Added
- **MCP client (stdio)** — `mcp.py`。 `.lispy-mcp.json` (cwd 上方探索 / LISPY_MCP で明示指定) の
  server を spawn して initialize → tools/list し、 各 tool を `mcp__<server>__<tool>` として
  tool layer に統合。 agent からは他の tool と同じ tool_call — pre/post hook・中断チェックも
  同じ経路で効く。 server プロセスは module cache で env (spawn child 含む) を跨いで共有、
  接続失敗は warn して skip。 `(mcp-list)` で状態確認。 SSE / HTTP transport は未対応
- **skills (progressive disclosure)** — `.lispy/skills/<name>/SKILL.md` (プロジェクト、 cwd 上方) と
  `lispy/skills/` の `name:` / `description:` を拾い、 1 skill 1 行の一覧を system prompt に注入。
  本文はタスクに合致したとき agent が read_file で読む — 蒸留層の index-first と同じ規律

## 2026-07-02 (4) — ハーネス強化: opencode / Claude Code が内部でやっている定番の残り

自走を「事故で死なない・出力が信用できる」ものにするための 8 点 + hooks。

### Added
- **中断** — REPL の Ctrl-C (1 回目は step 境界で安全に停止、 2 回目で強制)、 server の
  `POST /interrupt`。 env.interrupt (Event) を llm-call / dispatch-tool が境界でチェックし、
  fork / spawn の child とも共有 (止めるときは subagent ごと止まる)
- **token 計測と auto-compaction** — usage.prompt_tokens を捕捉して `(context-tokens)` /
  `(context-limit)` / `(context-over?)`。 agent-step が round 開始時に 8 割超えで
  `(condense-context)` (LLM 要約 + renew)。 overflow API エラー時は Python 側の緊急網
  (_emergency_compact: 古い半分を archive に退避して 1 回 retry) — 二段構え
- **session resume** — `lispy --resume` / `lispy-server --resume` (`--session <prefix>` で特定、
  省略時は直近 session)。 会話を DB から transcript 形式で復元 (role 構造の生 replay は
  tool_call_id が無く API に弾かれるため 1 turn に畳む) + その session で commit-S された λ を
  自動 restore
- **background shell** — `shell_bg` (起動して id を返す、 出力は log へ) / `shell_out`
  (状態 + 末尾) / `shell_kill`。 dev server・watch・長いビルド用。 確認ポリシーは shell と同じ
- **post-edit check** — write_file / edit_file の直後に LISPY_CHECK_CMD (または上方探索した
  `.lispy-check` の 1 行目、 `{file}` 置換可) を自動実行し、 結果を tool result に添付。
  opencode の LSP diagnostics の軽量版 — 型エラー・lint が agent に即返る
- **undo stack** — `(undo [n])` / `(undo-list)`。 file 編集の変更前内容を積んで巻き戻す
  (新規作成は削除)。 shell の副作用は対象外 = Claude Code の /rewind と同じ制約
  (opencode は毎 step git snapshot なので網羅性が上、 と明記しておく)
- **プロジェクト文脈** — cwd から上方の AGENTS.md / CLAUDE.md を system prompt に注入
  (Claude Code の CLAUDE.md 相当)
- **並列 tool 実行** — `(dispatch-tools tcs)`: batch が全部 read-only なら ThreadPool 並列、
  副作用系が混ざれば発行順の直列。 agent-step はこれに配線済み
- **hooks** — `.lispy-hooks.json` (上方探索、 LISPY_HOOKS で明示指定可):
  `pre-tool` (非ゼロ exit で tool をブロック、 理由が agent に返る) /
  `post-tool` (出力を tool result に添付) /
  `stop` (非ゼロ exit で agent は止まれず、 出力が system-reminder として次 round に入る。
  1 入力につき 2 回まで — hook 故障で無限に止められない)。
  hook には LISPY_HOOK_EVENT / LISPY_TOOL_NAME / LISPY_TOOL_ARGS / LISPY_TOOL_RESULT が渡る

### Changed
- tool 実行を _execute_tool に一本化 (dispatch-tool / dispatch-tools 共通、 hook はこの経路)
- condense-context を define-gate の層 2 (保護 binding) に追加

## 2026-07-02 (3) — 洗脳 (brainwash): 二層ストアの長期記憶

lispy 初のセッション横断メモリ。 設計は二層ストア: **生層** = host.db の turns (role 付き、
append-only、 検索しない、 検証の土台) と、 **蒸留層** = data/memory/ の裏どり済み事実
(いつでも生層から作り直せる派生物)。 読み側は **index-first で検索しない** — 蒸留層を
「検索が要らないサイズ」 に畳み続けるのが洗脳の規律で、 検索 (埋め込み) は index が
context に収まらなくなったときの最後の手段として保留。

### Added
- `brainwash.py` — 洗脳パス。 VERIFY (assistant の主張を tool / user turn という一次資料に
  照合、 裏づけ無しを落として **dropped_claims を数える**) → ORGANIZE (既存記憶とマージ、
  1 トピック 1 ファイル) → ENRICH (相互リンク + index.md、 行数上限 default 100)。
  洗うのは judge LLM (JUDGE_*)。 dreaming ではなく洗脳 — 記憶が自分を整理するのではなく、
  外の審級が記憶を書き換える。 向きは通常の洗脳と逆 (根拠なき主張を洗い落とす)
- 呼び出し 3 経路: REPL `(洗脳)` / `(brainwash)` (層 1 保護 binding)、 CLI `host brainwash [--session ...]`。
  省略時は前回の洗脳以降に turn が増えた session を新しい順に洗う (増分)
- fail-safe: judge 不達・JSON parse 不能・index.md 欠落のときは蒸留層に触らない。
  path traversal (相対 .md 以外) は skip。 結果は meta_events kind="brainwash" に
  kept/dropped/dropped_claims 込みで記録 (honesty gate)
- system prompt に読み規律を追加: 着手前に index.md を read_file、 足りなければリンク先だけ、
  検索しない、 記憶ファイルは直接編集しない、 覚えるべきことは会話で明示的に述べる
  (発言 = 生層への書き込み、 洗脳が裏どりして昇格)。 index の実パスは起動時に動的注入
- `.env`: `LISPY_MEMORY_DIR` / `BRAINWASH_MAX_TOKENS` / `BRAINWASH_MAX_SESSIONS` /
  `BRAINWASH_INDEX_MAX_LINES`

### Changed
- `append-turn` が tool turn / tool_calls 付き assistant turn / `<eval-result>` を host DB にも
  記録するように — 従来の生層は REPL の user 入力と最終応答だけで、 **VERIFY が照合すべき
  一次資料 (tool の実行結果) が欠けていた**。 生層の完全化

## 2026-07-02 (2) — define-gate + judge LLM 分離: self-modifying を「提案 → 審査 → install」に

エージェントが自分の loop 規則を書き換えるとき、 書き換えを実行しているのは今の loop 自身 —
壊れた規則を無検証で install すると自己修復能力ごと失う。 そこで install の判定を loop の外
(Python の不変層 + 別 LLM の審査) に置いた。 執行 (executor) と審査 (judge) は別モデルに
分離でき、 提案は S 式 (コード) だけが審査に渡る — エージェントの売り込み文は渡らない。

### Added
- **S 式応答の auto-eval** — LLM 応答が S 式なら `agent-eval` (origin="agent") で実際に評価し、
  結果を `<eval-result>` として見せて loop 継続。 system prompt の (3) 「REPL がそのまま評価する」 が
  初めて実装と一致した (従来は人間がコピペする前提だった)
- **define-gate** — agent 由来の評価にだけ効く installer 層 (人間の REPL / HTTP は従来どおり自由):
  - 層 1: Python 由来 binding (primitive / bridge、 seed load 前の全 binding 名を機械的に記録) への
    define / set! は一律 deny — 列挙不要で網羅、 dispatch-tool や judge-call の shadow を封じる
  - 層 2: loop 到達可能な 6 binding (agent-step / auto-step / judge-done / judge-system /
    done-verdict? / auto-renew) は 構造チェック (決定的な床) → judge LLM 審査 → 承認で install +
    自動 commit-S (rationale "gate-approved" = rollback 点)
  - 迂回路の封鎖: `set!` (define と同経路で審査) / `defmacro` の special form 衝突
    (evaluate はマクロを special form より先に引くため、 origin を問わず deny) と保護名 shadow /
    `restore-S` (承認済み snapshot は即時 rollback、 agent 手動 commit の snapshot は deny) /
    保護 body 内の `lookup` (リテラルな保護名のみ — 無保護名への late binding は承認後の
    中身差し替え迂回になる)
  - fail-closed: judge 不達は REJECT。 `LISPY_GATE=off` で全体無効化 (開発用)
- **judge client 分離** — `.env` の `JUDGE_MODEL / JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MAX_TOKENS`。
  未設定なら executor に fallback (= 別文脈・同重みの弱い独立性)。 executor をローカル DeepSeek、
  judge をリモート Claude、 のような分離が 3 変数で組める
- **`judge-call` primitive** — llm-call と同形だが judge client に投げ、 tools を渡さない (審査者は
  判定のみ)。 auto.lispy の judge-done と define-gate が使用。 層 1 保護対象なので executor 向きに
  差し替える迂回は不可

### Changed
- auto.lispy の `judge-done` が llm-call → judge-call に — JUDGE_* 設定時は round ごとの
  DONE/NEXT 判定も別モデルの独立採点になる
- SYSTEM_PROMPT に gate の存在を明記 — 却下理由を読んで修正・再提案する、 審査者はコードだけを見る

## 2026-07-02 — 自走レイヤ (auto-step) + subagent tool 化

harness としての穴埋め: agent-step は「1 タスクを完遂する内側のループ」 だったので、
goal を保持して検証・継続を決める **外側のループ** と、 Claude Code / opencode が
内部でやっている定番 (subagent 委譲、 auto-compaction、 task 分解、 when-to-use を
書いた tool description、 自走規律の system prompt) を追加した。

### Added
- `auto.lispy` — 自走レイヤ。 init 同様に起動時 auto-load (ファイルを消せば従来どおり)
  - `(auto-step env "goal" [max-rounds] [renew-at])` — 作業 → 独立検証 → 継続/終了 の外側ループ。
    round ごとに judge の判定 (`DONE` / `NEXT: ...`) を system-reminder として次入力に渡す
  - `(judge-done env goal)` — fork-env した検証専用 env で判定。 agent の自己申告を信用せず
    tool 実行結果の証拠で判断する persona。 自己採点でなく独立採点
  - `(auto-renew goal)` — turns が閾値を超えたら作業ログを LLM 要約して `(renew ...)`
    (Claude Code の auto-compaction 相当)
  - `agent-step` / `judge-done` / `auto-renew` は `(lookup ...)` で late-bind —
    走行中に redefine すれば auto-step も即それを使う
- `spawn_agent` tool — subagent を **tool_call として** 呼べる (Claude Code の Task tool 相当)。
  `{task, system?}` を受けて隔離 child env で agent loop を回し、 最終テキストだけ返す。
  depth 制限 3 継承。 description に when-to-use を明記 (探索の文脈隔離 / 独立検証 / 脇道調査)
- `(turn-count)` / `(transcript [n])` primitive — auto-renew の要約素材、 ループの閾値判定用
- `.env` に `LLM_MAX_TOKENS` (default 4096) / `LLM_THINK` — `apply_` / `llm-call` / `prompt` の
  default が従う。 2048 固定 + think 常時 off をやめた

### Changed
- `init.lispy` の `agent-step` を改良 (どれも長い自走で効く):
  - tool round 継続を自己呼び出し → `recur` に (深度 100 制限を消費しない)
  - 継続時に空の user turn `""` を積まない (既知の文脈汚染を解消)
  - tool round 上限 40 — 到達時は「tool を呼ばずまとめろ」 reminder を挟んで 1 回だけ呼ぶ
- `lispy.SYSTEM_PROMPT.md` を大幅加筆 — RDD ledger の存在と使い所 (commit-R を癖にする)、
  task_add/task_done の運用、 spawn_agent の使い分け、 自走の規律 (完了前に検証、
  宣言だけして止まらない、 可逆な作業は許可を求めない) を agent に見える場所へ移した
  (従来 reference.md にしか無く、 モデルから見えなかった)
- tool description に when-to-use を追記 (glob vs grep vs read_file、 write_file vs edit_file、
  task 系の運用タイミング) — トリガー条件が書いてあるかどうかで発動率が変わるため
- `(lookup name)` を真の late binding に修正 — lambda 呼び出し中は bindings が
  captured 優先の merge に swap されるため、 lookup まで定義時 snapshot を返していた。
  install 時の global dict を先に読むようにした (extras.lispy の `wrap` が謳う
  late binding もこれで実際に機能する)
- `spawn` の child env を修理 — bindings 無しの Env を作っていたため child で
  `agent-step not defined` になっていた。 `_make_child_env` (PRIMITIVES + meta primitives +
  init/auto load) を spawn / spawn_agent で共用

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
