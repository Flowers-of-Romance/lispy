# lispy

**走らせながら評価規則を書き換えられる agent 評価器**。

設計と実装と評価が同じ層で起きる場所。plan / implement / evaluate の分離がない。

**agent loop が S 式の binding** 

```lisp
(define agent-step
  (lambda (env input)
    (let ((env2 (append-turn env (make-turn "user" input))))
      (let ((response (llm-call env2)))
        (let ((env3 (append-turn env2 response)))
          (if (has-tool-calls? response)
              (agent-step
                (fold (lambda (e tc)
                        (append-turn e
                          (make-turn "tool"
                            (dispatch-tool (tool-call-name tc) (tool-call-args tc))
                            (tool-call-id tc))))
                      env3
                      (tool-calls response))
                "")
              response))))))
```

これは Python の関数ではなく lispy の binding の 1 つ。REPL で走らせ、結果を見て、
`(define agent-step ...)` で書き換え、再度走らせる。**すべて同じ REPL 内で。**
再起動なし、ファイル編集なし。

Python に残るのは「LLM を 1 回呼ぶ」「tool を 1 つ走らせる」「turn を作る/追加する」
という単発の primitive だけ。 agent loop の規則そのものは S 式の binding として
inspect / quote / redefine 可能。

## 構成

```
lispy/
├── lispy.py        # 評価器本体 (live-redefinable agent evaluator)
├── host.py         # host environment (DB, read 系 tool 群, LLM client, CLI)
├── edit.py         # 副作用 tool (shell / write-file / edit-file / append-file)
├── nl.py           # 日本語 → S 式 翻訳 REPL (lispy sidecar)
├── server.py       # lispy を HTTP で叩ける常駐プロセス (lispy sidecar)
├── lispy.SYSTEM_PROMPT.md   # lispy / nl 共有の system prompt 本体 (Python 外で編集)
├── nl.SYSTEM_PROMPT.md      # nl 固有の addendum (翻訳器ルール + binding/tool placeholder)
├── init.lispy      # 起動時 auto-load: agent-step / compose (言語の seed)
├── extras.lispy    # 派生 idiom 集 (list ops, control macros, combinators, match...)  (load で取り込む)
├── ds4.lispy       # ds4-server 固有機能 (directional steering / thinking)  (ds4 接続時のみ load)
├── host.db         # SQLite (sessions, turns, FTS5, meta_events, tasks)
├── data/
│   └── turns/      # 日付別 md (DB から再生成可能)
└── pyproject.toml
```

## 前提

- OpenAI 互換 API endpoint (OpenRouter / OpenAI / Anthropic OAI-compat / Ollama / 自前ホストの DeepSeek 等 何でも)
- Python 3.11+
- `uv` がインストール済み

## セットアップ

```bash
cd ~/lispy
uv sync                          # project venv を作成
uv tool install --editable .     # host / lispy CLI を ~/.local/bin に
cp .env.example .env             # provider を選んで API key / model / base url を編集
```

`.env` には **LLM_API_KEY / LLM_BASE_URL / LLM_MODEL** の 3 つを必ず書く (default は持たない設計)。
未設定で LLM を呼ぶと起動時ではなく **初回 LLM 呼び出し時** に明示エラーで止まる。
`host list` / `search` / `dump` 等の DB CLI と、 lispy の **pure Lisp 評価** は .env 無しでも動く。

## CLI: `host`

LLM 対話は `lispy` に集約しているので、 `host` 自体は DB / 記録の操作専用ユーティリティ。
LLM を使わない subcommand (list / search / dump / cross / events / domain) は **.env 無しでも動く**。

```bash
host list                  # session 一覧
host search "query"        # FTS5 (ASCII / 単語境界)
host search "query" --tri  # trigram (CJK 部分一致、3 字以上)
host search "query" --turns | --sessions
host dump                  # DB → 日付別 md 再生成
host domain                # domain (tag) 一覧
host events                # meta-event ledger
host cross "scope"         # session 横断で構造ラベル付き
host label <sid>           # LLM に title/keyphrases/tags を提案させて DB に書く (.env 必須)
```

環境変数:
- `LISPY_DB` — DB path (default: `./host.db`)
- `LISPY_TURN_DIR` — md 出力先

## lispy: `Lisp 風 evaluator`

`lispy` は Scheme 風の式評価器に LLM 呼び出しを混ぜたもの。同じ syntax で「計算」と「言語処理」が並ぶ。

### 起動

```bash
lispy                  # REPL (Ctrl+D で終了)
lispy demo             # 最小 demo (renew + eval-turn)
lispy demo-lambda      # λ 抽象のデモ
lispy demo-compose
lispy demo-compare     # Lisp 決定性 vs LLM 揺らぎ
```

REPL のプロンプトは `main>`。複数行 S 式は `(` で始まって閉じ括弧が揃うまで `...` プロンプトで待つ:

```
main> (define agent-step
...     (lambda (env input)
...       ...body...))
```

### 試してみる

REPL に入ったらまずこれを順に試すと、 lispy が「走らせながら評価規則を書き換えられる」感覚が掴める(かも)。

> **注意**: Recipe (3) と (4) は `agent-step` (= 評価器の loop 規則そのもの) を書き換える。
> 書き換え後は **「以前は動いてた pattern が動かなくなる」**。 各 Recipe 単独で試すか、 順にやって挙動の変化を観察する、 どちらでも OK。
>
> **default に戻したい時** は REPL を抜けて再起動するか、 `(load "init.lispy")` で再 load する。
> `init.lispy` は起動時に自動 load される seed ファイルで、 `agent-step` と `compose` の元定義が
> 入っている (Python 側に hard-code せず、 「核は S 式」 を実装と一致させる狙い)。

#### (1) 基本動作の確認

```
main> 富士山の標高は?
富士山の標高は 3,776 m です。
;; (agent-step env "富士山の標高は?") が S 式として走った結果

main> (lookup "agent-step")
<Lambda agent-step(env, input) [lisp]>

main> (lambda-body (lookup "agent-step"))
((let ((env2 (append-turn env (make-turn user input)))) ...))
;; ↑ agent loop の規則そのものが S 式として見える
```

#### (2) host の DB / file system を Lisp から触る

```
main> (current-time)
2026-05-20T17:35:00

main> (string-length (read-file "README.md"))
23443

main> (recall "lisp")
;; → 過去 session で "lisp" にヒットした turn の list (FTS5)

main> (glob "*.py")
/path/to/lispy.py
/path/to/host.py
```

#### (3) 走行中 redefine

agent-step を「LLM 呼び出し前に input を表示する」版に書き換える。

**1 行版 (paste 用、 推奨)**:

