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
import sys
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

# 答え手喪失の扱い (誤 deny と 600s 停止の間のバランス):
#   GRACE  — 登録時: 直近この秒数以内に watcher がいたなら登録を許す。 F5 / session
#            切替での SSE 再接続 (数秒) の隙間で、 見ている人がいるのに即 deny しない
#   LOST   — 待機中: 答え手不在がこの秒数続いたら deny。 SSE の keepalive (15s) で
#            切断は検知されるので、 閉じたタブを timeout (600s) まで待たない
ANSWERER_GRACE = 60.0
ANSWERER_LOST_DENY = 45.0


class GateRegistry:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._seq = 0
        self._pending: dict[int, dict] = {}
        self.version = 0          # 登録 / 解決で ++ (SSE の変化検知用)
        self.remote = False       # True = _confirm がここを使う。 server が起動時に立てる
        self.sid_provider: Any = None  # () -> record_sid。 server が env_box を差す
        # 「答え手」の把握 — 誰も見ていない gate は登録せず即 fail-closed (旧 _confirm の
        # 「non-tty なら即 skip」の保存)。 watchers = /view の SSE 接続数、
        # terminal_answerer = server の stdin REPL が tty で動いているか (REPL thread の
        # 生存期間だけ True — server 側が set/clear する)。
        self.watchers = 0
        self.terminal_answerer = False
        self.terminal_thread: int | None = None  # stdin REPL の thread id
        self.last_watcher_ts = 0.0  # watcher の増減があった時刻 (GRACE 判定用)
        # 最後に terminal へ告知した gate id — 裸の y/n はこの gate にだけ効く
        self.last_announced = 0

    def watcher_add(self) -> None:
        with self._cond:
            self.watchers += 1
            self.last_watcher_ts = time.time()

    def watcher_remove(self) -> None:
        with self._cond:
            self.watchers = max(0, self.watchers - 1)
            self.last_watcher_ts = time.time()

    def _answerers(self, asker: int) -> bool:
        """asker (thread id) 以外に答えられる者がいるか。 terminal は REPL thread 自身が
        gate を起こしている場合は数えない — その thread は ask() で block していて
        stdin を読めない。 呼び出し側が self._cond を握っていること。"""
        return (self.watchers > 0
                or (self.terminal_answerer and self.terminal_thread not in (None, asker)))

    def can_answer(self) -> bool:
        with self._cond:
            return self._answerers(threading.get_ident())

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
        sid = ""
        if self.sid_provider is not None:
            try:
                sid = self.sid_provider() or ""
            except Exception:
                pass
        # 答え手がいなければ登録せず即 fail-closed — 旧 _confirm (input() が non-tty で
        # 即 False) の性質を保存する。 headless 運用で eval が timeout ぶん止まらない。
        # ただし直近 GRACE 秒以内に watcher がいたなら登録を許す — F5 / session 切替の
        # SSE 再接続の隙間で、 実際には見ている人がいるのに即 deny しない。
        me = threading.get_ident()
        with self._cond:
            answerable = (self._answerers(me)
                          or time.time() - self.last_watcher_ts < ANSWERER_GRACE)
        if not answerable:
            print(f"  [gate] no answerer (browser/terminal とも不在) — fail-closed deny: {title[:80]}",
                  file=sys.stderr, flush=True)
            _log_meta_rw("confirm", sid, json.dumps({
                "gate_id": 0, "kind": kind, "title": title[:300],
                "decision": "deny", "source": "no-answerer",
            }, ensure_ascii=False))
            return False, "no-answerer"
        with self._cond:
            self._seq += 1
            gid = self._seq
            entry = {
                "id": gid, "kind": kind, "title": title[:300], "detail": detail[:2000],
                "diff": diff or [], "ts": time.time(), "sid": sid,
                "decision": None, "source": None,
            }
            self._pending[gid] = entry
            self.version += 1
            self.last_announced = gid
            # stderr へ — server の _eval_src は stdout を redirect するため、
            # stdout に出すと terminal に届かず buffer に飲まれる
            print(f"  [gate #{gid}] pending: {entry['title'][:80]} — /view で承認/却下 (terminal: y/n)",
                  file=sys.stderr, flush=True)
            deadline = time.time() + timeout
            lost_since: float | None = None
            while entry["decision"] is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    entry["decision"] = "deny"
                    entry["source"] = "timeout"
                    break
                # 待機中も答え手を監視 — タブが閉じられたら timeout まで待たずに deny。
                # 一時的な喪失 (再接続中) は LOST 秒まで許す。
                if self._answerers(me):
                    lost_since = None
                elif lost_since is None:
                    lost_since = time.time()
                elif time.time() - lost_since > ANSWERER_LOST_DENY:
                    entry["decision"] = "deny"
                    entry["source"] = "answerer-lost"
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

    def resolve_from_terminal(self, gid: int | None, decision: str) -> str:
        """terminal の y/n 用。 返り値は表示用メッセージ。

        誤射防止 (先着レースで別の gate に y が刺さる事故) のため:
          - `y 3` のように id 指定なら その gate だけを解決
          - 裸の y/n は「pending が 1 件だけ、 かつそれが最後に告知した gate」の
            ときにだけ効く。 それ以外は id 指定を要求する
        解決した gate のタイトルを必ずエコーする — 何を承認したかを事後即座に見せる。"""
        with self._cond:
            live = [e for e in self._pending.values() if e["decision"] is None]
            if not live:
                return "(gate: pending なし)"
            if gid is None:
                if len(live) > 1:
                    ids = " ".join(f"#{e['id']}" for e in sorted(live, key=lambda e: e["id"]))
                    return f"(gate: 複数 pending ({ids}) — 'y <id>' / 'n <id>' で指定すること)"
                if live[0]["id"] != self.last_announced:
                    return (f"(gate: 告知済みの gate と pending が食い違う — "
                            f"'y {live[0]['id']}' のように id を指定すること)")
                gid = live[0]["id"]
            entry = self._pending.get(gid)
            if entry is None or entry["decision"] is not None:
                return f"(gate: #{gid} は pending でない — 先着済み or 不明 id)"
            entry["decision"] = "approve" if decision == "approve" else "deny"
            entry["source"] = "terminal"
            self._cond.notify_all()
            verb = "approve" if entry["decision"] == "approve" else "deny"
            return f"(gate #{gid} {verb}: {entry['title'][:80]})"


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


