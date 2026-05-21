#!/usr/bin/env python3
"""server.py — lispy を HTTP で叩ける常駐プロセスにする薄い層。

env は server プロセスに 1 つ抱える。 起動時に extras.lispy をロード。
Claude / nl REPL / 別の bash 端末から **同じ env を共有** できる。

endpoints:
  GET  /                healthz: {ok, bindings, tools, session_id}
  POST /eval            body=<S 式>             eval して {ok, result, stdout, error?}
  POST /load            body=<path>            ファイルから read_all_sexp → 全 form を eval
  GET  /bindings        env.bindings の名前一覧
  GET  /recall?q=&k=&mode=  host の trajectory recall を直接叩く
  POST /reset           env を作り直す (旧 session を close → 新 session を open)

CLI:
  --host                bind host (default 127.0.0.1)
  --port                bind port (default 9000)
  --yolo                shell の y/N 確認を全 skip (常駐 process では実質必須)
  --session <id>        既存 session id (prefix 一致) に append する
  --stdin               stdin からも S 式を読む (server と並列の REPL 兼用)

evaluation は serialized (threading.Lock)。 同時 mutation は許さない。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, parse_qs

import lispy
import host

_HERE = Path(__file__).resolve().parent
_LOCK = threading.Lock()


def _load_extras(env: lispy.Env) -> None:
    p = _HERE / "extras.lispy"
    if not p.exists():
        return
    for form in lispy.read_all_sexp(p.read_text(encoding="utf-8")):
        try:
            lispy.eval_sexp(form, env)
        except Exception as e:
            print(f"  (extras load: {e})", file=sys.stderr)


def _value_text(v: Any) -> str:
    if isinstance(v, lispy.Value):
        return v.text or ""
    return lispy._to_lisp_string(v)


def _eval_src(env: lispy.Env, src: str) -> dict:
    """ロック取得 + stdout redirect + 全 form eval。 返り値は JSON 化用 dict。"""
    buf = io.StringIO()
    with _LOCK:
        try:
            with redirect_stdout(buf):
                last: Any = None
                for form in lispy.read_all_sexp(src):
                    last = lispy.eval_sexp(form, env)
            return {
                "ok": True,
                "result": _value_text(last) if last is not None else None,
                "stdout": buf.getvalue(),
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "stdout": buf.getvalue(),
            }


def _resume_or_new(sid_arg: str) -> str:
    """--session で与えた prefix から完全 sid を返す。 空なら空のまま (新規)。"""
    if not sid_arg:
        return ""
    db = host.init_db(host.DB_PATH)
    try:
        return host.resolve_session(db, sid_arg)
    except SystemExit as e:
        # resolve_session は ambiguous / 不存在で SystemExit する → 戻す
        print(f"  (--session 解決失敗: {e}; 新規 session を作る)", file=sys.stderr)
        return ""


def make_handler(env_box: list[lispy.Env]):
    """env_box は 1 要素 list。 /reset で box[0] を差し替える。"""

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return  # アクセスログを黙らせる

        # ----- helpers -----
        def _send_json(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> str:
            n = int(self.headers.get("content-length", 0) or 0)
            if n <= 0:
                return ""
            return self.rfile.read(n).decode("utf-8", errors="replace")

        # ----- GET -----
        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            env = env_box[0]
            if path in ("/", "/healthz"):
                self._send_json(200, {
                    "ok": True,
                    "bindings": len(env.bindings),
                    "tools": len(env.tools),
                    "session_id": env.record_sid,
                })
                return
            if path == "/bindings":
                names = sorted(k for k in env.bindings.keys() if not k.startswith("_"))
                self._send_json(200, {"ok": True, "count": len(names), "names": names})
                return
            if path == "/recall":
                qs = parse_qs(url.query)
                q = (qs.get("q", [""])[0] or "").strip()
                if not q:
                    self._send_json(400, {"ok": False, "error": "?q= required"})
                    return
                k = int(qs.get("k", ["5"])[0] or 5)
                mode = qs.get("mode", ["auto"])[0] or "auto"
                try:
                    text = host._tool_recall({"query": q, "k": k, "mode": mode})
                    self._send_json(200, {"ok": True, "result": text})
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                return
            self._send_json(404, {"ok": False, "error": f"no route: {path}"})

        # ----- POST -----
        def do_POST(self):
            url = urlsplit(self.path)
            path = url.path
            env = env_box[0]
            if path in ("/", "/eval"):
                src = self._read_body()
                if not src.strip():
                    self._send_json(400, {"ok": False, "error": "empty body"})
                    return
                self._send_json(200, _eval_src(env, src))
                return
            if path == "/load":
                target = self._read_body().strip()
                if not target:
                    self._send_json(400, {"ok": False, "error": "body must be a file path"})
                    return
                p = Path(target).expanduser()
                if not p.exists():
                    self._send_json(404, {"ok": False, "error": f"not found: {p}"})
                    return
                try:
                    src = p.read_text(encoding="utf-8")
                except OSError as e:
                    self._send_json(500, {"ok": False, "error": f"read error: {e}"})
                    return
                self._send_json(200, _eval_src(env, src))
                return
            if path == "/reset":
                with _LOCK:
                    try:
                        lispy.close_recording(env_box[0])
                    except Exception:
                        pass
                    new_env = lispy.build_default_env(record=True)
                    _load_extras(new_env)
                    env_box[0] = new_env
                self._send_json(200, {
                    "ok": True,
                    "session_id": env_box[0].record_sid,
                    "bindings": len(env_box[0].bindings),
                })
                return
            self._send_json(404, {"ok": False, "error": f"no route: {path}"})

    return H


def _stdin_repl(env_box: list[lispy.Env]) -> None:
    """stdin から S 式を 1 行ずつ読んで eval、 結果を stdout に出す。

    server と並列で動く軽量 REPL。 多行 S 式はカッコバランスで継続。
    """
    try:
        while True:
            line = input("lispy> ")
            if not line.strip():
                continue
            buf = [line]
            while not lispy._parens_balanced("\n".join(buf)):
                try:
                    buf.append(input("...    "))
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
            src = "\n".join(buf)
            r = _eval_src(env_box[0], src)
            if r.get("stdout"):
                sys.stdout.write(r["stdout"])
            if r.get("ok"):
                if r.get("result") is not None:
                    print(r["result"])
            else:
                print(f";; {r['error']}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print()


def main() -> None:
    p = argparse.ArgumentParser(prog="lispy-server", description="HTTP server for lispy")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--yolo", action="store_true",
                   help="副作用 tool の y/N 確認を全 skip (常駐 process では実質必須)")
    p.add_argument("--session", default="",
                   help="既存 session id (prefix 一致) を引き継ぐ。 省略で新規 session")
    p.add_argument("--stdin", action="store_true",
                   help="stdin からも S 式を読む REPL を server と並列で起動")
    args = p.parse_args()

    if args.yolo:
        try:
            import edit as _edit
            _edit.set_yolo(True)
        except ImportError:
            pass

    sid = _resume_or_new(args.session)
    env = lispy.build_default_env(record=True, sid=sid)
    _load_extras(env)
    env_box: list[lispy.Env] = [env]

    sid_note = f"resumed {env.record_sid[:12]}" if sid else f"new session {env.record_sid[:12]}"
    print(f"lispy-server on http://{args.host}:{args.port}  ({sid_note})")
    print(f"  bindings: {len(env.bindings)}  tools: {len(env.tools)}")
    print("  endpoints: POST /eval /load /reset  GET / /bindings /recall?q=")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(env_box))

    if args.stdin:
        t = threading.Thread(target=_stdin_repl, args=(env_box,), daemon=True)
        t.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n(server stopped)")
    finally:
        try:
            lispy.close_recording(env_box[0])
        except Exception:
            pass


if __name__ == "__main__":
    main()