```
main> (define agent-step (lambda (env input) (begin (print "[呼び出し直前] input:" input) (append-turn env (make-turn "user" input)) (let ((response (llm-call env))) (begin (append-turn env response) response)))))

main> 北海道について 1 行で
[呼び出し直前] input: 北海道について 1 行で
北海道は日本最北の島で、 広大な自然と冷涼な気候、 札幌・函館などの都市、 海の幸が有名です。
```

REPL を再起動せずに agent loop の規則が変わる。 print の副作用 (`[呼び出し直前]`) と本来の
LLM 応答が両方出る。

**整形版 (読みやすいが paste は terminal の挙動に依存)**:

```lisp
(define agent-step
  (lambda (env input)
    (begin
      (print "[呼び出し直前] input:" input)
      (append-turn env (make-turn "user" input))
      (let ((response (llm-call env)))
        (begin
          (append-turn env response)
          response)))))
```

`begin` は式を順に評価し、 最後の値を返す。 副作用 (print, append-turn) を並べて、
最後に応答を返すパターン。 同等のことは `(let ((_ ...)) ...)` のネストでも書けるが、
`begin` の方が読みやすい。

注: この簡略版は tool 呼び出しの処理を省いてる。 `(has-tool-calls? response)` 分岐を入れた
完全版は init で pre-defined されているものを参照。

#### (4) agent-step を LLM に見せて自己修正させる

```
main> (prompt (string-append
...     "次は agent-step の現行定義: "
...     (to-sexp (lambda-body (lookup "agent-step")))
...     "  これを、tool 呼び出し前に critique を挟む版に書き換えて。"
...     "S 式 1 つだけで返す。説明禁止。"))
;; → LLM が修正版の S 式を text で返す
```

ポイント:
- `(lambda-body ...)` は内部 tree (Python の list + Symbol オブジェクト) を返す
- `(to-sexp tree)` で Lisp 表記の文字列に変換 (`"user"` 等の文字列は quote 付き = read-sexp 可能)
- `(string-join " " ...)` だと Python の repr が出てしまって LLM が読めない

返ってきた S 式を `(eval (read-sexp ...))` で取り込めば、 評価器が自分の規則を LLM 由来で
更新する。 metacircular の極端な姿。 `to-sexp` は `read-sexp` の逆方向で、 セットで round-trip を成立させる。

#### (5) 失敗を見てみる

**前提**: default の agent-step が必要。 Recipe (3) や (4) で書き換えた場合は、 セクション冒頭の
リセット paste を先に流すか REPL を再起動してから。

紙のドラフト agent-step は recursion 時に `(agent-step ... "")` で空入力を渡す。 実際に踏む
と turns に空 user が混じる

```
main> current_time tool を呼んで時刻を 1 行で
;; tool 経路を通って正しい応答が返る

main> !turns
[xxx] user: current_time tool を…
[xxx] assistant: (tool_calls)
[xxx] tool: 2026-05-20T17:36:00
[xxx] user:                      ← 空 user turn
[xxx] assistant: 現在時刻は…
```

これが気に入らなければ agent-step を書き換えて、 input が空なら append-turn をスキップする
ロジックを足せる。

### S 式の書き方

```lisp
42                ; 数値 atom (int / float)
"hello"           ; 文字列 atom
foo               ; symbol (env から lookup)
(+ 1 2)           ; list = 関数適用
(quote (+ 1 2))   ; quote = リスト構造として持つ (評価しない)
```

### 入力の処理経路

REPL に入力した行は:
- `(` で始まる → **S 式として直接評価** (model を介さない)
- それ以外 → 平文として **LLM に投げる** (.env で指定した OpenAI 互換 endpoint)
- `!` で始まる → REPL のメタコマンド (`!env` `!archive` 等、後述)

### Lisp core (raw 値で計算)

#### 算術 / 比較 / 論理
```lisp
(+ 1 2 3)         ; → 6
(- 10 3 2)        ; → 5
(* 2 3 4)         ; → 24
(/ 12 3)          ; → 4.0
(= 5 5)           ; → #t
(< 3 7)           ; → #t
(> 7 3)           ; → #t
(and (> 5 3) (< 2 4))  ; → #t
(or #f #f #t)     ; → #t
(not (= 1 2))     ; → #t
```

Boolean は Scheme 流の `#t` / `#f` (alias: `#true` / `#false`) を式中に書ける。
`#f` と空 list `(list)` と `nil` が falsy、それ以外はぜんぶ truthy。

#### 制御 / 束縛
```lisp
(if (> x 0) "positive" "negative")
(define x 42)                          ; env.bindings に登録
(define inc (lambda (x) (+ x 1)))      ; 関数を define
(let ((a 3) (b 4)) (+ (* a a) (* b b)))  ; 局所束縛 → 25
```

#### list 操作
```lisp
(list 1 2 3)              ; → (1 2 3)
(car (list 1 2 3))        ; → 1
(cdr (list 1 2 3))        ; → (2 3)
(cons 0 (list 1 2 3))     ; → (0 1 2 3)
(null? (list))            ; → #t
```

#### 文字列述語 / 操作
```lisp
(string? "foo")                       ; → #t
(string-contains? "hello world" "wo") ; → #t
(string-prefix? "hello" "he")         ; → #t
(string-suffix? "hello" "lo")         ; → #t
(string-length "hello")               ; → 5
(string-upcase "hello")               ; → HELLO
(string-downcase "Hello")             ; → hello
(string-trim "  hi  ")                ; → hi
(string-append "foo" "/" "bar")       ; → foo/bar
(substring "hello" 1 4)               ; → ell
```
(REPL は文字列値を quote 無しで表示する)
LLM 出力で `if` を切るときに使う。例:
```lisp
(define rate (lambda (x) "{x} を 'high' / 'mid' / 'low' のうち 1 単語で"))
(if (string-contains? (rate "Lisp") "high") "important" "ok")
```

#### read-sexp と eval — text ↔ tree ↔ value の 3 段変換

`read-sexp` は **テキストを Lisp の tree に parse** する。`eval` は **tree を評価して値にする**。
合わせると **文字列として持っている S 式を実行できる**:

```lisp
(read-sexp "(+ 1 2)")           ; → (+ 1 2)   リスト構造 (まだ走ってない)
(eval (read-sexp "(+ 1 2)"))    ; → 3         tree を評価
```

step by step:

```lisp
main> (define text "(* 6 7)")    ; 文字列として持つ
main> text                       ; → (* 6 7)  (REPL は string を quote 無しで出す)
main> (string? text)             ; → #t       これは text
main> (define tree (read-sexp text))
main> tree                       ; → (* 6 7)  見た目は同じだが Lisp の list (内部は [Sym(*) 6 7])
main> (string? tree)             ; → #f       こちらは text ではなく tree
main> (eval tree)                ; → 42       tree を評価して値に
```