def _sym_name(v: Any) -> str | None:
    """lispy 側の _view_plainify が Symbol を {"sym": name} に写した marker を読む。
    文字列と keyword を区別するため — ":" で始まるただの文字列は attr キーにしない。"""
    if isinstance(v, dict) and set(v.keys()) == {"sym"} and isinstance(v["sym"], str):
        return v["sym"]
    return None


def _plain_value(v: Any, depth: int = 0) -> Any:
    """属性値の JSON 化。 scalar / symbol / (入れ子の) list だけ許す — 関数や env は通さない。
    文字列は text の content と同じ上限でキャップ (agent 制御の面は全部同じガード)。"""
    s = _sym_name(v)
    if s is not None:
        return s[:10000]
    if isinstance(v, str):
        return v[:10000]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        if depth >= 4:
            raise ViewError("attr の list 入れ子が深すぎる (max 4)")
        return [_plain_value(x, depth + 1) for x in v]
    raise ViewError(f"attr 値に使えない型: {type(v).__name__}")


def sexp_to_view(node: Any, _depth: int = 0, _count: list[int] | None = None) -> tuple[dict, int]:
    """plain 化済み S 式 (str / number / list / symbol marker) を検証して view JSON 木へ。
    返り値は (root_node, 総 node 数)。 語彙違反は ViewError。"""
    if _count is None:
        _count = [0]
    if _depth > VIEW_MAX_DEPTH:
        raise ViewError(f"入れ子が深すぎる (max {VIEW_MAX_DEPTH})")
    _count[0] += 1
    if _count[0] > VIEW_MAX_NODES:
        raise ViewError(f"node 数が多すぎる (max {VIEW_MAX_NODES})")
    if not (isinstance(node, list) and node):
        raise ViewError("node は (tag ...) のリストであること")
    tag = _sym_name(node[0]) or (node[0] if isinstance(node[0], str) else None)
    if tag is None:
        raise ViewError("node は (tag ...) のリストであること")
    spec = VIEW_VOCAB.get(tag)
    if spec is None:
        raise ViewError(f"unknown tag: {tag} (語彙: {', '.join(sorted(VIEW_VOCAB))})")

    # :key value ペアを先頭から読む → 残りが children。
    # symbol keyword は厳格 (語彙外 attr はエラー)。 文字列は原則 text 子要素だが、
    # 後方互換として「その tag の既知 attr 名に一致し、 値が続く」 ときだけ attr と
    # 読む — json-parse 等で組んだ木 (keyword が文字列になる) を壊さないため。
    # ":warning ..." のような普通の文章は既知 attr 名に一致しないので text のまま。
    attrs: dict[str, Any] = {}
    i = 1
    while i < len(node):
        k = _sym_name(node[i])
        if k is not None:
            if not k.startswith(":"):
                break
            key = k[1:]
            if key not in spec["attrs"]:
                raise ViewError(f"unknown attr :{key} for {tag}")
        elif (isinstance(node[i], str) and node[i].startswith(":")
              and node[i][1:] in spec["attrs"] and i + 1 < len(node)):
            key = node[i][1:]
        else:
            break
        if i + 1 >= len(node):
            raise ViewError(f":{key} に値がない ({tag})")
        attrs[key] = _plain_value(node[i + 1])
        i += 2
    rest = node[i:]

    out: dict[str, Any] = {"tag": tag, "attrs": attrs, "children": []}

    if tag == "text":
        parts = [attrs.pop("content", "")] if "content" in attrs else []
        for c in rest:
            s = _sym_name(c)
            if s is not None:
                c = s
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
            s = _sym_name(c)
            if s is not None:
                c = s
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


class CommentQueue:
    """executor 宛の人間コメントの受け皿 (POST /view/comment → auto-step の round 境界)。
    ledger (kind=comment) が表示用の真実で、 ここは「まだ agent に渡っていない分」だけを
    持つ配達キュー。 drain はまとめて取り出す — round 境界で一括注入するため。"""
    MAX = 100

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._q: list[dict] = []

    def push(self, text: str) -> bool:
        with self._lock:
            if len(self._q) >= self.MAX:
                return False
            self._q.append({"text": text, "ts": time.time()})
            return True

    def drain(self) -> list[dict]:
        with self._lock:
            out, self._q = self._q, []
            return out


