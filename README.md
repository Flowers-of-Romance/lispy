# lispy

走らせながら評価規則を書き換えられる agent 評価器。 + R/K/S 三層 ledger (= **RDD: Requirement-Don't-Die**)。

システム仕様の一望は [SPEC.md](SPEC.md)、 詳細・全 primitive・設計理由は [docs/reference.md](docs/reference.md)。 ここは **1 日の動かし方** だけ。

## RDD とは

lispy 流の spec-driven development。 ただし R は append-only:

- **R** (Requirement) — `commit-R` で刻む。 上書かれない、 `'replaces N` で lineage が動くだけ
- **S** (Specification) — 現在の λ binding そのもの (= agent-step 等)、 `commit-S` で snapshot
- **K** (domain Knowledge) — env 全体 (= 全 binding) + `commit-K` で binding に注釈

R は消えないので "Requirement-Don't-Die"。 詳細は [docs/reference.md の R/K event ledger 節](docs/reference.md)。

## セットアップ (初回のみ)

```bash
cd ~/lispy
uv sync
uv tool install --editable . --force   # lispy / host / lispy-server を ~/.local/bin に
cp .env.example .env                   # LLM_API_KEY / LLM_BASE_URL / LLM_MODEL を埋める
```

`.env` 無しでも `lispy` の pure eval と `host` の DB 操作は動く。 LLM 呼び出し系 (agent-step / commit-R の auto-judge / test-S-against-R 等) は初回呼び出し時に明示エラー。

## 1 日の workflow

### 1. 開ける

```bash
lispy-server --stdin --yolo                       # 新規 session
lispy-server --stdin --yolo --session 1779360770  # 昨日の続き (session id prefix)
```

REPL (stdin) + HTTP server + `/spec` (R/K/S/artifact 1 枚 HTML) が同時に立つ。 browser tab を開いておく:

```
http://127.0.0.1:9000/spec
```

### 2. 普通に作業

REPL で λ を define / 書き換え / 実行する。 これは ledger に何も書かない:

```lisp
main> (define agent-step (lambda (env input) ...))
main> (eval-turn env "hello")
main> (define agent-step (lambda (env input) ...new version...))
```

「動かす」と「記録する」は分離されてる。

### 3. rhythm point で刻む (= RDD)

何か気付いた瞬間に対応する 1 行を打つ。 全部任意:

```lisp
main> (session-intent "agent-step の loop 防止条件を見つける")
main> (commit-R "agent-step は tool_calls 空で必ず terminate")
main> (commit-S 'agent-step "loop 防止条件を追加")
main> (commit-K 'fold "n=0 のとき init を返す、 関数は呼ばれない")
main> (commit-artifact "session 要約" "...")
```

**`commit-R` の auto-judge**: `'replaces N` 無しで打つと **LLM が現 session の既存 R 群と照合**して a (= 無関係追加) / b (= refines R#N) / c (= contradicts R#N) を判定し、 payload に `@judge=* @judge-target @judge-reason @judge-impact` を残す。

```
main> (commit-R "agent-step の terminate 判定は tool_calls の length == 0 で行う")
(R: agent-step の terminate 判定は tool_calls の length == 0 で行う)
  [judge] b: → R#2
  [reason] 既存 R の終了条件をより具体的な実装で精緻化
  [impact] agent-step の終了判定が明確になり実装が安定する
```

`@replaces=` は自動付与しない (= advisory only)。 同意するなら次 turn で `(commit-R "..." 'replaces N)` を打ち直して lineage を明示する。

### 4. 思い出す

REPL で:

```lisp
main> (rk-log)                    ; 現 session の刻みを時系列で
main> (S-history 'agent-step)     ; λ の snapshot 履歴
main> (test-S-against-R)          ; R 群と現 S が整合してるか LLM judge
```

browser tab を reload:

- `http://127.0.0.1:9000/spec` — 現 session
- `http://127.0.0.1:9000/spec?session=all` — 全 session 集約

mermaid で R lineage まで描かれる。

### 5. 閉じる

`Ctrl-D` (stdin) または `Ctrl-C` (server) で停止。 `close_recording` が走って DB が綺麗に閉じる。

### 6. 翌日

```bash
lispy-server --stdin --yolo --session <prefix>
main> (restore-S 'agent-step)     ; 昨日 commit-S した λ を bindings に戻す
```

`λ は session を跨いで残らない` (default)。 残したい λ は `commit-S` で snapshot しておき、 翌日 `restore-S` で復帰する。

---

## 自走させる (auto-step)

普通の平文入力は agent-step (= 1 タスクを完遂する内側のループ) で処理される。
goal を渡して **完了判定つきで回し続けたい** ときは auto-step:

```lisp
main> (auto-step env "リポジトリの TODO を洗い出して docs/todo.md にまとめる")
main> (auto-step env "..." 20)      ; max-rounds を 20 に (default 12)
```

round ごとに 作業 (agent-step) → 検証 (judge-done: fork-env した独立採点、 自己申告を信用しない)
→ `DONE` なら終了、 `NEXT: ...` なら判定を system-reminder にして続行。
turns が伸びすぎたら auto-renew (要約して renew = compaction 相当) が挟まる。
定義は `auto.lispy` (起動時 auto-load)。 走行中に `(define judge-done ...)` で判定基準ごと書き換えられる。

agent 側からは `spawn_agent` tool で subagent に独立 subtask を委譲できる
(探索の文脈隔離 / 成果物の独立検証 / 脇道調査。 child env は会話履歴を見ない)。

## 長期記憶 (洗脳 / brainwash)

セッション横断の記憶は二層:

- **生層** — host.db の turns (role 付き、 append-only)。 検索も編集もしない、 検証の土台
- **蒸留層** — `data/memory/` の裏どり済み事実 + `index.md`。 agent はタスク前に index を
  read_file で読む (**index-first、 検索しない**)。 いつでも生層から作り直せる

```lisp
main> (洗脳)          ; 前回以降の session を洗って蒸留層を書き直す
```

```bash
host brainwash                       # CLI から (cron 向き)
host brainwash --session 1779360     # 特定 session だけ
```

洗脳 = VERIFY (assistant の主張を tool / user turn に照合、 裏づけ無しは落として数える) →
ORGANIZE (マージ・重複畳み) → ENRICH (相互リンク + 行数上限つき index)。 洗うのは judge LLM
(JUDGE_*)。 結果は `host events --kind brainwash` で振り返れる (何を信用しなかったかが残る)。

覚えさせたいことは **会話の中で明示的に言う** — 発言が生層に残り、 洗脳が裏どりして
記憶に昇格する。 tool で確認済みの事実だけが蒸留層に生き残る。

## 自己書き換え (define-gate)

agent の S 式応答は auto-eval される — つまり agent は自分の loop 規則を書き換えられる。
ただし loop 到達可能な binding (agent-step / judge-done 等 6 つ) への define / set! は
**installer (define-gate) の審査** を通る: 構造チェック → judge LLM が現行定義と提案の
S 式だけを見て APPROVE / REJECT → 承認なら install + 自動 commit-S (rollback 点)。
primitive の shadow は一律 deny。 人間の REPL 入力は gate 対象外 (従来どおり自由)。

judge は `.env` の `JUDGE_MODEL / JUDGE_BASE_URL / JUDGE_API_KEY` で executor と別モデルに
できる (例: executor = ローカル DeepSeek、 judge = Claude)。 未設定なら executor に fallback。
`LISPY_GATE=off` で gate ごと無効化 (開発用)。 judge-done (auto-step の round 判定) も同じ
judge client を使う。

## ハーネス機能

| | |
|---|---|
| 中断 | REPL: Ctrl-C (1 回目 = step 境界で停止、 2 回目 = 強制)。 server: `POST /interrupt` |
| auto-compaction | context の 8 割超えで自動要約 + renew。 overflow エラー時は緊急退避 + retry の二段構え |
| resume | `lispy --resume` / `lispy-server --resume` — 前回 session の会話 + commit-S 済み λ を復元 |
| bg プロセス | `shell_bg` / `shell_out` / `shell_kill` — dev server や長いビルドを待たずに回す |
| post-edit check | `LISPY_CHECK_CMD` (または `LISPY_CHECK_FILE`) のコマンドを編集直後に自動実行 → 結果を agent に返す |
| undo | `(undo [n])` — file 編集の巻き戻し (shell の副作用は対象外) |
| プロジェクト文脈 | cwd から上方の AGENTS.md / CLAUDE.md を system prompt に注入 |
| 並列 tool | read-only だけの batch は並列実行 (dispatch-tools) |
| hooks | `LISPY_HOOKS=<設定>` — pre-tool (ブロック可) / post-tool (結果に添付) / stop (止まれなくする) |
| MCP | `LISPY_MCP=<設定>` で opt-in した server (stdio) を `mcp__<server>__<tool>` として tool 化。 `(mcp-list)` で確認 |
| skills | `.lispy/skills/<name>/SKILL.md` の一覧を system prompt に常駐、 本文は合致時に agent が read。 agent 自身が更新できる (judge 審査つき — 検証を弱める変更は却下)。 更新履歴は rk-log の [skill] |

hooks の例 (`LISPY_HOOKS=./.lispy-hooks.json` で opt-in):

```json
{
  "post-tool": [{"match": "write_file|edit_file", "cmd": "ruff check ."}],
  "stop": [{"cmd": "test -f data/receipt.md"}]
}
```

設定ファイル (hooks / check / MCP) は **cwd で見つかっても自動実行されない** — repo 同梱の
設定で任意コマンドが走るのを防ぐため、 env での明示 opt-in が必要。

## CLI 3 つ

| | 用途 |
|---|---|
| `lispy-server --stdin --yolo` | **主経路**。 REPL + HTTP + `/spec` |
| `lispy` | REPL only (HTTP も `/spec` も無い、 試し打ち用) |
| `host` | DB 操作専用 (list / search / dump / events / cross / label) |

## 詳細

- 全 primitive と設計理由: [docs/reference.md](docs/reference.md)
- system prompt: [lispy.SYSTEM_PROMPT.md](lispy.SYSTEM_PROMPT.md)
- 変更履歴: [CHANGELOG.md](CHANGELOG.md)
