"""lispy — live-redefinable agent evaluator。

設計と実装と評価が同じ層で起きる場所。plan / implement の分離がない。
ウォーターフォールの対極。

核は agent loop が **S 式の binding** であること:

  (define agent-step
    (lambda (env input)
      (let ((env2 (append-turn env (make-turn "user" input))))
        (let ((response (llm-call env2)))
          (let ((env3 (append-turn env2 response)))
            (if (has-tool-calls? response)
                (agent-step ... "")
                response))))))

これは Python の関数ではなく lispy の bindings の 1 つ。
REPL で走らせ、結果を見て、(define agent-step ...) で書き換え、再度走らせる。
すべて同じ REPL 内で。Python の eval_ は agent-step に委譲するだけ。

Python に残る基盤 primitive (S 式から呼ぶ素朴な操作):
  llm-call          (env → Turn)              1 回 LLM を呼ぶ
  dispatch-tool     (name args-json → str)    1 つ tool を走らせる
  append-turn       (env turn → env)          env.turns に追加
  make-turn         (role content [tcid] → Turn)
  has-tool-calls?   (turn → bool)
  tool-calls        (turn → list of tc dicts)
  tool-call-name / tool-call-args / tool-call-id
  env               (symbol → 現 env オブジェクト)

host.py の関数を直接呼ぶ bridge:
  (current-time) (read-file p) (glob pat) (grep pat p) (list-dir p)
  (recall q) (recall-session sid) (task-list) (web-fetch url) (web-search q)

派生 (Lisp の上の例、 中心ではない):
  compose (pre-defined としては唯一)
  LLM λ ((lambda name (p) "body"))
  renew / quote-turn / eval-turn / eval-turn-pure / spawn / set-mode
  lens / debate / wrap / probe / transform-past / from-pack / condense などは
  user が必要に応じて自分で (define ...) する。 init では pre-define しない。

CLI:
  lispy                       REPL を起動
  lispy demo                  最小 demo
  lispy demo-lambda           λ 抽象のデモ
  lispy demo-compose          λ 合成のデモ
  lispy demo-compare          Lisp 決定性 vs LLM 揺らぎ
"""
from __future__ import annotations

import argparse
import json
import operator
import os
import random as _random
import re as _re
import sys
import time
import uuid
from functools import reduce
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# host から再利用するもの。同じディレクトリにある前提。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import host  # noqa: E402


# ---------------------------------------------------------------------------
# 型 — Expr, Env, Value
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """対話ログ 1 件。S 式の atom ではなく、env.turns / archive を構成するメタ要素。"""
    role: str
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    ts: float = field(default_factory=time.time)
    # tool_calls は assistant turn のみ。tool 結果は別 turn として残す。
    tool_calls: list[dict] = field(default_factory=list)
    # role=="tool" のときだけセット。OpenAI 形式の tool_call_id。
    tool_call_id: str = ""

    def to_message(self) -> dict:
        msg: dict = {"role": self.role, "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg


@dataclass
class Env:
    """評価環境。Lisp の env そのもの。

    turns は現在の context (history)。
    archive は過去の env を保管する場所 (id → list[Turn])。
    quoted は :quote されて評価を免除されている式 (turn_id → Turn)。
    lambdas は :lambda で定義された λ 抽象 (name → Lambda)。
    """
    system: str = ""
    turns: list[Turn] = field(default_factory=list)
    tools: dict[str, Callable[[dict, "Env"], str]] = field(default_factory=dict)
    tool_schema: list[dict] = field(default_factory=list)
    archive: dict[str, list[Turn]] = field(default_factory=dict)
    quoted: dict[str, Turn] = field(default_factory=dict)
    # 一般的な変数束縛 (Lisp の env そのもの)。primitive、define、lambda 全部ここ。
    bindings: dict[str, Any] = field(default_factory=dict)
    # 識別子。複数 env が並走するときに混同しないため。
    name: str = "main"
    # 再帰深度 (spawn 時に親から +1)。
    depth: int = 0
    # 永続記録: host DB / md に append する用 (lispy 起動時にセット)
    db_conn: Any = None
    record_sid: str = ""
    # λ 適用の入れ子深度 (compose / 自己再帰の暴走防止)。
    lambda_call_depth: int = 0
    # REPL の平文入力ぜんぶをこの λ に通す (set-mode で設定)。None なら素通し。
    input_mode: Any = None
    # マクロ定義 (defmacro)。bindings とは分離: 展開は evaluate 前、引数は非評価で渡る。
    macros: dict[str, "Lambda"] = field(default_factory=dict)

    def to_messages(self) -> list[dict]:
        msgs: list[dict] = []
        if self.system:
            msgs.append({"role": "system", "content": self.system})
        for t in self.turns:
            msgs.append(t.to_message())
        return msgs

    def __repr__(self) -> str:
        return (
            f"<Env {self.name} turns={len(self.turns)} archive={len(self.archive)} "
            f"bindings={len(self.bindings)} depth={self.depth}>"
        )

    def find_turn(self, turn_id: str) -> Turn | None:
        for t in self.turns:
            if t.id == turn_id:
                return t
        for archived in self.archive.values():
            for t in archived:
                if t.id == turn_id:
                    return t
        if turn_id in self.quoted:
            return self.quoted[turn_id]
        return None


@dataclass
class Lambda:
    """λ 抽象。kind により 2 種類:

    - kind="llm": body は文字列テンプレート。{param} を埋め込んで model に投げる。
                  返り値は model の応答テキスト。
    - kind="lisp": body は式 (list) の列。closure + 引数 binding で evaluate する。
                   返り値は最後の式の値 (raw Python).

    captured は定義時の env.bindings のスナップショット (静的 lexical scope)。
    呼び出し時に closure.bindings = captured + 引数束縛 に差し替え、終わったら元に戻す。
    """
    name: str
    params: list[str]
    body: Any  # str (LLM) or list[Any] (Lisp: 式の列)
    closure: "Env"
    captured: dict[str, Any] = field(default_factory=dict)
    kind: str = "llm"
    # &rest 残余引数の名前。 macro のみ対応 (regular lambda は固定 arity)。 "" なら無し。
    rest_param: str = ""

    def apply(self, args: list) -> Any:
        """LLM なら Value を、Lisp なら raw Python 値を返す。"""
        if self.kind == "llm":
            return self._apply_llm(args)
        return self._apply_lisp(args)

    def _apply_llm(self, args: list) -> "Value":
        if len(args) != len(self.params):
            return Value(text=f"apply {self.name} → arity mismatch (need {len(self.params)}, got {len(args)})")
        if self.closure.lambda_call_depth >= 5:
            return Value(text=f"apply {self.name} → lambda call depth limit (5)")
        # 引数は LLM 用に文字列化する
        str_args = [_to_lisp_string(a) for a in args]
        bindings = dict(zip(self.params, str_args))
        bindings["self"] = self.name
        try:
            instantiated = self.body.format(**bindings)
        except KeyError as e:
            return Value(text=f"apply {self.name} → missing param: {e}")
        prev_depth = self.closure.lambda_call_depth
        prev_system = self.closure.system
        self.closure.lambda_call_depth = prev_depth + 1
        self.closure.system = BODY_SYSTEM
        try:
            return eval_(self.closure, instantiated)
        finally:
            self.closure.lambda_call_depth = prev_depth
            self.closure.system = prev_system

    def _apply_lisp(self, args: list) -> Any:
        if len(args) != len(self.params):
            raise ValueError(f"arity mismatch: need {len(self.params)}, got {len(args)}")
        if self.closure.lambda_call_depth >= 100:
            raise RecursionError("lisp lambda call depth limit (100) — use (recur ...) for tail calls")
        saved_bindings = self.closure.bindings
        prev_depth = self.closure.lambda_call_depth
        self.closure.lambda_call_depth = prev_depth + 1
        try:
            # recur trampolining: body の評価結果が _Recur なら、 引数を rebind して loop。
            # スコープを layering: saved (call 時の global) → captured (def 時の closure) → params。
            # captured が後勝ちなので、closure で捕まえた変数は call 時 global に shadow されない。
            current_args = args
            while True:
                self.closure.bindings = {
                    **saved_bindings, **self.captured,
                    **dict(zip(self.params, current_args)),
                }
                result: Any = None
                for form in self.body:
                    result = evaluate(form, self.closure)
                if isinstance(result, _Recur):
                    if len(result.args) != len(self.params):
                        raise ValueError(
                            f"recur: arity mismatch (lambda {self.name} needs {len(self.params)}, "
                            f"got {len(result.args)})"
                        )
                    current_args = result.args
                    continue
                return result
        finally:
            self.closure.lambda_call_depth = prev_depth
            self.closure.bindings = saved_bindings

    def __repr__(self) -> str:
        return f"<Lambda {self.name}({', '.join(self.params)}) [{self.kind}]>"


@dataclass
class Value:
    """評価結果。最終 assistant text と、追加された turns、副作用フラグ。"""
    text: str = ""
    new_turns: list[Turn] = field(default_factory=list)
    # special form がメインループに伝える指示。None なら通常。
    directive: str | None = None
    payload: Any = None


class _Recur:
    """末尾呼び出し signal。 (recur a b c) が返す。

    nearest enclosing lambda の _apply_lisp が catch して引数を rebind し、 同じ body を loop し直す。
    末尾位置に置かないと、 args 評価 → 関数適用の途中で Python 例外になる (TypeError 等)。
    Lambda 境界を越えると、 内側 lambda が「自分宛て」 と誤認して吸う可能性がある (Clojure と同仕様)。
    """
    __slots__ = ("args",)

    def __init__(self, args: list):
        self.args = args

    def __repr__(self) -> str:
        return f"<recur {self.args!r}>"


@dataclass
class Box:
    """可変単一セル。 closure 経由で shared mutable state を持つときに使う。

    lispy の closure は call ごと bindings を {...captured, ...params} で再構築する snapshot 型。
    そのため (set! x 1) で局所変数を変えても次 call で見えない。 Box は object reference を渡すので、
    closure に捕まれた Box への (set-box! b v) は全 call 間で共有される。
    """
    value: Any = None

    def __repr__(self) -> str:
        return f"<box {self.value!r}>"


class LispError(Exception):
    """Lisp 側で扱える first-class エラー値。
    (try expr (catch (e) handler)) で捕捉、 (error "msg") で発生、 (error? v) で判定。
    Python 例外として raise されつつ、 catch されたあとは普通の値として binding に入る。
    """
    def __init__(self, message: str = "", tag: str = ""):
        super().__init__(message)
        self.message = str(message)
        self.tag = str(tag)

    def __repr__(self) -> str:
        if self.tag:
            return f"<error '{self.tag}': {self.message}>"
        return f"<error: {self.message}>"


# ---------------------------------------------------------------------------
# apply — 1 回のモデル呼び出し (= λ 適用)
# ---------------------------------------------------------------------------

BODY_SYSTEM = (
    "あなたは平文タスクを実行している。出力は **普通の自然言語のみ**。"
    "( で始まる行を一切書かない。コードブロック (```...```) も書かない。"
    "lambda / apply / quote / define / renew / eval-turn 等の記法、それを模した装飾、"
    "括弧で囲んだコマンドを **絶対に含めない**。"
    "結果だけを 1-3 文の平文で返す。"
)


SYSTEM_PROMPT = """lispy mode.

あなたは lispy REPL で動く agent。 ユーザーの依頼を実行するために以下を使い分ける:

**(1) tool_call を直接呼ぶ** — 副作用 / 観測のあるアクション全部:
  - 読み取り: read_file / list_dir / glob / grep / web_fetch / web_search / recall / recall_session
  - 書き込み / 実行: write_file / edit_file / append_file / shell
  - shell は git, ls, cat, build, test 等なんでも呼べる (危険コマンドは y/N で止まる)
  - 「git push できないか」 等の質問には、 まず shell の存在を前提に答える。 安易に「私にはできない」 と言わない

**(2) 平文の自然言語で答える** — 説明 / 助言 / 質問への回答:
  - lispy 記法を装飾として混ぜない。 コードブロックの中に lambda 等を書かない
  - 普通の日本語 / 英語で答える

**(3) S 式を出力するのは「評価器の環境を変える」 提案だけ** — REPL がそのまま評価する:
  - 新しい λ を定義: (lambda name (p) "body") / (define name (lambda (p) expr))
  - マクロ定義: (defmacro name (p) body)
  - 過去 turn 再評価: (eval-turn id)
  - env を切り直す: (renew "carry")
  - 既存 λ 呼び出し: (name arg1 arg2)

  (3) は副作用ではない (env 変更のみ)。 ファイル書き込みや shell は (3) ではなく (1)。

評価器の form 参考 (user が REPL で使う):
  (lambda name (p) "body")  (lambda (p) expr)  (define name expr)
  (defmacro name (p) body)  `x ,x ,@x  (macroexpand-1 'form)  (gensym)
  (try expr (catch (e) handler))  (error "msg")  (error? v)  (error-message e)
  (set! name expr)  (box v) (unbox b) (set-box! b v) (box? v)
  (recur a b ...)  — nearest lambda を tail call (stack 消費しない)
  (name arg ...)  (apply f arglist)  (compose f g)
  (quote expr)  (eval expr)  (if c t e)  (let ((x v)) body)
  (+ - * / = < >)  (list car cdr cons null?)
  (renew "carry")  (eval-turn id)  (spawn "task")
  (env) (turns) (archive) (lambdas) (quoted)
"""


def apply_(env: Env, max_tokens: int = 2048) -> Turn:
    """env を 1 回評価して、新しい assistant turn を返す。

    Lisp の `(apply lambda args)` に相当。env が λ の閉包、
    モデルが λ の body の評価器。
    """
    client = host.get_client()
    resp = client.chat.completions.create(
        model=host.MODEL,
        messages=env.to_messages(),
        tools=env.tool_schema or None,
        max_tokens=max_tokens,
        extra_body={"think": False},
    )
    msg = resp.choices[0].message
    content = msg.content or ""
    tool_calls_raw = getattr(msg, "tool_calls", None) or []
    tool_calls = [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments or "",
            },
        }
        for tc in tool_calls_raw
    ]
    return Turn(role="assistant", content=content, tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# eval_ — 評価器本体 (metacircular evaluator)
# ---------------------------------------------------------------------------

def eval_(env: Env, user_input: str, *, max_iters: int = 10) -> Value:
    """env に user_input を投げて評価する。

    流れ:
      0. user_input が S 式 ((...) で始まる) なら、モデルを介さず直接評価する
      1. それ以外は user turn を env に追加してモデル呼び出しループへ
      2. apply_ でモデルを呼ぶ
      3. assistant turn を見て:
         - text が S 式なら eval_sexp で評価
         - tool_calls があれば、primitive として dispatch して tool turn を追加 → 2 に戻る
         - どちらも無ければ終わり、Value を返す
    """
    # 0. user 入力が S 式なら直接評価 (model 経由しない)
    direct_tree = _try_read_sexp(user_input)
    if direct_tree is not None:
        return eval_sexp(direct_tree, env)

    # 平文入力は Lisp の agent-step に委譲する。
    # agent-step は (env input) → response の S 式。 走行中に redefine 可能。
    agent_step = env.bindings.get("agent-step")
    if not isinstance(agent_step, Lambda):
        return Value(text="(agent-step not defined — env initialization issue)")

    turns_before = len(env.turns)
    try:
        result = _apply_callable(agent_step, [env, user_input], env)
    except Exception as e:
        return Value(text=f"agent error: {e}")
    new_turns = env.turns[turns_before:]
    # 結果が Turn なら content を、それ以外はそのまま表示
    if isinstance(result, Turn):
        return Value(text=result.content or "", new_turns=list(new_turns))
    if isinstance(result, Value):
        result.new_turns = list(new_turns) + result.new_turns
        return result
    return Value(text=_to_lisp_string(result), new_turns=list(new_turns))


# ---------------------------------------------------------------------------
# S-expression: read / parse / eval
# ---------------------------------------------------------------------------


class Symbol:
    """unquoted トークン = 名前。文字列リテラルと区別するためのマーカー。"""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"Sym({self.name})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Symbol) and self.name == other.name

    def __hash__(self) -> int:
        return hash(("Symbol", self.name))


def _tokenize_sexp(s: str) -> list[str]:
    """S 式を token 列に分解。'"..."' は中身を保持したまま 1 token (quote 付き)。

    `;` から行末まではコメントとして無視する (Scheme / Common Lisp 慣用)。
    """
    tokens: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == ";":
            # 行末コメント: ; から \n まで読み飛ばす
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c in "()":
            tokens.append(c)
            i += 1
            continue
        # quote / quasiquote / unquote / unquote-splicing の reader 糖衣
        if c == "'":
            tokens.append("'")
            i += 1
            continue
        if c == "`":
            tokens.append("`")
            i += 1
            continue
        if c == ",":
            if i + 1 < n and s[i + 1] == "@":
                tokens.append(",@")
                i += 2
            else:
                tokens.append(",")
                i += 1
            continue
        if c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    nxt = s[j + 1]
                    if nxt == "n":
                        buf.append("\n")
                    elif nxt == "t":
                        buf.append("\t")
                    elif nxt in ('"', "\\"):
                        buf.append(nxt)
                    else:
                        buf.append(s[j])
                        buf.append(nxt)
                    j += 2
                else:
                    buf.append(s[j])
                    j += 1
            if j >= n:
                raise SyntaxError("unterminated string")
            tokens.append('"' + "".join(buf) + '"')
            i = j + 1
            continue
        # symbol
        j = i
        while j < n and not s[j].isspace() and s[j] not in '()"\'`,':
            j += 1
        tokens.append(s[i:j])
        i = j
    return tokens


_NUM_RE = _re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _try_parse_number(tok: str) -> Any:
    """数値リテラルなら int/float に、それ以外は None。"""
    if not _NUM_RE.match(tok):
        return None
    try:
        if "." in tok or "e" in tok or "E" in tok:
            return float(tok)
        return int(tok)
    except ValueError:
        return None


_READER_SUGAR = {
    "'": "quote",
    "`": "quasiquote",
    ",": "unquote",
    ",@": "unquote-splicing",
}


def _parse_tokens(tokens: list[str], i: int = 0) -> tuple[Any, int]:
    if i >= len(tokens):
        raise SyntaxError("unexpected EOF")
    tok = tokens[i]
    if tok == "(":
        node: list = []
        i += 1
        while i < len(tokens) and tokens[i] != ")":
            child, i = _parse_tokens(tokens, i)
            node.append(child)
        if i >= len(tokens):
            raise SyntaxError("missing )")
        return node, i + 1
    if tok == ")":
        raise SyntaxError("unexpected )")
    if tok in _READER_SUGAR:
        wrapped, j = _parse_tokens(tokens, i + 1)
        return [Symbol(_READER_SUGAR[tok]), wrapped], j
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1], i + 1
    # Scheme 風 boolean リテラル (Symbol ではなく真の bool として扱う)
    if tok == "#t" or tok == "#true":
        return True, i + 1
    if tok == "#f" or tok == "#false":
        return False, i + 1
    # nil literal → Python None (Symbol ではなく真の None として扱う)
    if tok == "nil":
        return None, i + 1
    num = _try_parse_number(tok)
    if num is not None:
        return num, i + 1
    return Symbol(tok), i + 1


