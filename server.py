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
  GET  /view            ledger ダッシュボード (view.py)
  GET  /view/state      ダッシュボードのスナップショット (JSON、 pending gate / agent view 含む)
  GET  /view/events     SSE — ledger (meta_events) + turns の追記 + gate/view の変化通知
  POST /view/gate/<id>  pending gate の解決。 body = "approve" | "deny" (先着採用)
  POST /view/action     agent view の button 押下 (action 記号 + inputs) を queue に積む
  POST /view/comment    thread へのコメント。 body = {"text", "to": "executor"|"judge"} —
                        executor 宛は queue (auto-step が round 境界で注入)、
                        judge 宛は judge LLM が ledger を根拠に即応答 (別スレッド)
  POST /view/delegate   body = {"goal"} — goal を auto-step で自走させる (別スレッド、
                        評価が実行中なら 409)
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
import time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, parse_qs

import lispy
import host

# View 層は optional — view.py を消しても server core (/eval 等) は動く
# (lispy.py / edit.py の optional import と同じ規約)。 無ければ /view 系は 503。
try:
    import view
except ImportError:
    view = None  # type: ignore[assignment]

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
    """ページ間ナビ (view / spec / sessions) + spec の current/all 切替。
    /spec からも /view に戻れるよう横断リンクを常に出す。"""
    return (
        f'<a href="/view">view</a>'
        f'<a href="/sessions">sessions</a>'
        f'<a href="/spec">spec: current</a>'
        f'<a href="/spec?session=all">spec: all</a>'
    )


def _render_sessions_html() -> str:
    """セッション一覧ページ。 各行から /view?session=<id> / /spec?session=<id> に飛べる。
    データは view.sessions_list (host.cmd_list の SQL + goal + judge verdict)。"""
    try:
        db = view.open_ro()
    except Exception as e:
        return _SPEC_HTML_TEMPLATE.format(
            scope_label="sessions", header_meta=f"db open: {html.escape(str(e))}",
            scope_switch=_render_scope_switch("sessions"), body="")
    try:
        rows = view.sessions_list(db)
    finally:
        db.close()

    def _badge(v: dict | None) -> str:
        if v is None:
            return '<span style="color:#888">—</span>'
        if v.get("done"):
            return '<span style="color:#1a7a34">達成 ✅</span>'
        nxt = html.escape(v.get("next") or "")
        return f'<span style="color:#b26a00">未達 ⏳</span>' + (
            f'<div class="id">NEXT: {nxt}</div>' if nxt else "")

    trs = []
    for s in rows:
        sid = s["id"]
        when = host.local_from_ts(s["started_at"]).strftime("%Y-%m-%d %H:%M")
        goal = html.escape(s["goal"] or s["title"] or "")
        dom = f'[{html.escape(s["domain"])}]' if s["domain"] else ""
        trs.append(
            "<tr>"
            f'<td class="id">{when}</td>'
            f'<td><a href="/view?session={html.escape(sid)}">{html.escape(sid[:16])}</a>'
            f' <span class="id">· <a href="/spec?session={html.escape(sid)}">spec</a></span></td>'
            f"<td>{s['turns']} turns</td>"
            f"<td>{_badge(s.get('verdict'))}</td>"
            f"<td>{dom} {goal}</td>"
            "</tr>"
        )
    body = (
        "<h2>sessions</h2>"
        '<table><thead><tr><th>開始</th><th>session</th><th>turns</th>'
        "<th>達成?</th><th>goal / title</th></tr></thead>"
        f"<tbody>{''.join(trs) or '<tr><td colspan=5 class=empty>なし</td></tr>'}</tbody></table>"
    )
    return _SPEC_HTML_TEMPLATE.format(
        scope_label="sessions",
        header_meta=f"{len(rows)} sessions — 行クリックでそのセッションの view / spec へ",
        scope_switch=_render_scope_switch("sessions"),
        body=body,
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
        except (Exception, SystemExit) as e:
            # SystemExit も捕まえる — host.get_client が LLM 未設定で投げる。
            # 素通しすると handler / delegate スレッドが応答を返さず死ぬ。
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "stdout": buf.getvalue(),
            }
        finally:
            # /interrupt で set された flag は 1 回の評価にだけ効かせる。
            # 残すと以後の /eval が全部即死する (REPL 側の finally と同じ扱い)。
            if env.interrupt is not None:
                env.interrupt.clear()


