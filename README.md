# lispy

走らせながら評価規則を書き換えられる agent 評価器。 + R/K/S 三層 ledger (= **RDD: Requirement-Don't-Die**)。

詳細・全 primitive・設計理由は [docs/reference.md](docs/reference.md)。 ここは **1 日の動かし方** だけ。

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