| | string | tree | value |
|---|---|---|---|
| 形 | `"(* 6 7)"` | `(* 6 7)` | `42` |
| Python 型 | `str` | `list` (中身 Symbol / number) | `int` |
| 順方向 | — `read-sexp` → | — `eval` → | |
| 逆方向 | ← `to-sexp` — | (なし — 値そのままで保持) | |

逆方向は `to-sexp`:

```lisp
(to-sexp (quote (+ 1 2)))            ; → "(+ 1 2)"   tree を Lisp text に
(eval (read-sexp (to-sexp expr)))    ; → 元の値      round-trip
```

`to-sexp` は文字列を quote 付きで出すので、 LLM に渡して再度 `read-sexp` で受け取っても
壊れない。 一方、 REPL 表示で使われる `_to_lisp_string` は文字列を quote 無しで出す
(ユーザー向け表示) ので、 round-trip には `to-sexp` の方を使う。

#### `(eval (read-sexp ...))` の使い所

主に **LLM の text 出力をコードとして走らせる** とき

```lisp
main> (prompt "1+2+3 を求める Lisp S 式を 1 つだけ、説明禁止")
"(+ 1 2 3)"
;; ↑ string が返ってくる

main> (eval (read-sexp "(+ 1 2 3)"))
6
;; ↑ string を tree にして評価
```

(3) で「LLM 生成コードを Lisp が評価する」metacircular パターンを使ったが、その実体はこれ。
他にも `(quote-turn ...)` で保存しておいた S 式テキストを実行するときなどに使う。

#### `eval` だけの使い方

`eval` は引数の **tree** を評価する。tree は `(quote ...)` で手動で作ることもできる

```lisp
(eval (quote (+ 1 2)))                          ; → 3
(eval (cons (quote *) (list 2 3 4)))            ; → 24
;; ↑ cons / list で operator + を * に置き換えた新 tree を作って eval
```

つまり tree は (a) `read-sexp` で text からパースする、(b) `quote` でリテラル指定する、
(c) `cons` / `list` / `car` / `cdr` で構造的に組み立てる、の 3 通り。どれも `eval` に
渡せば走る。

#### prompt (低レベル LLM 呼び出し)
```lisp
(prompt "1+2 は?")          ; → 3
```
- `(lambda ...)` の template 展開、system 切り替え、auto-eval を **使わない** 素の 1 ショット呼び出し
- `(lambda gen (q) "...{q}...")` は body system が「S 式を出すな」と命じるため、コード生成には不向き
- 「LLM 生成コードを自分で評価する」metacircular 用途は `prompt` を使う

```lisp
(define gen-code (lambda (q)
  (strip-code-fences (prompt (string-append
    "次を 1 つの Lisp S 式だけで書け。"
    "使える operator: + - * / list car cdr cons. "
    "括弧で始まる 1 式のみ、説明禁止。問: " q)))))

(eval (read-sexp (gen-code "1 から 10 までの和")))
; → LLM が (+ 1 2 3 4 5 6 7 8 9 10) を生成 → Lisp が評価 → 55
```

`strip-code-fences` は markdown の ```` ```lang ... ``` ```` を剥がすヘルパー。LLM がコードブロックを付けてきたときに正規化するために使う。

#### quote (評価せず tree のまま持つ)
```lisp
(quote (+ 1 2))           ; → (+ 1 2)  リストとして残る (eval されない)
(quote foo)               ; → foo      シンボルとして残る
```

`(quote expr)` は **expr を評価せず、そのままの tree を返す** 特殊形式。`'expr` の略記法は
今は無いので必ず `(quote ...)` と書く。code = data の片側 (data として保持) を支える。

評価側の `eval` と組み合わせると **tree を分解して書き換えて再評価** ができる

```lisp
(eval (cons (quote *) (cdr (quote (+ 2 3 4)))))   ; → 24
;; (+ 2 3 4) から先頭 + を取り除き、代わりに * を cons → (* 2 3 4) → 評価
```

この種の操作は LLM でも近似はできる (「`+` を `*` に書き換えて評価して」 と頼めば `24` を
返してくる)。 ただし LLM は text を pattern match しているだけで、 ユーザが `cdr` のように
直接 touch できる first-class な構造として持っているわけではない (内部に tree-like な何か
が抽出可能だとしても、 それを Lisp の `cdr` 相当として使う API はない)。 出力は確率分布の
ピークで、 浅い構造では鋭く当たるが、 nested expression や同じ token の多義性が深まるほど
鈍る。 Lisp なら式を tree として分解・再合成・再評価でき、 結果は構造的に決まる。

### Lambda — 2 種類

#### Lisp lambda (body は式)
```lisp
(lambda (x) (+ x 1))                   ; 匿名 Lisp lambda
(define inc (lambda (x) (+ x 1)))      ; 名前付け
(inc 41)                                ; → 42
(define square (lambda (x) (* x x)))
(square 7)                              ; → 49
```

#### LLM lambda (body は文字列テンプレ)
```lisp
(lambda critique (x) "次を 1 行で批判: {x}")    ; 名前付き、env.bindings に登録
(critique "Lisp は古い")                         ; → LLM の応答
```
- body 内の `{param_name}` が Python の `.format()` で展開される
- `{self}` は λ 自身の名前 (再帰用)
- 引数を文字列化してテンプレに埋め、結果プロンプトを LLM に投げる
- LLM 実行中は `BODY_SYSTEM` (= 「自然言語で答え、S 式を吐かない」) を system prompt に差し替える

#### 名前付き vs 匿名
```lisp
(lambda name (p) body)    ; 第 1 引数が symbol → 名前付き、env.bindings[name] に登録
(lambda (p) body)         ; 第 1 引数が list → 匿名、Lambda 値を返す
```

### compose (派生定義済み)

```lisp
; 内部で初期化時に実行されてる:
; (define compose (lambda (f g) (lambda (x) (g (f x)))))

(define double (lambda (x) (* x 2)))
(define inc (lambda (x) (+ x 1)))
((compose double inc) 5)               ; → (inc (double 5)) = 11
```

### 関数適用

#### Juxtaposition (普通の Lisp 形式)
```lisp
(f x)                ; f を x に適用
(f x y z)            ; 複数引数
(f (g x))            ; ネスト
```

#### apply (引数 list を展開)
```lisp
(apply + (list 1 2 3 4))      ; → 10
(apply f a b c)               ; → f(a, b, c) と同等
(apply f a b (list c d))      ; → f(a, b, c, d)  最後の list だけ展開
```

### 環境操作 (LLM 評価器メタ)

