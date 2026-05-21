#!/usr/bin/env python3
"""nl.py — 基本は lispy REPL。 日本語 (NL) を S 式に翻訳するだけの薄い層を被せる。

入力の振り分け:
  - `!...`           → lispy の meta コマンド (!env / !turns / !archive / !quoted /
                       !lambdas / !reset)
  - `@...`           → lispy の agent-step に直接渡す (NL 翻訳器を経由しない)
  - `(` で始まる     → S 式として評価 (継続行は括弧バランスで自動)
  - `\"\"\"` だけの行 → 次の `\"\"\"` 行までを NL の複数行入力として 1 つにまとめる
  - その他、平文     → env.input_mode が set されていればその lambda に渡す。
                       なければ LLM に S 式を返させ評価。

DB 記録は lispy REPL と同じく ON。 `host search` / `recall` で振り返れる。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import lispy
import host


_HERE = Path(__file__).resolve().parent

# eval 結果の最大長 (文字)。これ以上は truncate。
# 個別ターンの暴発 (read-file で全文) を防ぐ。 セッション通算の上限は設けない —
# 鬱陶しくなったら !reset (env.turns 共に翻訳履歴もクリア) を打つ運用。
_RESULT_SNIPPET_MAX = 600


def _load_nl_addendum() -> str:
    """nl.SYSTEM_PROMPT.md を読む。 翻訳器固有の addendum (lispy.SYSTEM_PROMPT に追記する分)。"""
    p = _HERE / "nl.SYSTEM_PROMPT.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


NL_ADDENDUM = _load_nl_addendum()


def _bindings_listing(env: lispy.Env) -> str:
    keys = sorted(k for k in env.bindings.keys() if not k.startswith("_"))
    return " ".join(keys)


def _tools_listing(env: lispy.Env) -> str:
    return " ".join(sorted(env.tools.keys()))


def _load_extras(env: lispy.Env) -> None:
    p = _HERE / "extras.lispy"
    if not p.exists():
        return
    for form in lispy.read_all_sexp(p.read_text(encoding="utf-8")):
        try:
            lispy.eval_sexp(form, env)
        except Exception as e:
            print(f"  (extras load: {e})", file=sys.stderr)


def _truncate(s: str, n: int = _RESULT_SNIPPET_MAX) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n;; … ({len(s) - n} 文字省略)"


def nl_to_sexp(
    client: Any,
    model: str,
    system: str,
    history: list[dict],
    user_text: str,
) -> str:
    messages = [{"role": "system", "content": system}, *history,
                {"role": "user", "content": user_text}]
    # max_tokens を必ず指定する。 LLM が degenerate mode に陥った場合の暴走止め。
    resp = client.chat.completions.create(
        model=model, messages=messages, temperature=0, max_tokens=2048,
    )
    text = (resp.choices[0].message.content or "").strip()
    text = lispy._strip_code_fences(text)
    # 防御: LLM が "(form)\n;; => fake-result\n<雑文>" を返してきたとき、
    # `;; =>` annotation 以降を捨てる。 fake annotation の後の雑文 (例: hallucinate した
    # git status 出力) を S 式として parse されないため。
    cut = text.find("\n;; =>")
    if cut != -1:
        text = text[:cut].rstrip()
    return text


def _read_balanced(first: str) -> str | None:
    if not first.startswith("(") or lispy._parens_balanced(first):
        return first
    buf = [first]
    while True:
        try:
            cont = input("... ")
        except (EOFError, KeyboardInterrupt):
            print("\n(input aborted)")
            return None
        buf.append(cont)
        joined = "\n".join(buf)
        if lispy._parens_balanced(joined):
            return joined


def _read_triple_quoted() -> str | None:
    buf: list[str] = []
    while True:
        try:
            l = input("... ")
        except (EOFError, KeyboardInterrupt):
            print("\n(input aborted)")
            return None
        if l.strip() == '"""':
            return "\n".join(buf)
        buf.append(l)


def _value_text(v: Any) -> str:
    if isinstance(v, lispy.Value):
        return v.text or ""
    return lispy._to_lisp_string(v)


def _print_value(v: Any) -> None:
    print(_value_text(v))
    if isinstance(v, lispy.Value) and v.directive:
        print(f"  [directive: {v.directive}]")