def read_sexp(s: str) -> Any:
    tokens = _tokenize_sexp(s)
    if not tokens:
        raise SyntaxError("empty input")
    tree, _ = _parse_tokens(tokens)
    return tree


def read_all_sexp(s: str) -> list:
    """text を複数 top-level S 式の列としてパース。
    .lispy ファイル (複数 define が並ぶ) を load するのに使う。"""
    tokens = _tokenize_sexp(s)
    forms: list = []
    i = 0
    while i < len(tokens):
        tree, i = _parse_tokens(tokens, i)
        forms.append(tree)
    return forms


def _try_read_sexp(text: str) -> Any | None:
    """text が ( で始まる S 式ならパース、それ以外は None。"""
    s = text.strip()
    if not s.startswith("("):
        return None
    try:
        return read_sexp(s)
    except (SyntaxError, IndexError):
        return None


def _serialize_sexp(tree: Any) -> str:
    if isinstance(tree, str):
        escaped = tree.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + escaped + '"'
    if isinstance(tree, Symbol):
        return tree.name
    if isinstance(tree, list):
        return "(" + " ".join(_serialize_sexp(t) for t in tree) + ")"
    return str(tree)


def _unwrap_value(v: Any) -> Any:
    """_SEXP_DISPATCH 系が返す Value を、内部評価用の raw 値に剥がす。

    優先順:
      payload が dict で "result" キーを持つ → payload["result"]
      payload が dict 以外で非 None → payload (turn id 等の文字列が来る)
      それ以外 → text

    Value 以外 (Lambda / number / list / string) はそのまま返す。
    """
    if isinstance(v, Value):
        p = v.payload
        if isinstance(p, dict) and "result" in p:
            return p["result"]
        if p is not None:
            return p
        return v.text
    return v


def _atom_to_str(node: Any, env: Env) -> str:
    """式を文字列値に縮約。

    str リテラル → そのまま。
    Symbol → bindings に見つかれば値を文字列化、無ければ名前 (turn id を裸で書けるように)。
    list → eval_sexp で評価 → _unwrap_value で raw に剥がして文字列化。
    """
    if isinstance(node, str):
        return node
    if isinstance(node, Symbol):
        if node.name in env.bindings:
            v = _unwrap_value(env.bindings[node.name])
            return v if isinstance(v, str) else _to_lisp_string(v)
        return node.name
    if isinstance(node, list):
        raw = _unwrap_value(eval_sexp(node, env))
        return raw if isinstance(raw, str) else _to_lisp_string(raw)
    return str(node)


def eval_sexp(tree: Any, env: Env) -> Value:
    """S 式を評価する Value 返却 wrapper。

    LLM 系 form (_SEXP_DISPATCH) は Value をそのまま返す。
    Lisp 計算 (+, car, if, …) は raw Python 値を返してくるので _to_lisp_string で包む。
    """
    try:
        result = evaluate(tree, env)
    except Exception as e:
        return Value(text=f"eval error: {e}")
    if isinstance(result, _Recur):
        return Value(text="eval error: (recur ...) used outside of any lambda body")
    if isinstance(result, Value):
        return result
    if isinstance(result, Lambda):
        return Value(
            text=f"lambda {result.name}({', '.join(result.params)}) [{result.kind}]",
            payload={"value": result},
        )
    return Value(text=_to_lisp_string(result), payload={"value": result})