#### renew
```lisp
(renew "覚書")                ; 現 env.turns を archive 退避、新 env へ
(renew)                        ; carry 無し
```
- env.turns を archive に保存 (archive_id 自動生成)
- env.turns を空にして、carry を system message として 1 件だけ追加
- env.bindings (λ や define) はそのまま残る

#### quote-turn
```lisp
(quote-turn (+ 2 3 4))           ; S 式を **評価せず** 保管 → 新 turn id を返す
(quote-turn "三平方の定理を一行で")  ; 平文プロンプトを保管
(quote-turn (define x 100))      ; 副作用付きの式も保管できる
```
- arg は評価されない (Lisp の `quote` 流儀)。str / symbol / list を text 化して env.quoted に登録
- 戻り値の payload に新 id が入る。(quoted) で一覧確認

#### eval-turn
```lisp
(eval-turn TURN_ID)            ; 過去の turn を今の env で再評価
```
- archive または quoted の turn を id で引いて評価し直す
- content が S 式形なら Lisp として直接評価、平文なら LLM に再投入
- system prompt や bindings を変えてから呼ぶと「同じ問いを別の評価器で走らせる」になる

#### spawn (subagent 風)
```lisp
(spawn "tell me about X")     ; child env を作って task を任せる、結果を返す
```
- 親 env の system, tools, schema を継承した独立 env を作る
- depth 制限 3

### 状態確認

```lisp
env                         ; 現 env オブジェクトそのもの (fork-env / llm-call に渡せる)
(env-info)                  ; name / depth / turns 数 / archive 数 / bindings 数 etc.
(turns)                     ; 直近 5 turn (プレビュー)
(turns 10)                  ; 直近 10 turn
(turn)                      ; 末尾 turn の content (文字列)
(turn "last")               ; 同上
(turn "last-user")          ; 直近 user turn の content
(turn "last-assistant")     ; 直近 assistant turn の content
(turn N)                    ; env.turns[N] (負の index も可: -1=末尾、-2=その 1 つ前…)
(turn <id>)                 ; id 指定で取得 (env.turns / archive / quoted を検索)
(turn "user" N)             ; user turn 列の N 番目 ((-1)=直近 user、-2=その前 …)
(turn "assistant" N)        ; assistant turn 列の N 番目
(archive)                   ; archive 一覧 (id, turn 数)
(lambdas)                   ; 登録済み λ 一覧 (params, kind, body プレビュー)
(quoted)                    ; (quote-turn ...) で保管された turn 一覧
```

`(turn ...)` は content **そのもの** を返すので、LLM λ にそのまま流せる。会話中の任意の発言を後から評価できる
```lisp
(lambda critique (x) "次を一行で批判: {x}")
(critique (turn "last-assistant"))      ; 直前の応答を自己批判
(critique (turn "assistant" -2))        ; 1 つ前の応答を批判
(critique (turn "user" 0))              ; 最初の user 質問を批判
(critique (turn 5))                     ; index 5 の turn を批判
```

REPL の meta コマンド (S 式じゃなく `!` prefix) でも同じ
```
!env  !archive  !lambdas  !turns  !quoted  !reset
```

### 平文と S 式の混在

```
main> 富士山の標高は？             ; 平文 → LLM に投げる
富士山の標高は 3776 m です。

main> (+ 1 2)                      ; S 式 → 直接評価
3

main> (define greet (lambda (x) "Hello, {x}!"))
main> (greet "world")              ; LLM lambda 経由でモデルへ
Hello, world!
```

### 言語拡張

Lisp core に上乗せされた機能群。 概念だけ列挙、 詳細は `extras.lispy` と REPL の help で。

#### マクロ (`defmacro` + quasiquote)

```lisp
(defmacro when (c &rest body) `(if ,c (begin ,@body) nil))
(when (> 3 2) (print "yes") "ok")        ; → ok