def main() -> None:
    one_shot: str | None = None
    if len(sys.argv) > 1 and sys.argv[1] in ("-e", "--eval"):
        one_shot = " ".join(sys.argv[2:]).strip()

    env = lispy.build_default_env(record=(one_shot is None))
    _load_extras(env)
    addendum = (
        NL_ADDENDUM
        .replace("<<BINDINGS>>", _bindings_listing(env))
        .replace("<<TOOLS>>", _tools_listing(env))
    )
    system = f"{lispy.SYSTEM_PROMPT}\n\n{addendum}"

    client = None
    history: list[dict] = []

    # ------------------------------------------------------------------
    # 評価ヘルパ — 共通で record と history 更新を行う
    # ------------------------------------------------------------------

    def _record_pair(user_content: str, assistant_text: str) -> None:
        lispy._record(env, "user", user_content)
        lispy._record(env, "assistant", assistant_text)

    def eval_sexp_src(sexp_src: str, history_user: str) -> None:
        try:
            forms = lispy.read_all_sexp(sexp_src)
            last: Any = None
            for form in forms:
                last = lispy.eval_sexp(form, env)
        except Exception as e:
            err = f"eval error: {e}"
            print(f"  {err}", file=sys.stderr)
            history.append({"role": "user", "content": history_user})
            history.append({
                "role": "assistant",
                "content": f"{sexp_src}\n;; => {err}",
            })
            _record_pair(history_user, err)
            return
        if last is None:
            return
        # eval 結果が nil (= Python None) のときは画面表示を抑制 — print 等の副作用
        # primitive が返す値を「結果」として再表示すると二重に見えて鬱陶しい。
        # 履歴・DB には "nil" を残して LLM が次ターンで認識できるようにする。
        is_nil = (
            isinstance(last, lispy.Value)
            and isinstance(last.payload, dict)
            and last.payload.get("value") is None
        )
        if not is_nil:
            _print_value(last)
        result_text = _value_text(last)
        history.append({"role": "user", "content": history_user})
        history.append({
            "role": "assistant",
            "content": f"{sexp_src}\n;; => {_truncate(result_text)}",
        })
        _record_pair(history_user, result_text)

    def handle_nl(nl_text: str) -> None:
        nonlocal client
        if not nl_text.strip():
            return
        if client is None:
            client = host.get_client()
        try:
            sexp = nl_to_sexp(client, host.MODEL, system, history, nl_text)
        except Exception as e:
            print(f"  llm error: {e}", file=sys.stderr)
            return
        print(f"  ;; {sexp}")
        eval_sexp_src(sexp, nl_text)

    def handle_agent(text: str) -> None:
        """@ 接頭辞: NL 翻訳器を介さず lispy.eval_ に渡す (agent-step ルート)。"""
        if not text:
            return
        try:
            value = lispy.eval_(env, text)
        except Exception as e:
            err = f"agent error: {e}"
            print(f"  {err}", file=sys.stderr)
            _record_pair(f"@{text}", err)
            return
        _print_value(value)
        _record_pair(f"@{text}", _value_text(value))

    def handle_input_mode(line: str) -> None:
        """env.input_mode が set された lambda 経由で平文を回す (lispy REPL 互換)。"""
        try:
            result = env.input_mode.apply([line])
        except Exception as e:
            err = f"mode error: {e}"
            print(f"  {err}", file=sys.stderr)
            _record_pair(line, err)
            return
        _print_value(result)
        _record_pair(line, _value_text(result))

    # ------------------------------------------------------------------
    # one-shot モード
    # ------------------------------------------------------------------

    if one_shot is not None:
        s = one_shot
        if s.startswith("!"):
            lispy._handle_meta(s, env)
        elif s.startswith("@"):
            handle_agent(s[1:].strip())
        elif s.startswith("("):
            eval_sexp_src(s, s)
        else:
            handle_nl(s)
        return

    # ------------------------------------------------------------------
    # 対話 REPL
    # ------------------------------------------------------------------

    print("nl REPL — 基本は lispy。 NL は LLM で S 式に翻訳して eval。 Ctrl+D で終了。")
    print(f"  bindings: {len(env.bindings)}  tools: {len(env.tools)}")
    print('  meta: !env !turns !archive !quoted !lambdas !reset')
    print('  agent-step に直接渡したいときは "@文" / 多行 NL は """ で挟む')
    if env.record_sid:
        print(f"  (recording to session {env.record_sid[:12]})")

    try:
        while True:
            try:
                raw = input("nl> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("!"):
                lispy._handle_meta(stripped, env)
                if stripped == "!reset":
                    history.clear()
                    print("(nl translator history also cleared)")
                continue
            if stripped.startswith("@"):
                handle_agent(stripped[1:].strip())
                continue
            if stripped == '"""':
                block = _read_triple_quoted()
                if block is None:
                    continue
                # env.input_mode が set されていれば NL 翻訳より優先
                if env.input_mode is not None:
                    handle_input_mode(block)
                else:
                    handle_nl(block)
                continue
            if raw.startswith("("):
                line = _read_balanced(raw)
                if line is None:
                    continue
                eval_sexp_src(line, line)
                continue
            # 平文: input_mode > NL 翻訳器
            if env.input_mode is not None:
                handle_input_mode(raw)
            else:
                handle_nl(raw)
    finally:
        lispy.close_recording(env)


if __name__ == "__main__":
    main()