def sexp_lambda(env: Env, args: list) -> Value:
    """以下 2 形式を受ける:
      (lambda name (p1 p2 ...) body...)   名前付き、env.bindings[name] に登録
      (lambda (p1 p2 ...) body...)        無名、Lambda 値を返す

    body が単一文字列のみなら kind=llm、それ以外なら kind=lisp。
    Lisp body は式の列で、最後の値が返り値。
    """
    if not args:
        return Value(text='lambda: (lambda (params) body) or (lambda name (params) body)')
    if isinstance(args[0], Symbol) and len(args) >= 3 and isinstance(args[1], list):
        named = True
        name = args[0].name
        params_node = args[1]
        body_nodes = args[2:]
    elif isinstance(args[0], list):
        named = False
        name = "anon"
        params_node = args[0]
        body_nodes = args[1:]
    else:
        return Value(text="lambda: bad shape; expected name? + params-list + body")

    params: list[str] = []
    for p in params_node:
        if not isinstance(p, Symbol):
            return Value(text="lambda: params must be symbols")
        params.append(p.name)

    if not body_nodes:
        return Value(text=f"lambda {name}: empty body")

    # kind の決定
    if len(body_nodes) == 1 and isinstance(body_nodes[0], str):
        kind = "llm"
        body: Any = body_nodes[0]
    else:
        kind = "lisp"
        body = list(body_nodes)

    # 静的 lexical: 定義時点の bindings をスナップショット
    captured = dict(env.bindings)
    lam = Lambda(
        name=name, params=params, body=body,
        closure=env, captured=captured, kind=kind,
    )

    if named:
        env.bindings[name] = lam
        lam.captured[name] = lam  # 自己再帰サポート (named lambda 版)
    # 常に Lambda を返す (anonymous / named どちらも)。
    # eval_sexp 表層で表示用 Value に包む。
    return lam


def sexp_renew(env: Env, args: list) -> Value:
    """(renew) or (renew "carry text")"""
    carry = _atom_to_str(args[0], env) if args else ""
    return _form_renew(env, carry)


def sexp_eval_turn(env: Env, args: list) -> Value:
    """(eval-turn turn-id)"""
    if not args:
        return Value(text="eval-turn: (eval-turn id)")
    turn_id = _atom_to_str(args[0], env)
    return _form_eval_turn(env, turn_id)


def sexp_eval_turn_pure(env: Env, args: list) -> Value:
    """(eval-turn-pure turn-id) — eval-turn の副作用なし版。
    env.turns / archive / quoted / bindings を snapshot して、評価後に戻す。
    同じ問いを k 回サンプリングする probe 用途。"""
    if not args:
        return Value(text="eval-turn-pure: (eval-turn-pure id)")
    turn_id = _atom_to_str(args[0], env)
    return _form_eval_turn_pure(env, turn_id)


def _form_eval_turn_pure(env: Env, turn_id: str) -> Value:
    target = env.find_turn(turn_id)
    if target is None:
        return Value(text=f":eval-turn-pure → not found: {turn_id}")
    saved_turns = list(env.turns)
    saved_archive = dict(env.archive)
    saved_quoted = dict(env.quoted)
    saved_bindings = dict(env.bindings)
    try:
        re_input = target.content
        sub_value = eval_(env, re_input)
    finally:
        env.turns = saved_turns
        env.archive = saved_archive
        env.quoted = saved_quoted
        env.bindings = saved_bindings
    inner = sub_value.payload
    if isinstance(inner, dict) and "value" in inner:
        result_payload = inner["value"]
    elif inner is not None:
        result_payload = inner
    else:
        result_payload = sub_value.text
    return Value(
        text=f":eval-turn-pure {turn_id} → {sub_value.text[:200]}",
        directive="eval_turn_done",
        payload=result_payload,
    )


def sexp_spawn(env: Env, args: list) -> Value:
    """(spawn "task" ...) — child env で task を評価。"""
    if not args:
        return Value(text="spawn: (spawn task)")
    task = " ".join(_atom_to_str(a, env) for a in args)
    return _form_spawn(env, task)


def sexp_quote_turn(env: Env, args: list) -> Value:
    """(quote-turn arg) — arg を **評価せず** テキスト化して env.quoted に保管。

    str リテラル → そのまま。symbol → 名前。list → S 式の serialize。
    返り値の payload に新しい turn id を載せ、(eval-turn <id>) で後から再評価できる。
    保管された content が S 式形なら eval-turn 時に直接評価、平文なら LLM に再投入。
    """
    if not args:
        return Value(text='quote-turn: (quote-turn arg)  — arg は評価されない (quote 流儀)')
    arg = args[0]
    if isinstance(arg, str):
        content = arg
    elif isinstance(arg, Symbol):
        content = arg.name
    elif isinstance(arg, list):
        content = _serialize_sexp(arg)
    else:
        content = str(arg)
    q_turn = Turn(role="user", content=content)
    env.quoted[q_turn.id] = q_turn
    return Value(
        text=f":quote-turn → stored as {q_turn.id}",
        directive="quoted",
        payload=q_turn.id,
    )


_SEXP_DISPATCH: dict[str, Callable[[Env, list], Value]] = {
    "lambda": sexp_lambda,
    "renew": sexp_renew,
    "eval-turn": sexp_eval_turn,
    "eval-turn-pure": sexp_eval_turn_pure,
    "spawn": sexp_spawn,
    "quote-turn": sexp_quote_turn,
    # apply / compose / lambdas は Lisp 層に移譲。
    # quote / if / define / let は evaluate() の _SPECIAL_FORM_NOEVAL で捌く。
}


# ---------------------------------------------------------------------------
# Lisp core: evaluate, special forms, primitives
# ---------------------------------------------------------------------------


_TRUE = True
_FALSE = False


def _truthy(v: Any) -> bool:
    """Lisp 風 truthy: #f / None / 空 list / 空 string 以外は真。"""
    if v is False or v is None:
        return False
    if isinstance(v, list) and not v:
        return False
    return True


# 引数を評価しない special form。引数の生 tree を受け取って Python 値を返す。
def _sf_quote(env: Env, args: list) -> Any:
    if not args:
        return None
    return args[0]


def _sf_if(env: Env, args: list) -> Any:
    if len(args) < 2:
        return None
    cond = _unwrap_value(evaluate(args[0], env))
    if _truthy(cond):
        return evaluate(args[1], env)
    if len(args) >= 3:
        return evaluate(args[2], env)
    return None


def _sf_define(env: Env, args: list) -> Any:
    if len(args) != 2:
        raise ValueError("define: (define name expr)")
    name_node = args[0]
    if not isinstance(name_node, Symbol):
        raise ValueError("define: name must be a symbol")
    value = _unwrap_value(evaluate(args[1], env))
    env.bindings[name_node.name] = value
    # 自己再帰サポート: lambda の captured に自分自身を入れて、本体内で自己参照できるようにする。
    # 例) (define fact (lambda (n) (if ... (fact ...))))
    if isinstance(value, Lambda):
        value.captured[name_node.name] = value
        # 匿名 λ (sexp_lambda が name="anon" でつけたやつ) を define で名前付け
        if value.name == "anon":
            value.name = name_node.name
    return value


def _sf_let(env: Env, args: list) -> Any:
    """(let ((x 1) (y 2)) body...) — 局所束縛で body を順に評価、最後の値を返す。"""
    if len(args) < 2:
        raise ValueError("let: (let ((var expr) ...) body ...)")
    bindings_node = args[0]
    if not isinstance(bindings_node, list):
        raise ValueError("let: bindings must be a list")
    body = args[1:]
    saved: dict[str, Any] = {}
    introduced: set[str] = set()
    for b in bindings_node:
        if not (isinstance(b, list) and len(b) == 2 and isinstance(b[0], Symbol)):
            raise ValueError("let: each binding is (name expr)")
        name = b[0].name
        if name in env.bindings:
            saved[name] = env.bindings[name]
        else:
            introduced.add(name)
        env.bindings[name] = _unwrap_value(evaluate(b[1], env))
    try:
        result: Any = None
        for expr in body:
            result = evaluate(expr, env)
        return result
    finally:
        for name in introduced:
            env.bindings.pop(name, None)
        for name, v in saved.items():
            env.bindings[name] = v


def _sf_begin(env: Env, args: list) -> Any:
    """(begin e1 e2 ... en) — 式を順に評価し、最後の値を返す。副作用の sequence 用。"""
    result: Any = None
    for expr in args:
        result = evaluate(expr, env)
    return result


_MISSING = object()


def _sf_recur(env: Env, args: list) -> Any:
    """(recur a b c ...) — nearest enclosing lisp lambda を tail call。 stack を消費しない。

    引数の数は対象 lambda の params と一致する必要 (lambda 側でチェック)。
    必ず末尾位置で使う (let の binding 部や、 関数の引数の途中で使うと壊れる)。
    """
    evaluated = [_unwrap_value(evaluate(a, env)) for a in args]
    return _Recur(evaluated)


def _sf_set_bang(env: Env, args: list) -> Any:
    """(set! name expr) — 既存 binding を新しい値で更新。 未定義の場合はエラー。

    制限: lispy の closure は call 毎 bindings を snapshot 再構築するため、
    closure 内で captured な局所変数を set! しても call を跨いで保たれない。
    shared mutable state には (box ...) と (set-box! ...) を使う。
    """
    if len(args) != 2:
        raise ValueError("set!: (set! name expr)")
    name_node = args[0]
    if not isinstance(name_node, Symbol):
        raise ValueError("set!: name must be a symbol")
    if name_node.name not in env.bindings:
        raise ValueError(f"set!: undefined symbol: {name_node.name}")
    value = _unwrap_value(evaluate(args[1], env))
    env.bindings[name_node.name] = value
    return value


def _sf_try(env: Env, args: list) -> Any:
    """(try expr (catch (var) handler...)) — expr の評価中に発生した例外を捕捉。

    expr が成功すればその値を返す。 失敗すれば handler を、 var に error 値を bind して評価。
    LispError は型保存、 それ以外の Python 例外は LispError(str(e)) に wrap される。
    catch の var は handler のスコープに限る (let 風)。
    """
    if len(args) < 2:
        raise ValueError("try: (try expr (catch (var) handler...))")
    expr = args[0]
    catch_form = args[1]
    if not (isinstance(catch_form, list) and len(catch_form) >= 2
            and isinstance(catch_form[0], Symbol) and catch_form[0].name == "catch"
            and isinstance(catch_form[1], list) and len(catch_form[1]) == 1
            and isinstance(catch_form[1][0], Symbol)):
        raise ValueError("try: 2nd arg must be (catch (var) handler...)")
    var_name = catch_form[1][0].name
    handler_body = catch_form[2:]
    try:
        return _unwrap_value(evaluate(expr, env))
    except LispError as e:
        err_value: LispError = e
    except Exception as e:  # Python 例外も拾って Lisp の世界に持ち込む
        err_value = LispError(str(e))
    # handler を var binding 付きで評価 (let と同じ scope 制御)
    saved = env.bindings.get(var_name, _MISSING)
    env.bindings[var_name] = err_value
    try:
        result: Any = None
        for form in handler_body:
            result = evaluate(form, env)
        return result
    finally:
        if saved is _MISSING:
            env.bindings.pop(var_name, None)
        else:
            env.bindings[var_name] = saved


