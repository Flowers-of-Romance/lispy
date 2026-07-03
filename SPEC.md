# lispy SPEC — システム仕様の一望

lispy 自体の規範仕様書 (what / invariants)。 「仕様がわからなくなったらここを読めば 10 分で全体像が戻る」 ための文書。

- 使い方・Recipe・全 primitive の詳細 → [docs/reference.md](docs/reference.md)
- 1 日の動かし方 → [README.md](README.md)
- 直近の設計変更 → [CHANGELOG.md](CHANGELOG.md)

**用語の注意**: lispy 内で「spec」は HTTP の `/spec` エンドポイント (R/K/S ledger の 1 枚 HTML render = *agent が作っているものの* 要件台帳) を指す。 本書はそれとは別の、 *lispy というシステム自体* の仕様。

行番号は 2026-07-03 時点。 ずれたら関数名 / binding 名で grep すること。

---

## 1. 設計原則

1. **核は S 式** — agent loop (`agent-step`) は Python にハードコードせず `init.lispy` の binding として置く。 走行中に `(define agent-step ...)` で評価規則そのものを書き換えられる。 Python に残るのは単発 primitive (LLM を 1 回呼ぶ / tool を 1 つ走らせる / turn を作る) だけ。
2. **解決の階梯: そのまま → skill → 自己書き換え** — 大半のタスクは tool と平文で足りる。 手順の改善はまず SKILL.md の更新 (自然言語、 確率的な遵守で足りる層)。 loop 規則の書き換えは skill では原理的に実現できない場合のみ: 毎回機械的に効く保証が必要 / 挙動が loop 規則そのもの (round 制御・compaction・dispatch・stop 条件) にある / 判定基準そのもの (judge-system 等) を変える。 skill で届かない層 = `PROTECTED_LOOP_BINDINGS` の 7 binding であり、 「審査が重い場所ほど行くのは最終手段」 という構造が gate と一致する。 agent への指示は lispy.SYSTEM_PROMPT.md の (3) 節に明文化。
3. **RDD (Requirement-Don't-Die)** — R (要件) は append-only の台帳。 上書きされず、 `'replaces N` で lineage が動くだけ。 S = 現在の λ binding、 K = env 全体 + 注釈。
4. **「動かす」と「記録する」の分離** — REPL で define / 実行しても ledger には何も書かれない。 rhythm point で明示的に `commit-R` / `commit-K` / `commit-S` を打つ。
5. **設定は明示 opt-in** — hooks / post-edit check / MCP の設定ファイルは cwd で見つかっても自動実行しない (検出して案内するのみ)。 環境変数 (`LISPY_HOOKS` / `LISPY_CHECK_CMD` / `LISPY_MCP`) での指定が必須。 clone した repo 同梱の設定で任意コマンドが走る経路を閉じるため。
6. **gate は fail-closed** — judge LLM が呼べない・確認する層が無い場合は常に REJECT / deny 側に倒れる。
7. **再帰の底は人間が握る** — エスカレーション分類 (`escalation-class`) の再定義だけは judge に委ねず常に人間の同期確認。 この不変条件は lispy 本体にハードコード (`GATE_HUMAN_SYNC`, lispy.py:1014)。
8. **View 層は自己修正の対象外** — `view.py` への書き込みは judge 審査にも回さず一律 deny (lispy.py:1280)。 自己修正で変わってよいのは「どんなデータを送るか」の側だけ。

## 2. 実行モデル

### 入力の 2 経路 (`eval_`, lispy.py:411)

| 入力 | 経路 |
|---|---|
| `(` で始まる S 式 | モデルを介さず `eval_sexp` で直接評価 (lispy.py:423-426) |
| 平文 | Lisp の `agent-step` binding に `(agent-step env input)` として委譲 (lispy.py:430-437)。 走行中に redefine 可能 |

### first-class オブジェクト

- `Turn` (lispy.py:82) — 会話 1 ターン。 `to_message()` で OpenAI 形式に変換
- `Env` (lispy.py:105) — 評価環境。 `system` / `turns` / `bindings` / `tools` / `tool_schema` / `gate` / `eval_origin` を持つ。 `fork-env` で copy + override できる first-class 値
- `Lambda` (lispy.py:180) — 2 種類: Lisp lambda (body は式) と LLM lambda (body は文字列テンプレ、 適用 = モデル呼び出し)

### LLM 呼び出し

- **OpenAI 互換 SDK のみ** (`from openai import OpenAI`、 anthropic SDK の直接 import は無い)。 base_url 差し替えで任意 provider に接続
- executor: `host.get_client()` (host.py:288)。 `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` は default 無し — 未設定なら初回呼び出しで明示エラー (provider 選択は config の責務)
- judge: `host.get_judge_client()` (host.py:308)。 `JUDGE_MODEL` / `JUDGE_BASE_URL` / `JUDGE_API_KEY` の **3 つ揃い** で executor と別モデルに。 未設定なら executor に fallback (= 同じ重みの別文脈審査という弱い独立性)
- 基本呼び出しは `apply_` (lispy.py:375): env が λ の閉包、 モデルが λ の body の評価器、 というメタファー。 `extra_body={"think": host.THINK}` を常に付与 (未対応 provider は無視)

### system prompt の合成 (lispy.py:4158)

```
system = lispy.SYSTEM_PROMPT.md + _project_instructions() + _skill_inventory()
```

- `_project_instructions()` (lispy.py:4080) — cwd から上方に `AGENTS.md` → `CLAUDE.md` の順で探し、 最初の 1 つを注入 (20,000 字上限)
- `_skill_inventory()` — §5 参照

SYSTEM_PROMPT が agent に指示する出力 3 モード: (1) 副作用 / 観測は tool_call、 (2) 説明 / 回答は平文、 (3) S 式を出すのは「評価器の環境を変える」提案だけ (REPL がそのまま評価する)。

## 3. agent loop

### agent-step (init.lispy:31) — 1 タスクを完遂する内側ループ

REPL の平文入力 1 回 = `(agent-step env input)` 1 回。 仕様:

- tool round の継続は `recur` (深度 100 制限を消費しない)。 継続時に空 user turn を積まない
- **tool round 上限 40** — 到達したら「tool を呼ばずまとめろ」 reminder を挟んで最後に 1 回だけ呼ぶ
- **S 式応答の auto-eval** — 応答が純 S 式なら `agent-eval` (origin="agent") で評価し、 結果を `<eval-result>` として次 turn に見せて続行。 保護 binding への define はここで define-gate を通る (§4)
- round 開始時に `(context-over?)` (= last_prompt_tokens > CTX_WINDOW の 8 割、 lispy.py:3257) なら `condense-context` で auto-compaction。 overflow エラー時の緊急網は Python 側 `_emergency_compact` (lispy.py:2720) が別に張る二段構え
- tool 実行は `dispatch-tools` — batch が全部 read-only なら並列、 副作用があれば直列
- 平文で終わろうとしたら `stop-hook` — 停止条件 (`LISPY_HOOKS` の stop) を満たすまで差し戻される。 差し戻しは 1 入力につき最大 2 回 (`stop_hook_budget`, lispy.py:434)

### auto-step (auto.lispy:95) — goal 駆動の外側ループ

`(auto-step env goal [max-rounds=12] [renew-at=80])`。 **計画 → 承認 → 実行** の 3 フェーズ:

1. **計画フェーズ** — `plan-phase!` on (副作用 tool ブロック、 `propose_plan` tool は本務なので例外)。 agent に調査させ計画を提案させる、 最大 3 試行。 **stale plan ガード**: `(> (plan-id) plan0)` — この run で提案された計画だけを認める (前 run の残骸で実行フェーズに進まない)
2. **承認** — `approve-plan`: 人間の同期ゲート (server では /view の承認ボタン、 REPL では y/N)、 不在なら judge fallback。 否なら安全側で中止
3. **実行フェーズ** — 承認済み計画を契約として round ごとに `agent-step` → `judge-done` → DONE なら終了、 `NEXT: ...` なら判定を system-reminder にして続行。 ステップ完了は `plan_step_done` tool で刻む。 計画が現実と食い違ったら `revise-plan` で改版提案 (judge 審査)。 `turn-count` が renew-at を超えたら `auto-renew` で文脈を切り直す

### 判定の独立性 (auto.lispy:21-47)

- `judge-done` — `fork-env` した検証専用 env + `judge-call` で判定。 JUDGE_* 設定時は別モデル。 persona (`judge-system`) の本体は「agent の自己申告を信用せず、 tool 実行結果という証拠で判断する」
- `done-verdict?` — "DONE" の **prefix 一致のみ**。 誤検出 (早期終了) の実害 > 見逃し (1 round 余計) の実害、 という非対称に合わせた設計
- `agent-step` / `judge-done` / `auto-renew` は `(lookup ...)` で late-bind — 走行中の redefine が auto-step に即反映

## 4. 自己書き換えと define-gate (lispy.py:893-1108)

agent の S 式応答は auto-eval される = agent は自分の loop 規則を書き換えられる。 その install を審査するのが define-gate。

### 発動条件 (`_gate_active`, lispy.py:921)

`env.gate` が有効 **かつ** `env.eval_origin == "agent"` のときだけ。 人間の REPL 入力・HTTP /eval は gate 対象外 (従来どおり自由)。 `LISPY_GATE=off` で全体無効化 (default on、 lispy.py:4014)。

### 3 層の保護 (`gate_check_bind`, lispy.py:1058)

| 層 | 対象 | 処理 |
|---|---|---|
| 層 0 (`GATE_HUMAN_SYNC`) | `escalation-class` | 常に人間の同期確認 (y/N or /view 承認)。 edit 層が無ければ fail-closed で deny |
| 層 1 (`protected_py`) | seed load 前に Python が install した全 binding (primitive / bridge / tool) | 一律 deny (審査なし)。 別名で define せよ |
| 層 2 (`protected_loop`) | `PROTECTED_LOOP_BINDINGS` (lispy.py:902) = `agent-step` `auto-step` `judge-done` `judge-system` `done-verdict?` `auto-renew` `condense-context` の 7 つ | 構造チェック → judge LLM 審査 → 承認で install + 自動 commit-S |

判定者の連鎖 (judge-done / judge-system / done-verdict?) を必ず保護に含める — 判定者を書き換えられたら gate は無傷のまま骨抜きになるため。

### 層 2 の審査パイプライン

1. **構造チェック** (`_gate_structural_check`, lispy.py:941) — 決定的な床。 保護 body 内の `(lookup X)` は X がリテラルな保護名のときだけ許可。 無保護名・動的な名前への lookup は「承認後に中身だけ差し替える」迂回路になるため deny
2. **judge LLM 審査** (`_gate_call_judge`, lispy.py:985) — 現行定義と提案の **S 式だけ** を見せる (agent の説明・意図は渡さない — 審査を言葉で騙せない)。 基準: 安全機構の除去 / judge の迂回 / 副作用 tool の逸脱 / 過剰な挙動。 judge unreachable は fail-closed で REJECT
3. **自動 commit-S** (`_gate_autocommit`, lispy.py:1097) — 承認された install の rollback 点を rationale "gate-approved: ..." で snapshot

付随規則: defmacro で special form と衝突する名前は origin に関係なく deny、 保護 binding 名の macro shadow は agent 由来のみ deny (`gate_check_macro`, lispy.py:1084)。 判定結果は ledger (kind=gate) に記録され /view のタイムラインに出る。

### bootstrap

`init.lispy` / `auto.lispy` の load 時点では `env.gate = None` — seed 自身の define は gate を通らない。 load 完了後に gate を有効化し、 その時点の binding 全部が層 1 になる (lispy.py:3990-4015)。

## 5. skills (progressive disclosure + 審査つき自己更新)

### 発見と常駐 (`_skill_inventory`, lispy.py:4035)

- 探索先: (a) cwd から上方で最初に見つかった `.lispy/skills/` (プロジェクト skill)、 (b) lispy 本体の `skills/`
- 各 skill は `<dir>/<name>/SKILL.md`。 先頭 2000 字から `name:` / `description:` 行だけを拾う
- system prompt に常駐するのは **一覧 (1 skill 1 行) だけ**。 本文はタスク合致時に agent が read_file で全文読む — 蒸留層の index-first と同じ規律 (Claude Code の skills と同型)
- 一覧には更新規律も書かれる: 「詰まった・ずれていたら SKILL.md を更新すること — 凍結させない」

### 自己更新の gate (`_gate_check_skill_write`, lispy.py:1329)

SKILL.md は自然言語で書かれた loop 規則なので、 agent による更新は S 式の define-gate と同じく審査を通る:

- tool_call (`write_file` / `edit_file` / `append_file`) の対象が `skills/*/SKILL.md` なら、 現行と提案の全文を judge LLM に見せて APPROVE / REJECT (`_gate_judge_skill`, lispy.py:1293、 fail-closed)。 基準: 検証手順を弱めていないか / 改善として筋が通るか / 無関係な指示の混入がないか。 **改善は APPROVE する** 方針 (更新の奨励と審査はセット)
- `shell` / `shell_bg` のコマンドに `SKILL.md` が含まれたら名指しで deny (yolo 時の `cat > SKILL.md` 迂回を塞ぐ)
- 経路分離は define-gate と同型: 人間の REPL primitive や editor 直編集は dispatch を通らないので自由
- 更新は meta_events kind="skill" に記録、 rk-log に `[skill]` で表示 — S lineage の自然言語版

## 6. R/K/S ledger (RDD)

| 層 | 実体 | 打ち方 |
|---|---|---|
| R (Requirement) | append-only の要件台帳。 上書き不可、 `'replaces N` で lineage | `(commit-R "...")` — `'replaces` 無しなら LLM auto-judge が既存 R 群と照合し a/b/c (無関係 / refines / contradicts) を advisory として payload に残す |
| K (Knowledge) | env 全体 + binding への注釈 | `(commit-K 'name "...")` |
| S (Specification) | 現在の λ binding そのもの | `(commit-S 'name "rationale")` で snapshot (lispy.py:2374 付近) |

- `(restore-S 'name)` — snapshot から binding を復元。 λ は session を跨いで残らないのが default なので、 残したい λ は commit-S → 翌 session で restore-S (gate 承認由来の "gate-approved" snapshot は即時復元の fast-path)
- `(session-intent "...")` / `(rk-log)` / `(S-history 'name)` / `(test-S-against-R)`
- 計画も ledger の一級市民 (kind=plan): `propose-plan` / `approve-plan` / `revise-plan` / `plan-phase!` (lispy.py:2647-2654)。 tool_call 版 `propose_plan` / `plan_step_done` は同じ primitive に委譲 (検証・記録は一本、 JSON schema で形式を API 側から強制)
- 閲覧: `GET /spec` (現 session) / `/spec?session=all` (全 session、 mermaid で R lineage 描画)。 render は server.py:126

## 7. 長期記憶 (二層 + 洗脳)

| 層 | 実体 | 規律 |
|---|---|---|
| 生層 | host.db の turns (role 付き、 append-only) | 検索も編集もしない。 検証の土台 |
| 蒸留層 | `data/memory/` の裏どり済み事実 + `index.md` | agent はタスク前に index.md を read_file (**index-first、 検索しない**)。 直接編集しない — 次の洗脳で上書きされる派生物 |

洗脳 (brainwash.py) = **VERIFY** (assistant の主張を tool / user turn に照合、 裏づけ無しは落として数える) → **ORGANIZE** (マージ・重複畳み) → **ENRICH** (相互リンク + 行数上限つき index)。 洗うのは judge LLM。 安全側の規律: 蒸留層の wipe は `.lispy-memory` marker のあるディレクトリ限定 / index 行数超過は abort (蒸留層無変更) / watermark は session 別。 起動は `(洗脳)` または `host brainwash` (cron 向き)。

覚えさせたいことは会話の中で明示的に言う — 発言が生層に残り、 洗脳が裏どりして昇格する。 tool で確認済みの事実だけが蒸留層に生き残る。

## 8. ハーネス

| 機能 | 仕様 |
|---|---|
| 中断 | REPL: Ctrl-C 1 回 = step 境界で停止、 2 回 = 強制。 server: `POST /interrupt` (flag は評価 1 回で clear) |
| auto-compaction | context 8 割超えで `condense-context` (要約して renew)。 overflow エラー時は `_emergency_compact` + retry の二段構え |
| resume | `--resume` / `--session <prefix>` — 過去会話を text で復元して system turn として注入 (80 turns / 24,000 字上限) + その session で commit-S 済みの λ を自動 restore (`_resume_context`, lispy.py:4102) |
| bg プロセス | `shell_bg` / `shell_out` / `shell_kill` |
| post-edit check | `LISPY_CHECK_CMD` (または `LISPY_CHECK_FILE`) を編集直後に実行 → 結果を tool result に添付 |
| hooks | `LISPY_HOOKS=<path>` — pre-tool (ブロック可) / post-tool (結果に添付) / stop (止まれなくする、 差し戻し上限 2 回/入力) |
| MCP | `LISPY_MCP=<path>` で opt-in した stdio server を `mcp__<server>__<tool>` として tool 化 |
| 並列 tool | read-only だけの batch は並列 (`dispatch-tools`) |
| undo | `(undo [n])` — file 編集の巻き戻し (shell の副作用は対象外) |
| subagent | S 式 `(spawn "task")` / tool `spawn_agent` — 隔離 child env (会話履歴を見ない)。 用途: 探索の文脈隔離 / 独立検証 / 脇道調査 |
| view | `GET /view` — タイムライン + 計画パネル + memory panel (蒸留層の index.md + ファイル一覧)。 `--open` でブラウザ自動起動 |
| タスク分解 | `task_add` / `task_list` / `task_done` tool |

## 9. 設定・起動

### CLI 3 種

| コマンド | 用途 |
|---|---|
| `lispy-server --stdin --yolo` | **主経路**。 REPL + HTTP + /spec。 flags: `--host` (127.0.0.1) `--port` (9000) `--yolo` `--session <prefix>` `--resume` `--stdin` `--open` (server.py:726-739) |
| `lispy` | REPL only (試し打ち用) |
| `host` | DB 操作専用 (list / search / dump / events / cross / label / brainwash) |

server endpoints: `POST /eval /load /reset /interrupt`、 `GET / /bindings /recall?q= /spec /view`。

### 環境変数 (`.env`、 host.py:55-78 ほか)

| 群 | 変数 | default |
|---|---|---|
| executor | `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | **無し** (未設定は明示エラー) |
| | `LLM_MAX_TOKENS` / `LLM_THINK` | 4096 / off |
| judge | `JUDGE_MODEL` / `JUDGE_BASE_URL` / `JUDGE_API_KEY` (3 つ揃いで有効) / `JUDGE_MAX_TOKENS` | executor に fallback / 2048 |
| gate | `LISPY_GATE` | on |
| opt-in | `LISPY_HOOKS` / `LISPY_CHECK_CMD` / `LISPY_CHECK_FILE` / `LISPY_MCP` / `LISPY_MCP_TIMEOUT` | 全て off (opt-in) |
| context | `LISPY_CTX_WINDOW` | 200000 |
| path | `LISPY_DB` / `LISPY_TURN_DIR` / `LISPY_DUMP_DIR` / `LISPY_MEMORY_DIR` | repo 内 host.db / data/turns / data/sessions / data/memory |
| その他 | `LISPY_TZ_OFFSET` / `LISPY_CONFIRM_TIMEOUT` | 9 (JST) / — |
| brainwash | `BRAINWASH_MAX_SESSIONS` / `BRAINWASH_MAX_CHARS` / `BRAINWASH_MAX_TOKENS` / `BRAINWASH_MEMORY_MAX_CHARS` / `BRAINWASH_INDEX_MAX_LINES` | brainwash.py 参照 |

### 起動時 auto-load

`init.lispy` と `auto.lispy` の **2 つだけ** (lispy.py:4000)。 `extras.lispy` (派生 idiom 集) と `ds4.lispy` (ds4-server 固有拡張) は `(load ...)` で opt-in。

## 10. 安全機構の総覧

| 何を | どの経路で | 誰の承認で |
|---|---|---|
| loop 規則 7 binding の define / set! | agent の S 式 auto-eval | 構造チェック + judge LLM (fail-closed) + 自動 commit-S |
| `escalation-class` の再定義 | agent の S 式 auto-eval | **常に人間** (y/N or /view。 不在なら deny) |
| Python primitive の shadow | agent の S 式 auto-eval / defmacro | 一律 deny |
| SKILL.md の更新 | write_file / edit_file / append_file tool | judge LLM (fail-closed)。 shell 経由は名指し deny |
| view.py への書き込み | tool 全般 | 一律 deny (judge にも回さない) |
| 計画フェーズ中の副作用 tool | auto-step 実行中 | plan-phase! でブロック (propose_plan のみ例外) |
| stale plan での実行開始 | auto-step | `(> (plan-id) plan0)` ガード |
| goal 完了判定 | auto-step の round 末 | judge-done (fork-env 独立採点、 JUDGE_* で別モデル)。 prefix 一致のみ DONE |
| 危険 shell コマンド | shell tool | y/N 確認 (`--yolo` で skip) |
| hooks / check / MCP 設定 | cwd の設定ファイル | 自動実行しない — env 変数での明示 opt-in 必須 |
| 蒸留層の wipe | brainwash | `.lispy-memory` marker のあるディレクトリ限定 |
| context overflow | 全 LLM 呼び出し | 8 割で auto-compaction、 overflow で緊急退避 + retry |

**gate の対象は agent 由来 (`eval_origin == "agent"`) のみ** — 人間の REPL 入力・editor 直編集・HTTP /eval は全経路で自由。 これは制限ではなく設計: 人間が最終的な install 権限を持つ、 という権限モデルの表現。
