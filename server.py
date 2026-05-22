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
import html
import io
import json
import re
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


_SPEC_HTML_TEMPLATE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<title>lispy spec — {scope_label}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true,theme:'default',securityLevel:'loose'}});</script>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;
       max-width:900px;margin:2em auto;padding:0 1em;color:#222;line-height:1.6}}
  h1{{border-bottom:2px solid #333;padding-bottom:.3em}}
  h2{{margin-top:2em;border-bottom:1px solid #ccc;padding-bottom:.2em}}
  .meta{{color:#666;font-size:.9em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #ddd;padding:.4em .6em;text-align:left;vertical-align:top}}
  th{{background:#f6f6f6}}
  .id{{color:#888;font-family:ui-monospace,monospace;font-size:.85em}}
  .replaced{{text-decoration:line-through;color:#999}}
  .kind-R{{background:#fff3cd}}
  .kind-K{{background:#d4edda}}
  .kind-S{{background:#cce5ff}}
  .kind-artifact{{background:#f8d7da}}
  .kind-intent{{background:#e2e3e5}}
  .kind-test-S-R,.kind-replay,.kind-restore-S{{background:#f5f5f5}}
  .empty{{color:#999;font-style:italic}}
  pre{{background:#f6f6f6;padding:.5em;overflow-x:auto;font-size:.85em}}
  .timeline li{{margin:.3em 0}}
  .switch{{margin:1em 0;font-size:.9em}}
  .switch a{{margin-right:1em}}
</style></head><body>
<h1>lispy spec — {scope_label}</h1>
<div class="meta">{header_meta}</div>
<div class="switch">{scope_switch}</div>
{body}
</body></html>
"""


def _parse_replaces(payload: str) -> int | None:
    """R event の payload 末尾の `@replaces=N` 行から N を抽出。 無ければ None。"""
    if not payload:
        return None
    for ln in payload.split("\n")[1:]:
        if ln.startswith("@replaces="):
            try:
                return int(ln.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def _render_spec_html(env: lispy.Env, session_filter: str) -> str:
    """meta_events を読んで spec 一枚 HTML を render。

    session_filter:
      "current" → env.record_sid だけ (default)
      "all"     → 全 session を集約
      "<sid>"   → 特定 session prefix を resolve

    mermaid で R lineage の連鎖を描画、 S/K/artifact を section ごとに table。"""
    db = env.db_conn
    if db is None:
        return _SPEC_HTML_TEMPLATE.format(
            scope_label="(no db)",
            header_meta="env.db_conn is None — recording 無し起動?",
            scope_switch="",
            body='<p class="empty">no data</p>',
        )

    if session_filter == "all":
        rows = db.execute(
            "SELECT id, ts, session_id, kind, payload FROM meta_events "
            "WHERE kind IN ('intent','R','K','S','artifact','replay','test-S-R','restore-S') "
            "ORDER BY ts ASC"
        ).fetchall()
        scope_label = "all sessions"
    else:
        if session_filter == "current":
            sid = env.record_sid
        else:
            try:
                sid = host.resolve_session(db, session_filter)
            except SystemExit:
                sid = session_filter
        if not sid:
            return _SPEC_HTML_TEMPLATE.format(
                scope_label="(no session)",
                header_meta="env.record_sid が無く、 ?session= も未指定",
                scope_switch=_render_scope_switch("current"),
                body='<p class="empty">no session selected</p>',
            )
        rows = db.execute(
            "SELECT id, ts, session_id, kind, payload FROM meta_events "
            "WHERE session_id = ? AND kind IN "
            "  ('intent','R','K','S','artifact','replay','test-S-R','restore-S') "
            "ORDER BY ts ASC",
            (sid,),
        ).fetchall()
        scope_label = f"session {sid[:12]}…" if sid else "(no session)"

    if not rows:
        return _SPEC_HTML_TEMPLATE.format(
            scope_label=scope_label,
            header_meta="no R/K/S/artifact events yet",
            scope_switch=_render_scope_switch(session_filter),
            body='<p class="empty">何も積まれてない。 (commit-R ...) (commit-S ...) 等を打って戻る</p>',
        )

    # event を kind 別に整理
    by_kind: dict[str, list[tuple[int, float, str | None, str]]] = {
        "intent": [], "R": [], "K": [], "S": [],
        "artifact": [], "replay": [], "test-S-R": [], "restore-S": [],
    }
    for row in rows:
        rid, ts, sid_, kind, payload = row
        by_kind.setdefault(kind, []).append((rid, ts, sid_, payload or ""))

    # R lineage の mermaid graph — replaces を edge にする
    r_events = by_kind.get("R", [])
    if r_events:
        replaced_ids: set[int] = set()
        edges: list[tuple[int, int]] = []
        for rid, _ts, _sid, payload in r_events:
            prev = _parse_replaces(payload)
            if prev is not None:
                edges.append((prev, rid))
                replaced_ids.add(prev)

        mermaid_lines = ["graph LR"]
        for rid, _ts, _sid, payload in r_events:
            head = (payload or "").split("\n", 1)[0][:40]
            head_escaped = head.replace('"', "'")
            css = "replaced" if rid in replaced_ids else "active"
            mermaid_lines.append(f'  R{rid}["#{rid}: {head_escaped}"]:::{css}')
        for prev, cur in edges:
            mermaid_lines.append(f"  R{prev} -->|replaces| R{cur}")
        mermaid_lines.append("  classDef replaced fill:#eee,stroke:#aaa,color:#999")
        mermaid_lines.append("  classDef active fill:#fff3cd,stroke:#c70")
        r_section = (
            "<h2>R lineage</h2>\n"
            f"<div class='mermaid'>\n{chr(10).join(mermaid_lines)}\n</div>\n"
            + _render_event_table(r_events, "R")
        )
    else:
        r_section = '<h2>R lineage</h2>\n<p class="empty">commit-R event なし</p>\n'

    # S history — name でグループ化、 最新の rationale + body preview
    s_events = by_kind.get("S", [])
    s_by_name: dict[str, list[dict]] = {}
    for rid, ts, _sid, payload in s_events:
        try:
            p = json.loads(payload)
            name = p.get("name", "?")
            s_by_name.setdefault(name, []).append({
                "id": rid, "ts": ts,
                "kind": p.get("kind", "?"),
                "rationale": p.get("rationale", ""),
                "body": p.get("body", ""),
            })
        except Exception:
            continue
    if s_by_name:
        rows_html = []
        for name, entries in sorted(s_by_name.items()):
            entries_sorted = sorted(entries, key=lambda e: e["ts"])
            latest = entries_sorted[-1]
            history_str = " → ".join(
                f"#{e['id']}" for e in entries_sorted
            )
            body_preview = latest["body"].replace("\n", " ")[:120]
            rows_html.append(
                "<tr>"
                f"<td>{html.escape(name)}</td>"
                f"<td>{html.escape(latest['kind'])}</td>"
                f"<td class='id'>{history_str}</td>"
                f"<td>{html.escape(latest['rationale'] or '(no rationale)')}</td>"
                f"<td><code>{html.escape(body_preview)}</code></td>"
                "</tr>"
            )
        s_section = (
            "<h2>S (lambda snapshots)</h2>\n"
            "<table><thead><tr><th>name</th><th>kind</th><th>lineage</th>"
            "<th>latest rationale</th><th>body preview</th></tr></thead><tbody>\n"
            + "\n".join(rows_html)
            + "\n</tbody></table>\n"
        )
    else:
        s_section = '<h2>S (lambda snapshots)</h2>\n<p class="empty">commit-S event なし</p>\n'

    # K list — name + content
    k_events = by_kind.get("K", [])
    k_section = "<h2>K (knowledge)</h2>\n"
    if k_events:
        k_section += _render_event_table(k_events, "K")
    else:
        k_section += '<p class="empty">commit-K event なし</p>\n'

    # artifact list
    a_events = by_kind.get("artifact", [])
    a_section = "<h2>artifacts</h2>\n"
    if a_events:
        a_section += _render_event_table(a_events, "artifact")
    else:
        a_section += '<p class="empty">commit-artifact event なし</p>\n'

    # session-intent
    i_events = by_kind.get("intent", [])
    intent_section = ""
    if i_events:
        items = "".join(
            f"<li><span class='id'>#{rid}</span> {html.escape((p.split(chr(10),1)[0])[:200])}</li>"
            for rid, _ts, _sid, p in i_events
        )
        intent_section = f"<h2>session-intent</h2>\n<ul>{items}</ul>\n"

    # timeline (全 event の時系列、 末尾 30 件)
    tail = rows[-30:]
    timeline_items = []
    for rid, ts, sid_, kind, payload in tail:
        head = (payload or "").split("\n", 1)[0][:120]
        if kind == "S":
            try:
                p = json.loads(payload)
                head = f"{p.get('name','?')} [{p.get('kind','?')}] — {p.get('rationale') or p.get('body','')[:60]}"
            except Exception:
                pass
        sid_short = (sid_ or "?")[:8]
        timeline_items.append(
            f'<li><span class="id">#{rid}</span> '
            f'<span class="kind-{kind}">[{kind}]</span> '
            f'<span class="id">{sid_short}</span> '
            f'{html.escape(head)}</li>'
        )
    timeline_section = (
        "<h2>recent events (timeline, last 30)</h2>\n"
        f"<ul class='timeline'>{''.join(timeline_items)}</ul>\n"
    )

    body = intent_section + r_section + s_section + k_section + a_section + timeline_section
    header_meta = (
        f"events: {len(rows)}  /  "
        f"R: {len(r_events)}  K: {len(k_events)}  S: {len(s_events)}  "
        f"artifact: {len(a_events)}  intent: {len(i_events)}"
    )
    return _SPEC_HTML_TEMPLATE.format(
        scope_label=html.escape(scope_label),
        header_meta=html.escape(header_meta),
        scope_switch=_render_scope_switch(session_filter),
        body=body,
    )


def _render_event_table(events: list, kind: str) -> str:
    """1 つの kind の events を簡易 table に。"""
    rows = []
    for rid, _ts, sid, payload in events:
        head = (payload or "").split("\n", 1)[0][:200]
        sid_short = (sid or "?")[:8]
        rows.append(
            "<tr>"
            f"<td class='id'>#{rid}</td>"
            f"<td class='id'>{sid_short}</td>"
            f"<td>{html.escape(head)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>id</th><th>session</th><th>content</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>\n"
    )


def _render_scope_switch(current: str) -> str:
    """scope 切替リンク (current / all)。"""
    return (
        f'<a href="/spec">current session</a>'
        f'<a href="/spec?session=all">all sessions</a>'
    )


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

        def _send_html(self, code: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "text/html; charset=utf-8")
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
            if path == "/spec":
                qs = parse_qs(url.query)
                session_filter = qs.get("session", ["current"])[0] or "current"
                try:
                    with _LOCK:
                        body = _render_spec_html(env, session_filter)
                    self._send_html(200, body)
                except Exception as e:
                    self._send_html(
                        500, f"<h1>spec render error</h1><pre>{html.escape(str(e))}</pre>"
                    )
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