def _sf_defmacro(env: Env, args: list) -> Any:
    """(defmacro name (params...) body...) — マクロ定義。
    body は **展開後 S 式を返す** Lisp 式の列。引数は非評価 (生の tree) で渡る。
    展開結果はその場で再評価される。

    可変長引数: (defmacro name (a b &rest rest) ...) — 残余引数は rest に list で bind。
    &rest の後ろにちょうど 1 つ symbol を置く。
    """
    if len(args) < 3:
        raise ValueError("defmacro: (defmacro name (params) body...)")
    name_node = args[0]
    params_node = args[1]
    if not isinstance(name_node, Symbol):
        raise ValueError("defmacro: name must be a symbol")
    if not isinstance(params_node, list):
        raise ValueError("defmacro: params must be a list")
    params: list[str] = []
    rest_param = ""
    i = 0
    while i < len(params_node):
        p = params_node[i]
        if not isinstance(p, Symbol):
            raise ValueError("defmacro: params must be symbols")
        if p.name == "&rest":
            if i + 1 >= len(params_node) or i + 2 != len(params_node):
                raise ValueError("defmacro: &rest must be followed by exactly one symbol at the end")
            tail = params_node[i + 1]
            if not isinstance(tail, Symbol):
                raise ValueError("defmacro: &rest name must be a symbol")
            rest_param = tail.name
            break
        params.append(p.name)
        i += 1
    body = list(args[2:])
    macro = Lambda(
        name=name_node.name, params=params, body=body,
        closure=env, captured=dict(env.bindings), kind="lisp",
        rest_param=rest_param,
    )
    env.macros[name_node.name] = macro
    return name_node.name


def _sf_quasiquote(env: Env, args: list) -> Any:
    """`x の展開。 ,y で評価値を埋め込み、 ,@y で list を spread。 ネストは level で追跡。"""
    if not args:
        return None
    return _expand_quasiquote(args[0], env, level=1)


def _expand_quasiquote(form: Any, env: Env, level: int) -> Any:
    if not isinstance(form, list):
        return form
    if form and isinstance(form[0], Symbol):
        head = form[0].name
        if head == "unquote":
            if level == 1:
                return _unwrap_value(evaluate(form[1], env))
            return [Symbol("unquote"), _expand_quasiquote(form[1], env, level - 1)]
        if head == "quasiquote":
            return [Symbol("quasiquote"), _expand_quasiquote(form[1], env, level + 1)]
    result: list = []
    for item in form:
        if (isinstance(item, list) and item
                and isinstance(item[0], Symbol)
                and item[0].name == "unquote-splicing"
                and level == 1):
            spliced = _unwrap_value(evaluate(item[1], env))
            if isinstance(spliced, list):
                result.extend(spliced)
            else:
                result.append(spliced)
        else:
            result.append(_expand_quasiquote(item, env, level))
    return result


def _expand_macro(macro: "Lambda", raw_args: list, env: Env) -> Any:
    """マクロを非評価引数に対して適用し、展開された S 式を返す。"""
    fixed = len(macro.params)
    if macro.rest_param:
        if len(raw_args) < fixed:
            raise ValueError(
                f"macro {macro.name}: arity mismatch (need at least {fixed}, got {len(raw_args)})"
            )
        bound = dict(zip(macro.params, raw_args[:fixed]))
        bound[macro.rest_param] = list(raw_args[fixed:])
    else:
        if len(raw_args) != fixed:
            raise ValueError(
                f"macro {macro.name}: arity mismatch (need {fixed}, got {len(raw_args)})"
            )
        bound = dict(zip(macro.params, raw_args))
    saved = macro.closure.bindings
    macro.closure.bindings = {**saved, **macro.captured, **bound}
    prev_depth = macro.closure.lambda_call_depth
    macro.closure.lambda_call_depth = prev_depth + 1
    try:
        result: Any = None
        for form in macro.body:
            result = evaluate(form, macro.closure)
        return _unwrap_value(result)
    finally:
        macro.closure.lambda_call_depth = prev_depth
        macro.closure.bindings = saved


_SPECIAL_FORM_NOEVAL = {
    "quote": _sf_quote,
    "if": _sf_if,
    "define": _sf_define,
    "let": _sf_let,
    "begin": _sf_begin,
    "defmacro": _sf_defmacro,
    "quasiquote": _sf_quasiquote,
    "try": _sf_try,
    "set!": _sf_set_bang,
    "recur": _sf_recur,
}


_PROMPT_SYSTEM = (
    "ユーザーの指示に簡潔に従って応答する。前置き・説明・装飾は付けない。"
    "コードを求められたら、コードブロック (```...```) を使わず、コードそのものだけを返す。"
)


_FENCE_RE = _re.compile(r"^```[a-zA-Z0-9_+\-]*\s*\n?(.*?)\n?```$", _re.DOTALL)


def _strip_code_fences(s: str) -> str:
    """LLM 出力の markdown ``` フェンスを剥がす。剥がせない場合は trim だけして返す。"""
    s = str(s).strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    # 片側だけ ``` のケースは荒っぽく落とす
    return s.strip("`").strip()


def _prim_prompt_call(text: str) -> str:
    """LLM への素朴な 1 ショット呼び出し。lambda template / auto-eval を介さない。"""
    client = host.get_client()
    resp = client.chat.completions.create(
        model=host.MODEL,
        messages=[
            {"role": "system", "content": _PROMPT_SYSTEM},
            {"role": "user", "content": text},
        ],
        max_tokens=2048,
        extra_body={"think": False},
    )
    return resp.choices[0].message.content or ""


def _prim_div(a: Any, *rest: Any) -> Any:
    if not rest:
        return 1 / a
    denom = reduce(operator.mul, rest, 1)
    return a / denom


def _prim_sub(a: Any, *rest: Any) -> Any:
    if not rest:
        return -a
    return a - sum(rest)


def _prim_cons(x: Any, lst: Any) -> list:
    if isinstance(lst, list):
        return [x] + lst
    return [x, lst]  # improper pair → 2-element list


def _prim_car(lst: Any) -> Any:
    if not isinstance(lst, list) or not lst:
        raise ValueError("car: needs a non-empty list")
    return lst[0]


def _prim_set_box(box: Any, value: Any) -> Any:
    if not isinstance(box, Box):
        raise ValueError(f"set-box!: not a box: {box!r}")
    box.value = value
    return value


def _prim_cdr(lst: Any) -> Any:
    if not isinstance(lst, list) or not lst:
        raise ValueError("cdr: needs a non-empty list")
    return lst[1:]


def _prim_eval_factory(env: Env) -> Callable[..., Any]:
    """(eval expr) — quote された tree を今の env で評価する。

    env を closure に取るため factory にする。build_default_env で生成する。
    """
    def _eval(tree: Any) -> Any:
        return evaluate(tree, env)
    return _eval


def _prim_fold_factory(env: Env) -> Callable[..., Any]:
    """(fold f init lst) — 左畳み込み。acc を init から始めて (f acc x) を順に適用。"""
    def _fold(fn: Any, init: Any, lst: Any) -> Any:
        if not isinstance(lst, list):
            raise ValueError(f"fold: third arg must be a list, got {type(lst).__name__}")
        acc = init
        for item in lst:
            acc = _apply_callable(fn, [acc, item], env)
        return acc
    return _fold


def _prim_label_factory(env: Env) -> Callable[..., Any]:
    """(label sid) — LLM に session の title/keyphrases/tags を提案させて DB に書く。

    `host.label_session` を呼ぶだけ。 失敗 (LLM が JSON 返さない、 空 session、 etc.) は None。
    成功時は dict 風の text 表現を返す。 sid 省略時は env.record_sid を使う。
    """
    def _label(sid: Any = None) -> str:
        if env.db_conn is None:
            return "(label: DB が開いてない)"
        if sid is None or sid == "":
            sid = env.record_sid
        if not sid:
            return "(label: sid が無い、 引数か env.record_sid 必須)"
        sid_str = str(sid)
        try:
            resolved = host.resolve_session(env.db_conn, sid_str)
        except Exception as e:
            return f"(label: session not found: {sid_str}, {e})"
        try:
            proposed = host.label_session(env.db_conn, resolved)
        except Exception as e:
            return f"(label: 失敗: {e})"
        if proposed is None:
            return f"(label: LLM 提案失敗 or session が空: {resolved})"
        return (
            f"labeled {resolved}: title={proposed['title']!r} "
            f"keyphrases={proposed['keyphrases']} tags={proposed['tags']}"
        )
    return _label


def _prim_load_factory(env: Env) -> Callable[..., Any]:
    """(load "path.lispy") — ファイル内の全 S 式を順に evaluate。

    .lispy ファイルに `(define ...)` を並べておけば、ライブラリとして load できる。
    パス先のファイルが ; コメントを含んでも OK (_tokenize_sexp が読み飛ばす)。
    """
    def _load(path: Any) -> str:
        p = Path(str(path)).expanduser()
        if not p.exists():
            return f"(load: file not found: {p})"
        text = p.read_text(encoding="utf-8")
        try:
            forms = read_all_sexp(text)
        except SyntaxError as e:
            return f"(load: parse error in {p}: {e})"
        count = 0
        for tree in forms:
            try:
                evaluate(tree, env)
                count += 1
            except Exception as e:
                return f"(load: eval error at form {count + 1} in {p}: {e})"
        return f"(loaded {count} forms from {p})"
    return _load


def _prim_lookup_factory(env: Env) -> Callable[..., Any]:
    """(lookup name) — env.bindings[name] の値をそのまま返す (Lambda はオブジェクトとして)。

    `(lambdas)` が info 文字列を返すのに対し、これは λ 本体を取り出せる。
    mutate / wrap で既存 λ を decorator で包む用途。
    """
    def _lookup(name: Any) -> Any:
        if isinstance(name, Symbol):
            name = name.name
        name = str(name)
        if name not in env.bindings:
            raise ValueError(f"lookup: not bound: {name}")
        return env.bindings[name]
    return _lookup


def _prim_llm_call(env_arg: Any) -> Turn:
    """(llm-call env) — env.to_messages() を LLM に投げ、assistant Turn を返す。

    agent loop を S 式で書くための基盤。template 展開 / auto-eval / 履歴 append は **しない**。
    呼び出し側で `(append-turn env response)` を打つ責任がある (= loop の規則が S 式に出る)。
    """
    if not isinstance(env_arg, Env):
        raise ValueError(f"llm-call: expected env, got {type(env_arg).__name__}")
    client = host.get_client()
    resp = client.chat.completions.create(
        model=host.MODEL,
        messages=env_arg.to_messages(),
        tools=env_arg.tool_schema or None,
        max_tokens=2048,
        extra_body={"think": False},
    )
    msg = resp.choices[0].message
    content = msg.content or ""
    tool_calls_raw = getattr(msg, "tool_calls", None) or []
    tool_calls = [
        {
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments or ""},
        }
        for tc in tool_calls_raw
    ]
    return Turn(role="assistant", content=content, tool_calls=tool_calls)


