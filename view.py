#!/usr/bin/env python3
"""view.py — ledger の読み取り専用ダッシュボード (View 層フェーズ 1)。

設計原則 (docs/ の View 層設計書):
  - データ駆動: ブラウザで走る HTML/JS はこのファイルが固定で持つ。 agent の
    自己修正はこのファイルに及ばない (書き換え対象外) — 送るのはデータのみ。
  - ledger (host.db) が唯一の真実: ここにあるのは SQL 読み取りと JSON 化だけ。
    eval しない、 外部 fetch しない、 書き込み系エンドポイントは無い。
  - step 実行は turns テーブル、 meta 操作 (R/K/S/gate/skill 等) は meta_events。
    タイムラインは両者を ts で merge して 1 本にする。

server.py が import して GET /view /view/state /view/events を張る。
更新は SSE + DB ポーリング (1s) — server と別プロセスの REPL からの追記も拾える
(log_meta へのコールバックだと同一プロセスの追記しか見えない)。
"""
from __future__ import annotations

import difflib
import json
import os
import sqlite3
import threading
import time
from typing import Any

import host

TIMELINE_LIMIT = 100   # 初期スナップショットの件数 (meta / turns それぞれ)
POLL_BATCH = 500       # SSE 1 回のポーリングで読む最大行数
HEAD_LIMIT = 400       # timeline 1 行に載せる本文の最大文字数


def _log_meta_rw(kind: str, sid: str | None, payload: str) -> None:
    """短命の rw 接続で ledger に 1 行 append。 env.db_conn (eval スレッド専有) を
    跨がないための独立経路。 DB 不在等の失敗は握りつぶす (view 層は本体を止めない)。"""
    try:
        conn = sqlite3.connect(host.DB_PATH, timeout=2.0)
        try:
            conn.execute(
                "INSERT INTO meta_events (ts, kind, session_id, payload) VALUES (?, ?, ?, ?)",
                (time.time(), kind, sid or None, payload or None),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# フェーズ 2 — pending gate registry
#
# edit.py の y/N 確認が (remote mode で) ここに載る。 eval スレッドが ask() で
# 登録して block、 ブラウザの POST /view/gate/<id> か terminal の y/n が resolve()
# する。 先着採用 — 2 番手は False が返る。 解決は kind=confirm で ledger に残る。
# ---------------------------------------------------------------------------

class GateRegistry:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._seq = 0
        self._pending: dict[int, dict] = {}
        self.version = 0          # 登録 / 解決で ++ (SSE の変化検知用)
        self.remote = False       # True = _confirm がここを使う。 server が起動時に立てる
        self.sid_provider: Any = None  # () -> record_sid。 server が env_box を差す

    def has_pending(self) -> bool:
        with self._cond:
            return bool(self._pending)

    def pending_list(self) -> list[dict]:
        with self._cond:
            return [
                {k: e[k] for k in ("id", "kind", "title", "detail", "diff", "ts")}
                for e in sorted(self._pending.values(), key=lambda e: e["id"])
            ]

    def ask(self, kind: str, title: str, detail: str = "",
            diff: list | None = None, timeout: float | None = None) -> tuple[bool, str]:
        """pending を登録して決定を待つ。 (approved, source) を返す。
        timeout (default LISPY_CONFIRM_TIMEOUT=600s) で fail-closed (deny)。

        注意: 呼び出しスレッドは block する。 server の stdin REPL から評価された
        eval の確認は terminal では答えられない (REPL スレッド自身が block 中) —
        その場合はブラウザ (または timeout) が唯一の出口。"""
        if timeout is None:
            try:
                timeout = float(os.environ.get("LISPY_CONFIRM_TIMEOUT", "600"))
            except ValueError:
                timeout = 600.0
        with self._cond:
            self._seq += 1
            gid = self._seq
            sid = ""
            if self.sid_provider is not None:
                try:
                    sid = self.sid_provider() or ""
                except Exception:
                    pass
            entry = {
                "id": gid, "kind": kind, "title": title[:300], "detail": detail[:2000],
                "diff": diff or [], "ts": time.time(), "sid": sid,
                "decision": None, "source": None,
            }
            self._pending[gid] = entry
            self.version += 1
            print(f"  [gate #{gid}] pending: {entry['title'][:80]} — /view で承認/却下 (terminal: y/n)",
                  flush=True)
            deadline = time.time() + timeout
            while entry["decision"] is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    entry["decision"] = "deny"
                    entry["source"] = "timeout"
                    break
                self._cond.wait(min(remaining, 1.0))
            del self._pending[gid]
            self.version += 1
        _log_meta_rw("confirm", entry["sid"], json.dumps({
            "gate_id": gid, "kind": kind, "title": entry["title"],
            "decision": entry["decision"], "source": entry["source"],
        }, ensure_ascii=False))
        return entry["decision"] == "approve", entry["source"] or "?"

    def resolve(self, gid: int, decision: str, source: str) -> bool:
        """先着採用: 既に解決済み / 不明 id なら False。"""
        with self._cond:
            entry = self._pending.get(gid)
            if entry is None or entry["decision"] is not None:
                return False
            entry["decision"] = "approve" if decision == "approve" else "deny"
            entry["source"] = source
            self._cond.notify_all()
            return True

    def resolve_oldest(self, decision: str, source: str) -> bool:
        """terminal の y/n 用 — 最も古い pending を解決する。"""
        with self._cond:
            for gid in sorted(self._pending):
                entry = self._pending[gid]
                if entry["decision"] is None:
                    entry["decision"] = "approve" if decision == "approve" else "deny"
                    entry["source"] = source
                    self._cond.notify_all()
                    return True
            return False


GATES = GateRegistry()


def diff_lines(before: str, after: str, max_lines: int = 400) -> list[dict]:
    """unified diff → [{op, text}] 行リスト。 op: '+' 追加 / '-' 削除 / ' ' 文脈 / '@' メタ。
    ブラウザ側は class 分けして textContent で描くだけ (HTML 注入なし)。"""
    out: list[dict] = []
    for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=3):
        if ln.startswith(("+++", "---", "@@")):
            op = "@"
        elif ln.startswith("+"):
            op = "+"
        elif ln.startswith("-"):
            op = "-"
        else:
            op = " "
        out.append({"op": op, "text": ln[:300]})
        if len(out) >= max_lines:
            out.append({"op": "@", "text": f"... (diff truncated at {max_lines} lines)"})
            break
    return out