CURRENT_VIEW = ViewSlot()
ACTIONS = ActionQueue()
COMMENTS = CommentQueue()


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
    elif kind == "plan":
        try:
            p = json.loads(payload)
            rep = f" (replaces #{p['replaces']})" if p.get("replaces") else ""
            rat = f" — 改版理由: {p['rationale']}" if p.get("rationale") else ""
            ev["head"] = _head(
                f"plan proposed: {p.get('goal', '?')} ({len(p.get('steps') or [])} steps){rep}{rat}")
            return ev
        except Exception:
            pass
    elif kind == "plan-approval":
        try:
            p = json.loads(payload)
            approved = bool(p.get("approved"))
            ev["head"] = (f"plan #{p.get('plan_id', '?')} — "
                          f"{'APPROVE' if approved else 'REJECT'} ({p.get('source', '?')})")
            ev["rejected"] = not approved
            ev["why"] = _head(p.get("why") or "")
            return ev
        except Exception:
            pass
    elif kind == "plan-progress":
        try:
            p = json.loads(payload)
            note = f" — {p['note']}" if p.get("note") else ""
            ev["head"] = _head(f"plan #{p.get('plan_id', '?')}: step {p.get('step', '?')} done{note}")
            return ev
        except Exception:
            pass
    elif kind == "comment":
        try:
            p = json.loads(payload)
            author = str(p.get("author") or "?")[:40]
            ev["author"] = author
            ev["text"] = str(p.get("text") or "")[:4000]
            ev["head"] = _head(f"{author}: {ev['text']}")
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


def _recent_kind_rows(db: sqlite3.Connection, sid: str | None,
                      kinds: tuple[str, ...], limit: int) -> list:
    """直近 limit 件だけを SQL 側で切る (_kind_rows の全走査を避ける)。 時系列順で返す。"""
    marks = ",".join("?" * len(kinds))
    if sid is None:
        rows = db.execute(
            f"SELECT id, ts, session_id, kind, payload FROM meta_events "
            f"WHERE kind IN ({marks}) ORDER BY id DESC LIMIT ?",
            kinds + (limit,),
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT id, ts, session_id, kind, payload FROM meta_events "
            f"WHERE kind IN ({marks}) AND session_id = ? ORDER BY id DESC LIMIT ?",
            kinds + (sid, limit),
        ).fetchall()
    return list(reversed(rows))