def _prim_append_turn(env_arg: Any, turn: Any) -> Any:
    """(append-turn env turn) — env.turns に turn を append、env を返す。

    内部は mutation だが、user's draft の functional style と整合させるため env を return。
    `(let ((env2 (append-turn env t1))) ...)` のように env2 = env として使える。
    """
    if not isinstance(env_arg, Env):
        raise ValueError(f"append-turn: expected env, got {type(env_arg).__name__}")
    if isinstance(turn, Turn):
        env_arg.turns.append(turn)
    return env_arg


def _prim_dispatch_tool_factory(env: Env) -> Callable[..., Any]:
    """(dispatch-tool name args-json-string) — env.tools[name] を引数 dict で呼ぶ。"""
    def _dispatch(name: Any, args_json: Any = "{}") -> str:
        name = str(name)
        if isinstance(args_json, dict):
            args = args_json
        else:
            try:
                args = json.loads(str(args_json) or "{}")
            except json.JSONDecodeError:
                args = {}
        handler = env.tools.get(name)
        if handler is None:
            return f"(unknown tool: {name})"
        try:
            return handler(args, env)
        except Exception as e:
            return f"(error: {e})"
    return _dispatch


def _prim_env_messages_factory(env: Env) -> Callable[..., Any]:
    """(env-messages) — 現 env.to_messages() を返す。llm-call に渡す材料。"""
    def _msgs() -> list:
        return env.to_messages()
    return _msgs


def _prim_env_add_turn_factory(env: Env) -> Callable[..., Any]:
    """(env-add-turn! turn) — env.turns に append。返り値は同じ turn。"""
    def _add(turn: Any) -> Any:
        if isinstance(turn, Turn):
            env.turns.append(turn)
        return turn
    return _add


def _prim_archive_turns_factory(env: Env) -> Callable[..., Any]:
    """(archive-turns) または (archive-turns archive-id) — archive 内の turn content を list で返す。

    引数なしなら全 archive の turn を flat list で。id を指定するとその archive のみ。
    """
    def _archive_turns(archive_id: Any = None) -> list:
        if archive_id is None:
            out: list = []
            for turns in env.archive.values():
                out.extend(t.content for t in turns)
            return out
        if isinstance(archive_id, Symbol):
            archive_id = archive_id.name
        archive_id = str(archive_id)
        if archive_id not in env.archive:
            return []
        return [t.content for t in env.archive[archive_id]]
    return _archive_turns


def _host_bridge_factory(env: Env) -> dict[str, Callable[..., Any]]:
    """host.TOOL_DISPATCH の関数を Lisp positional 引数で呼べるように bridge する。

    各 primitive 呼び出し前に _TOOL_CTX を現 env (sid / cwd / mode) で更新する。
    """
    def _call(name: str, args: dict) -> str:
        host._TOOL_CTX["sid"] = env.record_sid or None
        host._TOOL_CTX["cwd"] = os.getcwd()
        host._TOOL_CTX.setdefault("in_subagent", False)
        host._TOOL_CTX.setdefault("mode", "yolo")
        fn = host.TOOL_DISPATCH.get(name)
        if fn is None:
            return f"(unknown tool: {name})"
        return fn(args)

    return {
        "current-time":   lambda: _call("current_time", {}),
        "list-dir":       lambda path: _call("list_dir", {"path": str(path)}),
        "read-file":      lambda path: _call("read_file", {"path": str(path)}),
        "glob":           lambda pat: _call("glob", {"pattern": str(pat)}),
        "grep":           lambda pat, path: _call("grep", {"pattern": str(pat), "path": str(path)}),
        "recall":         lambda q, k=5: _call("recall", {"query": str(q), "k": int(k)}),
        "recall-session": lambda sid: _call("recall_session", {"session_id": str(sid)}),
        "task-list":      lambda: _call("task_list", {}),
        "task-add":       lambda content: _call("task_add", {"content": str(content)}),
        "task-done":      lambda tid: _call("task_done", {"id": int(tid)}),
        "web-fetch":      lambda url: _call("web_fetch", {"url": str(url)}),
        "web-search":     lambda q, limit=10: _call("web_search", {"query": str(q), "limit": int(limit)}),
    }