(defmacro with-retry (n expr)
  (if (= n 0) expr
      `(try ,expr (catch (e) (with-retry ,(- n 1) ,expr)))))
(with-retry 3 (llm-call env))             ; 3 回まで自動 retry
```

- `'x` / `` `x `` / `,x` / `,@x` の reader 糖衣
- `&rest` で残余引数
- `(gensym)` で衝突しない symbol 生成 (非 hygienic なので変数捕獲は自分で回避)
- `(macroexpand-1 'form)` で展開結果を覗ける

#### try / catch / error

```lisp
(try (car (list))
     (catch (e) (string-append "rescued: " (error-message e))))
; → "rescued: car: needs a non-empty list"

(error "custom" "io")                     ; tag 付きで raise
(error? v)  (error-message e)  (error-tag e)
```

#### set! / box (可変状態)

```lisp
(define x 10)
(set! x 20)                ; define 済みを更新

;; closure-shared mutable state には box を使う (lispy の closure は snapshot)
(define make-counter
  (lambda ()
    (let ((c (box 0)))
      (lambda () (set-box! c (+ (unbox c) 1))))))
(define ctr (make-counter))
(ctr) (ctr) (ctr)                        ; → 3
```

#### recur (明示 TCO)

`recur` は **nearest 内側 lambda を tail call** する。 stack を消費しないので
無限ループ用途で使える (普通の自己呼び出しは深度 100 制限)。

```lisp
(define sum-to
  (lambda (n acc)
    (if (= n 0) acc (recur (- n 1) (+ n acc)))))
(sum-to 1000000 0)                       ; → 500000500000
```

Clojure 流の明示形 (Scheme の auto TCO ではない)。 相互再帰には使えない。

#### fork-env (env を first-class に複製)

```lisp
(define alt (fork-env env 'system "you are a critic"))
;; turns / bindings / archive / macros / quoted は独立 copy
;; tools / db_conn / record_sid は元と共有
(llm-call alt)                            ; 別 system で同じ会話を試す
```

debate / counterfactual / parallel sampling を S 式だけで組める素地。

#### llm-call の options + logprob 観測

```lisp
(llm-call env 'temperature 1.5 'logprobs #t 'max-tokens 4096)

(define r (llm-call env 'logprobs #t))
(turn-logprobs r)                         ; → ((tok logp) (tok logp) ...)
(turn-entropy r)                          ; → 平均 -logprob (確信度の代理)

;; 確信度で分岐
(if (< (turn-entropy r) 0.5) r (renew "more deliberation"))
```

option は plist 形式 (`'key value 'key value`)。 サポート: `temperature` / `max-tokens` / `logprobs` /
`top-logprobs` / `think` / `extra`。

`'extra` は provider 固有 field を OpenAI SDK の `extra_body=` にそのまま流す窓口で、 plist を取る:

```lisp
(llm-call env 'extra (list 'dir-steering-ffn -1.0 'dir-steering-attn 0.5))
;; → kebab-case の key は snake_case に変換、 extra_body={'dir_steering_ffn': -1.0, ...} で送る
```

これを使えば ds4 / OpenRouter / OpenAI 固有の拡張 (= 標準 OAI 仕様にない field) を lispy.py を触らず通せる。
ds4 の directional steering を per-request に flip するのが典型 (後述 ds4.lispy を参照)。

#### 型述語

```lisp
(list? x)  (symbol? x)  (number? x)  (integer? x)  (boolean? x)
(lambda? x)  (pair? x)  (eq? a b)  (string? x)
```

`match` macro (`extras.lispy` で `(load ...)`) と組み合わせると tool-call dispatch が綺麗:

```lisp
(match resp
  ((list 'ok v)    (process v))
  ((list 'err msg) (handle msg))
  (_               (default)))
```

#### 副作用 tool (shell / write-file / edit-file / append-file)

agent が tool_call として呼べる。 user は REPL から直接も呼べる:

```lisp
(shell "git status")                      ; allow-list なので即実行
(shell "rm -rf foo")                      ; 確認 prompt が出る
(write-file "/tmp/note.md" "hello")
(edit-file "foo.py" "old" "new")
```

危険コマンド (rm, write 系) は y/N 確認が出る。 一括承認したいときは:

```bash
$ lispy --yolo                            # 起動時から全 skip
```

```lisp
main> (set-yolo #t)                       ; session 中だけ切り替え
main> ...
main> (set-yolo #f)                       ; 戻す
main> (yolo?)                             ; 現状確認
```

allow-list は `;` `&&` `|` backtick `$(` 等 shell metacharacter を検出すると無効化されるので、
`(shell "git status; rm -rf /")` は素通しせず confirm が出る。

#### ds4.lispy (ds4-server 接続時の拡張)

`ds4.lispy` は ds4-server (DwarfStar 4 / DeepSeek V4 Flash) を `LLM_BASE_URL` に
据えてるときだけ load する派生 idiom 集。 他 provider (OpenAI / Anthropic / OpenRouter)
では `extra_body` の field が無視 or error になる ので、 接続先で出し分ける:

```lisp
main> (load "ds4.lispy")
```

提供してる軸は 2 つ:

**(a) thinking mode の per-call 制御** — `think-on` / `think-off` / `(think-effort "max")`。
ds4 は reasoning effort を `"max"` (= Think Max) / `"high"` (default) / `"none"` で切れる。

**(b) directional steering の per-request scale 制御** — ds4-server を
`--dir-steering-file FILE` で起動しておけば、 vector 方向の scale を per-request に flip できる。
vector は server 起動時に 1 つ固定 (別 vector が要るなら別 port で別 server)。

符号の規約 (式: `y = y - scale * dir * dot(dir, y)`):
- **正 scale → vector 方向を 抑制** (build_direction.py の good - bad の good 側を消す)
- **負 scale → vector 方向を 増幅** (good 側を強める)

verbosity vector (= succinct - verbose) なら `+1` で verbose 寄り、 `-1` で succinct 寄り。

```lisp
(steering -1.0)                          ;; ffn scale だけ。 attn は 0。 plist で返る
(steering+ -1.0 0.5)                     ;; ffn / attn 両方指定
(llm-call env 'extra (steering -1.0))    ;; 直接 1 発投げる
(steer-call env -1.0)                    ;; 上の shortcut

(steer-sweep env '(-1 0 1 2))            ;; 同 env で 4 段階 scale を振って観測
;; → ((-1 "短い 応答") (0 "通常") (1 "長め") (2 "詳細"))

(steer-debate env -1 2)                  ;; 両端 1 発ずつ生成し (low-text high-text)

(steer-entropy-curve env '(-1 0 1 2))    ;; scale ごとに mean entropy を測る
;; → ((-1 0.42) (0 0.58) (1 0.71) (2 0.95))
```

`fork-env` × `(steering ...)` で **「同じ env / system / turns、 activation 方向だけズラした
counterfactual」** が組める。 lispy が固有に持つ位置付け (steering を first-class 値として
扱える唯一の LLM client)。

ds4 fork (上記の per-request scale patch) は: https://github.com/Flowers-of-Romance/ds4

### 使い方の典型 (Cookbook)

各 recipe は REPL (`main> `) でそのまま実行できる。前提として `lispy` で REPL を起動済み。

#### Recipe 1: 任意の発言を pick して評価する

会話を続けたあと、特定の応答を後から批判・要約・採点したい

```
main> 富士山の標高を 1 行で
富士山の標高は 3,776 m です。

main> 札幌の人口を 1 行で
札幌市の人口は約 195 万人です…

main> 那覇の気候を 1 行で
那覇は亜熱帯気候に属し、…

main> (lambda critique (x) "次を 1 行で批判: {x}")

main> (critique (turn "assistant" -2))   ; 1 つ前の assistant 応答 (= 札幌) を批判
札幌市の人口は約 195 万人という数字は 2023 年時点では正しいですが…
```

`(turn ...)` の指定の仕方:
- `(turn)` または `(turn "last")` — 末尾
- `(turn "last-user")` / `(turn "last-assistant")` — 直近の role
- `(turn N)` — index (負の値は末尾から)
- `(turn "user" N)` / `(turn "assistant" N)` — role 列の N 番目
- `(turn <id>)` — id 指定 (`!turns` で見える 8 文字 hex)

#### Recipe 2: 評価軸を λ として保存・合成する

```
main> (lambda summarize (x) "次を一文で要約: {x}")
main> (lambda en        (x) "次を英訳: {x}")
main> (define en-summary (compose summarize en))   ; summarize ∘ en

main> (en-summary (turn "last-assistant"))
…英訳した上で要約された結果…
```

`compose` は init 時に Lisp で派生定義済み:
`(define compose (lambda (f g) (lambda (x) (g (f x)))))`

#### Recipe 3: Lisp 条件 + LLM 分岐 (ハイブリッド)

```
main> (lambda praise (n) "{n} 点を 1 行で褒めて")
main> (lambda scold  (n) "{n} 点を 1 行で叱って")
main> (define grade (lambda (n) (if (> n 60) (praise n) (scold n))))

main> (grade 92)     ; → 褒めの 1 行
main> (grade 28)     ; → 叱りの 1 行
```

決定性が要る所は Lisp、文章生成は LLM、を 1 つの式で混ぜる。

#### Recipe 4: LLM 出力で if 分岐 (`string-contains?`)

```
main> (lambda judge (x) "{x} は computer science のトピックか? yes か no で 1 単語")
main> (define cs? (lambda (topic)
        (string-contains? (string-downcase (judge topic)) "yes")))

main> (if (cs? "Lisp の歴史") "CS" "non-CS")    ; → CS
main> (if (cs? "寿司の握り方") "CS" "non-CS")   ; → non-CS
```

#### Recipe 5: LLM がコード生成、Lisp が評価 (metacircular)

```
main> (define gen-code (lambda (q)
        (strip-code-fences (prompt (string-append
          "次を 1 つの Lisp S 式だけで書け。"
          "使える operator: + - * / list car cdr cons。"
          "括弧で始まる 1 式のみ、説明禁止。問: " q)))))

main> (eval (read-sexp (gen-code "1 から 10 までの和")))    ; → 55
main> (eval (read-sexp (gen-code "6 と 7 の積")))            ; → 42
```

なぜ `(lambda ...)` ではなく `prompt` を使うかは「落とし穴」を参照。

#### Recipe 6: 文脈の保存と切り替え

```
main> 富士山について
…
main> 北海道について
…
main> (renew "山と地域の話")            ; 現 env.turns を archive、新環境へ
:renew → archived as af3b5823 (carry: 8 chars)

main> !archive
  af3b5823: 4 turns
    [df2ace60] user: 富士山について
    [...] assistant: …
    …

main> (eval-turn df2ace60)               ; archive の任意 turn を今の env で再評価
```

`env.bindings` (λ や define) は `renew` でも残る。文脈だけを切る。

#### Recipe 7: bookmark — 再利用したい問いや式を保管する

```
main> (define recurring (quote-turn (rate (turn "last-assistant"))))
;; ↑ "直近応答を rate に流す" という評価器を保存
;; recurring に turn id 文字列が束縛される

main> 何か新しい話題について発話
…新しい応答…

main> (eval-turn recurring)
;; ↑ 保存しておいた評価器を今の最新応答に対して走らせる
```

`(quote-turn arg)` は arg を**評価せず**保存。`(eval-turn id)` で取り出し時に評価する。「あとで決まった処理を回したいので呼び出しを保管しておく」用途。

#### Recipe 8: 即席評価 — λ を定義したくないとき

```lisp
;; (a) 匿名 λ で 1 回だけ
((lambda (x) "次を 1 行で批判: {x}") (turn "last-assistant"))

;; (b) prompt で生 LLM 呼び出し
(prompt (string-append "次を 1 行で批判:\n" (turn "last-assistant")))
```

#### Recipe 9: 派生 idiom を `.lispy` ファイルとして load する

lispy は agent-step と compose 以外、 init で pre-define **しない**。 派生 idiom は
`.lispy` ファイル (S 式が並んだテキスト) として書いて、 `(load "path.lispy")` で取り込む:

```
main> (load "extras.lispy")
(loaded 7 forms from /Users/jm/lispy/extras.lispy)

main> (lens (list dbl inc) 5)
(10 6)
```

repo に同梱の `extras.lispy` には大別して以下が入っている:

```
list ops     map / length / reverse / append / filter / assoc / member
             take / drop / nth / last / every? / any? / zip / flatten
             range / iota
numeric      inc / dec / even? / odd? / zero? / positive? / negative?
combinator   identity / const / flip / pipe / compose / lens / wrap / debate / probe
control      when / unless / cond / let* / match (macro)
robust       memoize / with-retry / with-fallback / with-default
agent idiom  transform-past / from-pack / condense
```

map / reverse / filter / 等は **tail-recursive 化済** (recur + accumulator) なので 1000+ 要素でも安全。

`load` は `_tokenize_sexp` がコメント (`;` 行末まで) を読み飛ばす + `read_all_sexp` で
複数 S 式列をパースする仕組み。 自分で書いた `.lispy` ファイルを置いて load すれば
個人ライブラリができる。

「素材だけ与えて、 ライブラリは外置き」 が lispy の方針。 ファイル編集して `(load ...)` 再実行で
load し直せるので、 ライブラリ自体も live-redefinable。

#### Recipe 10: モードシフト REPL (set-mode) — 平文入力を λ 経由に

```
main> (lambda socratic (x) "次の問いをソクラテス風に問い返す。質問形式で 1 行: {x}")
main> (set-mode socratic)
(input_mode set: socratic)

main> 富士山の標高は？
そもそも「高さ」とは何を基準に測るべきか？

main> Lisp は何のためにあるのか？
そもそも言語は何のためにあるべきか？

main> (clear-mode)
(input_mode cleared)

main> 富士山の標高は？    ; 元に戻った
富士山の標高は 3,776 m です。
```

- `(set-mode <lambda>)` で `env.input_mode` を設定。以降の平文入力は `(<lambda> <input>)` 相当として処理される
- S 式 (`(...)` で始まる) はモードに関係なく直接評価される
- `(clear-mode)` または `(set-mode)` で解除

### R/K event ledger — 終わらない実装、 でも区切りはある

lispy が向き合ってる problem は **「SDD のように R (要件) を事前に書き下せない開発」**。
Kent Beck の TDD/XP を「技法」 ではなく「R/K/S の動的発見の運動」 として読む
([参考記事](https://zenn.dev/j_m/articles/e8ff79acc5c609)) と、 lispy の primitive 群は
ちょうどその運動を支える形に並ぶ:

| 概念 (記事) | lispy の対応 |
|---|---|
| **R** — 環境にある、 事前に書き下せない要求 | `commit-R` で append-only ledger に刻む |
| **S** — 実行可能な仕様 / 実装 | `define` の λ binding、 `commit-S` で snapshot |
| **K** — チームの判断密度 / 蓄積知識 | `bindings` / `fork-env` / `commit-K` |
| **K, S ⊢ R** が動的 | `test-S-against-R` で LLM に整合性を判定させる |

「実装は終わらない、 でも区切りはある」 を物理的に支えるために、 12 の primitive を用意:

```lisp
;; R/K/S/artifact event を ledger に刻む
(session-intent "...")                ;; この session の artifact 宣言
(commit-R "...")                      ;; R が見えた瞬間
(commit-R "..." 'replaces N)          ;; R の変更 (旧 event id を lineage に)
(commit-K 'name "...")                ;; 学んだことを binding に紐付け
(commit-S 'name ["rationale"])        ;; 現 λ body を snapshot
(commit-artifact "label" expr)        ;; 外に持ち出せる成果を明示

;; 観測 / 比較 / 復元
(rk-log)                              ;; 全 event を時系列 + lineage で表示
(S-history 'name)                     ;; 指定 λ の commit lineage を session 跨いで
(restore-S 'name [id])                ;; 旧 snapshot を bindings に戻す (id 省略で最新)
(diff-S 'name id1 id2)                ;; 2 snapshot の body unified diff
(diff-K env1 env2)                    ;; fork-env した 2 env の K 差分
(replay-with-K env id)                ;; 過去 turn を 現 K で再評価 + replay event 記録
(test-S-against-R)                    ;; LLM に R 群と S の整合性判定 + 結果を ledger に
```

全部 既存の `host.log_meta` 経由で `meta_events` テーブルに書く。 `host events --kind R / K / S /
intent / artifact / test-S-R / replay / restore-S` で CLI からも検索可能。
session_id 単位なので 後日 cross-session で振り返れる。

#### 典型的な多日 session の流れ

```lisp
;; 1 日目
main> (session-intent "user 入力から要約を作る tool")
main> (commit-R "要約は 3 行、 簡潔に")
main> (define summarize (lambda (x) "次を 3 行で要約: {x}"))
main> (commit-S 'summarize "v1: 3 行で要約")
main> (commit-artifact "summarize-v1" (lambda-body (lookup "summarize")))

;; 2 日目 — 実際に動かしたら「違う」 と気づいた
main> (load-session ...)  ;; or (restore-S 'summarize)
main> (commit-R "実際 user に見せたら『bullet 欲しい』 と言われた。 R が動いた"
                'replaces 2)
main> (commit-K 'summarize "3 行より bullet の方が R に合う")
main> (define summarize (lambda (x) "次を 3-5 個の bullet で要約: {x}"))
main> (commit-S 'summarize "v2: bullet 形式に")

;; 3 日目 — R が積み重なって、 S が満たせてるか不安
main> (test-S-against-R)
;; → LLM 判定: #4 (R v2) は満たすが #2 (R v1) との整合が怪しい...

;; 振り返り
main> (rk-log)
rk-log:
  #1 [intent] user 入力から要約を作る tool
  #2 [R] 要約は 3 行、 簡潔に
  #3 [S] summarize [llm] — v1: 3 行で要約
  #4 [art] summarize-v1
  #5 [R] 実際 user に見せたら... ← #2
  #6 [K] summarize: 3 行より bullet の方が R に合う
  #7 [S] summarize [llm] — v2: bullet 形式に
  #8 [test] LLM 判定...
```

#### 設計の含意

- **「やり直し」 概念が存在しない** — R が変わっても旧 R は ledger に残り、 新 R は append される。 spec doc を書き直す ≠ ledger に積む
- **session を跨いで実装が継続する** — `commit-S` した λ body は DB に持続、 翌日 `restore-S` で復帰。 server.py 経由なら process 生存中 env そのまま
- **観測道具が ledger backed** — `rk-log` / `S-history` / `diff-S` / `diff-K` は memory ではなく DB を読む。 落ちても残る
- **「区切り」 は user が明示的に刻む** — `commit-artifact` を打った瞬間が rhythm point。 道具は強要しない、 user の判断

これは production-y な「コードを書く agent」 ではなく **「R/K 発見運動の盤」** という lispy 固有の位置付け。
ds4-agent / Claude Code が S の高速生成に最適化されてるのに対して、 lispy は R/K の判断密度を引き受ける所に居る。

### 落とし穴

実際に試して引っかかりやすい点:

- **λ は session を跨いで残らない (default)** — REPL を再起動したら `(lambda critique ...)` から打ち直し。`!lambdas` / `(lambdas)` で確認できる。 永続化したい λ は `(commit-S 'name "...")` で snapshot し、 翌日 `(restore-S 'name)` で bindings に戻す。 server.py 経由なら process 生存中 env そのまま (落とすまで)
- **`(critique ...)` `(rate ...)` などは README の例の名前** — 実環境では未定義。未束縛の symbol を関数呼び出しすると次のエラーで落ちる:
  ```
  main> (critique (turn "last-assistant"))
  eval error: not callable: Sym(critique)
  ```
  Lisp の伝統で「未束縛 symbol は symbol そのものとして返る」ため、`(critique ...)` の `critique` が `Symbol("critique")` のままになり、`_apply_callable` が「呼べない」と弾く (`lispy.py:914`)。`!lambdas` で `(none defined)` または目的の名前が無ければこの状態。先に `(lambda critique (x) "...")` を打って登録する
- **REPL の `[xxxxxxxx]` 8 文字 hex id は session ごとに別** — 他人 (や README) の id をそのまま打っても `not found`。自分の env の `!turns` / `!archive` / `(quoted)` で確認する
- **LLM lambda 実行中は system prompt が `BODY_SYSTEM` に切り替わる** — 「`(` で始まる行を書くな」「コードブロック禁止」等が強制される。コード生成 / 構造化出力には `prompt` を使うこと
- **LLM 応答が `(` で始まると auto-eval される** — meta form (`(renew ...)` 等) を LLM に駆動させるための仕組みだが、コード生成では困る。`prompt` ならこの auto-eval をスキップして生 text を返す
- **`Lambda` 再帰深度上限 5 (LLM) / 100 (Lisp)** — Lisp lambda の上限は深度 100。 普通の再帰なら届かないが、 ループ的に深く回したいときは `(recur ...)` を使う (`recur` は frame 再利用なので深度を消費しない)。 `debate` 等の LLM lambda call は別カウンタ (5) で、 n=3 までが安全圏
- **`(archive)` `(quoted)` は空が初期状態** — それぞれ `(renew ...)` / `(quote-turn ...)` で初めて埋まる
- **`(define x (quote-turn ...))` の REPL 表示は id だけ** — `:quote-turn → stored as ...` の status 行は出ない (define が Value から payload を unwrap するため)。`(quote-turn ...)` 単独で打てば status は出る

### Lisp 系譜での位置

Scheme R5RS の語彙を一部だけ拾った評価器に LLM lambda と agent 用 primitive を混ぜたもの。
- 取り入れ済み: `#t/#f` / Lisp-1 / `define` / `let` / `let*` / Scheme 標準の `apply` / quasiquote (`` ` `` `,` `,@`) / `defmacro` (非 hygienic、 Common Lisp 流) / `try-catch` / `set!` / box / 短絡 `and`/`or` / `&rest` 残余引数 / `match` macro (extras)
- TCO は **明示形 `(recur ...)`** (Clojure 流。 Scheme の auto TCO ではない、 相互再帰は不可)
- 未実装: continuation / hygienic macro / module system / proper TCO / numeric tower
- LLM lambda の `"body with {x}"` 形式は Scheme 標準ではない (lispy 独自のテンプレ記法)

### 内部構造 (lispy.py のキー定数)

| シンボル | 意味 |
|---|---|
| `Env` | 評価環境。turns / archive / bindings / macros / system / tools / db_conn 等を持つ |
| `Lambda` | `kind="llm"` or `"lisp"`、closure と captured bindings、 `rest_param` (`&rest`) |
| `Turn` | role / content / tool_calls / **logprobs**。 `llm-call` で 'logprobs #t を渡すと埋まる |
| `Box` / `LispError` / `_Recur` | mutable cell / first-class error 値 / TCO signal |
| `evaluate(tree, env)` | 中核評価関数、raw Python 値を返す。 macros lookup → special forms → 通常 apply |
| `eval_sexp(tree, env)` | 表層 wrapper、Value を返す (REPL 表示用)。 `_Recur` 漏れも error 化 |
| `_SEXP_DISPATCH` | renew / eval-turn / spawn / lambda の評価器メタ form を分岐 |
| `_SPECIAL_FORM_NOEVAL` | quote / if / define / let / begin / defmacro / quasiquote / try / set! / recur / and / or |
| `PRIMITIVES` | + - * / mod abs min max car cdr cons list 型述語 box error 等 |
| `LISPY_PRIMITIVES` + `EDIT_TOOL_SCHEMA` | agent から tool_call で叩ける 16 ツール (read 系 12 + 副作用系 4) |
| `BODY_SYSTEM` | LLM lambda 実行時の差し替え system prompt |

## nl: 日本語 → S 式 翻訳 REPL

`nl.py` は **lispy REPL + 日本語翻訳器** の薄い sidecar。 自然文を 1 度 LLM に通して
S 式に変換し、 そのまま評価器に流す。 lispy 全機能 (binding / tool / macro / meta) は
そのまま使える。 既存の `lispy.py` / `host.py` / `init.lispy` は触らない。

```bash
.venv/bin/python nl.py                # 対話 REPL (DB 記録 ON)
.venv/bin/python nl.py -e "5の階乗"     # ワンショット (記録なし)
```

`.env` の `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` が要る。 S 式直叩きと `!env` 系
の meta だけなら LLM 無しでも動く (client は最初の NL 入力で lazy 初期化)。

### 入力の振り分け

- `!...` — lispy の meta コマンド (`!env` `!turns` `!archive` `!quoted` `!lambdas` `!reset`) に委譲
- `@文` — NL 翻訳器を介さず `lispy.eval_` に直接渡す (= 本来の `agent-step` ルート、
  tool-calling 多段 loop)
- `(` で始まる — S 式として直接評価 (継続行は括弧バランスで自動)
- `"""` だけの行 — 次の `"""` 行までを **NL の複数行入力** として 1 つにまとめる
- その他平文 — `env.input_mode` が set されていればその lambda 経由 (lispy 互換)、
  そうでなければ LLM で S 式に翻訳して評価

### 例

```
nl> 1 から 10 まで足して
  ;; (define sum (fold + 0 (range 1 11)))
sum
55

nl> さっきの結果を 2 倍して
  ;; (define result 55)
(* 2 result)
110

nl> (+ 1 2 3)
6

nl> @富士山の標高は?
;; agent-step (lispy 本来の LLM 多段 loop) が走る
富士山の標高は 3,776 m です。
```

### 仕様メモ

- 翻訳器には全 (NL ↔ 生成 S 式 + 結果) を **履歴** として積み上げ、 LLM に渡す。
  「さっきの〜」 「もう一度〜」 系の参照を解決させるため。 個別ターンの eval 結果は
  600 文字で truncate (read-file 全文等の暴発防止)。 セッション通算の上限は設けない —
  鬱陶しくなったら `!reset` で env.turns と翻訳履歴を一緒にクリア。
- 直接 eval した S 式も履歴に入る (LLM が `fib` 等を再参照できる)。
- 副作用 tool (`shell` / `write_file` / `edit_file` / `append_file`) の prompt 禁止はしない —
  実行時の y/N 確認 (`edit.py`) に任せる。
- LLM が誤った S 式を返したり eval が落ちたりしても履歴に残す。 次のターンで自己修正できる。
- DB 記録は対話モードでのみ ON。 lispy REPL と同じ session 形式で host.db に書く
  (`host search` / `recall` で振り返れる)。

## server: HTTP で叩ける常駐 lispy

`server.py` は **env を抱えたまま落ちない lispy** を HTTP の薄い経路で外に出す sidecar。
Claude / nl REPL / 別の bash 端末 / curl から **同じ env を共有** できる。 「考える側」 (LLM / user)
と「状態を持つ側」(lispy env) を別プロセスに分離する設計。

```bash
.venv/bin/python server.py                       # 127.0.0.1:9000 で起動
.venv/bin/python server.py --port 9000 --yolo    # shell 確認 skip (常駐 process では実質必須)
.venv/bin/python server.py --session 1779360770  # 過去 session に append (prefix 一致)
.venv/bin/python server.py --stdin               # server と並列に stdin REPL も起動
```

### endpoints

| method | path | body / query | 用途 |
|---|---|---|---|
| GET | `/` | — | healthz: `{ok, bindings, tools, session_id}` |
| GET | `/bindings` | — | env binding 名の一覧 |
| GET | `/recall` | `?q=&k=5&mode=auto` | host の trajectory recall (FTS5 / trigram) |
| POST | `/eval` | `<S 式>` (raw text) | eval して `{ok, result, stdout, error?}` |
| POST | `/load` | `<file path>` | ファイルから read-all-sexp して全 form を eval |
| POST | `/reset` | — | env を作り直す (新 session id 発行) |

`POST /` は `POST /eval` の alias。

### 例

```bash
$ curl -s -X POST http://127.0.0.1:9000/eval -d '(define double (lambda (x) (* x 2)))'
{"ok": true, "result": "lambda double(x) [lisp]", "stdout": ""}

$ curl -s -X POST http://127.0.0.1:9000/eval -d '(double 21)'
{"ok": true, "result": "42", "stdout": ""}

$ curl -s http://127.0.0.1:9000/                # healthz
{"ok": true, "bindings": 160, "tools": 16, "session_id": "1779361030-..."}

$ curl -s "http://127.0.0.1:9000/recall?q=lispy&k=3"
{"ok": true, "result": "# recall: 3 hits (mode=fts) ..."}

$ curl -s -X POST http://127.0.0.1:9000/reset   # env 作り直し
{"ok": true, "session_id": "...", "bindings": 159}
```

### 仕様メモ

- evaluation は serialized (`threading.Lock`)。 同時 mutation は許さない
- stdout (`(print ...)` 等) は HTTP response の `stdout` field に捕捉される
- eval error は `result` に文字列で入って HTTP 200 で返る (handler は完走、 500 にしない)
- `--session <prefix>` は DB の既存 session を引き継ぐ。 turns が同じ sid に append される
  (bindings 復元は無し、 あくまで「記録の継続」)
- `--stdin` は server thread と並列に立つ。 ターミナルで `lispy>` プロンプトに S 式を打てる
- 副作用 tool (shell / write-file 等) の y/N 確認は **常駐 process では事実上ブロック** になるので
  `--yolo` 推奨

## ライセンス

`LICENSE` 参照。