# ---------------------------------------------------------------------------
# フェーズ 3 — レイアウト語彙
#
# agent は (show-view '(column ...)) でデータだけを送る。 語彙 (部品) はここで固定。
# 語彙にないタグ / 属性は黙って通さずエラーにする。 インタラクションは全て往復 —
# button は action 記号を POST /view/action で送り返す以上のことをしない。
# ---------------------------------------------------------------------------

class ViewError(Exception):
    pass


VIEW_VOCAB: dict[str, dict] = {
    "column": {"attrs": frozenset(), "children": True},
    "row":    {"attrs": frozenset(), "children": True},
    "form":   {"attrs": frozenset(), "children": True},
    "text":   {"attrs": frozenset({"content"}), "children": "text"},
    "table":  {"attrs": frozenset({"header", "rows"}), "children": False},
    "diff":   {"attrs": frozenset({"file", "before", "after"}), "children": False},
    "input":  {"attrs": frozenset({"name", "label", "value"}), "children": False},
    "button": {"attrs": frozenset({"label", "action"}), "children": False},
}
VIEW_MAX_NODES = 2000
VIEW_MAX_DEPTH = 32


def _plain_value(v: Any, depth: int = 0) -> Any:
    """属性値の JSON 化。 scalar と (入れ子の) list だけ許す — 関数や env は通さない。"""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        if depth >= 4:
            raise ViewError("attr の list 入れ子が深すぎる (max 4)")
        return [_plain_value(x, depth + 1) for x in v]
    raise ViewError(f"attr 値に使えない型: {type(v).__name__}")


