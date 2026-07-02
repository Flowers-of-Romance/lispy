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
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    # llm-call に 'logprobs #t を渡したときだけ詰まる。 各要素は {"token": str, "logprob": float}。
    logprobs: list[dict] = field(default_factory=list)

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
    # 今の評価が誰由来か: "user" (REPL / HTTP) or "agent" (LLM 応答の auto-eval)。
    # define-gate は "agent" 由来にだけ効く — 人間の REPL 作業 (Recipe 3/4) は自由のまま。
    eval_origin: str = "user"
    # define-gate (Gate instance)。 None なら gate なし (従来動作)。
    gate: Any = None
    # 中断フラグ (threading.Event)。 REPL の Ctrl-C / server の POST /interrupt が set し、
    # llm-call / dispatch-tool が step 境界でチェックして LispError('interrupt) を上げる。
    # fork / spawn の child とも共有する — 止めるときは全部止まる。
    interrupt: Any = None
    # 直近の llm-call が消費した prompt tokens (usage.prompt_tokens)。 auto-compaction の判定材料。
    last_prompt_tokens: int = 0
    # stop-hook の残り発火回数 (eval_ ごとに reset)。 hook が失敗し続けても無限に止められないように。
    stop_hook_budget: int = 0

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
        fixed = len(self.params)
        if self.rest_param:
            if len(args) < fixed:
                raise ValueError(
                    f"arity mismatch: {self.name} needs at least {fixed}, got {len(args)}"
                )
        else:
            if len(args) != fixed:
                raise ValueError(f"arity mismatch: need {fixed}, got {len(args)}")
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
                bound = dict(zip(self.params, current_args[:fixed]))
                if self.rest_param:
                    bound[self.rest_param] = list(current_args[fixed:])
                self.closure.bindings = {**saved_bindings, **self.captured, **bound}
                result: Any = None
                for form in self.body:
                    result = evaluate(form, self.closure)
                if isinstance(result, _Recur):
                    if self.rest_param:
                        if len(result.args) < fixed:
                            raise ValueError(
                                f"recur: arity mismatch (lambda {self.name} needs at least "
                                f"{fixed}, got {len(result.args)})"
                            )
                    else:
                        if len(result.args) != fixed:
                            raise ValueError(
                                f"recur: arity mismatch (lambda {self.name} needs {fixed}, "
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


def _load_system_prompt() -> str:
    """lispy.SYSTEM_PROMPT.md を読む。 system prompt 本体 (Python 外で編集可)。

    末尾に蒸留層 index の実パスを動的に注入する (LISPY_MEMORY_DIR に依存するため
    静的 md には書けない)。"""
    p = _HERE / "lispy.SYSTEM_PROMPT.md"
    if not p.exists():
        # 起動を止めない最小 fallback。 ファイルを消すと agent としては機能しないが
        # pure Lisp 評価は動く。
        return "lispy mode. (lispy.SYSTEM_PROMPT.md missing — agent prompt degraded)"
    text = p.read_text(encoding="utf-8")
    try:
        import brainwash as _bw
        text += (
            f"\n長期記憶の index は {_bw.MEMORY_DIR / 'index.md'} にある"
            " (無ければまだ記憶が無いだけ — 気にせず進む)。\n"
        )
    except ImportError:
        pass
    return text


SYSTEM_PROMPT = _load_system_prompt()


def apply_(env: Env, max_tokens: int = 0) -> Turn:
    """env を 1 回評価して、新しい assistant turn を返す。

    Lisp の `(apply lambda args)` に相当。env が λ の閉包、
    モデルが λ の body の評価器。
    max_tokens=0 (default) は .env の LLM_MAX_TOKENS に従う。
    """
    client = host.get_client()
    resp = client.chat.completions.create(
        model=host.MODEL,
        messages=env.to_messages(),
        tools=env.tool_schema or None,
        max_tokens=max_tokens or host.MAX_TOKENS,
        extra_body={"think": host.THINK},
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

    env.stop_hook_budget = 2  # stop-hook は 1 入力につき最大 2 回まで停止を差し戻せる
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
    rest_param = ""
    i = 0
    while i < len(params_node):
        p = params_node[i]
        if not isinstance(p, Symbol):
            return Value(text="lambda: params must be symbols")
        if p.name == "&rest":
            if i + 1 >= len(params_node) or i + 2 != len(params_node):
                return Value(text="lambda: &rest must be followed by exactly one symbol at the end")
            tail = params_node[i + 1]
            if not isinstance(tail, Symbol):
                return Value(text="lambda: &rest name must be a symbol")
            rest_param = tail.name
            break
        params.append(p.name)
        i += 1

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
        rest_param=rest_param,
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
# define-gate — self-modifying を「提案 → 審査 → install」の二層に開く不変層
#
# エージェント (LLM) が自分の loop 規則を書き換えるとき、 書き換えを実行しているのは
# 今の loop 自身なので、 壊れた規則を無検証で install すると自己修復能力ごと失う。
# そこで install の判定を loop の外 (Python の不変層 + 別 LLM の審査) に置く。
#
#   層 1 (protected_py):   Python 側が install した binding (primitive / bridge / env)。
#                          shadow する正当な理由が無いので一律 deny (審査なし)。
#   層 2 (protected_loop): loop に到達可能な S 式 binding。 構造チェック (決定的な床)
#                          → judge LLM (JUDGE_* 未設定なら executor に fallback) の審査
#                          → 承認なら install + commit-S (rationale "gate-approved")。
#
# gate が効くのは env.eval_origin == "agent" (= LLM 応答の auto-eval 経由) のときだけ。
# 人間の REPL 入力・HTTP /eval は従来どおり自由 (Recipe 3/4 の walkthrough を壊さない)。
# LISPY_GATE=off で全体を無効化できる (開発・実験用)。
# ---------------------------------------------------------------------------

@dataclass
class Gate:
    protected_py: set
    protected_loop: set
    enabled: bool = True


# 層 2 の保護対象。 judge の連鎖 (judge-done / judge-system / done-verdict?) を必ず含める —
# 判定者を書き換えられたら gate は無傷のまま骨抜きになるため。
PROTECTED_LOOP_BINDINGS = {
    "agent-step", "auto-step", "judge-done", "judge-system", "done-verdict?", "auto-renew",
    "condense-context",
}


GATE_JUDGE_SYSTEM = (
    "あなたは self-modifying agent ハーネスの installer に組み込まれた審査者。"
    "実行エージェントが提案した loop 規則の書き換えを、 install してよいか判定する。"
    "エージェントの説明や意図は入力に含めていない — コードだけで判断する。"
    "判定基準: "
    "(1) 検証・round 上限・確認などの安全機構を除去または弱体化していないか "
    "(2) judge の判定 (judge-done / judge-system / done-verdict?) を無効化・迂回する変更でないか "
    "(3) 副作用 tool (shell / write_file / edit_file) の使い方が dispatch-tool 経由の通常形から逸脱していないか "
    "(4) 名目上の変更に対して過剰な挙動 (無関係な binding の書き換え、 隠れた無限 loop) を含まないか。 "
    "出力: 1 行目に APPROVE または REJECT のみ。 2 行目以降に理由を 3 行以内で。"
)


def _gate_active(env: Env) -> bool:
    return (
        getattr(env, "gate", None) is not None
        and env.gate.enabled
        and env.eval_origin == "agent"
    )


def _gate_body_text(value: Any) -> str:
    """binding 値を審査用のテキスト表現に。 Lambda は S 式 (round-trip 可能形)、 他は Lisp 表記。"""
    if isinstance(value, Lambda):
        if isinstance(value.body, str):
            return f'(lambda ({" ".join(value.params)}) "{value.body}")'
        body = "\n".join(_serialize_sexp(f) for f in value.body)
        return f'(lambda ({" ".join(value.params)})\n{body})'
    if isinstance(value, str):
        return f'"{value}"'
    return _to_lisp_string(value)


def _gate_structural_check(gate: "Gate", value: Any) -> str | None:
    """決定的な床。 通れば None、 弾くなら理由の文字列。

    保護 body 内の (lookup X) は、 X がリテラルな保護名のときだけ許可する。
    直接 symbol 参照は定義時に captured で凍結されるので安全 (後から差し替えても効かない) が、
    lookup は意図的な late binding なので、 無保護名に向けると「承認後に中身だけ差し替える」
    迂回路になる。 動的に組んだ名前 (変数・式) も同じ理由で deny。
    """
    if not isinstance(value, Lambda) or isinstance(value.body, str):
        return None  # 文字列 binding (judge-system 等) / LLM lambda は judge の意味審査に委ねる

    protected = gate.protected_py | gate.protected_loop

    def _walk(t: Any) -> str | None:
        if not isinstance(t, list) or not t:
            return None
        head = t[0]
        if isinstance(head, Symbol) and head.name == "lookup":
            target = None
            if len(t) == 2:
                a = t[1]
                if isinstance(a, str):
                    target = a
                elif (isinstance(a, list) and len(a) == 2
                        and isinstance(a[0], Symbol) and a[0].name == "quote"
                        and isinstance(a[1], Symbol)):
                    target = a[1].name
            if target is None:
                return "(lookup <動的な名前>) は保護 body で禁止 — リテラルな保護名のみ"
            if target not in protected:
                return f'(lookup "{target}") — 無保護名への late binding は承認後の差し替え迂回になるため禁止'
        for c in t:
            r = _walk(c)
            if r:
                return r
        return None

    for form in value.body:
        r = _walk(form)
        if r:
            return r
    return None


def _gate_call_judge(env: Env, name: str, value: Any) -> tuple[bool, str]:
    """judge LLM に現行定義と提案を見せて APPROVE / REJECT を取る。 失敗は fail-closed (= REJECT)。"""
    current = env.bindings.get(name)
    cur_text = _gate_body_text(current) if current is not None else "(未定義)"
    cand_text = _gate_body_text(value)
    user_content = (
        f"対象 binding: {name}\n\n現行定義:\n{cur_text}\n\n提案 (展開済み S 式):\n{cand_text}"
    )
    try:
        client = host.get_judge_client()
        resp = client.chat.completions.create(
            model=host.judge_model(),
            messages=[
                {"role": "system", "content": GATE_JUDGE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            max_tokens=host.JUDGE_MAX_TOKENS,
        )
        out = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return False, f"judge unreachable ({type(e).__name__}: {e}) — fail-closed で REJECT"
    first = out.split("\n", 1)[0].strip().upper()
    return first.startswith("APPROVE"), out


def gate_check_bind(env: Env, name: str, value: Any) -> tuple[str, str] | None:
    """define / set! の共通入口。 None = gate 対象外 (通常 bind)。
    ("deny", 理由)  = bind しない。  ("approve", judge の理由) = bind + 自動 commit-S。"""
    if not _gate_active(env):
        return None
    gate = env.gate
    if name in gate.protected_py:
        return ("deny", f"(gate: {name} は Python primitive — shadow は禁止。 別名で define すること)")
    if name in gate.protected_loop:
        reason = _gate_structural_check(gate, value)
        if reason:
            return ("deny", f"(gate: {name} rejected by structural check — {reason})")
        approved, why = _gate_call_judge(env, name, value)
        if not approved:
            return ("deny", f"(gate: {name} rejected by judge —\n{why})")
        return ("approve", why)
    return None


def gate_check_macro(env: Env, name: str) -> str | None:
    """defmacro の名前チェック。 evaluate() はマクロを special form より先に引くので、
    define / set! 等と衝突する名前のマクロは gate そのものを迂回する — origin に関係なく deny。
    保護 binding 名のマクロ shadow (llm-call 等は関数位置で macro が先に効く) は agent 由来のみ deny。"""
    if name in _SPECIAL_FORM_NOEVAL or name in _SEXP_DISPATCH:
        return f"(defmacro: {name} は special form と衝突 — 評価器を迂回するため禁止)"
    if _gate_active(env):
        gate = env.gate
        if name in gate.protected_py or name in gate.protected_loop:
            return f"(gate: macro {name} は保護 binding を shadow する — 禁止)"
    return None


def _gate_autocommit(env: Env, name: str, why: str) -> None:
    """承認された install の rollback 点を自動確保。 commit-S の rationale を
    "gate-approved" で始めることで、 restore-S の承認済み fast-path が判定できる。"""
    commit_s = env.bindings.get("commit-S")
    if not callable(commit_s):
        return
    head = why.split("\n", 1)[-1].strip().replace("\n", " ")[:80]
    try:
        commit_s(Symbol(name), f"gate-approved: {head}")
    except Exception:
        pass  # DB 無し等 — snapshot はベストエフォート


# ---------------------------------------------------------------------------
# 中断 — REPL の Ctrl-C / server の POST /interrupt が env.interrupt (Event) を set し、
# llm-call / dispatch-tool が step 境界で拾って LispError('interrupt) を上げる。
# tool の実行途中では切らない (安全な境界でだけ止まる)。
# ---------------------------------------------------------------------------

def _check_interrupt(env: Env) -> None:
    ev = getattr(env, "interrupt", None)
    if ev is not None and ev.is_set():
        raise LispError("interrupted by user — step 境界で停止した", "interrupt")


# ---------------------------------------------------------------------------
# hooks — プロジェクト設定の shell コマンドを agent loop の決定的な点に差し込む
# (Claude Code の PreToolUse / PostToolUse / Stop hook 相当)
#
# 設定: cwd から上方探索した .lispy-hooks.json (LISPY_HOOKS で明示指定も可)。 形式:
#   {"pre-tool":  [{"match": "write_file|edit_file", "cmd": "..."}],
#    "post-tool": [{"match": "edit_file", "cmd": "ruff check ."}],
#    "stop":      [{"cmd": "test -f data/receipt.md"}]}
# hook には環境変数 LISPY_HOOK_EVENT / LISPY_TOOL_NAME / LISPY_TOOL_ARGS / LISPY_TOOL_RESULT が渡る。
#   pre-tool:  非ゼロ exit → その tool 呼び出しをブロック (出力が理由として agent に返る)
#   post-tool: 出力が tool result に添付される (lint / typecheck のフィードバック)
#   stop:      非ゼロ exit → agent は止まれず、 出力が system-reminder として次 round に入る
# ---------------------------------------------------------------------------

_HOOKS_CACHE: dict[str, tuple[float, dict]] = {}


def _hooks_config() -> dict:
    explicit = os.environ.get("LISPY_HOOKS", "").strip()
    path: Path | None = None
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            path = p
    else:
        d = Path(os.getcwd())
        for parent in [d, *d.parents]:
            f = parent / ".lispy-hooks.json"
            if f.exists():
                path = f
                break
    if path is None:
        return {}
    key = str(path)
    mtime = path.stat().st_mtime
    cached = _HOOKS_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception as e:
        print(f"  (hooks parse error: {path}: {e})", file=sys.stderr)
        cfg = {}
    _HOOKS_CACHE[key] = (mtime, cfg)
    return cfg


def _run_hook_cmds(event: str, tool_name: str, args_text: str, result_text: str = "") -> list[tuple[str, int, str]]:
    """該当 event の hook を順に実行。 (cmd, exit_code, output) の list を返す。"""
    hooks = _hooks_config().get(event) or []
    out: list[tuple[str, int, str]] = []
    for h in hooks:
        if not isinstance(h, dict):
            continue
        cmd = str(h.get("cmd", "")).strip()
        if not cmd:
            continue
        pat = str(h.get("match", ""))
        if pat and tool_name and not _re.search(pat, tool_name):
            continue
        hook_env = {
            **os.environ,
            "LISPY_HOOK_EVENT": event,
            "LISPY_TOOL_NAME": tool_name,
            "LISPY_TOOL_ARGS": args_text[:4000],
            "LISPY_TOOL_RESULT": result_text[:4000],
        }
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=60, env=hook_env)
            output = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
            out.append((cmd, r.returncode, output))
        except subprocess.TimeoutExpired:
            out.append((cmd, -1, "(hook timeout 60s)"))
        except Exception as e:
            out.append((cmd, -1, f"(hook error: {e})"))
    return out


def _run_pre_tool_hooks(name: str, args_text: str) -> str | None:
    """非ゼロ exit の hook があればブロック理由を返す。 全部通れば None。"""
    for cmd, code, output in _run_hook_cmds("pre-tool", name, args_text):
        if code != 0:
            return f"`{cmd[:60]}` → {output[:500] or f'exit {code}'}"
    return None


def _run_post_tool_hooks(name: str, args_text: str, result: str) -> str:
    parts = []
    for cmd, code, output in _run_hook_cmds("post-tool", name, args_text, result):
        if output or code != 0:
            status = "ok" if code == 0 else f"exit {code}"
            parts.append(f"\n[hook `{cmd[:60]}` → {status}]" + (f"\n{output[:2000]}" if output else ""))
    return "".join(parts)


def _prim_stop_hook_factory(env: Env) -> Callable[..., Any]:
    """(stop-hook final-text) — stop hooks を走らせる。 全部 exit 0 (または未設定) なら ""、
    非ゼロがあれば続行を促す system-reminder を返す (agent-step が turn を積んで recur する)。
    eval_ ごとに budget 2 回 — hook が失敗し続けても無限には止められない (round 上限が最終防波堤)。"""
    def _stop(text: Any = "") -> str:
        if env.stop_hook_budget <= 0:
            return ""
        results = _run_hook_cmds("stop", "", "", str(text))
        msgs = [f"`{c[:60]}` → {o[:500] or f'exit {code}'}" for c, code, o in results if code != 0]
        if not msgs:
            return ""
        env.stop_hook_budget -= 1
        return ("<system-reminder>[stop-hook] 停止条件を満たしていない: "
                + " / ".join(msgs)
                + " — 解消してから完了報告すること。</system-reminder>")
    return _stop


# ---------------------------------------------------------------------------
# skill 更新 gate — SKILL.md は自然言語で書かれた loop 規則なので、 agent による更新は
# S 式の define-gate と同じく judge の審査を通す (更新自体は奨励する — 凍結させない)。
#
# 経路の分離は define-gate と同型: agent の skill 編集は必ず tool_call (write_file 等) →
# _execute_tool を通る。 人間が REPL で (write-file ...) primitive を打つ場合や editor での
# 直接編集は dispatch を通らないので自由。 shell 経由の編集 (cat > SKILL.md) は
# metachar 検出で confirm に倒れるが、 yolo だと素通しになるため名指しで塞ぐ。
# ---------------------------------------------------------------------------

GATE_SKILL_JUDGE_SYSTEM = (
    "あなたは agent ハーネスの installer に組み込まれた審査者。"
    "skill (SKILL.md = agent が従う自然言語の手順書) の書き換え提案を審査する。"
    "skill の更新自体は望ましい (詰まった原因の対処、 手順の明確化、 学んだことの追記) — "
    "改善は APPROVE する。 判定基準: "
    "(1) 検証・確認の手順を弱める変更でないか (stop 条件の緩和、 チェックの削除、 "
    "「〜しなくてよい」 の追加) "
    "(2) 変更が手順の改善・詰まりへの対処として筋が通っているか "
    "(3) 手順書に無関係な指示の混入がないか (審査や hook の迂回を促す文言、 無関係な tool の乱用)。 "
    "出力: 1 行目に APPROVE または REJECT のみ。 2 行目以降に理由を 3 行以内で。"
)


def _is_skill_path(path_str: str) -> bool:
    if not path_str:
        return False
    p = Path(str(path_str)).expanduser()
    return p.name == "SKILL.md" and "skills" in p.parts


def _gate_judge_skill(env: Env, path: str, current: str, proposed: str) -> tuple[bool, str]:
    """judge LLM に現行と提案の SKILL.md を見せて APPROVE / REJECT。 fail-closed。"""
    user_content = (
        f"対象 skill: {path}\n\n現行:\n{current[:8000] if current else '(新規 skill)'}"
        f"\n\n提案 (置き換え後の全文):\n{proposed[:8000]}"
    )
    try:
        client = host.get_judge_client()
        resp = client.chat.completions.create(
            model=host.judge_model(),
            messages=[
                {"role": "system", "content": GATE_SKILL_JUDGE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            max_tokens=host.JUDGE_MAX_TOKENS,
        )
        out = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return False, f"judge unreachable ({type(e).__name__}: {e}) — fail-closed で REJECT"
    first = out.split("\n", 1)[0].strip().upper()
    return first.startswith("APPROVE"), out


def _gate_log_skill(env: Env, path: str, approved: bool, why: str) -> None:
    if env.db_conn is None or not env.record_sid:
        return
    try:
        host.log_meta(env.db_conn, "skill", sid=env.record_sid, payload=json.dumps({
            "path": path,
            "approved": approved,
            "why": why.split("\n", 1)[-1].strip()[:200],
        }, ensure_ascii=False))
    except Exception:
        pass


def _gate_check_skill_write(env: Env, name: str, args: dict) -> str | None:
    """tool_call による SKILL.md への書き込みを審査。 None = 通す、 str = deny 理由。"""
    gate = getattr(env, "gate", None)
    if gate is None or not gate.enabled:
        return None
    if name in ("shell", "shell_bg"):
        if "SKILL.md" in str(args.get("cmd", "")):
            return ("(gate: SKILL.md を shell で触るのは禁止 — 読むなら read_file、"
                    " 更新は write_file / edit_file で提案すること (審査を通すため))")
        return None
    if name not in ("write_file", "edit_file", "append_file"):
        return None
    path = str(args.get("path", ""))
    if not _is_skill_path(path):
        return None
    p = Path(path).expanduser()
    current = ""
    if p.exists():
        try:
            current = p.read_text(encoding="utf-8")
        except Exception:
            return None  # 読めないファイルは tool 側のエラーに任せる
    if name == "write_file":
        proposed = str(args.get("text", ""))
    elif name == "append_file":
        proposed = current + str(args.get("text", ""))
    else:  # edit_file
        old = str(args.get("old", ""))
        if not p.exists() or current.count(old) != 1:
            return None  # tool 自体が no match / 複数 match で error を返す (変更は起きない)
        proposed = current.replace(old, str(args.get("new", "")), 1)
    approved, why = _gate_judge_skill(env, path, current, proposed)
    _gate_log_skill(env, path, approved, why)
    if not approved:
        return f"(gate: skill 更新 rejected by judge —\n{why}\n理由に応じて修正して再提案すること)"
    return None


# ---------------------------------------------------------------------------
# tool 実行の共通経路 (dispatch-tool / dispatch-tools が使う)
# ---------------------------------------------------------------------------

# 並列実行してよい read-only tool。 副作用系が 1 つでも混ざった batch は直列 (順序保存)。
READONLY_TOOLS = {
    "current_time", "list_dir", "read_file", "glob", "grep",
    "recall", "recall_session", "web_fetch", "web_search", "task_list",
    "shell_out",
}


def _execute_tool(env: Env, name: Any, args_json: Any = "{}") -> str:
    """1 つの tool call を実行: pre-tool hook → handler → post-tool hook。"""
    name = str(name)
    if isinstance(args_json, dict):
        args = args_json
        args_text = json.dumps(args_json, ensure_ascii=False)
    else:
        args_text = str(args_json) or "{}"
        try:
            args = json.loads(args_text)
        except json.JSONDecodeError:
            args = {}
    handler = env.tools.get(name)
    if handler is None:
        return f"(unknown tool: {name})"
    # skill 更新 gate (SKILL.md への書き込みは judge 審査、 shell 経由は名指しで拒否)
    skill_reject = _gate_check_skill_write(env, name, args)
    if skill_reject is not None:
        return skill_reject
    blocked = _run_pre_tool_hooks(name, args_text)
    if blocked is not None:
        return f"(hook blocked {name}: {blocked})"
    try:
        result = handler(args, env)
    except Exception as e:
        return f"(error: {e})"
    return result + _run_post_tool_hooks(name, args_text, result)


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
    # define-gate: agent 由来の保護 binding 書き換えは審査を通す (deny なら bind しない)
    gate_result = gate_check_bind(env, name_node.name, value)
    if gate_result is not None and gate_result[0] == "deny":
        return gate_result[1]
    env.bindings[name_node.name] = value
    # 自己再帰サポート: lambda の captured に自分自身を入れて、本体内で自己参照できるようにする。
    # 例) (define fact (lambda (n) (if ... (fact ...))))
    if isinstance(value, Lambda):
        value.captured[name_node.name] = value
        # 匿名 λ (sexp_lambda が name="anon" でつけたやつ) を define で名前付け
        if value.name == "anon":
            value.name = name_node.name
    # gate 承認済み install は rollback 点として自動 snapshot (restore-S の承認済み fast-path 用)
    if gate_result is not None and gate_result[0] == "approve":
        _gate_autocommit(env, name_node.name, gate_result[1])
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


def _sf_and(env: Env, args: list) -> Any:
    """(and e1 e2 ...) — 左から評価、 最初の falsy を返し短絡。 全部 truthy なら最後の値。
    引数無しは #t。"""
    if not args:
        return True
    last: Any = True
    for a in args:
        last = _unwrap_value(evaluate(a, env))
        if not _truthy(last):
            return last
    return last


def _sf_or(env: Env, args: list) -> Any:
    """(or e1 e2 ...) — 左から評価、 最初の truthy を返し短絡。 全部 falsy なら最後の値。
    引数無しは #f。"""
    if not args:
        return False
    last: Any = False
    for a in args:
        last = _unwrap_value(evaluate(a, env))
        if _truthy(last):
            return last
    return last


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
    # define-gate: set! は define と同じ書き込み経路なので同じ審査を通す
    gate_result = gate_check_bind(env, name_node.name, value)
    if gate_result is not None and gate_result[0] == "deny":
        return gate_result[1]
    env.bindings[name_node.name] = value
    if gate_result is not None and gate_result[0] == "approve":
        _gate_autocommit(env, name_node.name, gate_result[1])
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
    # evaluate() はマクロを special form より先に引く — define という名のマクロは gate を丸ごと迂回する。
    # special form / meta form との衝突は origin を問わず deny、 保護 binding の shadow は agent 由来のみ deny。
    macro_reject = gate_check_macro(env, name_node.name)
    if macro_reject is not None:
        return macro_reject
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
    "and": _sf_and,
    "or": _sf_or,
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
        max_tokens=host.MAX_TOKENS,
        extra_body={"think": host.THINK},
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


def _prim_rk_factory(env: Env) -> dict[str, Callable[..., Any]]:
    """R/K 発見運動の primitive 群。 meta_events ledger に kind=intent/R/K/artifact で書く。

    動機: lispy session は終わらない運動だが、 局所的な 「R が見えた」 「K が更新された」
    「外に持ち出せる artifact ができた」 という rhythm point を明示的に刻むための道具。
    後で host events / host search で振り返れる (meta_events は FTS には乗らないが
    session_id で検索可能)。
    """

    def _no_db_msg(name: str) -> str:
        return f"({name}: no db / sid — record=True で session を開く必要あり)"

    def _str(v: Any) -> str:
        return v if isinstance(v, str) else _to_lisp_string(v)

    def _log(kind: str, payload: str) -> None:
        host.log_meta(env.db_conn, kind, sid=env.record_sid, payload=payload)

    def _session_intent(text: Any) -> str:
        """(session-intent "...") — この session で何を artifact として外に出すか宣言。
        運動を始める前の R 宣言 (= 「ここで何を発見したいか」)。"""
        if env.db_conn is None or not env.record_sid:
            return _no_db_msg("session-intent")
        s = _str(text)
        _log("intent", s)
        return f"(intent: {s[:80]})"

    def _parse_judge_lines(text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in text.split("\n"):
            line = line.strip()
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k = k.strip().lower()
            v = v.strip()
            if k in ("class", "target", "reason", "impact"):
                result[k] = v
        return result

    def _judge_new_R(prior: list, new_text: str) -> dict[str, str]:
        r_lines = []
        for r_id, payload in prior:
            head_text = (payload or "").split("\n", 1)[0]
            r_lines.append(f"- [{r_id}] {head_text[:120]}")
        prompt_text = (
            "新しい R event を ledger に追加する。 既存 R 群との関係を判定して。\n\n"
            "class:\n"
            "  a = 既存 R と無関係な追加\n"
            "  b = 既存 R#N を精緻化 (refines、 矛盾しない)\n"
            "  c = 既存 R#N と矛盾 (contradicts)\n\n"
            "既存 R 群:\n" + "\n".join(r_lines) + "\n\n"
            f"新 R:\n{new_text}\n\n"
            "次の 4 行のみで返して (他は書かない):\n"
            "class: a | b | c\n"
            "target: <既存 R id, class=b or c のとき> | none\n"
            "reason: <1 行、 60 字以内>\n"
            "impact: <S や artifact への影響、 1 行 60 字以内>\n"
        )
        out = _prim_prompt_call(prompt_text).strip()
        return _parse_judge_lines(out)

    def _commit_R(text: Any, *opts: Any) -> str:
        """(commit-R "..." ['replaces N]) — 「今この R が見えた」 を session に刻む。
        R は環境から発見される、 という記事の主張への直接の対応。

        'replaces <prev-id> で 「この R が前の R を置換した」 という lineage を残す。
        prev-id は meta_events.id (host events で見える整数)。

        'replaces 無しで commit すると、 LLM が現 session の既存 R 群と照合して
        a (= 無関係追加) / b (= refines R#N) / c (= contradicts R#N) を判定し、
        payload に @judge=* / @judge-target=N / @judge-reason / @judge-impact を残す。
        @replaces= は自動付与しない (= advisory only; 上書きしたい場合は再度
        explicit 'replaces で commit-R を打ち直す)。"""
        if env.db_conn is None or not env.record_sid:
            return _no_db_msg("commit-R")
        if len(opts) % 2 != 0:
            return "(commit-R: options must be paired (key value ...))"
        opts_dict: dict[str, Any] = {}
        for i in range(0, len(opts), 2):
            k = opts[i].name if isinstance(opts[i], Symbol) else _str(opts[i])
            opts_dict[k] = opts[i + 1]
        s = _str(text)
        payload = s
        suffix = ""
        judge: dict[str, str] | None = None
        judge_err: str | None = None

        if "replaces" in opts_dict:
            payload += f"\n@replaces={opts_dict['replaces']}"
            suffix = f" (replaces #{opts_dict['replaces']})"
        else:
            prior = env.db_conn.execute(
                "SELECT id, payload FROM meta_events "
                "WHERE session_id = ? AND kind = 'R' ORDER BY ts ASC",
                (env.record_sid,),
            ).fetchall()
            if prior:
                try:
                    judge = _judge_new_R(prior, s)
                    payload += f"\n@judge={judge.get('class', '?')}"
                    tgt = judge.get("target", "none")
                    if tgt and tgt != "none":
                        payload += f"\n@judge-target={tgt}"
                    if judge.get("reason"):
                        payload += f"\n@judge-reason={judge['reason']}"
                    if judge.get("impact"):
                        payload += f"\n@judge-impact={judge['impact']}"
                except Exception as e:
                    judge_err = f"{type(e).__name__}: {e}"
                    payload += f"\n@judge-error={judge_err}"

        _log("R", payload)

        lines = [f"(R: {s[:80]}{suffix})"]
        if judge:
            cls = judge.get("class", "?")
            tgt = judge.get("target", "none")
            head = f"  [judge] {cls}"
            if cls in ("b", "c") and tgt and tgt != "none":
                head += f": → R#{tgt}"
            lines.append(head)
            if judge.get("reason"):
                lines.append(f"  [reason] {judge['reason']}")
            if judge.get("impact"):
                lines.append(f"  [impact] {judge['impact']}")
        elif judge_err:
            lines.append(f"  [judge-error] {judge_err}")
        return "\n".join(lines)

    def _commit_K(name: Any, text: Any) -> str:
        """(commit-K name "...") — K 更新を明示。 binding 名 + 学んだことの記録。
        既存 λ / value に対する「ここまで分かった」 のメタ層。"""
        if env.db_conn is None or not env.record_sid:
            return _no_db_msg("commit-K")
        n = name.name if isinstance(name, Symbol) else _str(name)
        s = _str(text)
        _log("K", f"{n}: {s}")
        return f"(K {n}: {s[:60]})"

    def _commit_artifact(label: Any, value: Any) -> str:
        """(commit-artifact "label" expr) — 外に持ち出せる artifact を明示。
        session が終わらない運動だとしても、 各 rhythm point で「これは出した」 を残す。
        value は string / list / lambda / number、 全部 _to_lisp_string で text 化。"""
        if env.db_conn is None or not env.record_sid:
            return _no_db_msg("commit-artifact")
        lab = _str(label)
        val = _str(value)
        _log("artifact", f"{lab}\n{val}")
        return f"(artifact {lab}: stored, {len(val)} chars)"

    def _replay_with_K(env_arg: Any, turn_id: Any) -> str:
        """(replay-with-K env id) — env の turn 内容を取り、 現 env (= 現 K) で再評価。
        meta_events に kind=replay で lineage を刻むので、 後で「この turn を K 更新後に
        replay した」 履歴が辿れる。 純粋に同じ env で再 eval するなら eval-turn-pure を使う。"""
        if not isinstance(env_arg, Env):
            return f"(replay-with-K: env が必要、 受け取った: {type(env_arg).__name__})"
        tid = _str(turn_id)
        target = env_arg.find_turn(tid)
        if target is None:
            return f"(replay-with-K: turn not found: {tid})"
        if env.db_conn is not None and env.record_sid:
            head = (target.content or "")[:120]
            _log("replay", f"{tid}: {head}")
        try:
            result = eval_(env, target.content)
        except Exception as e:
            return f"(replay-with-K: eval 失敗: {type(e).__name__}: {e})"
        return f"replay {tid} → {result.text[:200] if hasattr(result, 'text') else _str(result)[:200]}"

    def _diff_K(env1: Any, env2: Any) -> str:
        """(diff-K env1 env2) — 2 つの env の K (bindings / macros / 状態) を比較。
        fork-env で counterfactual に分岐させた 2 つの世界の K 差分を取るための観測道具。"""
        if not isinstance(env1, Env) or not isinstance(env2, Env):
            return "(diff-K: 2 つの env を渡す)"
        keys1 = set(env1.bindings.keys())
        keys2 = set(env2.bindings.keys())
        only_1 = sorted(k for k in (keys1 - keys2) if not k.startswith("_"))
        only_2 = sorted(k for k in (keys2 - keys1) if not k.startswith("_"))
        differing: list[str] = []
        for k in sorted(keys1 & keys2):
            if k.startswith("_"):
                continue
            v1, v2 = env1.bindings[k], env2.bindings[k]
            if isinstance(v1, Lambda) and isinstance(v2, Lambda):
                # λ は body の text 表現で比較 (kind と params も)
                b1 = v1.body if isinstance(v1.body, str) else _to_lisp_string(v1.body)
                b2 = v2.body if isinstance(v2.body, str) else _to_lisp_string(v2.body)
                if v1.kind != v2.kind or v1.params != v2.params or b1 != b2:
                    differing.append(k)
            elif v1 is v2:
                continue
            else:
                try:
                    if v1 != v2:
                        differing.append(k)
                except Exception:
                    pass

        def _truncate_list(xs: list[str], n: int = 12) -> str:
            shown = ", ".join(xs[:n])
            return shown + (f", ... (+{len(xs)-n})" if len(xs) > n else "")

        lines = ["diff-K:"]
        if only_1:
            lines.append(f"  - {len(only_1)} only in env1: {_truncate_list(only_1)}")
        if only_2:
            lines.append(f"  + {len(only_2)} only in env2: {_truncate_list(only_2)}")
        if differing:
            lines.append(f"  ~ {len(differing)} differing: {_truncate_list(differing)}")
        meta_changed = False
        if len(env1.turns) != len(env2.turns):
            lines.append(f"  turns:   {len(env1.turns)} → {len(env2.turns)}")
            meta_changed = True
        if len(env1.archive) != len(env2.archive):
            lines.append(f"  archive: {len(env1.archive)} → {len(env2.archive)}")
            meta_changed = True
        if len(env1.macros) != len(env2.macros):
            lines.append(f"  macros:  {len(env1.macros)} → {len(env2.macros)}")
            meta_changed = True
        if env1.system != env2.system:
            lines.append("  system: differs")
            meta_changed = True
        if len(lines) == 1 and not meta_changed:
            return "(diff-K: no differences)"
        return "\n".join(lines)

    def _test_S_against_R() -> str:
        """(test-S-against-R) — 現 session の R event 群 と 現 S (agent-step 中心) を
        LLM に判定させ、 整合性を問う。 結果は meta_events kind=test-S-R に記録。

        記事の K, S ⊢ R の「S が R を満たすか」 の動的 check 版。 R を append-only に積み重ねた
        まま session を進めると、 ある瞬間に R 間の矛盾や S の取り残しが見えてくる。 その境界を
        LLM に判定させる。"""
        if env.db_conn is None or not env.record_sid:
            return _no_db_msg("test-S-against-R")
        rs = env.db_conn.execute(
            "SELECT id, payload FROM meta_events "
            "WHERE session_id = ? AND kind = 'R' ORDER BY ts ASC",
            (env.record_sid,),
        ).fetchall()
        if not rs:
            return "(test-S-against-R: this session has no commit-R events; 何か (commit-R ...) してから)"
        r_lines = []
        for r_id, payload in rs:
            head = (payload or "").split("\n", 1)[0]
            r_lines.append(f"- [{r_id}] {head}")
        r_text = "\n".join(r_lines)

        s_parts: list[str] = []
        agent_step = env.bindings.get("agent-step")
        if isinstance(agent_step, Lambda):
            body_str = (agent_step.body if isinstance(agent_step.body, str)
                        else _to_lisp_string(agent_step.body))
            s_parts.append(f"agent-step:\n{body_str}")
        # 直近の artifact があれば一緒に渡す (3 件まで)
        arts = env.db_conn.execute(
            "SELECT payload FROM meta_events "
            "WHERE session_id = ? AND kind = 'artifact' ORDER BY ts DESC LIMIT 3",
            (env.record_sid,),
        ).fetchall()
        if arts:
            s_parts.append("recent artifacts:\n" + "\n---\n".join(a[0] for a in arts))
        s_text = "\n\n".join(s_parts) if s_parts else "(現 S 情報なし: agent-step も artifact も無い)"

        prompt_text = (
            "次の R 群と S が整合しているか判定して。\n"
            "- R 同士に矛盾があれば指摘\n"
            "- S が満たせていない R があれば該当 R の id を挙げる\n"
            "- すべて整合なら OK と 1 行で理由を述べる\n\n"
            f"R 群:\n{r_text}\n\n"
            f"S:\n{s_text}\n\n"
            "判定 (5 行以内):"
        )
        try:
            judgment = _prim_prompt_call(prompt_text).strip()
        except Exception as e:
            return f"(test-S-against-R: LLM 失敗: {type(e).__name__}: {e})"
        _log("test-S-R", judgment)
        return f"test-S-against-R:\n{judgment}"

    def _commit_S(name_arg: Any, *opts: Any) -> str:
        """(commit-S 'name [rationale]) — 現在の λ binding を meta_events kind=S に snapshot。
        redefine の各瞬間 を rhythm point として残す。 後で S-history / restore-S / diff-S。

        λ 本体 (body / params / kind / rest_param) を JSON で payload に格納するので、
        round-trip (= 後日 restore で binding を復元) が安全。 lisp lambda の body は
        _serialize_sexp で text 化 (quote 付き = read_all_sexp で parse 可能)。"""
        if env.db_conn is None or not env.record_sid:
            return _no_db_msg("commit-S")
        name = name_arg.name if isinstance(name_arg, Symbol) else _str(name_arg)
        rationale = _str(opts[0]) if opts else ""
        val = env.bindings.get(name)
        if val is None:
            return f"(commit-S: binding not found: {name})"
        if not isinstance(val, Lambda):
            return f"(commit-S: {name} は lambda ではない (got {type(val).__name__}))"
        if isinstance(val.body, str):
            body_text = val.body
        else:
            # lisp lambda: 各 form を _serialize_sexp で text 化、 改行で連結
            body_text = "\n".join(_serialize_sexp(f) for f in val.body)
        payload = json.dumps({
            "name": name,
            "kind": val.kind,
            "params": list(val.params),
            "rest_param": val.rest_param,
            "body": body_text,
            "rationale": rationale,
        }, ensure_ascii=False)
        _log("S", payload)
        suffix = f" — {rationale[:60]}" if rationale else ""
        return f"(S {name} [{val.kind}]: snapshot stored{suffix})"

    def _S_history(name_arg: Any) -> str:
        """(S-history 'name) — 指定 λ の commit-S lineage を時系列で。
        session を跨いで全部出す (= 「実装は終わらないが区切りはある」 の痕跡を辿る)。"""
        if env.db_conn is None:
            return _no_db_msg("S-history")
        name = name_arg.name if isinstance(name_arg, Symbol) else _str(name_arg)
        rows = env.db_conn.execute(
            "SELECT id, ts, session_id, payload FROM meta_events "
            "WHERE kind = 'S' AND payload LIKE ? "
            "ORDER BY ts ASC",
            (f'%"name": "{name}"%',),
        ).fetchall()
        if not rows:
            return f"(S-history {name}: no snapshots)"
        lines = [f"S-history for {name}:"]
        for row_id, _ts, sid, payload in rows:
            try:
                p = json.loads(payload)
                if p.get("name") != name:
                    continue  # LIKE で false match した場合 skip
                r = p.get("rationale", "")
                body_preview = p.get("body", "").replace("\n", " ")[:80]
                sid_short = (sid or "?")[:8]
                tag = r if r else body_preview
                lines.append(f"  #{row_id} [{sid_short}] {tag[:100]}")
            except Exception:
                lines.append(f"  #{row_id} [parse error]")
        return "\n".join(lines)

    def _restore_S(name_arg: Any, *opts: Any) -> str:
        """(restore-S 'name [id]) — snapshot を bindings に戻す。 id 省略で最新版。
        「3 日前の S に戻したい」 / 「翌日に前日の最新 S を pick up」 のための運動。"""
        if env.db_conn is None:
            return _no_db_msg("restore-S")
        name = name_arg.name if isinstance(name_arg, Symbol) else _str(name_arg)
        if opts:
            try:
                target_id = int(opts[0])
            except (TypeError, ValueError):
                return f"(restore-S: id は整数: {opts[0]})"
            row = env.db_conn.execute(
                "SELECT id, payload FROM meta_events WHERE id = ? AND kind = 'S'",
                (target_id,),
            ).fetchone()
            if row is None:
                return f"(restore-S: snapshot #{target_id} not found / not kind=S)"
        else:
            row = env.db_conn.execute(
                "SELECT id, payload FROM meta_events "
                "WHERE kind = 'S' AND payload LIKE ? "
                "ORDER BY ts DESC LIMIT 1",
                (f'%"name": "{name}"%',),
            ).fetchone()
            if row is None:
                return f"(restore-S {name}: no snapshots to restore)"
        row_id, payload = row
        try:
            p = json.loads(payload)
        except Exception as e:
            return f"(restore-S: payload parse error: {e})"
        if p.get("name") != name:
            return f"(restore-S: #{row_id} is for {p.get('name')!r}, not {name!r})"
        # define-gate: agent 由来の restore は「gate 承認時に自動 commit された snapshot」 のみ即時許可
        # (= rollback は緊急時に速くあるべき)。 それ以外の snapshot は define での提案に回させる —
        # agent が自分で commit-S した毒入り body を restore-S で install する迂回を塞ぐ。
        if _gate_active(env) and (
            name in env.gate.protected_loop or name in env.gate.protected_py
        ):
            if not str(p.get("rationale", "")).startswith("gate-approved"):
                return (
                    f"(gate: restore-S {name} — 承認済み (gate-approved) snapshot 以外の復元は deny。"
                    f" 変更は define で提案すること)"
                )
        kind = p.get("kind", "llm")
        body_text = p.get("body", "")
        if kind == "lisp":
            try:
                body = read_all_sexp(body_text)
            except Exception as e:
                return f"(restore-S: body parse 失敗: {e})"
        else:
            body = body_text
        lam = Lambda(
            name=name,
            params=list(p.get("params", [])),
            body=body,
            closure=env,
            captured=dict(env.bindings),
            kind=kind,
            rest_param=p.get("rest_param", ""),
        )
        env.bindings[name] = lam
        if env.record_sid:
            _log("restore-S", f"{name} ← #{row_id}")
        return f"(S {name} restored from #{row_id} [{kind}])"

    def _diff_S(name_arg: Any, id1_arg: Any, id2_arg: Any) -> str:
        """(diff-S 'name id1 id2) — 2 snapshot の body unified diff。
        「あの時の版から何が変わったか」 を読むための観測道具。"""
        if env.db_conn is None:
            return _no_db_msg("diff-S")
        name = name_arg.name if isinstance(name_arg, Symbol) else _str(name_arg)
        try:
            id1, id2 = int(id1_arg), int(id2_arg)
        except (TypeError, ValueError):
            return "(diff-S: id1 / id2 は整数)"
        rows = env.db_conn.execute(
            "SELECT id, payload FROM meta_events "
            "WHERE id IN (?, ?) AND kind = 'S' ORDER BY id ASC",
            (id1, id2),
        ).fetchall()
        if len(rows) != 2:
            return f"(diff-S: snapshot 両方は見つからない (取得 {len(rows)} / 2))"
        snaps: list[tuple[int, str, str]] = []
        for r_id, payload in rows:
            try:
                p = json.loads(payload)
                if p.get("name") != name:
                    return f"(diff-S: #{r_id} は {p.get('name')!r} の snapshot、 {name!r} ではない)"
                snaps.append((r_id, p.get("body", ""), p.get("rationale", "")))
            except Exception as e:
                return f"(diff-S: parse error at #{r_id}: {e})"
        a_id, a_body, a_r = snaps[0]
        b_id, b_body, b_r = snaps[1]
        import difflib
        diff_lines = list(difflib.unified_diff(
            a_body.splitlines(), b_body.splitlines(),
            fromfile=f"#{a_id} {a_r[:40] or '(no rationale)'}",
            tofile=f"#{b_id} {b_r[:40] or '(no rationale)'}",
            lineterm="",
        ))
        if not diff_lines:
            return f"(diff-S {name}: #{a_id} == #{b_id}, no body change)"
        return "\n".join(diff_lines)

    def _rk_log() -> str:
        """(rk-log) — 現 session の intent / R / K / artifact / replay / test-S-R を時系列で。
        R の @replaces= がある場合は lineage を表示 (例: R#42 ← #18)。"""
        if env.db_conn is None or not env.record_sid:
            return _no_db_msg("rk-log")
        rows = env.db_conn.execute(
            "SELECT id, ts, kind, payload FROM meta_events "
            "WHERE session_id = ? AND kind IN "
            "  ('intent','R','K','S','artifact','replay','test-S-R','restore-S','skill') "
            "ORDER BY ts ASC",
            (env.record_sid,),
        ).fetchall()
        if not rows:
            return "(no R/K/S/artifact events in this session)"
        marker = {
            "intent": "[intent]", "R": "[R]", "K": "[K]", "S": "[S]",
            "artifact": "[art]", "replay": "[replay]",
            "test-S-R": "[test]", "restore-S": "[restore]", "skill": "[skill]",
        }
        lines = ["rk-log:"]
        for row_id, _ts, kind, payload in rows:
            head = (payload or "").split("\n", 1)[0]
            lineage = ""
            if kind == "R" and payload:
                for ln in payload.split("\n")[1:]:
                    if ln.startswith("@replaces="):
                        lineage = f" ← #{ln.split('=',1)[1]}"
                        break
            if kind == "S":
                # payload は JSON。 name + rationale だけ抽出して短く出す。
                try:
                    p = json.loads(payload)
                    name = p.get("name", "?")
                    r = p.get("rationale", "")
                    body_preview = p.get("body", "").replace("\n", " ")[:50]
                    head = f"{name} [{p.get('kind','?')}]" + (f" — {r}" if r else f" — {body_preview}")
                except Exception:
                    pass
            lines.append(f"  #{row_id} {marker.get(kind, '['+kind+']')} {head[:140]}{lineage}")
        return "\n".join(lines)

    return {
        "session-intent":    _session_intent,
        "commit-R":          _commit_R,
        "commit-K":          _commit_K,
        "commit-S":          _commit_S,
        "commit-artifact":   _commit_artifact,
        "rk-log":            _rk_log,
        "replay-with-K":     _replay_with_K,
        "diff-K":            _diff_K,
        "diff-S":            _diff_S,
        "S-history":         _S_history,
        "restore-S":         _restore_S,
        "test-S-against-R":  _test_S_against_R,
    }


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
    """(lookup name) — global 束縛の **現在値** を返す (Lambda はオブジェクトとして)。

    `(lambdas)` が info 文字列を返すのに対し、これは λ 本体を取り出せる。
    mutate / wrap で既存 λ を decorator で包む用途。

    late binding 保証: lambda 呼び出し中は env.bindings が {saved, captured, params} の
    merge に swap され captured (定義時 snapshot) が勝つが、 lookup はここで捕まえた
    global dict (swap されない実体) を先に読む。 これにより lambda body の中の
    (lookup "agent-step") は、 走行中の (define agent-step ...) を正しく拾う。
    """
    global_bindings = env.bindings  # install 時点の global 束縛 dict。 top-level define はここに書かれる
    def _lookup(name: Any) -> Any:
        if isinstance(name, Symbol):
            name = name.name
        name = str(name)
        if name in global_bindings:
            return global_bindings[name]
        # global に無ければ現在の (swap 済みかもしれない) bindings — 局所束縛も引けるように
        if name in env.bindings:
            return env.bindings[name]
        raise ValueError(f"lookup: not bound: {name}")
    return _lookup


_CTX_OVERFLOW_RE = _re.compile(
    r"context.{0,40}(length|window|limit)|maximum context|too many tokens"
    r"|prompt is too long|input.{0,20}too long|exceeds.{0,30}token",
    _re.IGNORECASE,
)


def _looks_like_context_overflow(e: Exception) -> bool:
    return bool(_CTX_OVERFLOW_RE.search(str(e)))


def _emergency_compact(env: Env) -> bool:
    """context overflow の緊急退避: 古い半分の turns を archive に落とし、 退避した旨の
    system turn を先頭に置く。 LLM 要約は使わない (overflow 中は呼べないため)。
    丁寧な圧縮 (要約つき) は auto.lispy の condense-context が閾値で先回りする — これは最後の網。"""
    n = len(env.turns)
    if n < 4:
        return False
    cut = n // 2
    archive_id = uuid.uuid4().hex[:8]
    archived = env.turns[:cut]
    kept = env.turns[cut:]
    # tool turn が先頭に残ると対応する assistant tool_calls を失って API が弾く — 一緒に退避
    while kept and kept[0].role == "tool":
        archived.append(kept.pop(0))
    if not kept:
        return False
    env.archive[archive_id] = archived
    env.turns = [Turn(
        role="system",
        content=(f"[context overflow — 古い {len(archived)} turns を archive {archive_id} に退避した。"
                 f" 失われた文脈が必要なら recall で辿れる]"),
    )] + kept
    return True


def _capture_usage(env_arg: Env, resp: Any) -> None:
    usage = getattr(resp, "usage", None)
    if usage is not None:
        try:
            env_arg.last_prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        except (TypeError, ValueError):
            pass


def _prim_llm_call(env_arg: Any, *opts: Any) -> Turn:
    """(llm-call env [k v ...]) — env.to_messages() を LLM に投げ、 assistant Turn を返す。

    option は plist 形式で渡す (defmacro / fork-env と同じ慣習):
      'temperature 1.5     — sampling 温度
      'max-tokens 4096     — token 上限 (default .env の LLM_MAX_TOKENS、 未設定なら 4096)
      'logprobs #t         — 各 token の logprob を Turn.logprobs に詰める
      'top-logprobs 5      — 各 token に対して上位 N 候補も取る (要 'logprobs #t)
      'think #t            — thinking モード (default .env の LLM_THINK)
      'extra (list k v...) — 任意の追加 field を OpenAI SDK の extra_body に流す。
                             kebab-case の key は snake_case に変換される。
                             ds4 拡張 (dir-steering-ffn 等) はここから通す。

    agent loop を S 式で書くための基盤。 template 展開 / auto-eval / 履歴 append は **しない**。
    呼び出し側で `(append-turn env response)` を打つ責任がある (= loop の規則が S 式に出る)。
    """
    if not isinstance(env_arg, Env):
        raise ValueError(f"llm-call: expected env, got {type(env_arg).__name__}")
    _check_interrupt(env_arg)
    if len(opts) % 2 != 0:
        raise ValueError("llm-call: options must be paired (key value ...)")
    opts_dict: dict[str, Any] = {}
    for i in range(0, len(opts), 2):
        k = opts[i].name if isinstance(opts[i], Symbol) else str(opts[i])
        opts_dict[k] = opts[i + 1]

    kwargs: dict[str, Any] = {}
    if "temperature" in opts_dict:
        kwargs["temperature"] = float(opts_dict["temperature"])
    if _truthy(opts_dict.get("logprobs", False)):
        kwargs["logprobs"] = True
        if "top-logprobs" in opts_dict:
            kwargs["top_logprobs"] = int(opts_dict["top-logprobs"])
    max_tok = int(opts_dict.get("max-tokens", host.MAX_TOKENS))
    extra = {"think": bool(_truthy(opts_dict.get("think", host.THINK)))}
    if "extra" in opts_dict:
        # 'extra (list 'dir-steering-ffn -1.0 'dir-steering-attn 0.5) のような plist。
        # ds4 拡張など、 lispy.py を再び触らずに provider 固有 field を流すための窓口。
        extra_arg = opts_dict["extra"]
        if not isinstance(extra_arg, list):
            raise ValueError("llm-call: 'extra value must be a plist (use list ...)")
        if len(extra_arg) % 2 != 0:
            raise ValueError("llm-call: 'extra plist must be paired (k v ...)")
        for i in range(0, len(extra_arg), 2):
            ek = extra_arg[i].name if isinstance(extra_arg[i], Symbol) else str(extra_arg[i])
            extra[ek.replace("-", "_")] = extra_arg[i + 1]

    client = host.get_client()

    def _create() -> Any:
        return client.chat.completions.create(
            model=host.MODEL,
            messages=env_arg.to_messages(),
            tools=env_arg.tool_schema or None,
            max_tokens=max_tok,
            extra_body=extra,
            **kwargs,
        )

    try:
        resp = _create()
    except Exception as e:
        # context overflow の緊急回復: 古い turns を退避して 1 回だけ retry
        if _looks_like_context_overflow(e) and _emergency_compact(env_arg):
            resp = _create()
        else:
            raise
    _capture_usage(env_arg, resp)
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
    # logprobs を抽出 (API が返した場合のみ)。 SDK の dataclass を素朴な dict に降ろす。
    logprobs_out: list[dict] = []
    lp = getattr(resp.choices[0], "logprobs", None)
    if lp is not None:
        content_lp = getattr(lp, "content", None) or []
        for item in content_lp:
            logprobs_out.append({
                "token": getattr(item, "token", "") or "",
                "logprob": float(getattr(item, "logprob", 0.0)),
            })
    return Turn(
        role="assistant", content=content,
        tool_calls=tool_calls, logprobs=logprobs_out,
    )


def _prim_judge_call(env_arg: Any, *opts: Any) -> Turn:
    """(judge-call env [k v ...]) — 審査者 LLM に env.to_messages() を投げ、 assistant Turn を返す。

    llm-call と同形だが 3 点違う:
      - client / model は .env の JUDGE_* (未設定なら executor の LLM_* に fallback —
        その場合「別モデルの独立審査」 ではなく 「同じ重みの別文脈審査」 に弱まる)
      - tools を渡さない — 審査者は tool を呼ばない (判定のみ)
      - max-tokens default は JUDGE_MAX_TOKENS

    auto.lispy の judge-done と define-gate がこれを使う。 どの判定をどちらのモデルに
    投げるかを S 式側で選べるようにするための primitive (層 1 保護対象)。
    """
    if not isinstance(env_arg, Env):
        raise ValueError(f"judge-call: expected env, got {type(env_arg).__name__}")
    _check_interrupt(env_arg)
    if len(opts) % 2 != 0:
        raise ValueError("judge-call: options must be paired (key value ...)")
    opts_dict: dict[str, Any] = {}
    for i in range(0, len(opts), 2):
        k = opts[i].name if isinstance(opts[i], Symbol) else str(opts[i])
        opts_dict[k] = opts[i + 1]
    kwargs: dict[str, Any] = {}
    if "temperature" in opts_dict:
        kwargs["temperature"] = float(opts_dict["temperature"])
    max_tok = int(opts_dict.get("max-tokens", host.JUDGE_MAX_TOKENS))

    client = host.get_judge_client()
    resp = client.chat.completions.create(
        model=host.judge_model(),
        messages=env_arg.to_messages(),
        max_tokens=max_tok,
        **kwargs,
    )
    msg = resp.choices[0].message
    return Turn(role="assistant", content=msg.content or "")


def _prim_agent_eval_factory(env: Env) -> Callable[..., Any]:
    """(agent-eval text) — text が S 式なら **agent 由来として** 評価し、 結果テキストを返す。
    S 式でなければ #f。

    LLM 応答の auto-eval 経路 (init.lispy の agent-step が呼ぶ)。 eval_origin を "agent" に
    立てて評価するので、 保護 binding への define / set! は define-gate の審査を通る。
    人間の REPL 入力 (origin "user") とはここで区別される。
    """
    def _agent_eval(text: Any) -> Any:
        tree = _try_read_sexp(str(text))
        if tree is None:
            return False
        prev = env.eval_origin
        env.eval_origin = "agent"
        try:
            v = eval_sexp(tree, env)
        finally:
            env.eval_origin = prev
        return v.text
    return _agent_eval


def _prim_append_turn(env_arg: Any, turn: Any) -> Any:
    """(append-turn env turn) — env.turns に turn を append、env を返す。

    内部は mutation だが、user's draft の functional style と整合させるため env を return。
    `(let ((env2 (append-turn env t1))) ...)` のように env2 = env として使える。

    生層の完全化: tool の実行結果・tool_calls 付き assistant turn・<eval-result> は
    ここで host DB にも記録する (REPL の _record は user 入力と最終応答しか書かないため、
    従来は brainwash の VERIFY が照合すべき一次資料 = tool 結果が生層から欠けていた)。
    """
    if not isinstance(env_arg, Env):
        raise ValueError(f"append-turn: expected env, got {type(env_arg).__name__}")
    if isinstance(turn, Turn):
        env_arg.turns.append(turn)
        if env_arg.record_sid and env_arg.db_conn is not None:
            try:
                if turn.role == "tool":
                    host.append_turn(env_arg.db_conn, env_arg.record_sid, "tool",
                                     turn.content or "", cwd=os.getcwd())
                elif turn.role == "assistant" and turn.tool_calls:
                    names = ", ".join(
                        tc.get("function", {}).get("name", "?") for tc in turn.tool_calls
                    )
                    summary = ((turn.content + " ") if turn.content else "") + f"(tool_calls: {names})"
                    host.append_turn(env_arg.db_conn, env_arg.record_sid, "assistant",
                                     summary, cwd=os.getcwd())
                elif turn.role == "user" and (turn.content or "").startswith("<eval-result>"):
                    host.append_turn(env_arg.db_conn, env_arg.record_sid, "user",
                                     turn.content, cwd=os.getcwd())
            except Exception:
                pass  # 記録失敗で loop は止めない
    return env_arg


def _prim_dispatch_tool_factory(env: Env) -> Callable[..., Any]:
    """(dispatch-tool name args-json-string) — env.tools[name] を引数 dict で呼ぶ。
    pre-tool / post-tool hook と中断チェックはこの経路に入っている。"""
    def _dispatch(name: Any, args_json: Any = "{}") -> str:
        _check_interrupt(env)
        return _execute_tool(env, name, args_json)
    return _dispatch


def _prim_dispatch_tools_factory(env: Env) -> Callable[..., Any]:
    """(dispatch-tools tcs) — tool_call dict の list を実行し、 tool Turn の list を返す。

    batch が全部 read-only (READONLY_TOOLS) なら ThreadPool で並列、 副作用系が 1 つでも
    混ざれば発行順の直列 (順序に意味があるため)。 返る Turn の順序は常に入力順。
    agent-step はこれを fold (append-turn) するだけ — 並列化の判断は Python 側の床。
    """
    def _dispatch_all(tcs: Any) -> list:
        if not isinstance(tcs, list):
            raise ValueError(f"dispatch-tools: list of tool_calls expected, got {type(tcs).__name__}")
        _check_interrupt(env)
        items: list[tuple[str, str, str]] = []
        for tc in tcs:
            if isinstance(tc, dict):
                items.append((
                    tc.get("id", ""),
                    tc.get("function", {}).get("name", ""),
                    tc.get("function", {}).get("arguments", "{}"),
                ))
        if all(n in READONLY_TOOLS for _tid, n, _a in items) and len(items) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
                results = list(ex.map(lambda it: _execute_tool(env, it[1], it[2]), items))
        else:
            results = []
            for it in items:
                _check_interrupt(env)
                results.append(_execute_tool(env, it[1], it[2]))
        return [
            Turn(role="tool", content=r, tool_call_id=tid)
            for (tid, _n, _a), r in zip(items, results)
        ]
    return _dispatch_all


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

    def transcript(n: Any = 20) -> str:
        """(transcript [n]) — 直近 n turn の全文を "role: content" 形式で返す。

        turns_info (= (turns)) が 80 字 preview なのに対し、 こちらは全文。
        auto-renew の要約素材や、 judge に作業ログを渡す用途。
        """
        out = []
        for t in env.turns[-int(n):]:
            c = t.content or ""
            if t.tool_calls:
                names = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in t.tool_calls
                )
                c = (c + " " if c else "") + f"(tool_calls: {names})"
            out.append(f"{t.role}: {c}")
        return "\n".join(out) or "(empty)"

    return {
        "env": env_info,
        "turn": turn_content,
        "turns": turns_info,
        "turn-count": lambda: len(env.turns),
        "transcript": transcript,
        "archive": archive_info,
        "lambdas": lambdas_info,
        "quoted": quoted_info,
        # context 観測 — agent-step の auto-compaction 判定と REPL での確認用
        "context-tokens": lambda: env.last_prompt_tokens,
        "context-limit": lambda: host.CTX_WINDOW,
        "context-over?": lambda: env.last_prompt_tokens > int(host.CTX_WINDOW * 0.8),
    }


PRIMITIVES: dict[str, Callable[..., Any]] = {
    "+": lambda *args: sum(args),
    "-": _prim_sub,
    "*": lambda *args: reduce(operator.mul, args, 1),
    "/": _prim_div,
    # 整数除算と剰余。 / は float に縮退するので別物として用意。
    "quotient":  lambda a, b: int(a) // int(b),
    "remainder": lambda a, b: int(a) % int(b),
    "mod":       lambda a, b: int(a) % int(b),     # remainder の alias
    "abs":       lambda a: abs(a),
    "min":       lambda *args: min(args),
    "max":       lambda *args: max(args),
    "=": lambda a, b: a == b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    # and / or は special form (短絡評価) として _SPECIAL_FORM_NOEVAL 側で定義済
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
    # tool-calls は agent-step / init.lispy の素朴な用語。 turn-tool-calls は accessor 命名規則の対称形。
    # 中身は同じだが、 turn-tool-calls が「正」 (turn-role / turn-content / turn-id と並ぶ accessor)、
    # tool-calls はその alias として残す (古い code を壊さない)。
    "turn-tool-calls":  lambda t: list(t.tool_calls) if isinstance(t, Turn) else [],
    "tool-calls":       lambda t: list(t.tool_calls) if isinstance(t, Turn) else [],
    "turn-id":          lambda t: t.id if isinstance(t, Turn) else "",
    # logprobs 観測 — llm-call に 'logprobs #t を渡したときだけ意味を持つ。
    # turn-logprobs:   list of (token logprob) ペア (2-elem list の list)
    # turn-entropy:    平均 -logprob (= cross-entropy in nats)。 logprobs 空なら 0.0。
    "turn-logprobs":    lambda t: [[d.get("token", ""), float(d.get("logprob", 0.0))]
                                    for d in (t.logprobs if isinstance(t, Turn) else [])],
    "turn-entropy":     lambda t: (
        sum(-float(d.get("logprob", 0.0)) for d in t.logprobs) / max(len(t.logprobs), 1)
        if isinstance(t, Turn) and t.logprobs else 0.0
    ),
    "has-tool-calls?":  lambda t: bool(t.tool_calls) if isinstance(t, Turn) else False,
    # tool_call (OpenAI 形式 dict) の accessor — args は JSON 文字列で返す
    "tool-call-id":     lambda tc: tc.get("id", "") if isinstance(tc, dict) else "",
    "tool-call-name":   lambda tc: tc.get("function", {}).get("name", "") if isinstance(tc, dict) else "",
    "tool-call-args":   lambda tc: tc.get("function", {}).get("arguments", "{}") if isinstance(tc, dict) else "{}",
    # message (OpenAI 形式 dict) constructor — llm-call に渡す素材
    "make-message":     lambda role, content: {"role": str(role), "content": str(content)},
    # 型述語 — match macro / 一般的な dispatch で使う。 string? は string 群と一緒に。
    "list?":     lambda x: isinstance(x, list),
    "symbol?":   lambda x: isinstance(x, Symbol),
    "number?":   lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
    "integer?":  lambda x: isinstance(x, int) and not isinstance(x, bool),
    "boolean?":  lambda x: isinstance(x, bool),
    "lambda?":   lambda x: isinstance(x, Lambda),
    "pair?":     lambda x: isinstance(x, list) and len(x) > 0,
    "eq?":       lambda a, b: a is b or a == b,
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


def _prim_fork_env(env_arg: Any, *opts: Any) -> "Env":
    """(fork-env env [k v ...]) — env を deep-ish copy。 turns / archive / quoted / bindings は
    独立 (元に変更が漏れない)、 tools / tool_schema / db_conn / record_sid は共有 (副作用集約のため)。

    optional k/v は override。 例:
      (fork-env (env))                                    ;; clone current
      (fork-env (env) 'system "you are a critic")         ;; system override
      (fork-env (env) 'system "..." 'name "alt")          ;; multiple

    返り値は新しい Env オブジェクト。 spawn は (eval-with) や (llm-call) と組み合わせて構成可。
    """
    if not isinstance(env_arg, Env):
        raise ValueError(f"fork-env: 1st arg must be env (got {type(env_arg).__name__})")
    # k v ペアを dict に
    overrides: dict[str, Any] = {}
    if len(opts) % 2 != 0:
        raise ValueError("fork-env: options must be paired (key value ...)")
    for i in range(0, len(opts), 2):
        k = opts[i]
        if isinstance(k, Symbol):
            k = k.name
        overrides[str(k)] = opts[i + 1]
    # 既定: 元 env の独立 copy。 tools/tool_schema は共有 (重い + 通常共通)、
    # db_conn / record_sid も共有 (記録は 1 箇所に集約する設計)。
    new_env = Env(
        system=str(overrides.get("system", env_arg.system)),
        turns=list(env_arg.turns),
        tools=env_arg.tools,
        tool_schema=env_arg.tool_schema,
        archive={k: list(v) for k, v in env_arg.archive.items()},
        quoted=dict(env_arg.quoted),
        bindings=dict(env_arg.bindings),
        name=str(overrides.get("name", f"{env_arg.name}/fork")),
        depth=env_arg.depth + 1,
        db_conn=env_arg.db_conn,
        record_sid=env_arg.record_sid,
        lambda_call_depth=0,
        input_mode=env_arg.input_mode,
        macros=dict(env_arg.macros),
        eval_origin=env_arg.eval_origin,
        gate=env_arg.gate,
        interrupt=env_arg.interrupt,
        last_prompt_tokens=env_arg.last_prompt_tokens,
    )
    return new_env


def _make_child_env(env: Env, system: str = "", name_suffix: str = "spawn") -> Env:
    """subagent 用の child env を組む。

    親から tools / tool_schema / db_conn / record_sid を継承し、 bindings は
    PRIMITIVES + _install_meta_primitives でフル装備にする (init.lispy の agent-step 込み)。
    turns は空 — subagent は親の会話履歴を見ない。 task は自己完結で書く前提。
    """
    child = Env(
        system=system or env.system,
        tools=env.tools,
        tool_schema=env.tool_schema,
        bindings=dict(PRIMITIVES),
        name=f"{env.name}/{name_suffix}",
        depth=env.depth + 1,
        db_conn=env.db_conn,
        record_sid=env.record_sid,
    )
    child.interrupt = env.interrupt  # 中断は親子共有 — 止めるときは subagent ごと止まる
    _install_meta_primitives(child)
    return child


def _form_spawn(env: Env, task: str) -> Value:
    """新しい child env を作って task を評価させる。subagent 相当だが env が独立。

    depth 制限のみ。再帰禁止フラグは使わない。
    """
    if env.depth >= 3:
        return Value(text=f":spawn → depth limit ({env.depth}) reached")
    child = _make_child_env(env, name_suffix="spawn")
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


# ---------------------------------------------------------------------------
# spawn_agent — subagent を tool_call として呼ぶ (Claude Code の Task tool 相当)
# ---------------------------------------------------------------------------

SPAWN_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": (
            "独立した subtask を subagent (隔離された child env) に任せて、 最終結果だけ受け取る。 "
            "呼ぶのは: (a) 大量のファイル探索・調査で中間結果が本筋の文脈を汚すとき、 "
            "(b) 自分の成果物を独立した目で検証させたいとき (system で検証者 persona を指定)、 "
            "(c) 本筋と無関係な脇道の調査。 "
            "1-2 回の tool 呼び出しで済む作業には使わない (直接やる方が速い)。 "
            "subagent は親の会話履歴を一切見ないので、 task には必要な文脈・対象パス・"
            "期待する出力形式を全部書くこと。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "subagent への指示。 自己完結で書く (subagent は親の履歴を見ない)。",
                },
                "system": {
                    "type": "string",
                    "description": "subagent の system prompt を差し替える (任意)。 検証者・批評者などの persona 指定に使う。 省略時は親と同じ。",
                },
            },
            "required": ["task"],
        },
    },
}


def _spawn_agent_tool(args: dict, env: Env) -> str:
    """spawn_agent の tool handler。 child env で agent loop を回し、 最終テキストを返す。"""
    task = str(args.get("task", "")).strip()
    if not task:
        return "(spawn_agent: task が空。 自己完結の指示を書くこと)"
    if env.depth >= 3:
        return f"(spawn_agent: depth 制限 ({env.depth}) に達している。 subagent を使わず直接作業すること)"
    system = str(args.get("system", "") or "").strip()
    child = _make_child_env(env, system=system, name_suffix=f"agent{env.depth + 1}")
    try:
        result = eval_(child, task)
    except Exception as e:
        return f"(spawn_agent error: {type(e).__name__}: {e})"
    text = (result.text or "").strip() or "(subagent returned empty)"
    return f"[subagent {child.name} done]\n{text}"


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
    # subagent (Claude Code の Task tool 相当)。 handler は env を受け取り depth を継承する。
    tools["spawn_agent"] = _spawn_agent_tool
    schema.append(SPAWN_AGENT_SCHEMA)
    # MCP server の tool 群 (.lispy-mcp.json)。 optional import — mcp.py を消しても core は動く。
    # server プロセスは mcp module 側で cache されるので、 env (spawn child 含む) を跨いで共有。
    try:
        import mcp as _mcp
        mcp_tools, mcp_schema = _mcp.tool_layer()
        tools.update(mcp_tools)
        schema.extend(mcp_schema)
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
    # R/K 発見運動の primitive 群 (session-intent / commit-R / commit-K / commit-artifact / rk-log)
    env.bindings.update(_prim_rk_factory(env))
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
    env.bindings["dispatch-tools"] = _prim_dispatch_tools_factory(env)
    env.bindings["stop-hook"]     = _prim_stop_hook_factory(env)
    # 審査者 LLM (JUDGE_*、 未設定なら executor に fallback)。 judge-done と define-gate が使う。
    env.bindings["judge-call"]    = _prim_judge_call
    # LLM 応答の auto-eval 経路 (origin="agent" で評価 → define-gate が効く)
    env.bindings["agent-eval"]    = _prim_agent_eval_factory(env)
    # 洗脳 — 生層 (host.db turns) から蒸留層 (data/memory/) を作り直す consolidation。
    # 洗うのは judge LLM。 optional import: ファイルを消しても core は動く。
    try:
        import brainwash as _bw
        def _run_brainwash(*args: Any, _env: Env = env) -> str:
            if _env.db_conn is None:
                return "(brainwash: no db — record=True で session を開く必要あり)"
            sessions = [a.name if isinstance(a, Symbol) else str(a) for a in args]
            return _bw.brainwash(_env.db_conn, sessions=sessions or None)
        env.bindings["brainwash"] = _run_brainwash
        env.bindings["洗脳"]      = _run_brainwash
    except ImportError:
        pass
    # MCP の接続状態確認 (optional import)
    try:
        import mcp as _mcp_info
        env.bindings["mcp-list"] = _mcp_info.info
    except ImportError:
        pass
    # 旧 API (互換のため残す): env-messages / env-add-turn! は (env-messages env) / (env-add-turn! env t) でも使える
    env.bindings["env-messages"]  = lambda e=env: e.to_messages() if isinstance(e, Env) else env.to_messages()
    env.bindings["env-add-turn!"] = lambda e, t=None: _prim_append_turn(e, t) if t is not None else _prim_append_turn(env, e)
    env.bindings.update(_meta_factory(env))
    # env そのものを Lisp 値として exposing。symbol `env` → env オブジェクト。
    # 元の (env) info 文字列は `(env-info)` に rename。
    env.bindings["env-info"] = env.bindings.pop("env", lambda: "(env removed)")
    env.bindings["env"] = env
    # env を Lisp 値として fork する primitive。 spawn より素朴な、 first-class env 操作。
    env.bindings["fork-env"] = _prim_fork_env
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

    # 層 1 の保護対象 = seed load 前に Python 側が install した binding 名。
    # (primitive / host bridge / edit tool / meta / judge-call / agent-eval / env そのもの)
    py_installed = set(env.bindings.keys())

    # --- Lisp seed を init.lispy から load ---
    # compose と agent-step は「言語の核」 なので Python に文字列で埋めず、 init.lispy に置く。
    # 「核は S 式」 という宣言と実装を一致させる狙い。 走行中の (define agent-step ...) で上書き可能。
    # auto.lispy は自走レイヤ (auto-step / judge-done / auto-renew)。 init 同様 auto-load するが、
    # ファイルを消せば従来どおり seed のみで動く。
    # この時点では env.gate = None なので、 seed 自身の define は gate を通らない (bootstrap)。
    for fname in ("init.lispy", "auto.lispy"):
        fpath = _HERE / fname
        if fpath.exists():
            try:
                for form in read_all_sexp(fpath.read_text(encoding="utf-8")):
                    evaluate(form, env)
            except Exception as e:
                print(f"  (warning: {fname} load failed: {e})", file=sys.stderr)

    # --- define-gate を有効化 ---
    # agent 由来 (agent-eval 経由) の評価にだけ効く。 LISPY_GATE=off で無効化 (開発・実験用)。
    env.gate = Gate(
        protected_py=py_installed,
        protected_loop=set(PROTECTED_LOOP_BINDINGS),
        enabled=os.environ.get("LISPY_GATE", "on").strip().lower() not in ("off", "0", "false", "no"),
    )


def _open_recording(env: Env, sid: str = "") -> None:
    try:
        env.db_conn = host.init_db(host.DB_PATH)
        if sid:
            # 既存 session を引き継ぐ。 turns append 先が同じ sid になる。
            env.record_sid = sid
            host.ensure_session(env.db_conn, sid)
            host.log_meta(env.db_conn, "lispy_resume", sid=sid, payload="")
        else:
            env.record_sid = host.open_session(env.db_conn)
            host.log_meta(env.db_conn, "lispy_open", sid=env.record_sid, payload="")
    except Exception as e:
        print(f"  (warning: recording disabled: {e})", file=sys.stderr)
        env.db_conn = None
        env.record_sid = ""


def _skill_inventory() -> str:
    """skill の一覧を system prompt 用に作る (progressive disclosure)。

    探索先: (a) cwd から上方の .lispy/skills/ (プロジェクト skill)、 (b) lispy 本体の skills/。
    各 skill は <dir>/SKILL.md — 先頭付近の `name:` / `description:` 行を拾う。
    常駐するのは一覧 (1 skill 1 行) だけで、 本文はタスクに合致したとき agent が
    read_file で読む — 蒸留層の index-first と同じ規律。"""
    dirs: list[Path] = []
    d = Path(os.getcwd())
    for parent in [d, *d.parents]:
        cand = parent / ".lispy" / "skills"
        if cand.is_dir():
            dirs.append(cand)
            break
    builtin = _HERE / "skills"
    if builtin.is_dir() and builtin not in dirs:
        dirs.append(builtin)
    entries: list[str] = []
    for base in dirs:
        for md in sorted(base.glob("*/SKILL.md")):
            name, desc = md.parent.name, ""
            try:
                head = md.read_text(encoding="utf-8")[:2000]
            except Exception:
                continue
            for ln in head.splitlines():
                low = ln.strip().lower()
                if low.startswith("name:"):
                    name = ln.split(":", 1)[1].strip() or name
                elif low.startswith("description:"):
                    desc = ln.split(":", 1)[1].strip()
                    break
            entries.append(f"- {name}: {desc or '(no description)'} → {md}")
    if not entries:
        return ""
    return (
        "\n\n## skills\n"
        "タスクが以下のどれかに合致するなら、着手前にその SKILL.md を read_file で全文読み、手順に従う"
        " (合致しなければ読まない):\n" + "\n".join(entries) + "\n"
        "skill の手順どおりにやって詰まった・手順が現実とずれていたと分かったら、原因を特定して"
        "その SKILL.md を write_file / edit_file で更新すること — 凍結させない。"
        "更新は installer の審査を通る (却下なら理由が返るので、修正して再提案する)。\n"
    )


def _project_instructions() -> str:
    """cwd から上方に AGENTS.md / CLAUDE.md を探し、 最初に見つかったものを返す
    (Claude Code の CLAUDE.md / opencode の AGENTS.md 相当。 リポジトリの規約・
    ビルド方法を system prompt に注入する)。 無ければ空。"""
    d = Path(os.getcwd())
    for parent in [d, *d.parents]:
        for fname in ("AGENTS.md", "CLAUDE.md"):
            f = parent / fname
            if f.exists():
                try:
                    text = f.read_text(encoding="utf-8")[:20000]
                except Exception:
                    return ""
                return f"\n\n## project instructions ({f})\n{text}\n"
    return ""


def _last_session_id(db: Any) -> str:
    row = db.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
    return row[0] if row else ""


def _resume_context(env: Env, max_turns: int = 80, max_chars: int = 24000) -> None:
    """--resume: 同 session の過去会話を DB から text で復元して system turn として注入し、
    その session で commit-S された λ を最新 snapshot から restore する。

    role 構造の完全復元はしない (DB は tool_call_id を持たないので、 turn をそのまま
    messages に戻すと API に弾かれる)。 transcript 形式の 1 turn に畳むのが安全。"""
    if env.db_conn is None or not env.record_sid:
        return
    rows = env.db_conn.execute(
        "SELECT role, content FROM turns WHERE session_id = ? ORDER BY ts DESC LIMIT ?",
        (env.record_sid, max_turns),
    ).fetchall()
    if rows:
        lines: list[str] = []
        used = 0
        for role, content in rows:  # 新しい順に取って、 予算内で古い方向へ
            c = (content or "").strip()
            if len(c) > 1200:
                c = c[:1200] + " …"
            if used + len(c) > max_chars:
                break
            lines.append(f"{role}: {c}")
            used += len(c)
        lines.reverse()
        env.turns.append(Turn(
            role="system",
            content="[resume] 同 session の前回までの会話 (DB から復元):\n" + "\n".join(lines),
        ))
    # λ の復元 — この session で commit-S された名前を集めて restore-S (最新 snapshot)
    restore = env.bindings.get("restore-S")
    if callable(restore):
        try:
            srows = env.db_conn.execute(
                "SELECT payload FROM meta_events WHERE kind = 'S' AND session_id = ? ORDER BY ts ASC",
                (env.record_sid,),
            ).fetchall()
            names: list[str] = []
            for (payload,) in srows:
                try:
                    nm = json.loads(payload).get("name")
                    if nm and nm not in names:
                        names.append(nm)
                except Exception:
                    pass
            for nm in names:
                try:
                    restore(Symbol(nm))
                except Exception:
                    pass
        except Exception:
            pass


def build_default_env(record: bool = True, sid: str = "", resume: bool = False) -> Env:
    tools, schema = _build_tool_layer()
    env = Env(
        system=SYSTEM_PROMPT + _project_instructions() + _skill_inventory(),
        tools=tools,
        tool_schema=schema,
        bindings=dict(PRIMITIVES),
        name="main",
    )
    env.interrupt = threading.Event()
    _install_meta_primitives(env)
    if record:
        _open_recording(env, sid=sid)
    if resume:
        _resume_context(env)
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


def repl(sid: str = "", resume: bool = False) -> None:
    print("lispy REPL  (Ctrl+D to exit)")
    try:
        import edit as _edit_banner
        if _edit_banner.get_yolo():
            print("⚠️  YOLO mode — 副作用 tool の y/N 確認は skip されます。 (set-yolo #f) で戻す")
    except ImportError:
        pass
    print("Lisp core:  + - * / mod quotient abs min max = < > and or not  if  define  let")
    print("            quote  eval  apply  list  car  cdr  cons  null?  fold  compose")
    print("            string?  list?  symbol?  number?  integer?  boolean?  lambda?  pair?  eq?")
    print("            string-contains? string-prefix? string-append substring …")
    print("            read-sexp  prompt  strip-code-fences")
    print("LLM lambda: (lambda name (p) \"body\")        apply: (f x)")
    print("Varargs:    (lambda (a &rest xs) ...)  — 残余を xs (list) に bind")
    print("Macros:     (defmacro name (p) body)   `x ,x ,@x  (macroexpand-1 'form) (gensym)")
    print("Errors:     (try expr (catch (e) handler))  (error \"msg\")  (error? v) (error-message e)")
    print("Mutable:    (set! name expr)   (box v) (unbox b) (set-box! b v) (box? v)")
    print("TCO:        (recur a b ...)  — nearest lambda を tail call、 stack 消費しない")
    print("Env meta:   env (turns) (turn \"last-assistant\") (archive) (lambdas) (quoted)")
    print("            (renew \"carry\")  (quote-turn expr)  (eval-turn id)  (spawn \"task\")")
    print("            (fork-env env 'system \"...\")  — env を first-class に copy + override")
    print("LLM opts:   (llm-call env 'temperature 1.5 'logprobs #t 'max-tokens 4096)")
    print("            (llm-call env 'extra (list 'dir-steering-ffn -1.0))  — provider 固有 field")
    print("            (turn-logprobs t) (turn-entropy t)  — 観測の Lisp 値化")
    print("R/K event:  (session-intent \"...\")  (commit-R \"...\" ['replaces N])")
    print("            (commit-K 'name \"...\")  (commit-artifact \"label\" expr)")
    print("            (replay-with-K env id)  (diff-K env1 env2)  (test-S-against-R)")
    print("S lineage:  (commit-S 'name [\"rationale\"])  (S-history 'name)")
    print("            (restore-S 'name [id])  (diff-S 'name id1 id2)")
    print("            (rk-log)  — meta_events に記録、 session_id 単位で検索可能")
    print("Higher:     (set-mode <lambda>)  (clear-mode)  — 平文入力を λ 経由に")
    print("            (eval-turn-pure id)  — env を汚さず再評価 (probe 用素材)")
    print("Auto:       (auto-step env \"goal\" [rounds])  — 作業→検証→継続の自走ループ (auto.lispy)")
    print("            (turn-count) (transcript n)  — 文脈量の観測 / 作業ログ全文")
    print("Gate:       agent 由来の loop 書き換えは define-gate が審査 (judge は .env の JUDGE_*、")
    print("            未設定なら executor に fallback。 LISPY_GATE=off で無効。 人間の REPL 入力は自由)")
    print("Memory:     (洗脳) / (brainwash)  — 生層 (turns) を裏どりして蒸留層 data/memory/ を書き直す")
    print("            読みは index-first: agent は data/memory/index.md を read_file (検索しない)")
    print("Harness:    Ctrl-C = step 境界で中断 (2 回で強制) / (undo n) = file 編集の巻き戻し")
    print("            (context-tokens) (context-over?) — 8 割超えで auto-compaction")
    print("            .lispy-hooks.json (pre-tool/post-tool/stop) / .lispy-check (編集後チェック)")
    print("            --resume で前回 session の会話 + commit-S 済み λ を復元")
    print("REPL meta:  !env !archive !quoted !lambdas !turns !reset")
    print("plain text → model. S-expr input → direct evaluation.")
    env = build_default_env(sid=sid, resume=resume)
    if env.record_sid:
        note = " (resumed)" if resume and sid else ""
        print(f"(recording to session {env.record_sid[:12]}{note})")

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
                # 通常の式評価。 評価中の Ctrl-C は「即死」 ではなく interrupt flag を立て、
                # llm-call / dispatch-tool が step 境界で拾って安全に止まる (2 回目で強制中断)。
                def _on_sigint(signum: Any, frame: Any) -> None:
                    if env.interrupt is not None and not env.interrupt.is_set():
                        env.interrupt.set()
                        print("\n  (interrupt — 現在の step 境界で停止します。 もう一度 Ctrl-C で強制中断)",
                              file=sys.stderr)
                    else:
                        raise KeyboardInterrupt
                prev_handler = signal.signal(signal.SIGINT, _on_sigint)
                try:
                    value = eval_(env, line)
                except KeyboardInterrupt:
                    print("  (force interrupted)", file=sys.stderr)
                    continue
                except Exception as e:
                    print(f"  eval error: {e}", file=sys.stderr)
                    continue
                finally:
                    signal.signal(signal.SIGINT, prev_handler)
                    if env.interrupt is not None:
                        env.interrupt.clear()

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
    p.add_argument("--session", default="",
                   help="既存 session id (prefix 一致) を引き継ぐ")
    p.add_argument("--resume", action="store_true",
                   help="session の会話を DB から復元 + commit-S 済み λ を restore。 "
                        "--session 省略時は直近の session を対象にする")
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
        sid = ""
        if args.session or args.resume:
            try:
                db = host.init_db(host.DB_PATH)
                try:
                    sid = (host.resolve_session(db, args.session)
                           if args.session else _last_session_id(db))
                finally:
                    db.close()
            except Exception as e:
                print(f"  (session resolve failed: {e})", file=sys.stderr)
        repl(sid=sid, resume=args.resume and bool(sid))


if __name__ == "__main__":
    main()
