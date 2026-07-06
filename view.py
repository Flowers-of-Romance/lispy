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


def s_diff(db: sqlite3.Connection, event_id: int) -> dict | None:
    """meta_events.id (kind='S') 1 件と、同名の直前 S スナップショットとの diff。

    直前候補の特定は lispy.py の _S_history/_restore_S と同じ二段構え:
    `payload LIKE '%"name": "<name>"%'` で粗選別 (id < 自分、id 降順 LIMIT 5) →
    json.loads して name 完全一致で確定。**session を跨いで探す** — S lineage は
    session 再開後も続く既存の規約に合わせる (同一 session 限定だと再開直後の
    commit-S が毎回「新規定義」に誤表示される)。

    戻り値のキーは `lambda_kind` — payload 内の `kind` は λ の種別 (lisp/host等) で
    meta_events.kind の "S" (event 種別) とは別物なので、 混同を避けて改名する。
    該当行が無い (id 不正 / kind != S) 場合は None — 呼び出し側で 404 に変換する。
    """
    row = db.execute(
        "SELECT id, ts, session_id, payload FROM meta_events WHERE id = ? AND kind = 'S'",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    rid, ts, sid, payload = row
    try:
        p = json.loads(payload or "")
    except Exception:
        return None
    name = p.get("name", "?")
    body = p.get("body", "") or ""

    # LIKE 用に \ % _ をエスケープ — name にワイルドカード文字が含まれると別名にも
    # 広く一致し、 LIMIT 5 の候補窓から本来の直前 S が落ちて誤って「新規定義」になる。
    like_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    candidates = db.execute(
        "SELECT id, ts, session_id, payload FROM meta_events "
        "WHERE kind = 'S' AND id < ? AND payload LIKE ? ESCAPE '\\' "
        "ORDER BY id DESC LIMIT 5",
        (rid, f'%"name": "{like_name}"%'),
    ).fetchall()
    prev = None
    for p_id, p_ts, p_sid, p_payload in candidates:
        try:
            pp = json.loads(p_payload or "")
        except Exception:
            continue
        if pp.get("name") == name:
            prev = (p_id, p_ts, p_sid, pp)
            break  # id 降順で最初に一致したものが直前

    base = {
        "id": rid, "ts": ts, "sid": sid, "name": name,
        "lambda_kind": p.get("kind", "?"),
        "rationale": p.get("rationale", ""),
    }
    if prev is None:
        base.update({
            "is_first": True,
            "prev_id": None, "prev_ts": None, "prev_sid": None,
            "lines": diff_lines("", body),
        })
        return base
    p_id, p_ts, p_sid, pp = prev
    base.update({
        "is_first": False,
        "prev_id": p_id, "prev_ts": p_ts, "prev_sid": p_sid,
        "lines": diff_lines(pp.get("body", "") or "", body),
    })
    return base


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
            ev["source"] = p.get("source") or "?"
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
            ev["source"] = p.get("source") or "?"
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


def actors_state() -> dict:
    """実行系アクターの env 由来メタ情報 (モデル名等)。読み取り専用 — ハードコード禁止。
    executor は host.MODEL (.env の LLM_MODEL)、judge は host.judge_model()
    (JUDGE_MODEL 未設定なら executor に fallback — judge_configured() で見分ける)。"""
    return {
        "executor": {"model": host.MODEL},
        "judge": {"model": host.judge_model(), "configured": host.judge_configured()},
    }


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


def _classify_verdict(text: Any) -> dict | None:
    """judge コメント text を verdict に分類。 DONE 始まり→達成、 NEXT: 始まり→未達
    (+次の一手)。 それ以外 (@judge 宛の自由発言等) は verdict でない → None。"""
    stripped = str(text or "").strip().lstrip()
    if stripped.upper().startswith("DONE"):
        return {"done": True, "next": ""}
    if stripped.upper().startswith("NEXT:"):
        return {"done": False, "next": _head(stripped[5:].strip())}
    return None


def _latest_verdict(db: sqlite3.Connection, sid: str | None) -> dict | None:
    """最新の judge verdict (kind=comment で author=judge)。 達成バッジの真値。
    judge 発言が無ければ None (未判定)。 sessions_list の一括版と意味論を揃える —
    judge 発言だけを新しい順に舐め、 最初に DONE/NEXT へ分類できた行を採る
    (自由発言が何件積まれても最新 verdict を取りこぼさない。 LIMIT は掛けない —
    judge 発言は元々少なく、 SQL 側で author=judge に絞ってある)。"""
    if sid is None:
        rows = db.execute(
            "SELECT payload FROM meta_events WHERE kind = 'comment' "
            "AND json_extract(payload, '$.author') = 'judge' "
            "ORDER BY id DESC").fetchall()
    else:
        rows = db.execute(
            "SELECT payload FROM meta_events WHERE kind = 'comment' AND session_id = ? "
            "AND json_extract(payload, '$.author') = 'judge' "
            "ORDER BY id DESC", (sid,)).fetchall()
    for (payload,) in rows:
        try:
            p = json.loads(payload or "")
        except Exception:
            continue
        v = _classify_verdict(p.get("text"))
        if v is not None:
            return v
    return None


def sessions_list(db: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """セッション一覧 (/sessions ページ用)。 host.cmd_list と同じ土台に
    goal (session-intent) と最新 judge verdict を足す。 開始時刻降順。
    intent / judge comment は一括で 1 クエリずつ取って session ごとにバケットする
    (行ごとに _latest_* を呼ぶ N+1 を避ける)。"""
    rows = db.execute(
        """
        SELECT s.id, s.started_at, s.title, s.domain, COUNT(t.id) AS n
        FROM sessions s LEFT JOIN turns t ON t.session_id = s.id
        GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))

    # 最新 intent per session — id 昇順で舐めて最後が残る = 最新
    intents: dict[str, str] = {}
    for s_id, payload in db.execute(
        f"SELECT session_id, payload FROM meta_events WHERE kind = 'intent' "
        f"AND session_id IN ({marks}) ORDER BY id ASC", ids):
        intents[s_id] = (payload or "").strip()

    # judge verdict per session — 昇順で舐め、 DONE/NEXT に分類できたものだけ残す
    # (自由発言 = None は上書きしない) → 各 session の最新 verdict が残る。
    verdicts: dict[str, dict] = {}
    for s_id, payload in db.execute(
        f"SELECT session_id, payload FROM meta_events WHERE kind = 'comment' "
        f"AND json_extract(payload, '$.author') = 'judge' "
        f"AND session_id IN ({marks}) ORDER BY id ASC", ids):
        try:
            v = _classify_verdict(json.loads(payload or "").get("text"))
        except Exception:
            v = None
        if v is not None:
            verdicts[s_id] = v

    return [
        {
            "id": sid, "started_at": ts, "title": title or "",
            "domain": domain or "", "turns": n,
            "goal": _head(intents.get(sid, "")),
            "verdict": verdicts.get(sid),
        }
        for sid, ts, title, domain, n in rows
    ]


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
# ダッシュボード HTML — 静的 1 枚 (alook 風 3 ペイン・ダークテーマ)。 レンダラーは
# 受け取った JSON を textContent で DOM に足すだけ (eval / innerHTML / 外部 fetch なし)。
#
# 画面構成:
#   header      — session id / SSE 接続インジケータ / health 概要 (24h 集計)
#   左サイドバー — executor / judge / auto-step の状態カード + session 切替
#   中央        — turns + meta_events を ts で 1 本にした chat 風 timeline
#                 (kind ごとに見た目を分け、 上部の kind フィルタで絞れる)。
#                 R/K/S 台帳・memory・agent view (show-view)・gate 判定履歴は
#                 掘る用の折りたたみとして下に並べる (機能は全て従来のまま)。
#   右サイドバー — gate インボックス (承認/却下) / plan / goal board (delegate の
#                 実行状況をコメントログから導出) / 入力 (comment・delegate)
#
# データは /view/state (初期スナップショット) + /view/events (SSE 追記) のみ —
# 新しい書き込み系エンドポイントは足していない。 goal board は既存の comment
# (kind=comment, author=human/system, text="[委譲] ..." / "[委譲 run ...]") を
# 手がかりに client 側で導出するだけで、 バックエンドは 1 行も増えていない。
# ---------------------------------------------------------------------------

# /view と /spec /sessions (server.py) が共有するダーク CSS の土台。 view.py 側に
# 一本化する (フロントは view.py が固定で持つ、という既存原則の延長)。 :root 変数と
# 基本要素は VIEW_HTML から移設 (コピーでなく移動 — 二重定義を作らない)。
# table/pre/.kind-*/.switch は view.py 自身は使わないが、/spec /sessions 用に
# ここへ追加する (置き場を一本化するため)。
BASE_CSS = """
  :root{
    --bg:#0b0d12; --bg2:#0f1218; --panel:#151922; --panel2:#1b202b;
    --border:#2a3040; --text:#e6edf3; --muted:#8b96a5; --accent:#58a6ff;
    --ok:#3fb950; --warn:#d29922; --danger:#f85149; --info:#a371f7;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;
       background:var(--bg);color:var(--text);line-height:1.5;font-size:14px}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  h1,h2,h3{margin:.2em 0;font-weight:600}
  h3{font-size:.95em;color:var(--muted)}
  .note{color:var(--muted);font-size:.82em}
  .id{color:var(--muted);font-family:ui-monospace,monospace;font-size:.85em}
  .empty{color:var(--muted);font-style:italic;list-style:none;margin-left:-1.1em}
  table{border-collapse:collapse;width:100%;margin:.5em 0}
  th,td{border:1px solid var(--border);padding:.4em .6em;text-align:left;vertical-align:top;color:var(--text)}
  th{background:var(--panel2)}
  pre{background:#0d1016;padding:.5em;overflow-x:auto;font-size:.85em;color:var(--text);
      border:1px solid var(--border);border-radius:4px}
  .replaced{text-decoration:line-through;color:var(--muted)}
  .kind-R{background:#241b06} .kind-K{background:#0f2a17} .kind-S{background:#0d1b33}
  .kind-artifact{background:#2a1013} .kind-intent{background:#1b202b}
  .kind-test-S-R,.kind-replay,.kind-restore-S{background:var(--panel2)}
  .switch{margin:1em 0;font-size:.9em} .switch a{margin-right:1em}

  /* ---- side-block / panels / 折りたたみ — VIEW_HTML から移設 (/sessions /spec でも使う) ---- */
  .side-block{margin-bottom:1.3em}
  .side-block h2{font-size:.78em;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);
                 border-bottom:1px solid var(--border);padding-bottom:.3em;margin-bottom:.5em}

  .panels{display:flex;gap:1em;margin:.5em 0;flex-wrap:wrap}
  .panel{flex:1;min-width:12em;border:1px solid var(--border);border-radius:6px;
         padding:.3em .6em .5em;background:var(--panel2)}
  .panel ul{margin:0;padding-left:1.1em;font-size:.85em}
  .panel li{margin:.25em 0}

  details.fold{border:1px solid var(--border);border-radius:6px;background:var(--panel);margin:.6em 0}
  details.fold>summary{cursor:pointer;padding:.4em .6em;font-size:.85em;color:var(--muted);
                       background:var(--panel2);border-radius:6px;list-style:none}
  details.fold>summary::-webkit-details-marker{display:none}
  details.fold>summary::before{content:"▸ ";}
  details.fold[open]>summary::before{content:"▾ ";}
  details.fold[open]>summary{border-bottom:1px solid var(--border);border-radius:6px 6px 0 0}
  details.fold>div,details.fold>ul,details.fold>ol,details.fold>pre{margin:0;padding:.6em .8em}

  /* ---- ページヘッダ / ナビピル — /sessions /spec の全幅化用 (/view の #topbar と同じトーン) ---- */
  .page-header{display:flex;align-items:center;justify-content:space-between;gap:1em;
               flex-wrap:wrap;padding:.6em 1em;background:var(--panel);
               border-bottom:1px solid var(--border);margin-bottom:1em}
  .page-header h1{font-size:1.1em;margin:0;border:none;padding:0}
  .nav-pills{display:flex;gap:.5em;flex-wrap:wrap}
  .nav-pills a{padding:.25em .9em;border-radius:14px;background:var(--panel2);
               border:1px solid var(--border);color:var(--muted);font-size:.85em}
  .nav-pills a:hover{border-color:var(--accent);color:var(--text);text-decoration:none}

  /* ---- S 書き替え diff トグル (S 台帳 / タイムライン共通) ---- */
  .diff-link{color:var(--accent);cursor:pointer;font-size:.78em;margin-left:.6em;text-decoration:underline}
  .diff-link:hover{color:var(--text)}
  .sdiff-box{margin:.3em 0 .6em}
"""

# /spec /sessions が /view のモーダル iframe 内に埋め込まれたときのリンク挙動調整。
# iframe 内で /view 系リンクを踏むと iframe の中に /view が入れ子で開いてしまうため、
# top ウィンドウ遷移 (target=_top) に切り替える。/sessions ↔ /spec の相互リンクは
# iframe 内遷移のままで良いので触らない。フロント資産は view 層が固定で持つ原則に
# 従いここに置き、server.py の _spec_page が shared_css と同様に値渡しで埋め込む
# (テンプレートへ直書きすると JS のブレースで .format() が壊れる)。
IFRAME_NAV_JS = """
if (window.self !== window.top) {
  var links = document.querySelectorAll('a[href^="/view"]');
  for (var i = 0; i < links.length; i++) links[i].target = "_top";
}
"""

VIEW_HTML = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<title>lispy view</title>
<style>""" + BASE_CSS + """
  /* ---- header ---- */
  #topbar{display:flex;align-items:center;gap:1.2em;flex-wrap:wrap;
          padding:.5em 1em;background:var(--panel);border-bottom:1px solid var(--border)}
  .topbar-left{display:flex;align-items:center;gap:.5em}
  .topbar-left h1{font-size:1.1em}
  .topbar-mid{display:flex;gap:.6em;flex-wrap:wrap;flex:1}
  .topbar-right{display:flex;gap:.5em;align-items:center;margin-left:auto}
  .dot{display:inline-block;width:.65em;height:.65em;border-radius:50%;background:var(--muted)}
  .dot.on{background:var(--ok);box-shadow:0 0 6px var(--ok)}
  .dot.off{background:var(--danger)}
  .pill{font-size:.78em;padding:.2em .7em;border-radius:10px;background:var(--panel2);
        color:var(--muted);border:1px solid var(--border)}
  .pill#pill-busy{background:#3a2c05;color:var(--warn);border-color:var(--warn)}

  .stat{display:flex;flex-direction:column;align-items:center;min-width:6.5em;
        padding:.3em .7em;border:1px solid var(--border);border-radius:6px;background:var(--panel2);
        cursor:pointer;color:var(--text)}
  .stat:hover{border-color:var(--accent)}
  .stat.dead{opacity:.4;cursor:default}
  .stat.dead:hover{border-color:var(--border)}
  .stat b{font-size:1.3em;line-height:1.2}
  .stat span{font-size:.7em;color:var(--muted)}
  .stat.alert{background:#3b1418;border-color:var(--danger)}
  .stat.alert b{color:var(--danger)}
  .stat.attn{background:#3a2c05;border-color:var(--warn)}
  .stat.attn b{color:var(--warn)}

  /* ---- goal banner ---- */
  #goal-wrap{margin:.7em 1em;padding:.6em 1em;border:1px solid var(--border);border-radius:8px;
             background:var(--panel)}
  #goal-badge{font-size:1.05em;font-weight:bold;margin-bottom:.2em}
  #goal-badge.done{color:var(--ok)}
  #goal-badge.pending{color:var(--warn)}
  #goal-badge.unknown{color:var(--muted)}
  #goal-text{font-size:1em}

  /* ---- 3 ペインレイアウト ---- */
  .layout{display:grid;grid-template-columns:220px minmax(0,1fr) 320px;gap:0;
          align-items:start}
  #sidebar-left,#sidebar-right{background:var(--bg2);padding:.8em;
          max-height:calc(100vh - 6.5em);overflow-y:auto;position:sticky;top:0}
  #main-pane{padding:.8em 1em 2em;min-width:0;display:flex;flex-direction:column}

  /* .side-block は BASE_CSS へ移設済み (view.py 冒頭参照) */
  .switch{display:flex;flex-direction:column;gap:.3em;font-size:.9em}

  /* ---- ページモーダル (/sessions /spec を iframe で重ね表示) ---- */
  #page-modal{position:fixed;inset:0;display:none;z-index:1000;
              background:rgba(0,0,0,.6);align-items:center;justify-content:center}
  #page-modal.open{display:flex}
  #page-modal-box{width:92%;height:90%;background:var(--bg);border:1px solid var(--border);
                  border-radius:10px;display:flex;flex-direction:column;overflow:hidden}
  #page-modal-head{display:flex;align-items:center;gap:.6em;padding:.4em .8em;
                   background:var(--panel);border-bottom:1px solid var(--border)}
  #page-modal-title{font-size:.9em;color:var(--muted)}
  #page-modal-close{margin-left:auto;background:none;border:1px solid var(--border);
                    border-radius:6px;color:var(--text);cursor:pointer;font-size:1em;
                    padding:.1em .6em;line-height:1.4}
  #page-modal-close:hover{border-color:var(--accent)}
  #page-modal-frame{flex:1;border:0;width:100%;background:var(--bg)}

  /* ---- agent カード ---- */
  .agent-card{background:var(--panel);border:1px solid var(--border);border-radius:6px;
              padding:.5em .7em;margin-bottom:.5em}
  .agent-card-head{display:flex;align-items:center;gap:.45em;font-weight:600}
  .agent-dot{width:.6em;height:.6em;border-radius:50%;background:var(--muted);flex:none}
  .agent-dot.busy{background:var(--accent);box-shadow:0 0 6px var(--accent)}
  .agent-dot.idle{background:var(--muted)}
  .agent-dot.ok{background:var(--ok)}
  .agent-dot.attn{background:var(--warn)}
  .agent-dot.danger{background:var(--danger);box-shadow:0 0 6px var(--danger)}
  .agent-status{font-size:.82em;color:var(--muted);margin-top:.25em;white-space:pre-wrap}

  /* ---- kind フィルタ ---- */
  .filter-bar{display:flex;gap:.4em;flex-wrap:wrap;margin-bottom:.6em}
  .filt{background:var(--panel2);border:1px solid var(--border);color:var(--muted);
        border-radius:12px;padding:.2em .8em;font-size:.8em;cursor:pointer}
  .filt.active{background:var(--accent);color:#04101f;border-color:var(--accent);font-weight:600}

  /* ---- chat 風 timeline ---- */
  #chat-timeline{list-style:none;margin:0 0 1em;padding:.6em;flex:0 0 auto;
                 display:flex;flex-direction:column;gap:.4em;overflow-y:auto;
                 border:1px solid var(--border);border-radius:8px;background:var(--panel);
                 min-height:16em;max-height:60vh}
  .ev{border-radius:6px;padding:.35em .6em;font-size:.87em;border-left:3px solid var(--border);
      background:var(--panel2)}
  .ev-head{display:flex;gap:.5em;align-items:baseline;flex-wrap:wrap}
  .ev-ts{color:var(--muted);font-family:ui-monospace,monospace;font-size:.8em}
  .ev-badge{font-family:ui-monospace,monospace;font-size:.76em;padding:.05em .55em;
            border-radius:8px;background:#22272e;color:var(--muted)}
  .ev-body{white-space:pre-wrap;word-break:break-word;margin-top:.15em}
  .ev-why{color:var(--danger);font-size:.85em;margin-top:.25em;white-space:pre-wrap}
  .ev.rejected{background:#3b1418;border-left-color:var(--danger)}
  .ev.rejected .ev-body{color:#ff9a95;font-weight:600}
  .ev.verdict-done{outline:1px solid var(--ok)}
  .ev.verdict-next{outline:1px solid var(--warn)}
  .ev.verdict-stuck{outline:2px solid var(--danger)}
  .ev.verdict-stuck .ev-body{color:#ff9a95;font-weight:600}
  /* ---- actor (誰の発言/動作か) 別インデント。 データ由来の色帯 (data-tag) とは
     別次元として共存させる — 変更しない。 #chat-timeline は既に
     display:flex;flex-direction:column なので align-self がそのまま効く。 */
  .ev[data-actor="executor"]{margin-right:4em}
  .ev[data-actor="judge"]{margin-left:1.6em;margin-right:2.4em}
  .ev[data-actor="human"]{margin-left:4em;align-self:flex-end;max-width:80%}
  .ev[data-actor="system"]{margin-left:2.4em;margin-right:2.4em;opacity:.85;font-style:italic}
  .ev[data-tag="assistant"]{border-left-color:var(--info)}
  .ev[data-tag="tool"]{border-left-color:var(--warn)}
  .ev[data-tag="user"]{border-left-color:var(--muted)}
  .ev[data-tag="R"]{border-left-color:#d2b13a}
  .ev[data-tag="K"]{border-left-color:var(--ok)}
  .ev[data-tag="S"]{border-left-color:var(--accent)}
  .ev[data-cat="gate"]{border-left-color:var(--ok)}
  .ev[data-cat="plan"]{border-left-color:#2dd4bf}
  .ev[data-cat="comment"]{border-left-color:#7ee787}
  .ev-badge.author-human{background:#123a5e;color:#79c0ff}
  .ev-badge.author-executor{background:#2f1e57;color:#d2a8ff}
  .ev-badge.author-judge{background:#123d1f;color:#7ee787}
  .ev-badge.author-system{background:#22272e;color:var(--muted)}
  #chat-timeline.hide-turn .ev[data-cat="turn"]{display:none}
  #chat-timeline.hide-gate .ev[data-cat="gate"]{display:none}
  #chat-timeline.hide-comment .ev[data-cat="comment"]{display:none}
  #chat-timeline.hide-spec .ev[data-cat="spec"]{display:none}
  #chat-timeline.hide-plan .ev[data-cat="plan"]{display:none}
  #chat-timeline.hide-other .ev[data-cat="other"]{display:none}

  /* details.fold は BASE_CSS へ移設済み */

  /* ---- R issue カード ---- */
  #issues{display:flex;flex-direction:column;gap:.5em;margin:.3em 0 .8em}
  .issue{border:1px solid var(--border);border-left:4px solid var(--warn);border-radius:6px;
         padding:.4em .7em;background:var(--panel2)}
  .issue.contested{border-left-color:var(--danger)}
  .issue-head{display:flex;align-items:center;gap:.5em;flex-wrap:wrap}
  .issue-text{margin:.15em 0 .3em;font-size:.95em}
  .issue-why{color:var(--muted);font-size:.82em;margin:.1em 0 0 .3em}
  .chip{display:inline-block;font-size:.72em;padding:.05em .55em;border-radius:9px;
        border:1px solid var(--border);background:var(--panel);color:var(--muted);
        font-family:ui-monospace,monospace}
  .chip.warn{background:#3b1418;border-color:var(--danger);color:#ff9a95}
  .chip.ok{background:#123d1f;border-color:var(--ok);color:#7ee787}
  .chip.info{background:#123a5e;border-color:var(--accent);color:#79c0ff}
  .issue-btns button{font-size:.78em;padding:.1em .8em;border-radius:4px;border:1px solid var(--border);
                     background:var(--panel);color:var(--text);cursor:pointer}
  .issue-btns button:hover{border-color:var(--accent)}

  /* .panels/.panel は BASE_CSS へ移設済み */

  /* ---- plan checklist ---- */
  #plan-list{list-style:none;margin:0;padding:0;font-size:.88em}
  #plan-list li{margin:.3em 0;padding:.2em 0}
  #plan-list li.done .what{color:var(--ok)}
  #plan-list .mark{font-family:ui-monospace,monospace;margin-right:.3em}
  .plan-why{color:var(--muted);font-size:.85em;margin-left:1.4em}

  /* ---- gate インボックス ---- */
  .gate{border:1px solid var(--warn);border-radius:6px;padding:.5em .7em;margin:.5em 0;
        background:#241b06}
  .gate-head{font-weight:bold}
  .gate-detail{font-size:.85em;color:var(--muted);margin:.3em 0;white-space:pre-wrap}
  .gate-btns{margin-top:.5em;display:flex;gap:.6em}
  .gate-btns button{padding:.25em 1.1em;border-radius:4px;border:1px solid var(--border);
                    cursor:pointer;font-size:.88em;background:var(--panel2);color:var(--text)}
  .gate-btns button.approve{background:#123d1f;border-color:var(--ok);color:#7ee787}
  .gate-btns button.deny{background:#3b1418;border-color:var(--danger);color:#ff9a95}

  .diff{font-family:ui-monospace,monospace;font-size:.8em;background:#0d1016;
        border:1px solid var(--border);border-radius:4px;padding:.3em .5em;margin:.3em 0;
        max-height:20em;overflow:auto;white-space:pre}
  .diff-file{color:var(--muted);font-weight:bold;margin-bottom:.2em}
  .d-add{background:#0f2a17;color:#7ee787}
  .d-del{background:#3b1418;color:#ff9a95}
  .d-meta{color:var(--muted)}
  .d-ctx{color:var(--text)}

  /* ---- agent view (show-view) ---- */
  #aview{border:1px solid var(--info);border-radius:6px;padding:.6em .8em;background:#171227;font-size:.9em}
  .v-row{display:flex;gap:1em;flex-wrap:wrap}
  .v-col,.v-form{display:flex;flex-direction:column;gap:.4em}
  .v-form{border:1px dashed var(--info);border-radius:4px;padding:.4em .6em}
  .v-text{white-space:pre-wrap}
  .v-input input{margin-left:.4em;padding:.15em .4em;border:1px solid var(--border);
                 border-radius:3px;background:var(--bg2);color:var(--text)}
  .v-button{align-self:flex-start;padding:.25em 1.2em;border-radius:4px;border:1px solid var(--info);
            background:#241a3d;color:var(--text);cursor:pointer}
  .v-err{color:var(--danger);font-style:italic}
  #aview table{border-collapse:collapse}
  #aview th,#aview td{border:1px solid var(--border);padding:.2em .5em;text-align:left}
  #aview th{background:var(--panel2)}

  /* ---- memory ---- */
  #memory-index{max-height:14em;overflow-y:auto;font-size:.85em;background:#0d1016;
                border:1px solid var(--border);border-radius:4px;padding:.5em .7em;
                white-space:pre-wrap;color:var(--text)}
  #memory-files{font-size:.85em}

  /* ---- goal board ---- */
  #goalboard{list-style:none;margin:0;padding:0;font-size:.86em;display:flex;
             flex-direction:column;gap:.4em}
  .goal-item{border:1px solid var(--border);border-radius:6px;padding:.4em .6em;background:var(--panel2)}
  .goal-badge{display:inline-block;font-size:.72em;padding:.05em .6em;border-radius:9px;
              margin-bottom:.2em;font-family:ui-monospace,monospace}
  .status-running .goal-badge{background:#123a5e;color:#79c0ff}
  .status-done .goal-badge{background:#123d1f;color:#7ee787}
  .status-failed .goal-badge{background:#3b1418;color:#ff9a95}
  .goal-item.judge-task{border-left:4px solid var(--info)}
  .status-judge-task .goal-badge{background:#241a3d;color:#d2a8ff}
  .goal-text{white-space:pre-wrap;word-break:break-word}
  .goal-result{color:var(--muted);font-size:.85em;margin-top:.2em;white-space:pre-wrap}

  /* ---- 入力フォーム ---- */
  .stack-form{display:flex;flex-direction:column;gap:.4em;margin-bottom:.8em}
  .stack-form select,.stack-form textarea{padding:.3em .5em;border:1px solid var(--border);
      border-radius:4px;background:var(--bg2);color:var(--text);font-family:inherit;font-size:.88em}
  .stack-form button{padding:.35em 1em;border-radius:4px;border:1px solid var(--accent);
      background:#123a5e;color:#cfe6ff;cursor:pointer;font-size:.88em}
  .stack-form button:hover{background:#1a4a75}

  /* ---- レスポンシブ (最低限) ---- */
  @media (max-width: 980px){
    .layout{display:block}
    #sidebar-left,#sidebar-right{max-height:none;position:static;overflow-y:visible}
    #chat-timeline{max-height:40vh}
  }
</style></head><body>
<header id="topbar">
  <div class="topbar-left">
    <h1>lispy view</h1>
    <span class="dot" id="conn"></span>
  </div>
  <div class="topbar-mid" id="summary">
    <a class="stat" href="#chat-timeline"><b id="st-steps">0</b><span>step (24h)</span></a>
    <a class="stat" href="#chat-timeline"><b id="st-tools">0</b><span>tool (24h)</span></a>
    <a class="stat" id="stat-rejects" href="#gates-wrap"><b id="st-rejects">0</b><span>REJECT (24h)</span></a>
    <a class="stat" id="stat-skill" href="#gates-wrap"><b id="st-skill">0</b><span>SKILL.md 書換 (24h)</span></a>
    <a class="stat" id="stat-pending" href="#pending-wrap"><b id="st-pending">0</b><span>承認待ち</span></a>
    <a class="stat" id="stat-plan" href="#plan-wrap"><b id="st-plan">—</b><span>plan</span></a>
  </div>
  <div class="topbar-right">
    <span class="pill" id="pill-busy" style="display:none">評価実行中</span>
    <span class="pill" id="pill-watchers">閲覧中 0</span>
  </div>
</header>
<div id="goal-wrap" style="display:none">
  <div id="goal-badge"></div>
  <div id="goal-text"></div>
  <div id="goal-next" class="note"></div>
</div>
<div class="layout">
  <aside id="sidebar-left">
    <div class="side-block">
      <h2>エージェント</h2>
      <div class="agent-card" id="agent-executor">
        <div class="agent-card-head"><span class="agent-dot" id="dot-executor"></span><span>executor</span></div>
        <div class="agent-status" id="status-executor">-</div>
      </div>
      <div class="agent-card" id="agent-judge">
        <div class="agent-card-head"><span class="agent-dot" id="dot-judge"></span><span>judge</span></div>
        <div class="agent-status" id="status-judge">-</div>
      </div>
      <div class="agent-card" id="agent-autostep">
        <div class="agent-card-head"><span class="agent-dot" id="dot-autostep"></span><span>auto-step</span></div>
        <div class="agent-status" id="status-autostep">-</div>
      </div>
    </div>
    <div class="side-block">
      <h2>セッション</h2>
      <div class="note">scope: <span id="scope"></span></div>
      <div class="note" style="margin-bottom:.6em">session: <span id="sess">…</span></div>
      <nav class="switch">
        <a href="/view">現在の session</a>
        <a href="/view?session=all">全 session</a>
        <a href="/sessions" data-modal="sessions">session 一覧</a>
        <a href="/spec" data-modal="spec">spec</a>
      </nav>
    </div>
  </aside>

  <main id="main-pane">
    <div class="filter-bar" id="kind-filters">
      <button class="filt active" data-cat="turn" type="button">step</button>
      <button class="filt active" data-cat="gate" type="button">gate</button>
      <button class="filt active" data-cat="comment" type="button">comment</button>
      <button class="filt active" data-cat="spec" type="button">R/K/S</button>
      <button class="filt active" data-cat="plan" type="button">plan</button>
      <button class="filt active" data-cat="other" type="button">other</button>
    </div>
    <ol id="chat-timeline"></ol>

    <details class="fold" id="aview-wrap" style="display:none">
      <summary>agent view <span class="note">(show-view による提示)</span></summary>
      <div id="aview"></div>
    </details>

    <details class="fold" id="ledger-wrap" open>
      <summary>R / K / S 台帳</summary>
      <div id="issues-wrap">
        <h3>R — requirements <span class="note">(委譲 = その R を goal に auto-step を起動)</span></h3>
        <div id="issues"></div>
      </div>
      <div class="panels">
        <div class="panel"><h3>K</h3><ul id="panel-K"></ul></div>
        <div class="panel"><h3>S</h3><ul id="panel-S"></ul></div>
      </div>
    </details>

    <details class="fold" id="memory-wrap" style="display:none">
      <summary>memory <span class="note" id="memory-meta"></span></summary>
      <pre id="memory-index"></pre>
      <ul id="memory-files"></ul>
    </details>

    <details class="fold" id="gates-wrap">
      <summary>gate / confirm 判定履歴 <span class="note">(直近 20 件)</span></summary>
      <ul id="gates"></ul>
    </details>
  </main>

  <aside id="sidebar-right">
    <div class="side-block" id="pending-wrap" style="display:none">
      <h2>gate インボックス</h2>
      <div id="pending"></div>
    </div>
    <div class="side-block" id="plan-wrap" style="display:none">
      <h2>plan <span class="note" id="plan-meta"></span></h2>
      <ol id="plan-list"></ol>
    </div>
    <div class="side-block" id="goalboard-wrap">
      <h2>goal board <span class="note">(delegate 実行状況、 ログから導出)</span></h2>
      <ul id="goalboard"></ul>
    </div>
    <div class="side-block" id="input-wrap">
      <h2>入力</h2>
      <form id="comment-form" class="stack-form">
        <select id="comment-to">
          <option value="executor">→ executor</option>
          <option value="judge">→ judge</option>
        </select>
        <textarea id="comment-text" rows="2"
                  placeholder="executor 宛は次の round 境界で注入、judge 宛は即応答"></textarea>
        <button type="submit">送信</button>
      </form>
      <form id="delegate-form" class="stack-form">
        <textarea id="delegate-text" rows="2" placeholder="goal を入力して auto-step を起動 (委譲)"></textarea>
        <button type="submit">委譲 (auto-step 起動)</button>
      </form>
    </div>
  </aside>
</div>

<!-- /sessions /spec をページ遷移せず重ねて見るためのモーダル (中身は同一オリジン iframe。
     /spec の mermaid CDN 読込は iframe 内に閉じるので /view 本体の自己完結原則は保たれる) -->
<div id="page-modal">
  <div id="page-modal-box">
    <div id="page-modal-head">
      <span id="page-modal-title"></span>
      <button id="page-modal-close" type="button" title="閉じる (ESC)">×</button>
    </div>
    <iframe id="page-modal-frame" src="about:blank" title="埋め込みページ"></iframe>
  </div>
</div>
<script>
"use strict";
const params = new URLSearchParams(location.search);
const scope = params.get("session") || "current";
document.getElementById("scope").textContent = scope;

let es = null;
let retryTimer = null;
let pinned = true;              // chat-timeline を最新に追随させるか (上にスクロール中は止める)
const seen = new Set();         // dedupe key: "<src>:<id>"
let timelineEvents = [];        // 表示中の event (goal board 導出にも使う)
const TIMELINE_MAX = 300;
let memTail = "";               // memory dir 検出用 (renderMemory が設定)
let aviewVersion = -1;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function fmtTs(ts) {
  return new Date(ts * 1000).toLocaleTimeString("ja-JP", { hour12: false });
}
function setStatus(on) {
  document.getElementById("conn").className = "dot " + (on ? "on" : "off");
}
function bumpSummary(id) {
  const b = document.getElementById(id);
  b.textContent = (parseInt(b.textContent, 10) || 0) + 1;
}

// --- kind 分類 (フィルタ / 見た目の両方で使う) ---
function evCategory(ev) {
  if (ev.src === "turn") return "turn";
  switch (ev.tag) {
    case "gate": case "skill": case "confirm": return "gate";
    case "comment": return "comment";
    case "R": case "K": case "S": return "spec";
    case "plan": case "plan-approval": case "plan-progress": return "plan";
    default: return "other";
  }
}

// --- actor (誰の発言/動作か) の導出 ---
// turns は全部 executor 側 trajectory (judge の応答は append-turn されず DB 非記録 —
// auto.lispy の judge-call / lispy.py _prim_judge_call 参照)。 comment は author を
// そのまま使う (未知値は system にフォールバック)。 confirm / plan-approval は
// source (view.py _meta_to_event が新規透過するフィールド)。 gate は why 文字列の
// プレフィックスによるヒューリスティック (lispy.py の文言が変わると judge に
// フォールバックするだけで壊れない)。 skill は judge 固定 (_gate_judge_skill)。
// view-action はブラウザ操作なので human。 それ以外 (R/K/S/intent/plan/
// plan-progress/replay/test-S-R/restore-S 等の ledger 記帳イベント) は簡略化して
// executor 扱い (会話の主役ではないため actor インデントは主に効かせない)。
//
// | イベント             | 条件                                          | actor    |
// |----------------------|-----------------------------------------------|----------|
// | src=turn              | 常に                                           | executor |
// | tag=comment           | author (既知集合外は system)                   | author   |
// | tag=confirm           | source が browser/terminal                     | human    |
// | 〃                    | source が no-answerer/timeout/answerer-lost    | system   |
// | tag=plan-approval     | source が human                                | human    |
// | 〃                    | source が judge                                | judge    |
// | tag=gate              | why が "human-approved" で始まる               | human    |
// | 〃                    | why が "python primitive"/"structural check:"  | system   |
// | 〃 (既定)             | 上記以外 (_gate_call_judge)                    | judge    |
// | tag=skill             | 常に (_gate_judge_skill は judge 固定)          | judge    |
// | tag=view-action       | 常に (ボタン押下)                              | human    |
// | それ以外 (既定)       | R/K/S/intent/plan/plan-progress/replay 等       | executor |
const CANON_ACTORS = new Set(["executor", "judge", "human", "system"]);
function evActor(ev) {
  if (ev.src === "turn") return "executor";
  switch (ev.tag) {
    case "comment": {
      const a = ev.author || "";
      return CANON_ACTORS.has(a) ? a : "system";
    }
    case "confirm":
      if (ev.source === "browser" || ev.source === "terminal") return "human";
      return "system"; // no-answerer/timeout/answerer-lost、および未知値は fail-closed で system
    case "plan-approval":
      if (ev.source === "human") return "human";
      if (ev.source === "judge") return "judge";
      return "system"; // 未知値は fail-closed で system
    case "gate": {
      const why = ev.why || "";
      if (why.startsWith("human-approved")) return "human";
      if (why.startsWith("python primitive") || why.startsWith("structural check:")) return "system";
      return "judge"; // _gate_call_judge による判定の既定
    }
    case "skill": return "judge";
    case "view-action": return "human";
    default: return "executor"; // R/K/S/intent/plan/plan-progress/replay 等の記帳イベント
  }
}

// 生タグ → 日本語ラベル (要件 3)。 comment は actorLabel() を別途使うので含めない。
const TAG_LABELS = {
  tool: "ツール実行", assistant: "ステップ", user: "入力",
  plan: "計画提案", "plan-approval": "計画承認", "plan-progress": "計画進捗",
  gate: "ゲート判定", confirm: "承認/却下", skill: "スキル更新",
  R: "要件 (R)", K: "知識 (K)", S: "実装 (S)", intent: "意図宣言",
  "view-action": "画面操作",
};

// executor/judge の actor バッジにモデル名を添える (要件 2)。 ACTORS は st.actors
// (view.actors_state() — env 由来、ハードコード禁止) を renderPanels() で反映する。
let ACTORS = {};
function actorLabel(actor) {
  if (actor === "executor")
    return "executor" + (ACTORS.executor && ACTORS.executor.model ? " · " + ACTORS.executor.model : "");
  if (actor === "judge") {
    const j = ACTORS.judge || {};
    const suffix = j.model ? " · " + j.model + (j.configured === false ? " (executor fallback)" : "") : "";
    return "judge" + suffix;
  }
  if (actor === "human") return "human";
  return "system";
}

// judge の comment を DONE/NEXT で分類 (host 側 _classify_verdict と同じ規則)。
// 見た目のバッジ付け専用 — 真値 (goal 達成/未達) は st.verdict (サーバ側導出) を使う。
function classifyVerdictText(text) {
  const s = (text || "").trim();
  if (/^done/i.test(s)) return "done";
  if (/^next:/i.test(s)) return "next";
  return null;
}

// judge NEXT: 連発の検出 (要件 5)。 stuck 相当のイベントに verdict-stuck class を
// 付けて強調する。 init() の再初期化で 0 にリセットする (下記 init() 参照)。
const JUDGE_STUCK_STREAK = 3; // この回数以上 NEXT が連続したら「詰まっている」とみなす
let judgeNextStreak = 0;

// --- chat 風 timeline (turns + meta_events を 1 本にした merge の描画) ---
// li.dataset.tag は既存 CSS (.ev[data-tag="..."] の色帯) が依存するのでそのまま残す —
// 表示文字列だけ TAG_LABELS / actorLabel() で日本語化・モデル名付与する。
function renderEventLi(ev) {
  const cat = evCategory(ev);
  const actor = evActor(ev);
  const li = el("li", "ev" + (ev.rejected ? " rejected" : ""));
  li.dataset.cat = cat;
  li.dataset.tag = ev.tag;
  li.dataset.actor = actor;
  const head = el("div", "ev-head");
  head.append(el("span", "ev-ts", fmtTs(ev.ts)));
  if (cat === "comment") {
    // ev.to (宛先) は _meta_to_event が持たない (payload には入っているが未使用の
    // 既存フィールドを増やすと SSE プロトコルに触れるため見送り) — actor だけ出す。
    head.append(el("span", "ev-badge author-" + actor, actorLabel(actor)));
    const v = classifyVerdictText(ev.text);
    if (v === "next") li.classList.add(judgeNextStreak >= JUDGE_STUCK_STREAK ? "verdict-stuck" : "verdict-next");
    else if (v) li.classList.add("verdict-" + v);
  } else {
    head.append(el("span", "ev-badge author-" + actor, TAG_LABELS[ev.tag] || ev.tag));
  }
  li.append(head);
  const bodyText = ev.text !== undefined ? ev.text : (ev.head || "");
  li.append(el("div", "ev-body", bodyText));
  if (ev.why) li.append(el("div", "ev-why", ev.why));
  if (ev.tag === "S") {
    const link = el("span", "diff-link", "diff を見る");
    link.onclick = () => toggleSdiff(ev.id, link);
    li.append(link);
  }
  return li;
}

function appendChatEvent(ev) {
  const key = ev.src + ":" + ev.id;
  if (seen.has(key)) return;
  seen.add(key);
  // judge の verdict streak を先に更新してから描画する — renderEventLi が
  // judgeNextStreak を見て verdict-stuck を付けるかどうかを決めるため。
  if (ev.tag === "comment" && ev.author === "judge") {
    const v = classifyVerdictText(ev.text);
    if (v === "next") judgeNextStreak += 1;
    else if (v === "done") judgeNextStreak = 0;
  }
  timelineEvents.push(ev);
  if (timelineEvents.length > TIMELINE_MAX) {
    // 退避イベントの dedupe キーも seen から落とす — さもないと長時間開いた
    // タブで seen が単調増加する。 id は単調増加 + SSE はカーソルで新規行のみ
    // 送るので、 退避キーを消しても重複再追加の心配は無い。
    const dropped = timelineEvents.shift();
    seen.delete(dropped.src + ":" + dropped.id);
  }
  const tl = document.getElementById("chat-timeline");
  tl.append(renderEventLi(ev));
  while (tl.children.length > TIMELINE_MAX) tl.removeChild(tl.firstChild);
  if (pinned) tl.scrollTop = tl.scrollHeight;
}

document.getElementById("chat-timeline").addEventListener("scroll", () => {
  const tl = document.getElementById("chat-timeline");
  pinned = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 40;
});

// 初期スナップショットは st.timeline (turns+meta 直近 100) と st.comments (直近 50、
// comment だけの深掘り) を id で dedupe して 1 本にする。 SSE は逐次 appendChatEvent。
function mergeInitial(st) {
  const map = new Map();
  for (const ev of (st.timeline || [])) map.set(ev.src + ":" + ev.id, ev);
  for (const ev of (st.comments || [])) map.set(ev.src + ":" + ev.id, ev);
  return Array.from(map.values()).sort((a, b) => (a.ts - b.ts) || (a.id - b.id));
}

// --- kind フィルタ ---
function wireFilters() {
  for (const btn of document.querySelectorAll("#kind-filters .filt")) {
    btn.addEventListener("click", () => {
      btn.classList.toggle("active");
      document.getElementById("chat-timeline").classList.toggle(
        "hide-" + btn.dataset.cat, !btn.classList.contains("active"));
    });
  }
}

// --- header / health ---
function renderHealth(st) {
  document.getElementById("pill-busy").style.display = st.busy ? "" : "none";
  document.getElementById("pill-watchers").textContent = "閲覧中 " + (st.watchers || 0);
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
  document.getElementById("stat-pending").classList.toggle("dead", nPending === 0);
  document.getElementById("stat-plan").classList.toggle("dead", !p);
}

// サマリ数字のクリック — 折りたたみ details や display:none 先を開いてからスクロール。
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

// --- 左サイドバー: エージェント俯瞰 ---
// executor は server.py 側で足した st.busy (_LOCK.locked()) から。 judge / auto-step は
// ledger から導出した既存フィールド (verdict / plan) の言い換え — 新しい状態は持たない。
function setAgentCard(key, label, state) {
  document.getElementById("status-" + key).textContent = label;
  document.getElementById("dot-" + key).className = "agent-dot " + state;
}

// 「直近」の定義はヒューリスティックなので定数化して根拠をここに置く —
// 30 件は #chat-timeline の初期表示件数感覚に合わせた目安 (仕様として厳密ではない)。
const RECENT_WINDOW = 30;

function renderAgentCards(st) {
  // executor: 直近 RECENT_WINDOW 件以内に gate/confirm/plan-approval の却下
  // (ev.rejected === true) があれば警告表示に切り替える。
  const recentRejected = timelineEvents.slice(-RECENT_WINDOW).some(e => e.rejected === true);
  setAgentCard("executor",
    (st.busy ? "稼働中 — 評価が進行中" : "待機中") + (recentRejected ? "\\n⚠ 直近で却下あり" : ""),
    recentRejected ? "danger" : (st.busy ? "busy" : "idle"));

  const v = st.verdict;
  if (!v) setAgentCard("judge", "未判定 (verdict なし)", "idle");
  else if (v.done) setAgentCard("judge", "達成と判定", "ok");
  else {
    // judgeNextStreak (NEXT 連発カウンタ) を「未達」表示に反映、 JUDGE_STUCK_STREAK
    // 以上で dot を attn → danger に格上げ (詰まっていることの視覚強調)。
    const streakNote = judgeNextStreak >= 2 ? " (" + judgeNextStreak + " 回連続)" : "";
    setAgentCard("judge", "未達" + streakNote + (v.next ? "\\nNEXT: " + v.next : ""),
      judgeNextStreak >= JUDGE_STUCK_STREAK ? "danger" : "attn");
  }

  const p = st.plan;
  const board = computeGoalBoard(timelineEvents);
  const autostepFailed = board.length > 0 && board[0].status === "失敗";
  if (!p) setAgentCard("autostep", "plan なし", "idle");
  else if (p.status === "approved")
    setAgentCard("autostep", "#" + p.id + " " + p.done + "/" + p.total + " step 完了", autostepFailed ? "danger" : "ok");
  else if (p.status === "rejected") setAgentCard("autostep", "#" + p.id + " 却下", autostepFailed ? "danger" : "attn");
  else setAgentCard("autostep", "#" + p.id + " 承認待ち", autostepFailed ? "danger" : "attn");
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
    head.append(el("span", "ev-ts", fmtTs(r.ts)));
    const btns = el("span", "issue-btns");
    const dg = el("button", "", "委譲 → executor");
    dg.onclick = () => delegateGoal(r.head, dg, r.id);
    btns.append(dg);
    head.append(btns);
    card.append(head, el("div", "issue-text", r.head || ""));
    if (r.reason) card.append(el("div", "issue-why", "理由: " + r.reason));
    if (r.impact) card.append(el("div", "issue-why", "影響: " + r.impact));
    box.append(card);
  }
  if (nRep > 0) box.append(el("div", "empty", "(+" + nRep + " replaced)"));
}

async function delegateGoal(goal, btn, rid) {
  const label = rid ? ("R#" + rid) : "goal";
  if (!confirm(label + " を goal に auto-step を起動する?\\n\\n" + goal)) return;
  if (btn) btn.disabled = true;
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
    if (btn) btn.disabled = false;
  }
}

// --- 右サイドバー: 入力 (comment / delegate) ---
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

async function sendDelegate(e) {
  e.preventDefault();
  const ta = document.getElementById("delegate-text");
  const goal = ta.value.trim();
  if (!goal) return;
  try {
    const r = await fetch("/view/delegate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ goal: goal }),
    }).then(x => x.json());
    if (r.ok) ta.value = "";
    else alert("委譲できない: " + (r.error || "?"));
  } catch (err) {
    alert("委譲の送信に失敗: " + err);
  }
}

// --- 右サイドバー: goal board (delegate の実行状況をコメントログから導出) ---
// [委譲] auto-step 起動: <goal>  →  [委譲 run 終了] <result> / [委譲 run 失敗] <error>
// (server.py の _delegate_run / POST /view/delegate が刻む固定文言、 新しい書き込みは
// 増やさず既存 kind=comment の中身をパターンマッチで読むだけ)。 実行は _LOCK で直列化
// されるので FIFO キューとして対応させれば足りる。
function computeGoalBoard(events) {
  const startRe = /^\\[委譲\\] auto-step 起動: ([\\s\\S]*)$/;
  const endRe = /^\\[委譲 run 終了\\] ([\\s\\S]*)$/;
  const failRe = /^\\[委譲 run 失敗\\] ([\\s\\S]*)$/;
  const comments = events.filter(e => e.tag === "comment")
    .slice().sort((a, b) => (a.ts - b.ts) || (a.id - b.id));
  const board = [];
  const inFlight = [];
  for (const ev of comments) {
    const text = ev.text !== undefined ? ev.text : "";
    let m;
    if ((m = startRe.exec(text))) {
      const item = { goal: m[1], status: "実行中", result: "" };
      board.push(item);
      inFlight.push(item);
    } else if ((m = endRe.exec(text))) {
      const item = inFlight.shift();
      if (item) { item.status = "完了"; item.result = m[1]; }
    } else if ((m = failRe.exec(text))) {
      const item = inFlight.shift();
      if (item) { item.status = "失敗"; item.result = m[1]; }
    }
  }
  return board.slice(-20).reverse();
}

// judge が NEXT: と判定した内容も goal board にタスクとして出す (要件 4)。
// クライアント側導出のみ (ledger 書き込み無し)。
function judgeVerdictComments(events) {
  return events.filter(e => e.tag === "comment" && e.author === "judge")
    .slice().sort((a, b) => (a.ts - b.ts) || (a.id - b.id));
}

// 最後に確定した verdict が NEXT: のときだけ 1 件返す (streak = 末尾から連続する
// NEXT の回数、間に DONE が挟まれば streak はリセット)。 最後が DONE (または judge
// 発言なし) なら null — 「NEXT が複数回出ても最新 1 件しか出ない (重複しない)」
// 「後続の DONE で自動的に消える」の両方をこれで満たす。 過去の NEXT を履歴として
// 積み上げる仕様ではない (設計判断 — 変えたくなったらここを再設計すること)。
function latestJudgeTask(events) {
  let streak = 0;
  let goal = "";
  for (const ev of judgeVerdictComments(events)) {
    const v = classifyVerdictText(ev.text);
    if (v === "next") {
      streak += 1;
      goal = (ev.text || "").trim().replace(/^next:\\s*/i, "").trim();
    } else if (v === "done") {
      streak = 0;
      goal = "";
    }
  }
  return streak > 0 ? { goal, streak } : null;
}

function renderGoalBoard(events) {
  const ul = document.getElementById("goalboard");
  ul.replaceChildren();
  const board = computeGoalBoard(events);
  const judgeTask = latestJudgeTask(events);
  if (!board.length && !judgeTask) { ul.append(el("li", "empty", "delegate 実行なし")); return; }
  if (judgeTask) {
    const li = el("li", "goal-item judge-task status-judge-task");
    const badge = judgeTask.streak >= JUDGE_STUCK_STREAK
      ? "judge 提案 (NEXT × " + judgeTask.streak + " 連続)" : "judge 提案 (NEXT)";
    li.append(el("span", "goal-badge", badge));
    li.append(el("div", "goal-text", judgeTask.goal));
    const btns = el("div", "issue-btns");
    const dg = el("button", "", "委譲 → executor");
    dg.onclick = () => delegateGoal(judgeTask.goal, dg);
    btns.append(dg);
    li.append(btns);
    ul.append(li);
  }
  for (const item of board) {
    const cls = item.status === "実行中" ? "running" : item.status === "完了" ? "done" : "failed";
    const li = el("li", "goal-item status-" + cls);
    li.append(el("span", "goal-badge", item.status));
    li.append(el("div", "goal-text", item.goal));
    if (item.result) li.append(el("div", "goal-result", item.result));
    ul.append(li);
  }
}

// --- K / S パネル、 gate/confirm 判定履歴、 memory、 plan、 pending gate、 agent view ---
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

// --- S (lambda snapshot) の書き替え diff トグル ---
// sdiffCache: meta_events.id -> /view/sdiff レスポンス。 同じ id を二度目に開くときは
// 再 fetch しない (畳んで開き直しても内容は変わらないため)。
const sdiffCache = new Map();
async function toggleSdiff(id, anchorEl) {
  const next = anchorEl.nextElementSibling;
  if (next && next.classList.contains("sdiff-box")) {
    next.remove();  // 二度押しで畳む
    return;
  }
  let data = sdiffCache.get(id);
  if (!data) {
    try {
      const r = await fetch("/view/sdiff?id=" + encodeURIComponent(id));
      data = await r.json();
      if (r.ok) sdiffCache.set(id, data);
    } catch (e) {
      return;
    }
  }
  const box = el("div", "sdiff-box");
  if (!data || data.error) {
    box.append(el("div", "diff-file", "diff 取得エラー: " + ((data && data.error) || "?")));
  } else {
    const label = data.is_first
      ? "新規定義 (全文) — " + data.name
      : "#" + data.prev_id + " → #" + data.id + " の diff — " + data.name;
    box.append(el("div", "diff-file", label));
    if (data.rationale) box.append(el("div", "gate-detail", data.rationale));
    box.append(renderDiffLines(data.lines || [], ""));
  }
  anchorEl.insertAdjacentElement("afterend", box);
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
                el("span", "ev-badge", g.kind + " "),
                el("span", "", g.title));
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

// --- パネル一括再描画 ---
function renderPanels(st) {
  ACTORS = st.actors || {}; // executor/judge のモデル名 (env 由来、actorLabel() が参照)
  renderGoal(st);
  renderSummary(st);
  renderHealth(st);
  renderAgentCards(st);
  document.getElementById("sess").textContent =
    st.session_id === null ? "(all)" : (st.session_id || "(none)");

  renderIssues(st);

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
    const link = el("span", "diff-link", "diff を見る");
    link.onclick = () => toggleSdiff(s.id, link);
    li.append(link);
    S.append(li);
  }

  const G = document.getElementById("gates");
  G.replaceChildren();
  if (!(st.gates || []).length) G.append(el("li", "empty", "gate 判定なし"));
  for (const g of (st.gates || [])) G.append(renderEventLi(g));

  renderPending(st.pending || []);
  renderPlan(st.plan || null);
  renderAgentView(st.view || null);
  renderMemory(st.memory || null);
  renderGoalBoard(timelineEvents);
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
    appendChatEvent(ev);
    // 要約カウンタの即時更新 (真値は refreshPanels の再取得で揃う)
    if (ev.src === "turn" && ev.tag === "assistant") bumpSummary("st-steps");
    if (ev.src === "turn" && ev.tag === "tool") bumpSummary("st-tools");
    if (["R", "K", "S", "gate", "skill", "confirm", "intent", "comment",
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
  seen.clear();
  timelineEvents = [];
  judgeNextStreak = 0;
  const tl = document.getElementById("chat-timeline");
  tl.replaceChildren();
  pinned = true;
  for (const ev of mergeInitial(st)) appendChatEvent(ev);
  renderPanels(st);
  connect(st.cursors || { meta: 0, turn: 0 });
}

// ---- ページモーダル: /sessions /spec を iframe で重ねて表示 ----
// 開くたびに src をセットし、閉じたら about:blank に戻して iframe 内のリソースを止める。
// spec は現在の scope を ?session= で引き継ぐ (/spec ハンドラのパラメータ形式に合わせる)。
const pageModal = document.getElementById("page-modal");
const pageModalFrame = document.getElementById("page-modal-frame");

function openPageModal(kind) {
  let url, title;
  if (kind === "sessions") {
    url = "/sessions";
    title = "session 一覧";
  } else {
    url = "/spec?session=" + encodeURIComponent(scope);
    title = "spec (scope: " + scope + ")";
  }
  document.getElementById("page-modal-title").textContent = title;
  pageModalFrame.src = url;
  pageModal.classList.add("open");
}

function closePageModal() {
  pageModal.classList.remove("open");
  pageModalFrame.src = "about:blank";
}

for (const a of document.querySelectorAll('.switch a[data-modal]')) {
  // href は残す (新規タブで開く・直接 URL アクセスは従来どおり) — 通常クリックだけモーダルに
  a.addEventListener("click", (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    openPageModal(a.dataset.modal);
  });
}
document.getElementById("page-modal-close").addEventListener("click", closePageModal);
pageModal.addEventListener("click", (e) => { if (e.target === pageModal) closePageModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && pageModal.classList.contains("open")) closePageModal();
});

wireStatClicks();
wireFilters();
document.getElementById("comment-form").addEventListener("submit", sendComment);
document.getElementById("delegate-form").addEventListener("submit", sendDelegate);
init();
</script>
</body></html>
"""