def _parse_r_annotations(payload: str) -> dict:
    """R payload の @judge= 系注釈行を読む (commit-R の auto-judge が書く形式)。"""
    out: dict[str, Any] = {"judge": None, "target": None, "reason": None, "impact": None}
    for ln in (payload or "").split("\n")[1:]:
        if ln.startswith("@judge="):
            out["judge"] = ln.split("=", 1)[1].strip()[:8]
        elif ln.startswith("@judge-target="):
            try:
                out["target"] = int(ln.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif ln.startswith("@judge-reason="):
            out["reason"] = ln.split("=", 1)[1].strip()[:200]
        elif ln.startswith("@judge-impact="):
            out["impact"] = ln.split("=", 1)[1].strip()[:200]
    return out


def _rks(db: sqlite3.Connection, sid: str | None) -> dict:
    """上段パネル用の R / K / S 現在値。 /spec と同じ導出規則:
    R は @replaces lineage で置換済みをマーク、 K / S は name ごとに最新。"""
    # R — issue カード用に lineage / judge 注釈も添える (client 側で active を絞る)
    r_rows = _kind_rows(db, sid, ("R",))
    replaced: set[int] = set()
    contested: dict[int, list[int]] = {}   # 対象 R id → contradicts と判定した R id 群
    refined: dict[int, list[int]] = {}     # 対象 R id → refines と判定した R id 群
    ann_by_id: dict[int, dict] = {}
    for rid, _ts, _sid, _kind, payload in r_rows:
        prev = _parse_replaces(payload or "")
        if prev is not None:
            replaced.add(prev)
        ann = _parse_r_annotations(payload or "")
        ann_by_id[rid] = ann
        if ann["target"] is not None:
            if ann["judge"] == "c":
                contested.setdefault(ann["target"], []).append(rid)
            elif ann["judge"] == "b":
                refined.setdefault(ann["target"], []).append(rid)
    r_list = [
        {
            "id": rid, "ts": ts,
            "head": _head((payload or "").split("\n", 1)[0]),
            "replaced": rid in replaced,
            "judge": ann_by_id[rid]["judge"],
            "target": ann_by_id[rid]["target"],
            "reason": ann_by_id[rid]["reason"],
            "impact": ann_by_id[rid]["impact"],
            "contested_by": contested.get(rid, []),
            "refined_by": refined.get(rid, []),
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


def plan_state(db: sqlite3.Connection) -> dict | None:
    """最新の計画 + 承認 + 進捗をチェックリストに畳む。 導出規則は host.plan_*
    (lispy.py の plan primitives と共有) — ここは表示用の整形だけを持つ。
    ledger が唯一の真実で、 scope に依らず最新 1 件 (計画は run 単位)。"""
    p = host.plan_latest(db)
    if p is None:
        return None
    plan_id = p["id"]
    appr = host.plan_approval(db, plan_id)
    if appr is None:
        status, source = "proposed", ""
    else:
        status = "approved" if appr.get("approved") else "rejected"
        source = appr.get("source", "")
    done = host.plan_done_steps(db, plan_id)
    steps = [
        {"what": _head(s.get("what", "")), "why": _head(s.get("why", "")), "done": i in done}
        for i, s in enumerate(p.get("steps") or [], 1)
    ]
    return {
        "id": plan_id, "goal": _head(p.get("goal", "")),
        "status": status, "source": source, "replaces": p.get("replaces"),
        "rationale": _head(p.get("rationale") or ""),
        "steps": steps,
        "done": sum(1 for s in steps if s["done"]), "total": len(steps),
    }


def summary_24h(db: sqlite3.Connection) -> dict:
    """最上段の要約 (要約ファースト)。 直近 24 時間・全 session の活動量 —
    抜き取り検査の一枚目なので、 scope に依らず箱全体を集計する。
    ledger + turns からの導出のみ (メモリ上の独自集計は持たない — 再起動に強い)。"""
    since = time.time() - 86400
    out = {"steps": 0, "tools": 0, "rejects": 0, "skill_updates": 0}
    for role, n in db.execute(
        "SELECT role, COUNT(*) FROM turns WHERE ts > ? GROUP BY role", (since,),
    ).fetchall():
        if role == "assistant":
            out["steps"] = n
        elif role == "tool":
            out["tools"] = n
    for kind, payload in db.execute(
        "SELECT kind, payload FROM meta_events WHERE ts > ? AND kind IN ('gate', 'skill', 'confirm')",
        (since,),
    ).fetchall():
        try:
            p = json.loads(payload or "")
        except Exception:
            continue
        if kind == "confirm":
            # escalation の confirm は同一イベントが kind=gate 行にも記録される
            # (_gate_log_bind) — 二重計上しない
            if p.get("kind") == "escalation":
                continue
            if p.get("decision") != "approve":
                out["rejects"] += 1
        elif not p.get("approved"):
            out["rejects"] += 1
        elif kind == "skill":
            out["skill_updates"] += 1
    return out


def _memory_state() -> dict | None:
    """蒸留層 (data/memory/) のスナップショット。 dir 解決は brainwash.MEMORY_DIR と同じ規則
    (import はしない — brainwash.py を消しても view は動く)。 dir が無ければ None。"""
    from pathlib import Path
    mem_dir = Path(os.environ.get(
        "LISPY_MEMORY_DIR", str(Path(__file__).resolve().parent / "data" / "memory")))
    if not mem_dir.is_dir():
        return None
    files = []
    index_text = ""
    for p in sorted(mem_dir.rglob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue
        rel = str(p.relative_to(mem_dir))
        files.append({"path": rel, "mtime": st.st_mtime, "size": st.st_size})
        if rel == "index.md":
            try:
                index_text = p.read_text(encoding="utf-8")[:4000]
            except (OSError, UnicodeDecodeError):
                # UnicodeDecodeError は OSError でない — 逃すと /view/state 全体が 500 になる
                pass
    return {"dir": str(mem_dir), "files": files, "index": index_text}


def _latest_intent(db: sqlite3.Connection, sid: str | None) -> str | None:
    """この session の最新 session-intent (kind=intent の payload)。 goal パネル用。
    scope=all のときは箱全体の最新 1 件。"""
    if sid is None:
        row = db.execute(
            "SELECT payload FROM meta_events WHERE kind = 'intent' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = db.execute(
            "SELECT payload FROM meta_events WHERE kind = 'intent' AND session_id = ? "
            "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    return (row[0] or "").strip() if row else None


def _latest_verdict(db: sqlite3.Connection, sid: str | None) -> dict | None:
    """最新の judge verdict (kind=comment で author=judge)。 達成バッジの真値。
    text が DONE 始まりなら達成、 NEXT: 始まりなら未達 (+次の一手)。 judge 発言が
    無ければ None (未判定)。"""
    if sid is None:
        rows = db.execute(
            "SELECT payload FROM meta_events WHERE kind = 'comment' "
            "ORDER BY id DESC LIMIT 50").fetchall()
    else:
        rows = db.execute(
            "SELECT payload FROM meta_events WHERE kind = 'comment' AND session_id = ? "
            "ORDER BY id DESC LIMIT 50", (sid,)).fetchall()
    for (payload,) in rows:
        try:
            p = json.loads(payload or "")
        except Exception:
            continue
        if str(p.get("author") or "") != "judge":
            continue
        text = str(p.get("text") or "").strip()
        stripped = text.lstrip()
        if stripped.upper().startswith("DONE"):
            return {"done": True, "next": ""}
        if stripped.upper().startswith("NEXT:"):
            return {"done": False, "next": _head(stripped[5:].strip())}
        # judge の自由発言 (@judge 宛の応答等) は verdict でない — 次を見る
    return None


def sessions_list(db: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """セッション一覧 (/sessions ページ用)。 host.cmd_list と同じ土台に
    goal (session-intent) と最新 judge verdict を足す。 開始時刻降順。"""
    rows = db.execute(
        """
        SELECT s.id, s.started_at, s.title, s.domain, COUNT(t.id) AS n
        FROM sessions s LEFT JOIN turns t ON t.session_id = s.id
        GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for sid, ts, title, domain, n in rows:
        intent = _latest_intent(db, sid)
        verdict = _latest_verdict(db, sid)
        out.append({
            "id": sid, "started_at": ts, "title": title or "",
            "domain": domain or "", "turns": n,
            "goal": _head(intent or ""),
            "verdict": verdict,
        })
    return out


def state_json(db: sqlite3.Connection, sid: str | None, scope: str) -> dict:
    """初期スナップショット。 cursors は timeline より先に読む — 隙間の行は SSE 側と
    重複して届く可能性があるが、 client が (src, id) で dedupe する (欠落よりまし)。"""
    meta_max = db.execute("SELECT COALESCE(MAX(id), 0) FROM meta_events").fetchone()[0]
    turn_max = db.execute("SELECT COALESCE(MAX(id), 0) FROM turns").fetchone()[0]
    body = {
        "ok": True,
        "scope": scope,
        "session_id": sid,
        "intent": _latest_intent(db, sid),
        "verdict": _latest_verdict(db, sid),
        "plan": plan_state(db),
        "gates": [
            _meta_to_event(*row)
            for row in _recent_kind_rows(db, sid, ("gate", "skill", "confirm"), 20)
        ],
        "comments": [
            _meta_to_event(*row)
            for row in _recent_kind_rows(db, sid, ("comment",), 50)
        ],
        "timeline": _timeline(db, sid),
        "cursors": {"meta": meta_max, "turn": turn_max},
        "memory": _memory_state(),
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
  #goal-wrap{margin:1em 0;padding:.7em 1em;border:1px solid #ddd;border-radius:8px;
             background:#fbfbfd}
  #goal-badge{font-size:1.1em;font-weight:bold;margin-bottom:.2em}
  #goal-badge.done{color:#1a7a34}
  #goal-badge.pending{color:#b26a00}
  #goal-badge.unknown{color:#888}
  #goal-text{font-size:1.05em;color:#222}
  #goal-next{margin-top:.25em}
  .stats{display:flex;gap:.8em;margin:1em 0;flex-wrap:wrap}
  .stat{display:flex;flex-direction:column;align-items:center;min-width:7.5em;
        padding:.5em .8em;border:1px solid #ddd;border-radius:6px;background:#fafafa;
        text-decoration:none;color:#222;cursor:pointer}
  .stat.dead{opacity:.5;cursor:default}
  .stat b{font-size:1.6em;line-height:1.2}
  .stat span{font-size:.75em;color:#666}
  .stat.alert{background:#f8d7da;border-color:#c33}
  .stat.alert b{color:#a11}
  .stat.attn{background:#fff8e6;border-color:#c70}
  .stat.attn b{color:#c70}
  details.fold{border:1px solid #ddd;border-radius:4px;background:#fff;margin:.4em 0}
  details.fold>summary{cursor:pointer;padding:.4em .6em;font-size:.9em;color:#444;
                       background:#f6f6f6;border-radius:4px}
  details.fold[open]>summary{border-bottom:1px solid #ddd;border-radius:4px 4px 0 0}
  details.fold>#timeline{border:none;border-radius:0}
  .why{color:#a11;font-size:.9em;margin-left:1.5em;white-space:pre-wrap}
  .replaced{text-decoration:line-through;color:#999}
  li.tag-confirm{background:#e8f5e9}
  li.tag-comment{background:#e8f1fb}
  li.tag-view,li.tag-view-action{background:#ede7f6}
  li.tag-plan,li.tag-plan-approval,li.tag-plan-progress{background:#e0f2f1}
  #plan-list{list-style:none;margin:0;padding:.4em .6em;border:1px solid #ddd;
             border-radius:4px;background:#fff;font-size:.9em}
  #plan-list li{margin:.25em 0}
  #plan-list li.done .what{color:#2a2}
  #plan-list .mark{font-family:ui-monospace,monospace;margin-right:.3em}
  .plan-why{color:#888;font-size:.85em;margin-left:1.7em}
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
  #issues{display:flex;flex-direction:column;gap:.5em;margin:.5em 0}
  .issue{border:1px solid #ddd;border-left:4px solid #c70;border-radius:4px;
         padding:.4em .7em;background:#fffdf5}
  .issue.contested{border-left-color:#c33;background:#fff5f5}
  .issue-head{display:flex;align-items:center;gap:.5em;flex-wrap:wrap}
  .issue-text{margin:.15em 0 .3em;font-size:.95em}
  .issue-why{color:#777;font-size:.82em;margin:.1em 0 0 .3em}
  .chip{display:inline-block;font-size:.72em;padding:.05em .55em;border-radius:9px;
        border:1px solid #bbb;background:#f0f0f0;color:#555;font-family:ui-monospace,monospace}
  .chip.warn{background:#f8d7da;border-color:#c33;color:#a11}
  .chip.ok{background:#d4edda;border-color:#2a2;color:#161}
  .chip.info{background:#cce5ff;border-color:#69c;color:#247}
  .issue-btns button{font-size:.78em;padding:.1em .8em;border-radius:4px;border:1px solid #999;
                     background:#eef;cursor:pointer}
  #thread{list-style:none;margin:0;padding:.4em .6em;border:1px solid #ddd;border-radius:4px;
          background:#fff;font-size:.88em;max-height:24em;overflow-y:auto}
  #thread li{margin:.45em 0;padding:.3em .6em;border-radius:6px;background:#f6f6f6;
             border-left:3px solid #bbb}
  #thread .author{font-weight:bold;font-size:.85em;margin-right:.6em}
  #thread .body{white-space:pre-wrap;word-break:break-word;display:block;margin-top:.1em}
  #thread li.a-human{background:#e8f1fb;border-left-color:#369}
  #thread li.a-human .author{color:#369}
  #thread li.a-executor{background:#f3ecf9;border-left-color:#849}
  #thread li.a-executor .author{color:#849}
  #thread li.a-judge{background:#e9f6ee;border-left-color:#2a2}
  #thread li.a-judge .author{color:#181}
  #thread li.a-system{background:#f4f4f4;border-left-color:#999;color:#666}
  #comment-form{display:flex;gap:.5em;margin:.5em 0;align-items:flex-start}
  #comment-form textarea{flex:1;padding:.3em .5em;border:1px solid #999;border-radius:4px;
                         font-family:inherit;font-size:.9em}
  #comment-form select{padding:.25em;border:1px solid #999;border-radius:4px;font-size:.85em}
  #comment-form button{padding:.3em 1.2em;border-radius:4px;border:1px solid #369;
                       background:#dde9ff;cursor:pointer}
</style></head><body>
<h1>lispy view <span class="dot" id="conn"></span></h1>
<div class="meta">scope: <span id="scope"></span> — session: <span id="sess">…</span></div>
<div class="switch">
  <a href="/view">current session</a>
  <a href="/view?session=all">all sessions</a>
  <a href="/sessions">sessions</a>
  <a href="/spec">spec</a>
</div>
<div id="goal-wrap" style="display:none">
  <div id="goal-badge"></div>
  <div id="goal-text"></div>
  <div id="goal-next" class="note"></div>
</div>
<div class="stats" id="summary">
  <a class="stat" href="#timeline-wrap"><b id="st-steps">0</b><span>step (24h)</span></a>
  <a class="stat" href="#timeline-wrap"><b id="st-tools">0</b><span>tool (24h)</span></a>
  <a class="stat" id="stat-rejects" href="#gates-wrap"><b id="st-rejects">0</b><span>REJECT (24h)</span></a>
  <a class="stat" id="stat-skill" href="#gates-wrap"><b id="st-skill">0</b><span>SKILL.md 書換 (24h)</span></a>
  <a class="stat" id="stat-pending" href="#pending-wrap"><b id="st-pending">0</b><span>承認待ち</span></a>
  <a class="stat" id="stat-plan" href="#plan-wrap"><b id="st-plan">—</b><span>plan</span></a>
</div>
<div id="pending-wrap" style="display:none">
  <h2>承認待ち gate</h2>
  <div id="pending"></div>
</div>
<div id="plan-wrap" style="display:none">
  <h2>plan <span class="note" id="plan-meta"></span></h2>
  <ol id="plan-list"></ol>
</div>
<div id="issues-wrap">
  <h2>R — requirements <span class="note">(委譲 = その R を goal に auto-step を起動)</span></h2>
  <div id="issues"></div>
</div>
<div class="panels" style="grid-template-columns:1fr 1fr">
  <div class="panel"><h2>K</h2><ul id="panel-K"></ul></div>
  <div class="panel"><h2>S</h2><ul id="panel-S"></ul></div>
</div>
<div id="memory-wrap" style="display:none">
  <h2>memory <span class="note" id="memory-meta"></span></h2>
  <pre id="memory-index" style="max-height:14em;overflow-y:auto;font-size:.85em;
       background:#fafafa;border:1px solid #ddd;border-radius:4px;padding:.5em .7em;
       white-space:pre-wrap"></pre>
  <ul id="memory-files" style="font-size:.85em"></ul>
</div>
<div id="thread-wrap">
  <h2>thread <span class="note">(human ↔ executor ↔ judge)</span></h2>
  <ol id="thread"></ol>
  <form id="comment-form">
    <select id="comment-to">
      <option value="executor">→ executor</option>
      <option value="judge">→ judge</option>
    </select>
    <textarea id="comment-text" rows="2" placeholder="executor 宛は次の round 境界で注入、judge 宛は即応答"></textarea>
    <button type="submit">送信</button>
  </form>
</div>
<div id="aview-wrap" style="display:none">
  <h2>agent view <span class="note">(show-view による提示)</span></h2>
  <div id="aview"></div>
</div>
<details class="fold" id="timeline-wrap">
  <summary>timeline — 掘る用 <span class="note">(turns + ledger、 直近 100 件)</span></summary>
  <ol id="timeline"></ol>
</details>
<div id="gates-wrap">
  <h2>gate / confirm 判定履歴 <span class="note">(直近 20 件)</span></h2>
  <ul id="gates"></ul>
</div>
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

function renderSummary(st) {
  const s = st.summary || {};
  const nPending = (st.pending || []).length;
  document.getElementById("st-steps").textContent = s.steps || 0;
  document.getElementById("st-tools").textContent = s.tools || 0;
  document.getElementById("st-rejects").textContent = s.rejects || 0;
  document.getElementById("st-skill").textContent = s.skill_updates || 0;
  document.getElementById("st-pending").textContent = nPending;
  document.getElementById("stat-rejects").className =
    "stat" + ((s.rejects || 0) > 0 ? " alert" : "");
  document.getElementById("stat-pending").className =
    "stat" + (nPending > 0 ? " attn" : "");
  const p = st.plan;
  document.getElementById("st-plan").textContent =
    p ? (p.status === "approved" ? p.done + "/" + p.total : p.status) : "—";
  document.getElementById("stat-plan").className =
    "stat" + (p && p.status === "rejected" ? " alert"
            : p && p.status === "proposed" ? " attn" : "");
  // 中身が空/未活性のタイルは薄く (押しても何も無いことを見た目で示す)
  document.getElementById("stat-pending").classList.toggle("dead", nPending === 0);
  document.getElementById("stat-plan").classList.toggle("dead", !p);
}

// goal パネル — session-intent (何を目指すか) と judge verdict (達成したか) を最上部に。
function renderGoal(st) {
  const wrap = document.getElementById("goal-wrap");
  const badge = document.getElementById("goal-badge");
  const text = document.getElementById("goal-text");
  const next = document.getElementById("goal-next");
  const intent = st.intent || "";
  const v = st.verdict;  // {done, next} | null
  if (!intent && !v) { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  text.textContent = intent ? ("goal: " + intent) : "";
  if (v && v.done) {
    badge.className = "done"; badge.textContent = "達成 ✅"; next.textContent = "";
  } else if (v) {
    badge.className = "pending"; badge.textContent = "未達 ⏳";
    next.textContent = v.next ? ("NEXT: " + v.next) : "";
  } else {
    badge.className = "unknown"; badge.textContent = "未判定 —"; next.textContent = "";
  }
}

// サマリ数字のクリック — アンカー飛びだけだと折りたたみ details や display:none 先で
// 何も起きないので、 対象を開いて/表示してからスクロールする。
function wireStatClicks() {
  for (const a of document.querySelectorAll("#summary .stat")) {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      if (a.classList.contains("dead")) return;
      const href = a.getAttribute("href") || "";
      const id = href.startsWith("#") ? href.slice(1) : "";
      const tgt = id && document.getElementById(id);
      if (!tgt) return;
      if (tgt.tagName === "DETAILS") tgt.open = true;
      if (tgt.style && tgt.style.display === "none") return;  // 中身無し = 飛ばない
      tgt.scrollIntoView({behavior: "smooth", block: "start"});
    });
  }
}

function renderPlan(p) {
  const wrap = document.getElementById("plan-wrap");
  const list = document.getElementById("plan-list");
  list.replaceChildren();
  if (!p) { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  document.getElementById("plan-meta").textContent =
    "#" + p.id + " [" + p.status + (p.source ? " by " + p.source : "") + "]" +
    (p.replaces ? " (replaces #" + p.replaces + ")" : "") + " — " + (p.goal || "") +
    (p.rationale ? "  ／ 改版理由: " + p.rationale : "");
  for (const s of p.steps || []) {
    const li = el("li", s.done ? "done" : "");
    li.append(el("span", "mark", s.done ? "☑" : "☐"), el("span", "what", s.what || ""));
    if (s.why) li.append(el("div", "plan-why", s.why));
    list.append(li);
  }
}

// memory 書き込み turn の検出用 (SSE trigger)。 tool result のパスは絶対とも相対とも
// 限らないので、 dir 末尾 2 セグメント (例 "data/memory") で照合する。
// 誤発火は refreshPanels 1 回分 (冪等・軽量) なので広めに取る。
let memTail = "";

function renderMemory(m) {
  const wrap = document.getElementById("memory-wrap");
  if (!m) { wrap.style.display = "none"; memTail = ""; return; }
  wrap.style.display = "";
  memTail = (m.dir || "").split("/").filter(Boolean).slice(-2).join("/");
  document.getElementById("memory-meta").textContent =
    m.dir + " — " + (m.files || []).length + " files";
  document.getElementById("memory-index").textContent =
    m.index || "(index.md なし)";
  const ul = document.getElementById("memory-files");
  ul.replaceChildren();
  const byNew = (m.files || []).slice().sort((a, b) => b.mtime - a.mtime);
  for (const f of byNew.slice(0, 12)) {
    const li = el("li");
    li.append(document.createTextNode(
      f.path + " (" + f.size + "B, " + new Date(f.mtime * 1000).toLocaleString() + ")"));
    ul.append(li);
  }
}

function bumpSummary(id) {
  const b = document.getElementById(id);
  b.textContent = (parseInt(b.textContent, 10) || 0) + 1;
}

function renderPanels(st) {
  renderGoal(st);
  renderSummary(st);
  document.getElementById("sess").textContent =
    st.session_id === null ? "(all)" : (st.session_id || "(none)");

  renderIssues(st);
  renderThread(st.comments || []);

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
  renderPlan(st.plan || null);
  renderAgentView(st.view || null);
  renderMemory(st.memory || null);
}

// --- R issue カード ---
function renderIssues(st) {
  const box = document.getElementById("issues");
  box.replaceChildren();
  const all = st.R || [];
  const active = all.filter(r => !r.replaced);
  const nRep = all.length - active.length;
  if (!active.length) {
    box.append(el("div", "empty", "R なし — (commit-R \\"...\\") で刻まれるとここに出る"));
    return;
  }
  for (const r of active.slice(-12)) {
    const card = el("div", "issue" + ((r.contested_by || []).length ? " contested" : ""));
    const head = el("div", "issue-head");
    head.append(el("span", "id", "#" + r.id));
    if (r.judge === "b" && r.target) {
      const c = el("span", "chip info", "refines #" + r.target);
      if (r.reason) c.title = r.reason;
      head.append(c);
    }
    if (r.judge === "c" && r.target) {
      const c = el("span", "chip warn", "contradicts #" + r.target);
      if (r.reason) c.title = r.reason;
      head.append(c);
    }
    if ((r.refined_by || []).length)
      head.append(el("span", "chip ok", "refined by " + r.refined_by.map(i => "#" + i).join(" ")));
    if ((r.contested_by || []).length)
      head.append(el("span", "chip warn", "contested by " + r.contested_by.map(i => "#" + i).join(" ")));
    head.append(el("span", "ts", fmtTs(r.ts)));
    const btns = el("span", "issue-btns");
    const dg = el("button", "", "委譲 → executor");
    dg.onclick = () => delegateGoal(r.id, r.head, dg);
    btns.append(dg);
    head.append(btns);
    card.append(head, el("div", "issue-text", r.head || ""));
    // why — judge の分類理由 (@judge-reason) と影響 (@judge-impact) を可視化
    if (r.reason) card.append(el("div", "issue-why", "理由: " + r.reason));
    if (r.impact) card.append(el("div", "issue-why", "影響: " + r.impact));
    box.append(card);
  }
  if (nRep > 0) box.append(el("div", "empty", "(+" + nRep + " replaced)"));
}

async function delegateGoal(id, goal, btn) {
  if (!confirm("R#" + id + " を goal に auto-step を起動する?\\n\\n" + goal)) return;
  btn.disabled = true;
  try {
    const r = await fetch("/view/delegate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ goal: goal }),
    }).then(x => x.json());
    if (!r.ok) alert("委譲できない: " + (r.error || "?"));
  } catch (e) {
    alert("委譲の送信に失敗: " + e);
  } finally {
    btn.disabled = false;
  }
}

// --- comment thread (human ↔ executor ↔ judge) ---
const threadSeen = new Set();

function threadLi(ev) {
  const author = ev.author || "?";
  const li = el("li", "a-" + author);
  li.append(el("span", "author", author),
            el("span", "ts", fmtTs(ev.ts)),
            el("span", "body", ev.text !== undefined ? ev.text : (ev.head || "")));
  return li;
}

function appendThread(ev) {
  const key = "meta:" + ev.id;
  if (threadSeen.has(key)) return;
  threadSeen.add(key);
  const th = document.getElementById("thread");
  const emp = th.querySelector(".empty");
  if (emp) emp.remove();
  const nearBottom = th.scrollTop + th.clientHeight >= th.scrollHeight - 40;
  th.append(threadLi(ev));
  while (th.children.length > 200) th.removeChild(th.firstChild);
  if (nearBottom) th.scrollTop = th.scrollHeight;
}

function renderThread(list) {
  const th = document.getElementById("thread");
  th.replaceChildren();
  threadSeen.clear();
  if (!list.length) th.append(el("li", "empty", "コメントなし — 下のフォームから送る"));
  for (const ev of list) appendThread(ev);
  th.scrollTop = th.scrollHeight;
}

async function sendComment(e) {
  e.preventDefault();
  const ta = document.getElementById("comment-text");
  const to = document.getElementById("comment-to").value;
  const text = ta.value.trim();
  if (!text) return;
  try {
    const r = await fetch("/view/comment", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text: text, to: to }),
    }).then(x => x.json());
    if (r.ok) ta.value = "";
    else alert("送信できない: " + (r.error || "?"));
  } catch (err) {
    alert("送信に失敗: " + err);
  }
}
document.getElementById("comment-form").addEventListener("submit", sendComment);

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

let aviewVersion = -1;

function renderAgentView(v) {
  const wrap = document.getElementById("aview-wrap");
  const root = document.getElementById("aview");
  if (!v || !v.root) {
    aviewVersion = -1;
    root.replaceChildren();
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "";
  // 同一 version なら DOM を作り直さない — 再構築すると人間が入力中の
  // (input ...) の値が消えるため。 agent が新しい view を示したときだけ描き直す。
  if (v.version === aviewVersion) return;
  aviewVersion = v.version;
  root.replaceChildren();
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
    if (ev.src === "meta" && ev.tag === "comment") { appendThread(ev); refreshPanels(); }
    // 要約カウンタの即時更新 (真値は refreshPanels の再取得で揃う)
    if (ev.src === "turn" && ev.tag === "assistant") bumpSummary("st-steps");
    if (ev.src === "turn" && ev.tag === "tool") bumpSummary("st-tools");
    if (["R", "K", "S", "gate", "skill", "confirm", "intent",
         "plan", "plan-approval", "plan-progress", "brainwash"].includes(ev.tag)) refreshPanels();
    // memory dir へのファイル書き込み (write_file 等の tool result) でも memory panel を更新
    if (ev.src === "turn" && ev.tag === "tool" && memTail &&
        (ev.head || "").includes(memTail)) refreshPanels();
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
  // 再接続 = server が再起動している可能性 — version 番号は process ごとに 1 から
  // 振り直されるため、 前 process の番号と衝突して stale view を掴まないようリセット
  aviewVersion = -1;
  renderPanels(st);
  seen.clear();
  document.getElementById("timeline").replaceChildren();
  for (const ev of st.timeline || []) appendTimeline(ev);
  const tl = document.getElementById("timeline");
  tl.scrollTop = tl.scrollHeight;
  connect(st.cursors || { meta: 0, turn: 0 });
}

wireStatClicks();
init();
</script>
</body></html>
"""