def sexp_to_view(node: Any, _depth: int = 0, _count: list[int] | None = None) -> tuple[dict, int]:
    """plain 化済み S 式 (str / number / list のみ) を検証して view JSON 木へ。
    返り値は (root_node, 総 node 数)。 語彙違反は ViewError。"""
    if _count is None:
        _count = [0]
    if _depth > VIEW_MAX_DEPTH:
        raise ViewError(f"入れ子が深すぎる (max {VIEW_MAX_DEPTH})")
    _count[0] += 1
    if _count[0] > VIEW_MAX_NODES:
        raise ViewError(f"node 数が多すぎる (max {VIEW_MAX_NODES})")
    if not (isinstance(node, list) and node and isinstance(node[0], str)):
        raise ViewError("node は (tag ...) のリストであること")
    tag = node[0]
    spec = VIEW_VOCAB.get(tag)
    if spec is None:
        raise ViewError(f"unknown tag: {tag} (語彙: {', '.join(sorted(VIEW_VOCAB))})")

    # :key value ペアを先頭から読む → 残りが children
    attrs: dict[str, Any] = {}
    i = 1
    while i < len(node) and isinstance(node[i], str) and node[i].startswith(":"):
        key = node[i][1:]
        if key not in spec["attrs"]:
            raise ViewError(f"unknown attr :{key} for {tag}")
        if i + 1 >= len(node):
            raise ViewError(f":{key} に値がない ({tag})")
        attrs[key] = _plain_value(node[i + 1])
        i += 2
    rest = node[i:]

    out: dict[str, Any] = {"tag": tag, "attrs": attrs, "children": []}

    if tag == "text":
        parts = [attrs.pop("content", "")] if "content" in attrs else []
        for c in rest:
            if not isinstance(c, (str, int, float)):
                raise ViewError("text の子は文字列/数値のみ")
            parts.append(str(c))
        out["attrs"] = {"content": " ".join(str(p) for p in parts if p != "")[:10000]}
        return out, _count[0]

    if tag == "diff":
        before = str(attrs.pop("before", ""))
        after = str(attrs.pop("after", ""))
        out["lines"] = diff_lines(before, after)
        return out, _count[0]

    if tag == "button" and not attrs.get("action"):
        raise ViewError("button には :action が必須")
    if tag == "input" and not attrs.get("name"):
        raise ViewError("input には :name が必須")

    if spec["children"] is True:
        for c in rest:
            if isinstance(c, (str, int, float)):
                _count[0] += 1
                out["children"].append(
                    {"tag": "text", "attrs": {"content": str(c)[:10000]}, "children": []})
            else:
                child, _ = sexp_to_view(c, _depth + 1, _count)
                out["children"].append(child)
    elif rest:
        raise ViewError(f"{tag} は子要素を取らない")
    return out, _count[0]


class ViewSlot:
    """agent が提示中の画面 (1 枚)。 show-view で差し替え、 clear-view で消す。"""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._root: dict | None = None
        self.version = 0

    def set(self, root: dict | None) -> None:
        with self._lock:
            self._root = root
            self.version += 1

    def get(self) -> dict | None:
        with self._lock:
            if self._root is None:
                return None
            return {"version": self.version, "root": self._root}


class ActionQueue:
    """button 押下 (action 記号 + form inputs) の受け皿。 agent が pop して解釈する —
    何が起きるかを決めるのは常に server 側 (= lispy の loop)。"""
    MAX = 100

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._q: list[dict] = []

    def push(self, action: str, inputs: dict) -> bool:
        with self._lock:
            if len(self._q) >= self.MAX:
                return False
            self._q.append({"action": action, "inputs": inputs, "ts": time.time()})
            return True

    def pop(self) -> dict | None:
        with self._lock:
            return self._q.pop(0) if self._q else None


CURRENT_VIEW = ViewSlot()
ACTIONS = ActionQueue()


def open_ro() -> sqlite3.Connection:
    """host.db への読み取り専用接続。 env.db_conn (書き込み側) とは分離する —
    eval 中の _LOCK と無関係に読めるし、 誤って書く経路が構造的に無い。"""
    conn = sqlite3.connect(f"file:{host.DB_PATH}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout = 2000")
    return conn


def resolve_sid(db: sqlite3.Connection, session_filter: str, current_sid: str) -> str | None:
    """scope 文字列 → session_id。 None = 全 session。 prefix 一致は 1 件のときだけ解決。"""
    if session_filter == "all":
        return None
    if session_filter in ("", "current"):
        return current_sid or ""
    rows = db.execute(
        "SELECT DISTINCT session_id FROM ("
        "  SELECT session_id FROM turns UNION SELECT session_id FROM meta_events"
        ") WHERE session_id LIKE ? LIMIT 2",
        (session_filter + "%",),
    ).fetchall()
    if len(rows) == 1 and rows[0][0]:
        return rows[0][0]
    return session_filter  # 不明 / 曖昧 → そのまま filter (一致 0 件の表示になる)


def _head(text: str) -> str:
    return (text or "").replace("\n", " ")[:HEAD_LIMIT]