def _log_comment(sid: str | None, author: str, to: str, text: str) -> None:
    """thread への 1 コメントを ledger (kind=comment) に刻む。 表示は /view の SSE が拾う。"""
    view._log_meta_rw("comment", sid, json.dumps(
        {"author": author, "to": to, "text": text[:4000]}, ensure_ascii=False))


JUDGE_REPLY_SYSTEM = (
    "あなたは lispy ハーネスの検証者 (judge)。人間がブラウザの thread から質問している。"
    "与えられた R (要件台帳)・計画・直近の作業ログだけを根拠に、日本語で簡潔に (5 行以内で) 答える。"
    "根拠がログに無いことは「ログに根拠がない」と言う。推測を事実のように書かない。tool は呼ばない。"
)


def _judge_reply(env_box: list, question: str) -> None:
    """judge 宛コメントへの応答 (人間 ↔ judge の直接チャネル)。 executor の loop を
    経由しない — _LOCK も取らない。 読みは read-only 接続、 書きは _log_meta_rw。"""
    sid = env_box[0].record_sid
    try:
        db = view.open_ro()
        try:
            rows = db.execute(
                "SELECT role, content FROM turns WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 40", (sid,)).fetchall()
            transcript = "\n".join(
                f"[{role}] {(content or '')[:600]}" for role, content in reversed(rows))
            actives = [r for r in view._rks(db, sid)["R"] if not r["replaced"]][-15:]
            r_text = "\n".join(f"R#{r['id']}: {r['head']}" for r in actives) or "(なし)"
            p = host.plan_latest(db)
            plan_text = f"{p.get('goal', '?')} ({len(p.get('steps') or [])} steps)" if p else "(なし)"
        finally:
            db.close()
        client = host.get_judge_client()
        resp = client.chat.completions.create(
            model=host.judge_model(),
            messages=[
                {"role": "system", "content": JUDGE_REPLY_SYSTEM},
                {"role": "user", "content":
                    f"R (要件台帳):\n{r_text}\n\n計画: {plan_text}\n\n"
                    f"直近の作業ログ:\n{transcript}\n\n人間の質問: {question}"},
            ],
            max_tokens=host.JUDGE_MAX_TOKENS,
        )
        out = (resp.choices[0].message.content or "").strip() or "(空応答)"
        _log_comment(sid, "judge", "human", out)
    except (Exception, SystemExit) as e:
        # host.get_client 系は未設定で SystemExit を投げる — thread を殺さず thread に残す
        _log_comment(sid, "system", "human", f"(judge 応答失敗: {type(e).__name__}: {e})")