def _file_append(path: Any, text: Any) -> str:
    """(file-append path text) — path にテキストを追記。末尾改行を自動補完。"""
    p = Path(str(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    s = str(text)
    if not s.endswith("\n"):
        s += "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(s)
    return f"(appended {len(s)} chars to {p})"


def _prim_set_mode_factory(env: Env) -> Callable[..., Any]:
    """(set-mode <lambda>) — REPL 平文入力を λ 経由でモデルに送るモードに切り替える。
    (set-mode) または (clear-mode) で解除。"""
    def _set_mode(mode_lambda: Any = None) -> str:
        if mode_lambda is None:
            env.input_mode = None
            return "(input_mode cleared)"
        if not isinstance(mode_lambda, Lambda):
            return f"(set-mode: expected lambda, got {type(mode_lambda).__name__})"
        env.input_mode = mode_lambda
        return f"(input_mode set: {mode_lambda.name})"
    return _set_mode


def _prim_clear_mode_factory(env: Env) -> Callable[..., Any]:
    def _clear() -> str:
        env.input_mode = None
        return "(input_mode cleared)"
    return _clear


def _prim_apply_factory(env: Env) -> Callable[..., Any]:
    """Lisp 標準 apply。

    (apply f arglist)         → f を arglist の要素に適用
    (apply f a b c)           → f(a, b, c)
    (apply f a b lst)         → f(a, b, *lst)  (Scheme 慣習)
    """
    def _apply(fn: Any, *rest: Any) -> Any:
        if not rest:
            return _apply_callable(fn, [], env)
        last = rest[-1]
        if isinstance(last, list):
            args = list(rest[:-1]) + list(last)
        else:
            args = list(rest)
        return _apply_callable(fn, args, env)
    return _apply


def _meta_factory(env: Env) -> dict[str, Callable[..., Any]]:
    """(env), (turns), (turn ...), (archive), (lambdas), (quoted) を返す primitive 群。"""
    def _coerce_int(v: Any) -> Any:
        """Symbol("-1") のような形で来た index を int に。失敗したら None。"""
        if isinstance(v, int):
            return v
        if isinstance(v, Symbol):
            v = v.name
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return None
        return None

    def turn_content(*args: Any) -> str:
        """turn の content を文字列で返す。多形:

          (turn)                    末尾 turn
          (turn "last")             同上
          (turn "last-user")        直近 user turn
          (turn "last-assistant")   直近 assistant turn
          (turn N)                  env.turns[N] (負も可)
          (turn <id>)               id で env.turns / archive / quoted から検索
          (turn "user" N)           user turn 列の N 番目 (負も可)
          (turn "assistant" N)      assistant turn 列の N 番目

        Symbol は .name に丸めて扱う (裸の id や role を quote なしで書けるように)。
        """
        # (turn role N) — 2 引数で role 内 index
        if len(args) == 2:
            role = args[0]
            if isinstance(role, Symbol):
                role = role.name
            role = str(role)
            idx = _coerce_int(args[1])
            if idx is None:
                return f"(turn: bad index: {args[1]!r})"
            filtered = [t for t in env.turns if t.role == role]
            if not filtered:
                return f"(no {role} turns)"
            try:
                return filtered[idx].content
            except IndexError:
                return f"({role} index out of range: {idx})"

        # (turn)
        if not args:
            return env.turns[-1].content if env.turns else "(no turns)"

        # 1 引数
        target = args[0]
        if isinstance(target, Symbol):
            target = target.name
        # 整数 index
        if isinstance(target, int):
            if not env.turns:
                return "(no turns)"
            try:
                return env.turns[target].content
            except IndexError:
                return f"(index out of range: {target})"
        if not isinstance(target, str):
            target = str(target)
        # 文字列 marker
        if target == "last":
            return env.turns[-1].content if env.turns else "(no turns)"
        if target == "last-user":
            for t in reversed(env.turns):
                if t.role == "user":
                    return t.content
            return "(no user turns)"
        if target == "last-assistant":
            for t in reversed(env.turns):
                if t.role == "assistant":
                    return t.content
            return "(no assistant turns)"
        # それ以外は id 解決
        found = env.find_turn(target)
        return found.content if found else f"(no turn: {target})"

    def env_info() -> str:
        lams = _lambda_entries(env)
        return (
            f"name={env.name} depth={env.depth} turns={len(env.turns)} "
            f"archive={len(env.archive)} quoted={len(env.quoted)} "
            f"bindings={len(env.bindings)} lambdas={len(lams)}"
        )

    def turns_info(n: int = 5) -> str:
        out = []
        for t in env.turns[-int(n):]:
            preview = t.content[:80].replace("\n", " ")
            out.append(f"[{t.id}] {t.role}: {preview}")
        return "\n".join(out) or "(empty)"

    def archive_info() -> str:
        if not env.archive:
            return "(empty)"
        out = []
        for aid, turns in env.archive.items():
            out.append(f"{aid}: {len(turns)} turns")
        return "\n".join(out)

    def lambdas_info() -> str:
        lams = _lambda_entries(env)
        if not lams:
            return "(none defined)"
        out = []
        for k, v in lams:
            body = (v.body if isinstance(v.body, str) else _to_lisp_string(v.body))[:80]
            body = body.replace("\n", " ")
            out.append(f"{k}({', '.join(v.params)}) [{v.kind}]: {body}")
        return "\n".join(out)

    def quoted_info() -> str:
        if not env.quoted:
            return "(empty)"
        out = []
        for qid, t in env.quoted.items():
            preview = t.content[:80].replace("\n", " ")
            out.append(f"[{qid}] {preview}")
        return "\n".join(out)

    return {
        "env": env_info,
        "turn": turn_content,
        "turns": turns_info,
        "archive": archive_info,
        "lambdas": lambdas_info,
        "quoted": quoted_info,
    }


PRIMITIVES: dict[str, Callable[..., Any]] = {
    "+": lambda *args: sum(args),
    "-": _prim_sub,
    "*": lambda *args: reduce(operator.mul, args, 1),
    "/": _prim_div,
    "=": lambda a, b: a == b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "and": lambda *args: all(_truthy(a) for a in args),
    "or": lambda *args: any(_truthy(a) for a in args),
    "not": lambda a: not _truthy(a),
    "list": lambda *args: list(args),
    "car": _prim_car,
    "cdr": _prim_cdr,
    "cons": _prim_cons,
    "null?": lambda lst: lst == [] or lst is None,
    "print": lambda *args: (print(*args), None)[1],
    # マクロ衛生用: 衝突しない一意の Symbol を作る。 (gensym) / (gensym "tmp")。
    "gensym": lambda prefix="g": Symbol(f"{prefix}__{uuid.uuid4().hex[:6]}"),
    # Lisp 側からエラーを発生 / 検査 / 取り出す。 try で catch される。
    # (error msg) または (error msg tag)。 既に LispError ならそのまま再 raise。
    "error": (lambda msg, tag="":
              (_ for _ in ()).throw(
                  msg if isinstance(msg, LispError) else LispError(str(msg), str(tag))
              )),
    "error?":         lambda v: isinstance(v, LispError),
    "error-message":  lambda e: e.message if isinstance(e, LispError) else "",
    "error-tag":      lambda e: e.tag if isinstance(e, LispError) else "",
    # 可変セル。 closure 経由で shared mutable state を持つ唯一の方法。
    "box":      lambda v=None: Box(v),
    "unbox":    lambda b: b.value if isinstance(b, Box) else b,
    "set-box!": _prim_set_box,
    "box?":     lambda v: isinstance(v, Box),
    # text → 構造: LLM 生成 S 式を tree 化。eval と組み合わせて metacircular に。
    "read-sexp": read_sexp,
    # 構造 → text: tree を Lisp S 式文字列に戻す (read-sexp の逆方向)。
    # lambda-body や quote した式を LLM に見せて (eval (read-sexp ...)) で取り込みたいとき必須。
    # _serialize_sexp は文字列を quote 付きで出すので round-trip 安全。
    "to-sexp": lambda tree: _serialize_sexp(tree),
    # LLM を 1 関数として呼ぶ低レベル primitive。
    # lambda template 展開なし / system swap なし / auto-eval なし、生 text を返す。
    "prompt": lambda text: _prim_prompt_call(str(text)),
    # markdown コードフェンス (```lang...```) を剥がす。prompt 結果の正規化に。
    "strip-code-fences": _strip_code_fences,
    # list を乱数で並べ替え (元の list は変更しない、 新しい list を返す)。
    "shuffle": lambda lst: _random.sample(lst, len(lst)) if isinstance(lst, list) else lst,
    # 文字列を separator で繋ぐ。string-append の list 版。
    "string-join": lambda sep, lst: str(sep).join(str(x) for x in (lst if isinstance(lst, list) else [lst])),
    # path に追記 (改行を補う)。log-probe 等の永続化に。
    "file-append": _file_append,
    # Lambda の内部情報 accessor (mutate / wrap で λ を素材として扱うため)
    "lambda-body":   lambda lam: lam.body if isinstance(lam, Lambda) else None,
    "lambda-params": lambda lam: list(lam.params) if isinstance(lam, Lambda) else [],
    "lambda-name":   lambda lam: lam.name if isinstance(lam, Lambda) else "",
    "lambda-kind":   lambda lam: lam.kind if isinstance(lam, Lambda) else "",
    # Turn の accessor & constructor (agent-step を S 式で書く基盤)
    # make-turn は 2 引数 (role, content) または 3 引数 (role, content, tool_call_id) で呼べる
    "make-turn":        lambda role, content, tcid="": Turn(
        role=str(role), content=str(content),
        tool_call_id=str(tcid) if tcid else "",
    ),
    "make-tool-turn":   lambda content, tcid: Turn(role="tool", content=str(content), tool_call_id=str(tcid)),
    "turn-role":        lambda t: t.role if isinstance(t, Turn) else "",
    "turn-content":     lambda t: t.content if isinstance(t, Turn) else "",
    "turn-tool-calls":  lambda t: list(t.tool_calls) if isinstance(t, Turn) else [],
    "tool-calls":       lambda t: list(t.tool_calls) if isinstance(t, Turn) else [],  # user's draft 名
    "turn-id":          lambda t: t.id if isinstance(t, Turn) else "",
    "has-tool-calls?":  lambda t: bool(t.tool_calls) if isinstance(t, Turn) else False,
    # tool_call (OpenAI 形式 dict) の accessor — args は JSON 文字列で返す
    "tool-call-id":     lambda tc: tc.get("id", "") if isinstance(tc, dict) else "",
    "tool-call-name":   lambda tc: tc.get("function", {}).get("name", "") if isinstance(tc, dict) else "",
    "tool-call-args":   lambda tc: tc.get("function", {}).get("arguments", "{}") if isinstance(tc, dict) else "{}",
    # message (OpenAI 形式 dict) constructor — llm-call に渡す素材
    "make-message":     lambda role, content: {"role": str(role), "content": str(content)},
    # 文字列述語 / 操作 — LLM 出力で if を切る、normalize する、つなぐ等
    "string?":            lambda x: isinstance(x, str),
    "string-contains?":   lambda s, sub: sub in s,
    "string-prefix?":     lambda s, p: s.startswith(p),
    "string-suffix?":     lambda s, p: s.endswith(p),
    "string-length":      lambda s: len(s),
    "string-upcase":      lambda s: s.upper(),
    "string-downcase":    lambda s: s.lower(),
    "string-trim":        lambda s: s.strip(),
    "string-append":      lambda *args: "".join(str(a) for a in args),
    "substring":          lambda s, start, end=None: s[start:end],
}


def evaluate(tree: Any, env: Env) -> Any:
    """raw Python 値を返す中核評価関数。

    Symbol → env.bindings から lookup (見つからなければ Symbol のまま)
    list → 先頭が特殊形式なら _SPECIAL_FORM_NOEVAL、
           Lisp form (lambda/apply/...) なら _SEXP_DISPATCH (Value 返却),
           それ以外は (operator arg1 ...) として通常関数適用 (引数を全部 evaluate)。
    """
    if isinstance(tree, (int, float, bool)):
        return tree
    if isinstance(tree, str):
        return tree
    if isinstance(tree, Symbol):
        if tree.name in env.bindings:
            return env.bindings[tree.name]
        return tree
    if not isinstance(tree, list):
        return tree
    if not tree:
        return []
    head = tree[0]
    if isinstance(head, Symbol):
        # マクロ展開を special form より先に試す。展開結果は再評価する。
        macro = env.macros.get(head.name)
        if macro is not None:
            expanded = _expand_macro(macro, tree[1:], env)
            return evaluate(expanded, env)
        # Lisp 標準 special form (引数を評価しない)
        sf = _SPECIAL_FORM_NOEVAL.get(head.name)
        if sf is not None:
            return sf(env, tree[1:])
        # 評価器メタ form (LLM 系) — Value を返すので包んで Python 値へ
        meta = _SEXP_DISPATCH.get(head.name)
        if meta is not None:
            v = meta(env, tree[1:])
            return v
    # 通常の関数適用: head を評価し、args を全評価し、適用
    # 引数が _SEXP_DISPATCH 由来の Value なら raw に剥がしてから関数へ渡す
    fn = evaluate(head, env)
    args_vals = [_unwrap_value(evaluate(a, env)) for a in tree[1:]]
    return _apply_callable(fn, args_vals, env)


def _apply_callable(fn: Any, args: list, env: Env) -> Any:
    if isinstance(fn, Lambda):
        return _unwrap_value(fn.apply(args))
    if callable(fn):
        try:
            return fn(*args)
        except LispError:
            raise  # try で catch できるよう型を保つ
        except Exception as e:
            raise ValueError(f"primitive error: {e}")
    raise ValueError(f"not callable: {fn!r}")


def _to_lisp_string(val: Any) -> str:
    """Lisp 値 → 文字列表現。LLM や print に渡すとき使う。"""
    if isinstance(val, str):
        return val
    if val is True:
        return "#t"
    if val is False:
        return "#f"
    if val is None:
        return "nil"
    if isinstance(val, Symbol):
        return val.name
    if isinstance(val, LispError):
        return repr(val)  # <error 'tag': message> 形式
    if isinstance(val, Box):
        return f"<box {_to_lisp_string(val.value)}>"
    if isinstance(val, list):
        return "(" + " ".join(_to_lisp_string(x) for x in val) + ")"
    return str(val)


def _form_renew(env: Env, carry: str) -> Value:
    """現 env の turns を archive に退避し、turns を空にする。

    動作の流れ:
      1. 現在の DB session を LLM が label (title / keyphrases / tags を書き込み)
      2. DB session を close、 新しい DB session を open
      3. env.turns を archive に退避、 turns を空に
      4. carry が指定されれば system message として 1 件だけ注入

    label は best-effort: 失敗しても renew は続行する。
    """
    # (1) 現セッションを label (env.turns を直接渡す。 DB に未反映の assistant turn も含める)
    label_text = ""
    if env.record_sid and env.db_conn is not None and env.turns:
        try:
            proposed = host.label_session(env.db_conn, env.record_sid, turns=env.turns)
            if proposed is not None:
                label_text = (
                    f"\n  title:      {proposed['title']}"
                    f"\n  keyphrases: {' / '.join(proposed['keyphrases'])}"
                    f"\n  tags:       {' / '.join(proposed['tags'])}"
                )
        except Exception as e:
            label_text = f"\n  (label 失敗: {e})"

    # (2) DB session boundary
    if env.record_sid and env.db_conn is not None:
        try:
            host.close_session(env.db_conn, env.record_sid)
            env.record_sid = host.open_session(env.db_conn)
            host.log_meta(env.db_conn, "lispy_open", sid=env.record_sid, payload="(after renew)")
        except Exception:
            pass

    # (3) in-memory archive
    archive_id = uuid.uuid4().hex[:8]
    env.archive[archive_id] = list(env.turns)
    env.turns = []

    # (4) carry
    if carry:
        env.turns.append(Turn(
            role="system",
            content=f"[carry from archive {archive_id}]: {carry}",
        ))

    return Value(
        text=f":renew → archived as {archive_id} (carry: {len(carry)} chars){label_text}",
        directive="renewed",
        payload=archive_id,
    )


def _form_eval_turn(env: Env, turn_id: str) -> Value:
    """archive または quoted の turn を、今の env で再評価する。

    これが Lisp の eval そのもの。データとして保管された式を、
    現在の環境というインタプリタで走らせ直す。

    content が S 式 ('(' 始まり) なら直接 Lisp 評価。
    それ以外は LLM に prefix 付きで再投入する。
    """
    target = env.find_turn(turn_id)
    if target is None:
        return Value(text=f":eval-turn → not found: {turn_id}")
    if _try_read_sexp(target.content) is not None:
        re_input = target.content
    else:
        re_input = f"[re-evaluating turn {turn_id} from prior context]\n{target.content}"
    sub_value = eval_(env, re_input)
    # payload は評価の結果値 (id ではなく)。eval_sexp が {"value": raw} で包んでくる場合と
    # eval_ が plain text を返す場合がある。前者は raw を、後者は text を採用。
    inner = sub_value.payload
    if isinstance(inner, dict) and "value" in inner:
        result_payload = inner["value"]
    elif inner is not None:
        result_payload = inner
    else:
        result_payload = sub_value.text
    return Value(
        text=f":eval-turn {turn_id} → {sub_value.text[:200]}",
        new_turns=sub_value.new_turns,
        directive="eval_turn_done",
        payload=result_payload,
    )


def _form_spawn(env: Env, task: str) -> Value:
    """新しい child env を作って task を評価させる。subagent 相当だが env が独立。

    depth 制限のみ。再帰禁止フラグは使わない。
    """
    if env.depth >= 3:
        return Value(text=f":spawn → depth limit ({env.depth}) reached")
    child = Env(
        system=env.system,
        tools=env.tools,
        tool_schema=env.tool_schema,
        name=f"{env.name}/spawn",
        depth=env.depth + 1,
    )
    sub_value = eval_(child, task)
    return Value(
        text=f":spawn[{child.name}] → {sub_value.text}",
        directive="spawned",
        payload={"child_name": child.name, "result": sub_value.text},
    )


# ---------------------------------------------------------------------------
# primitive — host の TOOL_DISPATCH を wrap して env を受け取る形に
# ---------------------------------------------------------------------------

def _wrap_agent_tool(name: str) -> Callable[[dict, Env], str]:
    """host の tool 関数は (args) しか取らないので、env を捨てて呼ぶ。"""
    fn = host.TOOL_DISPATCH[name]

    def wrapped(args: dict, env: Env) -> str:
        # 現 env の状態を _TOOL_CTX に反映してから handler を呼ぶ。
        # 上書きするのは sid と cwd だけ。mode / in_subagent は呼び出し側の指定を尊重する。
        host._TOOL_CTX["sid"] = env.record_sid or None
        host._TOOL_CTX["cwd"] = os.getcwd()
        host._TOOL_CTX.setdefault("in_subagent", False)
        host._TOOL_CTX.setdefault("mode", "yolo")
        return fn(args)

    return wrapped


# lispy で使う primitive subset。session_new / session_close は special form に置き換えたので除外。
LISPY_PRIMITIVES = [
    "current_time", "list_dir", "read_file", "glob", "grep",
    "recall", "recall_session", "web_fetch", "web_search",
    "task_list", "task_add", "task_done",
]


def _wrap_edit_tool(name: str, dispatch: dict) -> Callable[[dict, Env], str]:
    """edit.EDIT_TOOL_DISPATCH 用 wrapper。 host と違い _TOOL_CTX を触らない (副作用は edit.py 内で自己完結)。"""
    fn = dispatch[name]
    def wrapped(args: dict, env: Env) -> str:
        return fn(args)
    return wrapped


def _build_tool_layer() -> tuple[dict[str, Callable[[dict, Env], str]], list[dict]]:
    """host.TOOL_DISPATCH と edit.EDIT_TOOL_DISPATCH を統合して tool layer を組む。
    host = read 専門、 edit = write/shell。 同じ tool_schema 層に並べて agent に渡す。"""
    tools: dict[str, Callable[[dict, Env], str]] = {}
    schema: list[dict] = []
    schema_by_name = {s["function"]["name"]: s for s in host.TOOL_SCHEMA}
    for name in LISPY_PRIMITIVES:
        if name not in host.TOOL_DISPATCH:
            continue
        tools[name] = _wrap_agent_tool(name)
        if name in schema_by_name:
            schema.append(schema_by_name[name])
    # edit.py を optional に取り込む。 import 失敗時は read-only のままで動作。
    try:
        import edit as _edit  # noqa: E402
        for s in _edit.EDIT_TOOL_SCHEMA:
            name = s["function"]["name"]
            if name not in _edit.EDIT_TOOL_DISPATCH:
                continue
            tools[name] = _wrap_edit_tool(name, _edit.EDIT_TOOL_DISPATCH)
            schema.append(s)
    except ImportError:
        pass
    return tools, schema


def _install_meta_primitives(env: Env) -> None:
    """env を closure に持つ primitive を bindings に注入。

    eval / apply / fold / lookup / load — Lisp 計算 + env を closure に取る primitive
    env / turn / turns / archive / lambdas / quoted — メタ確認
    set-mode / clear-mode — REPL 平文入力の経路変更
    compose — pre-defined な FP idiom
    agent-step — 評価器の loop 本体 (S 式の binding として置く)
    """
    env.bindings["eval"] = _prim_eval_factory(env)
    env.bindings["apply"] = _prim_apply_factory(env)
    env.bindings["fold"] = _prim_fold_factory(env)
    env.bindings["lookup"] = _prim_lookup_factory(env)
    env.bindings["load"] = _prim_load_factory(env)
    env.bindings["label"] = _prim_label_factory(env)
    env.bindings["archive-turns"] = _prim_archive_turns_factory(env)
    env.bindings["set-mode"] = _prim_set_mode_factory(env)
    env.bindings["clear-mode"] = _prim_clear_mode_factory(env)
    # マクロ展開 1 段だけ。 debug 用。 (macroexpand-1 '(my-macro x y)) → 展開後の tree。
    def _macroexpand_1(form: Any, _env: Env = env) -> Any:
        if (isinstance(form, list) and form
                and isinstance(form[0], Symbol)
                and form[0].name in _env.macros):
            return _expand_macro(_env.macros[form[0].name], form[1:], _env)
        return form
    env.bindings["macroexpand-1"] = _macroexpand_1
    # agent-step を S 式で書く基盤
    env.bindings["llm-call"]      = _prim_llm_call
    env.bindings["append-turn"]   = _prim_append_turn
    env.bindings["dispatch-tool"] = _prim_dispatch_tool_factory(env)
    # 旧 API (互換のため残す): env-messages / env-add-turn! は (env-messages env) / (env-add-turn! env t) でも使える
    env.bindings["env-messages"]  = lambda e=env: e.to_messages() if isinstance(e, Env) else env.to_messages()
    env.bindings["env-add-turn!"] = lambda e, t=None: _prim_append_turn(e, t) if t is not None else _prim_append_turn(env, e)
    env.bindings.update(_meta_factory(env))
    # env そのものを Lisp 値として exposing。symbol `env` → env オブジェクト。
    # 元の (env) info 文字列は `(env-info)` に rename。
    env.bindings["env-info"] = env.bindings.pop("env", lambda: "(env removed)")
    env.bindings["env"] = env
    # host の DB / file / web tool 群を Lisp primitive として直接公開
    env.bindings.update(_host_bridge_factory(env))
    # edit.py (副作用系: write-file / edit-file / shell / append-file) を optional import。
    # ファイル削除しても lispy core は動く。
    try:
        import edit  # noqa: E402
        edit.install_primitives(env)
        # session 中の yolo toggle / 状態確認。 起動時 --yolo と同じ flag を触る。
        env.bindings["set-yolo"] = edit.set_yolo
        env.bindings["yolo?"]    = edit.get_yolo
    except ImportError:
        pass

    # --- Lisp 派生定義 ---
    # compose: 古典的な関数合成。FP の universal idiom として、これだけは pre-define。
    evaluate(read_sexp("(define compose (lambda (f g) (lambda (x) (g (f x)))))"), env)
    # --- agent loop の本体を S 式の binding として置く ---
    # この agent-step が lispy の核。
    # 走行中に (define agent-step (lambda (env input) ...)) で書き換え可能。
    # REPL の平文入力は eval_ から (agent-step env input) で呼ばれる。
    evaluate(read_sexp(
        "(define agent-step"
        " (lambda (env input)"
        "  (let ((env2 (append-turn env (make-turn \"user\" input))))"
        "   (let ((response (llm-call env2)))"
        "    (let ((env3 (append-turn env2 response)))"
        "     (if (has-tool-calls? response)"
        "         (agent-step"
        "           (fold (lambda (e tc)"
        "                   (append-turn e"
        "                     (make-turn \"tool\""
        "                       (dispatch-tool (tool-call-name tc)"
        "                                      (tool-call-args tc))"
        "                       (tool-call-id tc))))"
        "                 env3"
        "                 (tool-calls response))"
        "           \"\")"
        "         response))))))"
    ), env)


def _open_recording(env: Env) -> None:
    try:
        env.db_conn = host.init_db(host.DB_PATH)
        env.record_sid = host.open_session(env.db_conn)
        host.log_meta(env.db_conn, "lispy_open", sid=env.record_sid, payload="")
    except Exception as e:
        print(f"  (warning: recording disabled: {e})", file=sys.stderr)
        env.db_conn = None
        env.record_sid = ""


def build_default_env(record: bool = True) -> Env:
    tools, schema = _build_tool_layer()
    env = Env(
        system=SYSTEM_PROMPT,
        tools=tools,
        tool_schema=schema,
        bindings=dict(PRIMITIVES),
        name="main",
    )
    _install_meta_primitives(env)
    if record:
        _open_recording(env)
    return env


def _record(env: Env, role: str, content: str) -> None:
    """host DB と日付別 md の両方に append する。env.record_sid が空なら no-op。"""
    if not env.record_sid or env.db_conn is None or not content:
        return
    try:
        host.append_turn(env.db_conn, env.record_sid, role, content, cwd=os.getcwd())
        host.append_to_date_md(env.record_sid, role, content, cwd=os.getcwd())
    except Exception as e:
        # 記録失敗で REPL 自体は止めない
        print(f"  (record error: {e})", file=sys.stderr)


def close_recording(env: Env) -> None:
    if env.db_conn is not None and env.record_sid:
        try:
            host.close_session(env.db_conn, env.record_sid)
            host.log_meta(env.db_conn, "lispy_close", sid=env.record_sid, payload="")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# REPL — Lisp の REPL そのもの。read → eval → print → loop。
# ---------------------------------------------------------------------------

def _parens_balanced(s: str) -> bool:
    """( と ) の対応がとれているか。string literal 内は無視する。"""
    depth = 0
    in_str = False
    escape = False
    for c in s:
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return True  # 余計な ) は parser 側で落としてもらう
    return depth == 0


def repl() -> None:
    print("lispy REPL  (Ctrl+D to exit)")
    try:
        import edit as _edit_banner
        if _edit_banner.get_yolo():
            print("⚠️  YOLO mode — 副作用 tool の y/N 確認は skip されます。 (set-yolo #f) で戻す")
    except ImportError:
        pass
    print("Lisp core:  + - * / = < > and or not  if  define  let  quote  eval  apply")
    print("            list  car  cdr  cons  null?  fold  compose (derived)")
    print("            string-contains? string-prefix? string-append substring …")
    print("            read-sexp  prompt  strip-code-fences")
    print("LLM lambda: (lambda name (p) \"body\")   apply: (f x)")
    print("Macros:     (defmacro name (p) body)   `x ,x ,@x  (macroexpand-1 'form) (gensym)")
    print("Errors:     (try expr (catch (e) handler))  (error \"msg\")  (error? v) (error-message e)")
    print("Mutable:    (set! name expr)   (box v) (unbox b) (set-box! b v) (box? v)")
    print("TCO:        (recur a b ...)  — nearest lambda を tail call、 stack 消費しない")
    print("Env meta:   (env) (turns) (turn \"last-assistant\") (archive) (lambdas) (quoted)")
    print("            (renew \"carry\")  (quote-turn expr)  (eval-turn id)  (spawn \"task\")")
    print("Higher:     (set-mode <lambda>)  (clear-mode)  — 平文入力を λ 経由に")
    print("            (eval-turn-pure id)  — env を汚さず再評価 (probe 用素材)")
    print("REPL meta:  !env !archive !quoted !lambdas !turns !reset")
    print("plain text → model. S-expr input → direct evaluation.")
    env = build_default_env()
    if env.record_sid:
        print(f"(recording to session {env.record_sid[:12]})")

    try:
        while True:
            try:
                line = input(f"{env.name}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue

            # meta コマンド — evaluator の状態を覗く道具
            if line.startswith("!"):
                _handle_meta(line, env)
                continue

            # 複数行 S 式: ( で始まって閉じ括弧が足りない間は継続行を読む
            if line.startswith("(") and not _parens_balanced(line):
                buf = [line]
                aborted = False
                while True:
                    try:
                        cont = input("... ").rstrip()
                    except (EOFError, KeyboardInterrupt):
                        print("\n(input aborted)")
                        aborted = True
                        break
                    buf.append(cont)
                    joined = "\n".join(buf)
                    if _parens_balanced(joined):
                        line = joined
                        break
                if aborted:
                    continue

            # 記録: user 入力
            _record(env, "user", line)

            # input_mode が設定されていて平文 (S 式でない) なら、その λ を経由してモデルへ
            if env.input_mode is not None and not line.startswith("("):
                try:
                    mode_result = env.input_mode.apply([line])
                    if isinstance(mode_result, Value):
                        value = mode_result
                    else:
                        value = Value(text=_to_lisp_string(mode_result))
                except Exception as e:
                    print(f"  mode error: {e}", file=sys.stderr)
                    continue
            else:
                # 通常の式評価
                try:
                    value = eval_(env, line)
                except Exception as e:
                    print(f"  eval error: {e}", file=sys.stderr)
                    continue

            print(value.text)
            if value.directive:
                print(f"  [directive: {value.directive}]")
            # 記録: assistant 応答 (text があるとき)
            _record(env, "assistant", value.text or "")
    finally:
        close_recording(env)


def _lambda_entries(env: Env) -> list[tuple[str, "Lambda"]]:
    return [(k, v) for k, v in env.bindings.items() if isinstance(v, Lambda)]


def _handle_meta(line: str, env: Env) -> None:
    cmd = line[1:].strip()
    if cmd == "env":
        lams = _lambda_entries(env)
        print(f"name={env.name}  depth={env.depth}  turns={len(env.turns)}  "
              f"archive={len(env.archive)}  quoted={len(env.quoted)}  "
              f"bindings={len(env.bindings)}  lambdas={len(lams)}  "
              f"lambda_depth={env.lambda_call_depth}")
    elif cmd == "archive":
        if not env.archive:
            print("(empty)")
            return
        for aid, turns in env.archive.items():
            print(f"  {aid}: {len(turns)} turns")
            for t in turns[-2:]:
                preview = t.content[:60].replace("\n", " ")
                print(f"    [{t.id}] {t.role}: {preview}")
    elif cmd == "quoted":
        if not env.quoted:
            print("(empty)")
            return
        for qid, t in env.quoted.items():
            preview = t.content[:80].replace("\n", " ")
            print(f"  [{qid}] {preview}")
    elif cmd == "lambdas":
        lams = _lambda_entries(env)
        if not lams:
            print("(none defined)")
            return
        for name, lam in lams:
            body_preview = (lam.body if isinstance(lam.body, str) else _to_lisp_string(lam.body))[:80]
            body_preview = body_preview.replace("\n", " ")
            print(f"  {name}({', '.join(lam.params)}) [{lam.kind}]: {body_preview}")
    elif cmd == "turns":
        for t in env.turns[-5:]:
            preview = t.content[:80].replace("\n", " ")
            print(f"  [{t.id}] {t.role}: {preview}")
    elif cmd == "reset":
        env.turns.clear()
        print("(turns cleared)")
    else:
        print(f"unknown meta: !{cmd}")


# ---------------------------------------------------------------------------
# demo — Lisp 的振る舞いの最小デモ
# ---------------------------------------------------------------------------

def demo() -> None:
    print("=== demo: renew → eval-turn の再評価サイクル ===\n")
    env = build_default_env()

    print(">>> 「素数とは何か」を聞く")
    v1 = eval_(env, "素数とは何か、一行で答えて")
    print(f"<<< {v1.text}\n")

    print(">>> (renew \"...\") で env を切る")
    v2 = eval_(env, '(renew "前のテーマは数論")')
    print(f"<<< {v2.text}\n")

    print(">>> 新しい env で archive の中の過去 turn を見せる")
    if env.archive:
        archive_id = list(env.archive.keys())[0]
        archived_turns = env.archive[archive_id]
        for t in archived_turns:
            print(f"  archived [{t.id}] {t.role}: {t.content[:60]}")
        last_assistant = next(
            (t for t in reversed(archived_turns) if t.role == "assistant"),
            None,
        )
        if last_assistant:
            print(f"\n>>> (eval-turn {last_assistant.id}) で過去の発話を再評価")
            v3 = eval_(env, f"(eval-turn {last_assistant.id})")
            print(f"<<< {v3.text}\n")


def demo_lambda() -> None:
    """λ 抽象のデモ。critique を 3 対象に適用、空引数 λ を 3 回呼んで揺らぎを観察する。"""
    print("=== demo-lambda: λ 抽象による評価 ===\n")
    env = build_default_env()

    print(">>> critique という λ を定義")
    v1 = eval_(env, '(lambda critique (x) "次の主張を3つの観点で短く批判せよ: {x}")')
    print(f"<<< {v1.text}\n")

    print(">>> 3 つの異なる主張に同じ λ を適用")
    for claim in ["Lisp は関数型言語である", "tail call は最適化されるべき", "型は推論できる"]:
        v = eval_(env, f'(critique "{claim}")')
        print(f"  [{claim}]")
        if v.payload and isinstance(v.payload, dict):
            print(f"    → {v.payload.get('result', v.text)[:200]}")
        else:
            print(f"    → {v.text[:200]}")
        print()

    print(">>> 空引数 λ を 3 回呼んで出力の揺らぎを観察する")
    eval_(env, '(lambda describe-self () "現在のあなたを 1 文で説明して")')
    for i in range(3):
        v = eval_(env, "(describe-self)")
        if v.payload and isinstance(v.payload, dict):
            ans = v.payload.get("result", v.text)
        else:
            ans = v.text
        print(f"  回 {i+1}: {ans[:200]}")
    print("\n>>> 同じ λ、同じ (空) 引数、3 つの異なる出力 (LLM は参照透明でない)。\n")

    print(">>> (lambdas) で定義済み λ 一覧")
    v = eval_(env, "(lambdas)")
    print(f"<<< {v.text}\n")


def demo_compose() -> None:
    """λ の合成デモ。"""
    print("=== demo-compose: λ の合成 ===\n")
    env = build_default_env()

    print(">>> summarize と critique を定義")
    eval_(env, '(lambda summarize (x) "次を一文で要約: {x}")')
    eval_(env, '(lambda critique (x) "次の主張への反論を一文で: {x}")')

    print(">>> compose は派生定義済み: (compose summarize critique) で critique ∘ summarize")
    eval_(env, '(define critique_of_summary (compose summarize critique))')

    print(">>> 合成した λ を長い文章に適用")
    long_text = (
        "S 式は () で囲まれた入れ子構造で、先頭が operator、続いて argument が並ぶ。"
        "コードとデータが同じ形式で表現できるため、quote / eval で式の構造的書き換えが可能。"
        "Lisp の特徴はこの code = data にある。"
    )
    v = eval_(env, f'(critique_of_summary "{long_text}")')
    print(f"<<< {v.text[:500]}\n")


def demo_compare() -> None:
    """同じ tag の式を Lisp と LLM の両方で評価して差を観察する。

    狙い:
      - 決定性 vs 非決定性 (LLM は参照透明でない)
      - LLM のみが扱える式 (text の意味理解)
      - Lisp 条件で LLM 分岐 (ハイブリッド)
      - code-as-data: 式の構造的書き換え (Lisp のみ)
    """
    print("=== demo-compare: Lisp vs LLM、同じ syntax で並べる ===\n")
    env = build_default_env()

    print(">>> [1] Lisp の比較述語: 同じ式は常に同じ値")
    eval_(env, '(define cmp-lisp (lambda (a b) (if (> a b) "yes" "no")))')
    for i in range(3):
        v = eval_(env, "(cmp-lisp 7 5)")
        print(f"  (cmp-lisp 7 5) [{i+1}] => {v.text}")

    print()
    print(">>> [2] LLM の比較述語: 同じ問い、出力は揺らぐ")
    eval_(env, '(lambda cmp-llm (a b) "{a} は {b} より大きい? yes か no だけ 1 単語で")')
    for i in range(3):
        v = eval_(env, "(cmp-llm 7 5)")
        ans = v.payload.get("value", v.text) if v.payload else v.text
        print(f"  (cmp-llm 7 5) [{i+1}] => {ans!r}")

    print()
    print(">>> [3] LLM のみが扱える式 (意味理解が要る)")
    eval_(env, '(lambda interpret (x) "{x} を 1 文で簡潔に解説して")')
    v = eval_(env, '(interpret "アキレスと亀のパラドックス")')
    ans = v.payload.get("value", v.text) if v.payload else v.text
    print(f'  (interpret "アキレスと亀のパラドックス") => {ans[:200]}')

    print()
    print(">>> [4] 混在: Lisp 条件で LLM 分岐")
    eval_(env, '(lambda praise (x) "{x} を 1 行で褒める")')
    eval_(env, '(lambda scold (x) "{x} を 1 行で叱る")')
    eval_(env,
        '(define judge (lambda (n) (if (> n 10) (praise n) (scold n))))')
    for n in (5, 100):
        v = eval_(env, f"(judge {n})")
        ans = v.payload.get("value", v.text) if v.payload else v.text
        print(f"  (judge {n}) => {ans[:200]}")

    print()
    print(">>> [5] 同じ Lisp 式を quote → eval で再構成")
    print("  (eval (cons (quote *) (cdr (quote (+ 2 3 4))))) → operator を書き換えて再評価")
    v = eval_(env, "(eval (cons (quote *) (cdr (quote (+ 2 3 4)))))")
    print(f"  => {v.text}")
    print("  LLM 評価器ではこの「式の構造的書き換え」はできない (text は構造を持たない)")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(prog="lispy", description="Lisp-style evaluator over host")
    p.add_argument(
        "--yolo", action="store_true",
        help="副作用 tool (shell / write-file / edit-file) の y/N 確認を全 skip。 "
             "session 中は (set-yolo #t/#f) で切り替え可。",
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("repl", help="interactive REPL (default)")
    sub.add_parser("demo", help="run a minimal demo (renew + eval-turn)")
    sub.add_parser("demo-lambda", help="λ 抽象のデモ (critique + self_describe)")
    sub.add_parser("demo-compose", help="λ の合成デモ (compose)")
    sub.add_parser("demo-compare", help="Lisp vs LLM の対比デモ")
    args = p.parse_args()
    # --yolo が指定されたら起動直後に edit 側の runtime flag を立てる。
    # edit が import できない環境 (read-only build 等) では何もしない。
    if args.yolo:
        try:
            import edit as _edit
            _edit.set_yolo(True)
        except ImportError:
            pass
    cmd = args.cmd or "repl"
    if cmd == "demo":
        demo()
    elif cmd == "demo-lambda":
        demo_lambda()
    elif cmd == "demo-compose":
        demo_compose()
    elif cmd == "demo-compare":
        demo_compare()
    else:
        repl()


if __name__ == "__main__":
    main()