def _parse_replaces(payload: str) -> int | None:
    """R event payload の `@replaces=N` 行から N を抽出 (server._parse_replaces と同じ規約)。"""
    for ln in (payload or "").split("\n")[1:]:
        if ln.startswith("@replaces="):
            try:
                return int(ln.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def _meta_to_event(rid: int, ts: float, sid: str | None, kind: str, payload: str | None) -> dict:
    """meta_events 1 行 → timeline event dict。 kind=S は JSON、 gate/skill は判定 JSON。"""
    payload = payload or ""
    ev: dict[str, Any] = {"src": "meta", "id": rid, "ts": ts, "sid": sid or "", "tag": kind}
    if kind == "S":
        try:
            p = json.loads(payload)
            ev["head"] = _head(
                f"{p.get('name', '?')} [{p.get('kind', '?')}] — "
                f"{p.get('rationale') or p.get('body', '')[:80]}"
            )
            return ev
        except Exception:
            pass
    elif kind in ("gate", "skill"):
        try:
            p = json.loads(payload)
            approved = bool(p.get("approved"))
            label = p.get("name") or p.get("path") or "?"
            ev["head"] = f"{_head(label)} — {'APPROVE' if approved else 'REJECT'}"
            ev["rejected"] = not approved
            ev["why"] = _head(p.get("why") or "")
            return ev
        except Exception:
            pass
    elif kind == "confirm":
        try:
            p = json.loads(payload)
            approved = p.get("decision") == "approve"
            ev["head"] = (f"#{p.get('gate_id', '?')} {_head(p.get('title') or '?')} — "
                          f"{'APPROVE' if approved else 'DENY'} ({p.get('source', '?')})")
            ev["rejected"] = not approved
            return ev
        except Exception:
            pass
    elif kind == "view-action":
        try:
            p = json.loads(payload)
            ev["head"] = _head(f"action: {p.get('action', '?')} {p.get('inputs') or ''}")
            return ev
        except Exception:
            pass
    ev["head"] = _head(payload.split("\n", 1)[0])
    return ev


def _turn_to_event(rid: int, ts: float, sid: str | None, role: str, content: str | None) -> dict:
    return {
        "src": "turn", "id": rid, "ts": ts, "sid": sid or "",
        "tag": role, "head": _head(content or ""),
    }


def _kind_rows(db: sqlite3.Connection, sid: str | None, kinds: tuple[str, ...]) -> list:
    marks = ",".join("?" * len(kinds))
    if sid is None:
        return db.execute(
            f"SELECT id, ts, session_id, kind, payload FROM meta_events "
            f"WHERE kind IN ({marks}) ORDER BY ts ASC",
            kinds,
        ).fetchall()
    return db.execute(
        f"SELECT id, ts, session_id, kind, payload FROM meta_events "
        f"WHERE kind IN ({marks}) AND session_id = ? ORDER BY ts ASC",
        kinds + (sid,),
    ).fetchall()


def _rks(db: sqlite3.Connection, sid: str | None) -> dict:
    """上段パネル用の R / K / S 現在値。 /spec と同じ導出規則:
    R は @replaces lineage で置換済みをマーク、 K / S は name ごとに最新。"""
    # R — replaced フラグ付き全件 (client 側で active を絞る)
    r_rows = _kind_rows(db, sid, ("R",))
    replaced: set[int] = set()
    for _rid, _ts, _sid, _kind, payload in r_rows:
        prev = _parse_replaces(payload or "")
        if prev is not None:
            replaced.add(prev)
    r_list = [
        {
            "id": rid, "ts": ts,
            "head": _head((payload or "").split("\n", 1)[0]),
            "replaced": rid in replaced,
        }
        for rid, ts, _sid, _kind, payload in r_rows
    ]

    # K — "name: text" 形式。 name ごとに最新を残す
    k_by_name: dict[str, dict] = {}
    for rid, ts, _sid, _kind, payload in _kind_rows(db, sid, ("K",)):
        name, sep, text = (payload or "").partition(": ")
        if not sep:
            name, text = "?", payload or ""
        k_by_name[name] = {"id": rid, "ts": ts, "name": name, "text": _head(text)}
    k_list = sorted(k_by_name.values(), key=lambda k: k["ts"])

    # S — JSON payload {name, kind, rationale, body}。 name ごとに最新
    s_by_name: dict[str, dict] = {}
    for rid, ts, _sid, _kind, payload in _kind_rows(db, sid, ("S",)):
        try:
            p = json.loads(payload or "")
        except Exception:
            continue
        name = p.get("name", "?")
        s_by_name[name] = {
            "id": rid, "ts": ts, "name": name,
            "kind": p.get("kind", "?"),
            "rationale": _head(p.get("rationale") or ""),
        }
    s_list = sorted(s_by_name.values(), key=lambda s: s["ts"])

    return {"R": r_list, "K": k_list, "S": s_list}


def _timeline(db: sqlite3.Connection, sid: str | None, limit: int = TIMELINE_LIMIT) -> list[dict]:
    """meta_events + turns の直近 limit 件を ts で merge。"""
    if sid is None:
        m_rows = db.execute(
            "SELECT id, ts, session_id, kind, payload FROM meta_events "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        t_rows = db.execute(
            "SELECT id, ts, session_id, role, content FROM turns "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        m_rows = db.execute(
            "SELECT id, ts, session_id, kind, payload FROM meta_events "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?", (sid, limit)).fetchall()
        t_rows = db.execute(
            "SELECT id, ts, session_id, role, content FROM turns "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?", (sid, limit)).fetchall()
    events = [_meta_to_event(*row) for row in m_rows] + [_turn_to_event(*row) for row in t_rows]
    events.sort(key=lambda e: (e["ts"], e["id"]))
    return events[-limit:]


def state_json(db: sqlite3.Connection, sid: str | None, scope: str) -> dict:
    """初期スナップショット。 cursors は timeline より先に読む — 隙間の行は SSE 側と
    重複して届く可能性があるが、 client が (src, id) で dedupe する (欠落よりまし)。"""
    meta_max = db.execute("SELECT COALESCE(MAX(id), 0) FROM meta_events").fetchone()[0]
    turn_max = db.execute("SELECT COALESCE(MAX(id), 0) FROM turns").fetchone()[0]
    body = {
        "ok": True,
        "scope": scope,
        "session_id": sid,
        "gates": [
            _meta_to_event(*row) for row in _kind_rows(db, sid, ("gate", "skill", "confirm"))
        ][-20:],
        "timeline": _timeline(db, sid),
        "cursors": {"meta": meta_max, "turn": turn_max},
    }
    body.update(_rks(db, sid))
    return body


def poll_events(
    db: sqlite3.Connection, sid: str | None, meta_after: int, turn_after: int,
) -> tuple[list[dict], int, int]:
    """cursor 以降の追記を返す。 cursor は sid 不一致行も含めて前進させる
    (毎回同じ行を舐め直さない)。 SQLITE_BUSY は空振り扱い — 次の周回で拾う。"""
    events: list[dict] = []
    try:
        for row in db.execute(
            "SELECT id, ts, session_id, kind, payload FROM meta_events "
            "WHERE id > ? ORDER BY id ASC LIMIT ?", (meta_after, POLL_BATCH),
        ).fetchall():
            meta_after = row[0]
            if sid is None or row[2] == sid:
                events.append(_meta_to_event(*row))
        for row in db.execute(
            "SELECT id, ts, session_id, role, content FROM turns "
            "WHERE id > ? ORDER BY id ASC LIMIT ?", (turn_after, POLL_BATCH),
        ).fetchall():
            turn_after = row[0]
            if sid is None or row[2] == sid:
                events.append(_turn_to_event(*row))
    except sqlite3.OperationalError:
        pass
    events.sort(key=lambda e: (e["ts"], e["id"]))
    return events, meta_after, turn_after


# ---------------------------------------------------------------------------
# ダッシュボード HTML — 静的 1 枚。 レンダラーは受け取った JSON を textContent で
# DOM に足すだけ (eval / innerHTML / 外部 fetch なし)。
# ---------------------------------------------------------------------------

VIEW_HTML = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<title>lispy view</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;
       max-width:1000px;margin:1.5em auto;padding:0 1em;color:#222;line-height:1.5}
  h1{border-bottom:2px solid #333;padding-bottom:.3em;font-size:1.4em}
  h2{margin:1em 0 .4em;font-size:1em;border-bottom:1px solid #ccc;padding-bottom:.2em}
  .meta{color:#666;font-size:.85em}
  .dot{display:inline-block;width:.6em;height:.6em;border-radius:50%;background:#bbb;margin-left:.4em}
  .dot.on{background:#2a2}
  .dot.off{background:#c33}
  .switch{margin:.6em 0;font-size:.9em}
  .switch a{margin-right:1em}
  .panels{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1em;margin:1em 0}
  .panel{border:1px solid #ddd;border-radius:4px;padding:.4em .7em .6em;background:#fafafa;min-height:6em}
  .panel h2{margin:.2em 0 .4em;border-bottom:1px solid #ddd}
  .panel ul{margin:0;padding-left:1.1em;font-size:.85em}
  .panel li{margin:.25em 0}
  .id{color:#888;font-family:ui-monospace,monospace;font-size:.85em}
  .empty{color:#999;font-style:italic;list-style:none;margin-left:-1.1em}
  .note{color:#999;font-size:.8em}
  #timeline,#gates{list-style:none;margin:0;padding:.3em .5em;border:1px solid #ddd;
                   border-radius:4px;font-size:.85em;background:#fff}
  #timeline{max-height:28em;overflow-y:auto}
  #timeline li,#gates li{margin:.15em 0;padding:.1em .3em;border-radius:3px}
  .ts{color:#999;font-family:ui-monospace,monospace;font-size:.85em;margin-right:.5em}
  .tag{font-family:ui-monospace,monospace;font-size:.85em;margin-right:.5em}
  li.tag-R{background:#fff3cd}
  li.tag-K{background:#d4edda}
  li.tag-S{background:#cce5ff}
  li.tag-artifact{background:#f8d7da}
  li.tag-intent{background:#e2e3e5}
  li.tag-gate,li.tag-skill{background:#e8f5e9}
  li.tag-user .tag{color:#369}
  li.tag-assistant .tag{color:#636}
  li.tag-tool .tag{color:#a60}
  li.rejected{background:#f8d7da}
  li.rejected .head{color:#a11;font-weight:bold}
  .why{color:#a11;font-size:.9em;margin-left:1.5em;white-space:pre-wrap}
  .replaced{text-decoration:line-through;color:#999}
  li.tag-confirm{background:#e8f5e9}
  li.tag-view,li.tag-view-action{background:#ede7f6}
  .gate{border:2px solid #c70;border-radius:4px;padding:.5em .7em;margin:.5em 0;background:#fff8e6}
  .gate-head{font-weight:bold}
  .gate-detail{font-size:.85em;color:#555;margin:.3em 0;white-space:pre-wrap}
  .gate-btns{margin-top:.5em}
  .gate-btns button{margin-right:.7em;padding:.25em 1.2em;border-radius:4px;border:1px solid #999;
                    cursor:pointer;font-size:.9em}
  .gate-btns button.approve{background:#d4edda;border-color:#2a2}
  .gate-btns button.deny{background:#f8d7da;border-color:#c33}
  .diff{font-family:ui-monospace,monospace;font-size:.8em;background:#f6f6f6;border:1px solid #ddd;
        border-radius:3px;padding:.3em .5em;margin:.3em 0;max-height:20em;overflow:auto;white-space:pre}
  .diff-file{color:#666;font-weight:bold;margin-bottom:.2em}
  .d-add{background:#d4edda;color:#0a3}
  .d-del{background:#f8d7da;color:#a11}
  .d-meta{color:#888}
  .d-ctx{color:#444}
  #aview{border:1px solid #99c;border-radius:4px;padding:.6em .8em;background:#f6f8ff;font-size:.9em}
  .v-row{display:flex;gap:1em;flex-wrap:wrap}
  .v-col,.v-form{display:flex;flex-direction:column;gap:.4em}
  .v-form{border:1px dashed #aac;border-radius:4px;padding:.4em .6em}
  .v-text{white-space:pre-wrap}
  .v-input input{margin-left:.4em;padding:.15em .4em;border:1px solid #999;border-radius:3px}
  .v-button{align-self:flex-start;padding:.25em 1.2em;border-radius:4px;border:1px solid #669;
            background:#dde5ff;cursor:pointer;font-size:.9em}
  .v-err{color:#a11;font-style:italic}
  #aview table{border-collapse:collapse}
  #aview th,#aview td{border:1px solid #bbb;padding:.2em .5em;text-align:left}
  #aview th{background:#eef}
</style></head><body>
<h1>lispy view <span class="dot" id="conn"></span></h1>
<div class="meta">scope: <span id="scope"></span> — session: <span id="sess">…</span></div>
<div class="switch">
  <a href="/view">current session</a>
  <a href="/view?session=all">all sessions</a>
  <a href="/spec">spec</a>
</div>
<div class="panels">
  <div class="panel"><h2>R</h2><ul id="panel-R"></ul></div>
  <div class="panel"><h2>K</h2><ul id="panel-K"></ul></div>
  <div class="panel"><h2>S</h2><ul id="panel-S"></ul></div>
</div>
<div id="pending-wrap" style="display:none">
  <h2>承認待ち gate</h2>
  <div id="pending"></div>
</div>
<div id="aview-wrap" style="display:none">
  <h2>agent view <span class="note">(show-view による提示)</span></h2>
  <div id="aview"></div>
</div>
<h2>timeline <span class="note">(turns + ledger)</span></h2>
<ol id="timeline"></ol>
<h2>gate / confirm 判定履歴 <span class="note">(直近 20 件)</span></h2>
<ul id="gates"></ul>
<script>
"use strict";
const params = new URLSearchParams(location.search);
const scope = params.get("session") || "current";
document.getElementById("scope").textContent = scope;
const seen = new Set();
let es = null;
let retryTimer = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function fmtTs(ts) {
  return new Date(ts * 1000).toLocaleTimeString("ja-JP", { hour12: false });
}
function evLi(ev) {
  const li = el("li", "tag-" + ev.tag + (ev.rejected ? " rejected" : ""));
  li.append(el("span", "ts", fmtTs(ev.ts)),
            el("span", "tag", "[" + ev.tag + "]"),
            el("span", "head", ev.head || ""));
  if (ev.why) li.append(el("div", "why", ev.why));
  return li;
}
function setStatus(on) {
  document.getElementById("conn").className = "dot " + (on ? "on" : "off");
}

function renderPanels(st) {
  document.getElementById("sess").textContent =
    st.session_id === null ? "(all)" : (st.session_id || "(none)");

  const R = document.getElementById("panel-R");
  R.replaceChildren();
  const active = (st.R || []).filter(r => !r.replaced);
  const nRep = (st.R || []).length - active.length;
  if (!active.length) R.append(el("li", "empty", "なし"));
  for (const r of active.slice(-8)) {
    const li = el("li");
    li.append(el("span", "id", "#" + r.id + " "), document.createTextNode(r.head));
    R.append(li);
  }
  if (nRep > 0) R.append(el("li", "empty", "(+" + nRep + " replaced)"));

  const K = document.getElementById("panel-K");
  K.replaceChildren();
  if (!(st.K || []).length) K.append(el("li", "empty", "なし"));
  for (const k of (st.K || []).slice(-8)) {
    const li = el("li");
    li.append(el("span", "id", "#" + k.id + " "),
              document.createTextNode(k.name + ": " + k.text));
    K.append(li);
  }

  const S = document.getElementById("panel-S");
  S.replaceChildren();
  if (!(st.S || []).length) S.append(el("li", "empty", "なし"));
  for (const s of (st.S || []).slice(-8)) {
    const li = el("li");
    li.append(el("span", "id", "#" + s.id + " "),
              document.createTextNode(s.name + " [" + s.kind + "] " + (s.rationale || "")));
    S.append(li);
  }

  const G = document.getElementById("gates");
  G.replaceChildren();
  if (!(st.gates || []).length) G.append(el("li", "empty", "gate 判定なし"));
  for (const g of (st.gates || [])) G.append(evLi(g));

  renderPending(st.pending || []);
  renderAgentView(st.view || null);
}

function renderDiffLines(lines, file) {
  const box = el("div", "diff");
  if (file) box.append(el("div", "diff-file", file));
  for (const ln of lines) {
    const cls = ln.op === "+" ? "d-add" : ln.op === "-" ? "d-del"
              : ln.op === "@" ? "d-meta" : "d-ctx";
    box.append(el("div", cls, ln.text || " "));
  }
  return box;
}

function renderPending(list) {
  const wrap = document.getElementById("pending-wrap");
  const P = document.getElementById("pending");
  P.replaceChildren();
  wrap.style.display = list.length ? "" : "none";
  for (const g of list) {
    const box = el("div", "gate");
    const head = el("div", "gate-head");
    head.append(el("span", "id", "#" + g.id + " "),
                el("span", "tag", "[" + g.kind + "] "),
                el("span", "head", g.title));
    box.append(head);
    if (g.detail) box.append(el("div", "gate-detail", g.detail));
    if (g.diff && g.diff.length) box.append(renderDiffLines(g.diff, ""));
    const btns = el("div", "gate-btns");
    const ok = el("button", "approve", "承認");
    const ng = el("button", "deny", "却下");
    ok.onclick = () => decideGate(g.id, "approve");
    ng.onclick = () => decideGate(g.id, "deny");
    btns.append(ok, ng);
    box.append(btns);
    P.append(box);
  }
}

async function decideGate(id, decision) {
  try {
    await fetch("/view/gate/" + id, { method: "POST", body: decision });
  } catch (e) { /* refresh で実状態に揃う */ }
  refreshPanels();
}

function renderAgentView(v) {
  const wrap = document.getElementById("aview-wrap");
  const root = document.getElementById("aview");
  root.replaceChildren();
  if (!v || !v.root) { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  root.append(renderNode(v.root));
}

// データ駆動レンダラー — 受け取った JSON 木を DOM に写すだけ。
// textContent のみ使用 (innerHTML / eval / 外部 fetch なし)。 未知タグはエラー表示。
function renderNode(n) {
  if (n === null || typeof n !== "object") return el("div", "v-text", String(n));
  const a = n.attrs || {};
  switch (n.tag) {
    case "column": case "row": case "form": {
      const d = el("div", "v-" + (n.tag === "column" ? "col" : n.tag));
      for (const c of n.children || []) d.append(renderNode(c));
      return d;
    }
    case "text":
      return el("div", "v-text", a.content || "");
    case "table": {
      const t = document.createElement("table");
      if (Array.isArray(a.header)) {
        const tr = document.createElement("tr");
        for (const h of a.header) tr.append(el("th", "", String(h)));
        t.append(tr);
      }
      for (const row of (Array.isArray(a.rows) ? a.rows : [])) {
        const tr = document.createElement("tr");
        for (const c of (Array.isArray(row) ? row : [row])) tr.append(el("td", "", String(c)));
        t.append(tr);
      }
      return t;
    }
    case "diff":
      return renderDiffLines(n.lines || [], a.file || "");
    case "input": {
      const w = el("label", "v-input", String(a.label || a.name || ""));
      const inp = document.createElement("input");
      inp.name = String(a.name || "");
      inp.value = a.value === undefined || a.value === null ? "" : String(a.value);
      w.append(inp);
      return w;
    }
    case "button": {
      const b = el("button", "v-button", String(a.label || a.action || "?"));
      b.onclick = () => sendAction(String(a.action || ""), b);
      return b;
    }
    default:
      return el("div", "v-err", "(unknown tag: " + String(n.tag) + ")");
  }
}

async function sendAction(action, btn) {
  // button は action 記号と (同じ form 内の) input 値を送り返すだけ。
  const scopeEl = btn.closest(".v-form") || document.getElementById("aview");
  const inputs = {};
  for (const i of scopeEl.querySelectorAll("input[name]")) inputs[i.name] = i.value;
  try {
    await fetch("/view/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: action, inputs: inputs }),
    });
  } catch (e) { /* 失敗はサーバー側 ledger に残らないだけ */ }
}

function appendTimeline(ev) {
  const key = ev.src + ":" + ev.id;
  if (seen.has(key)) return;
  seen.add(key);
  const tl = document.getElementById("timeline");
  const nearBottom = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 40;
  tl.append(evLi(ev));
  while (tl.children.length > 300) tl.removeChild(tl.firstChild);
  if (nearBottom) tl.scrollTop = tl.scrollHeight;
}

async function refreshPanels() {
  try {
    const st = await fetch("/view/state?session=" + encodeURIComponent(scope))
      .then(r => r.json());
    if (st.ok) renderPanels(st);
  } catch (e) { /* 次の event でまた試す */ }
}

function scheduleRetry() {
  setStatus(false);
  if (es) { es.close(); es = null; }
  if (retryTimer) return;
  retryTimer = setTimeout(() => { retryTimer = null; init(); }, 3000);
}

function connect(cur) {
  es = new EventSource("/view/events?session=" + encodeURIComponent(scope) +
                       "&meta_after=" + cur.meta + "&turn_after=" + cur.turn);
  es.onopen = () => setStatus(true);
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (ev.type === "session") { if (es) { es.close(); es = null; } init(); return; }
    if (ev.type === "gate" || ev.type === "view") { refreshPanels(); return; }
    appendTimeline(ev);
    if (["R", "K", "S", "gate", "skill", "confirm", "intent"].includes(ev.tag)) refreshPanels();
  };
  es.onerror = scheduleRetry;
}

async function init() {
  if (es) { es.close(); es = null; }
  let st;
  try {
    st = await fetch("/view/state?session=" + encodeURIComponent(scope))
      .then(r => r.json());
  } catch (e) { scheduleRetry(); return; }
  if (!st.ok) { scheduleRetry(); return; }
  renderPanels(st);
  seen.clear();
  document.getElementById("timeline").replaceChildren();
  for (const ev of st.timeline || []) appendTimeline(ev);
  const tl = document.getElementById("timeline");
  tl.scrollTop = tl.scrollHeight;
  connect(st.cursors || { meta: 0, turn: 0 });
}

init();
</script>
</body></html>
"""