def _delegate_run(env_box: list, goal: str) -> None:
    """/view/delegate の実行体。 _eval_src が _LOCK を取るので他の評価と直列化される。
    終了 (正常/異常) は必ず system コメントとして thread に残す — 黙って消えない。"""
    src = f"(auto-step env {json.dumps(goal, ensure_ascii=False)})"
    try:
        r = _eval_src(env_box[0], src)
    except (Exception, SystemExit) as e:
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    sid = env_box[0].record_sid
    if r.get("ok"):
        # 正常終了時の返り値は最終 assistant Turn の repr のことが多い — 中身は既に
        # post-round-report が thread に流している。 Turn repr はノイズなので落とす。
        result = r.get("result") or ""
        if result.startswith("Turn("):
            result = "(最終報告は上の executor コメントを参照)"
        _log_comment(sid, "system", "human", f"[委譲 run 終了] {result[:500] or '(結果なし)'}")
    else:
        _log_comment(sid, "system", "human", f"[委譲 run 失敗] {str(r.get('error'))[:500]}")


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
            if path.startswith("/view") and view is None:
                self._send_json(503, {"ok": False, "error": "view 層なし (view.py が無い)"})
                return
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
            if path == "/view":
                self._send_html(200, view.VIEW_HTML)
                return
            if path == "/sessions":
                try:
                    self._send_html(200, _render_sessions_html())
                except Exception as e:
                    self._send_html(
                        500, f"<h1>sessions error</h1><pre>{html.escape(str(e))}</pre>")
                return
            if path == "/view/state":
                # 読み取り専用接続で ledger を読む。 _LOCK は取らない —
                # eval が長時間 lock を握っていても view は固まらない。
                qs = parse_qs(url.query)
                scope = qs.get("session", ["current"])[0] or "current"
                try:
                    db = view.open_ro()
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": f"db open: {e}"})
                    return
                try:
                    sid = view.resolve_sid(db, scope, env.record_sid)
                    body = view.state_json(db, sid, scope)
                    # 要約 (直近 24h・全 session) — 抜き取り検査の一枚目
                    body["summary"] = view.summary_24h(db)
                    # pending gate と agent view は ledger でなくプロセス内の runtime 状態
                    body["pending"] = view.GATES.pending_list()
                    body["view"] = view.CURRENT_VIEW.get()
                    self._send_json(200, body)
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                finally:
                    db.close()
                return
            if path == "/view/events":
                self._serve_view_events(url)
                return
            self._send_json(404, {"ok": False, "error": f"no route: {path}"})

        def _serve_view_events(self, url) -> None:
            """SSE — ledger (meta_events) + turns の追記を 1s ポーリングで流す。
            scope=current で env の session が切り替わったら (renew / reset)
            {"type": "session"} を送って閉じる — client 側が再接続して仕切り直す。"""
            qs = parse_qs(url.query)
            scope = qs.get("session", ["current"])[0] or "current"
            try:
                meta_after = int(qs.get("meta_after", ["0"])[0] or 0)
                turn_after = int(qs.get("turn_after", ["0"])[0] or 0)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "meta_after/turn_after must be int"})
                return
            try:
                db = view.open_ro()
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"db open: {e}"})
                return

            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.end_headers()

            def emit(obj: dict) -> None:
                data = json.dumps(obj, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

            # SSE 接続中 = ブラウザが見ている = gate の答え手がいる、 を registry に知らせる
            view.GATES.watcher_add()
            try:
                sid = view.resolve_sid(db, scope, env_box[0].record_sid)
                # -1 始まり = 接続直後の 1 周目で必ず gate/view イベントを流す。
                # client の /view/state 取得とこの snapshot の間に登録された gate が
                # 「version 変化なし」 で永久に見えなくなる隙間を閉じる。
                gate_v = -1
                view_v = -1
                idle = 0
                while True:
                    if scope == "current" and env_box[0].record_sid != sid:
                        emit({"type": "session", "session_id": env_box[0].record_sid})
                        return
                    sent = False
                    if view.GATES.version != gate_v:
                        gate_v = view.GATES.version
                        emit({"type": "gate"})
                        sent = True
                    if view.CURRENT_VIEW.version != view_v:
                        view_v = view.CURRENT_VIEW.version
                        emit({"type": "view"})
                        sent = True
                    events, meta_after, turn_after = view.poll_events(
                        db, sid, meta_after, turn_after)
                    for ev in events:
                        emit(ev)
                    if events or sent:
                        idle = 0
                    else:
                        idle += 1
                        if idle >= 15:  # keepalive — 切断検知も兼ねる
                            self.wfile.write(b": ka\n\n")
                            self.wfile.flush()
                            idle = 0
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # client 切断
            finally:
                view.GATES.watcher_remove()
                db.close()

        # ----- POST -----
        def do_POST(self):
            url = urlsplit(self.path)
            path = url.path
            env = env_box[0]
            if path.startswith("/view") and view is None:
                self._send_json(503, {"ok": False, "error": "view 層なし (view.py が無い)"})
                return
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
            m = re.match(r"^/view/gate/(\d+)$", path)
            if m:
                # pending gate の解決。 body は action 記号のみ ("approve" | "deny")。
                # _LOCK は取らない — eval スレッドが lock を握って承認待ちしている。
                decision = self._read_body().strip().lower()
                if decision not in ("approve", "deny"):
                    self._send_json(400, {"ok": False, "error": 'body must be "approve" or "deny"'})
                    return
                if view.GATES.resolve(int(m.group(1)), decision, "browser"):
                    self._send_json(200, {"ok": True, "decision": decision})
                else:
                    self._send_json(409, {"ok": False, "error": "not pending (先着済み or 不明 id)"})
                return
            if path == "/view/action":
                # agent view の button 押下。 queue に積むだけ — 解釈は agent 側
                # (view-next-action / await-view-action)。 _LOCK は取らない。
                try:
                    body = json.loads(self._read_body() or "{}")
                    action = str(body.get("action", ""))[:200]
                    inputs_raw = body.get("inputs") or {}
                    if not isinstance(inputs_raw, dict):
                        raise ValueError("inputs must be an object")
                    inputs = {str(k)[:100]: str(v)[:10000]
                              for k, v in list(inputs_raw.items())[:100]}
                except (json.JSONDecodeError, ValueError) as e:
                    self._send_json(400, {"ok": False, "error": f"bad body: {e}"})
                    return
                if not action:
                    self._send_json(400, {"ok": False, "error": "action required"})
                    return
                if not view.ACTIONS.push(action, inputs):
                    self._send_json(429, {"ok": False, "error": "action queue full"})
                    return
                view._log_meta_rw("view-action", env_box[0].record_sid, json.dumps(
                    {"action": action, "inputs": inputs}, ensure_ascii=False))
                self._send_json(200, {"ok": True})
                return
            if path == "/view/comment":
                # thread へのコメント。 ledger (kind=comment) が真実 — SSE が全タブに配る。
                # executor 宛は配達 queue にも積む (auto-step の round 境界で注入)。
                # judge 宛は executor の loop を経由せず judge LLM が別スレッドで応答する。
                try:
                    body = json.loads(self._read_body() or "{}")
                    text = str(body.get("text", "")).strip()
                    to = str(body.get("to", "executor"))
                except json.JSONDecodeError as e:
                    self._send_json(400, {"ok": False, "error": f"bad body: {e}"})
                    return
                if not text:
                    self._send_json(400, {"ok": False, "error": "text required"})
                    return
                if to not in ("executor", "judge"):
                    self._send_json(400, {"ok": False, "error": 'to must be "executor" or "judge"'})
                    return
                if to == "executor" and not view.COMMENTS.push(text[:4000]):
                    self._send_json(429, {"ok": False, "error": "comment queue full"})
                    return
                _log_comment(env.record_sid, "human", to, text)
                if to == "judge":
                    threading.Thread(target=_judge_reply, args=(env_box, text), daemon=True).start()
                self._send_json(200, {"ok": True, "to": to})
                return
            if path == "/view/delegate":
                # R カードの「委譲」 — goal を auto-step で自走させる。 実行は別スレッド
                # (_eval_src が _LOCK で直列化)。 既に評価中なら 409 で断る — queue に
                # 黙って積むと「押したのに何も起きない」時間が生まれる。
                try:
                    body = json.loads(self._read_body() or "{}")
                    goal = " ".join(str(body.get("goal", "")).split()).strip()
                except json.JSONDecodeError as e:
                    self._send_json(400, {"ok": False, "error": f"bad body: {e}"})
                    return
                if not goal:
                    self._send_json(400, {"ok": False, "error": "goal required"})
                    return
                if _LOCK.locked():
                    self._send_json(409, {"ok": False, "error":
                        "評価が実行中 — 完了を待つか、executor 宛コメントで指示する"})
                    return
                _log_comment(env.record_sid, "human", "executor", f"[委譲] auto-step 起動: {goal}")
                threading.Thread(target=_delegate_run, args=(env_box, goal[:2000]),
                                 daemon=True).start()
                self._send_json(200, {"ok": True, "goal": goal[:200]})
                return
            if path == "/interrupt":
                # 走行中の agent loop を step 境界で止める。 _LOCK は取らない —
                # /eval が lock を握って走っている最中に外から叩けることが要件。
                if env.interrupt is not None:
                    env.interrupt.set()
                    self._send_json(200, {"ok": True, "interrupt": "requested"})
                else:
                    self._send_json(200, {"ok": False, "error": "no interrupt event on env"})
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
    # terminal が gate の答え手になれるのはこの thread が生きている間だけ —
    # Ctrl-D で抜けたら clear する (立てっぱなしだと誰も読まない stdin を
    # 答え手として数え、 headless の 600s 停止が再発する)。
    if view is not None:
        view.GATES.terminal_answerer = sys.stdin.isatty()
        view.GATES.terminal_thread = threading.get_ident()
    try:
        _stdin_repl_loop(env_box)
    finally:
        if view is not None:
            view.GATES.terminal_answerer = False
            view.GATES.terminal_thread = None


def _stdin_repl_loop(env_box: list[lispy.Env]) -> None:
    try:
        while True:
            line = input("lispy> ")
            if not line.strip():
                continue
            # pending gate への terminal 回答 (ブラウザとの先着採用)。
            # 裸の y/n は「pending 1 件 + 最後に告知した gate」のときだけ効き、
            # 複数 pending 時は `y <id>` / `n <id>` で対象を指定する (誤射防止)。
            # 注意: この REPL スレッド自身が評価した eval の確認には答えられない
            # (スレッドが eval 内で block 中) — その場合はブラウザが出口。
            m = re.match(r"^(y|yes|n|no)(?:\s+(\d+))?$", line.strip().lower())
            if m and view is not None and view.GATES.has_pending():
                gid = int(m.group(2)) if m.group(2) else None
                msg = view.GATES.resolve_from_terminal(
                    gid, "approve" if m.group(1) in ("y", "yes") else "deny")
                print(f"  {msg}")
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
    p.add_argument("--resume", action="store_true",
                   help="session の会話を DB から復元 + commit-S 済み λ を restore。 "
                        "--session 省略時は直近の session を対象にする")
    p.add_argument("--stdin", action="store_true",
                   help="stdin からも S 式を読む REPL を server と並列で起動")
    p.add_argument("--open", action="store_true",
                   help="起動後にブラウザで /view を開く (agent の作業を動的に見る)")
    args = p.parse_args()

    if args.yolo:
        try:
            import edit as _edit
            _edit.set_yolo(True)
        except ImportError:
            pass

    sid = _resume_or_new(args.session)
    if args.resume and not args.session and not sid:
        # --resume 単独のときだけ直近 session に fallback。 --session が明示されて
        # 解決に失敗した場合は fallback しない (別の session を誤って resume しない)
        db = host.init_db(host.DB_PATH)
        try:
            sid = lispy._last_session_id(db)
        finally:
            db.close()
    env = lispy.build_default_env(record=True, sid=sid, resume=args.resume and bool(sid))
    _load_extras(env)
    env_box: list[lispy.Env] = [env]

    # View 層 (フェーズ 2): server では y/N 確認を pending gate に載せる —
    # ブラウザ (/view) と terminal (y/n) の先着採用。 --yolo では確認自体が skip される。
    # 答え手 (SSE watcher / stdin REPL) がいなければ gate は登録されず即 fail-closed。
    # terminal_answerer は _stdin_repl が thread の生存期間だけ立てる。
    if view is not None:
        view.GATES.remote = True
        view.GATES.sid_provider = lambda: env_box[0].record_sid

    sid_note = f"resumed {env.record_sid[:12]}" if sid else f"new session {env.record_sid[:12]}"
    print(f"lispy-server on http://{args.host}:{args.port}  ({sid_note})")
    print(f"  bindings: {len(env.bindings)}  tools: {len(env.tools)}")
    print("  endpoints: POST /eval /load /reset /interrupt  GET / /bindings /recall?q= /spec /view")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(env_box))

    if args.open:
        if view is None:
            print("  (--open: view 層なし (view.py が無い) — skip)", file=sys.stderr)
        else:
            # serve_forever が回り始めてから開く (競合してもブラウザ側が retry するが行儀として)
            import webbrowser
            threading.Timer(
                0.3, webbrowser.open, (f"http://{args.host}:{args.port}/view",)).start()

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
