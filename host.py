"""host — lispy の host environment。

lispy (live-redefinable agent evaluator) が乗っかってる支援層:
  - SQLite DB (sessions / turns / FTS5 / trigram / meta_events / tasks)
  - tool 関数群 (current_time / read_file / glob / grep / recall / web_fetch ...)
  - LLM client setup (get_client, MODEL)
  - CLI 操作 (list / search / dump / cross / events / domain)

subcommands:
  list            session 一覧。
  dump            DB から日付別 md を再生成（migration / 再構築用）。
  search          FTS5 + trigram で turns / sessions を検索。
  cross           session 横断で構造ラベル付きに並べる。
  events          meta-event ledger を見る。
  domain          session に domain tag を付ける / 一覧。

TOOL_DISPATCH / TOOL_SCHEMA / get_client / MODEL は lispy.py から再利用される。
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

# script の親ディレクトリを base にする (hook 経由で cwd が異なっても安定)
_ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """最小限の .env loader (依存追加せず自前パース)。既存 env を上書きしない。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(_ROOT / ".env")

DB_PATH = Path(os.environ.get("LISPY_DB", str(_ROOT / "host.db")))
TURN_DIR = Path(os.environ.get("LISPY_TURN_DIR", str(_ROOT / "data" / "turns")))
DUMP_DIR = Path(os.environ.get("LISPY_DUMP_DIR", str(_ROOT / "data" / "sessions")))
TZ_OFFSET_HOURS = int(os.environ.get("LISPY_TZ_OFFSET", "9"))
## LLM 設定は .env に書く (.env.example を copy)。
## host.py は default 値を持たない: provider 選択は code でなく config の責務。
MODEL    = os.environ.get("LLM_MODEL", "")
BASE_URL = os.environ.get("LLM_BASE_URL", "")
API_KEY  = os.environ.get("LLM_API_KEY", "")
CTX_WINDOW = int(os.environ.get("LISPY_CTX_WINDOW", "200000"))
## agent 呼び出しの default max_tokens (llm-call / apply_ / prompt が使う)。
## 2048 は agentic な出力には足りないことが多い。 provider に合わせて .env で調整。
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
## thinking mode の default。 ds4 / DeepSeek 系は extra_body の "think" を解釈する。
## 他 provider は未知 field として無視するのが普通 (今までも "think": False を常に送っていた)。
THINK = os.environ.get("LLM_THINK", "").strip().lower() in ("1", "true", "yes", "on")
## 審査者 (judge) LLM — define-gate の install 審査と auto.lispy の judge-done が使う。
## executor (LLM_*) と別のモデルを据えるための設定。 3 つとも未設定なら executor に fallback
## (= 文脈は分かれるが重みは同じ、 という弱い独立性で動く)。
JUDGE_MODEL    = os.environ.get("JUDGE_MODEL", "")
JUDGE_BASE_URL = os.environ.get("JUDGE_BASE_URL", "")
JUDGE_API_KEY  = os.environ.get("JUDGE_API_KEY", "")
JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "2048"))
## round 毎の証拠確認 (judge-done) 専用の model。 判定基準が prompt 側でチェックリスト化
## されているぶん要求能力が低く、 誤判定も NEXT 側 (= 1 round 余計) に倒れるので、
## gate / plan 審査より安いモデルでよい。 未設定なら JUDGE_MODEL に fallback。
JUDGE_MODEL_DONE = os.environ.get("JUDGE_MODEL_DONE", "")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def init_db(path: Path) -> sqlite3.Connection:
    # check_same_thread=False: Textual worker thread から append_turn する。
    # 同時書きは Textual 側で 1 turn ずつシリアル化されるので race は起きない。
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            ended_at REAL,
            title TEXT,
            summary TEXT,
            domain TEXT,
            derived_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL,
            cwd TEXT,
            model TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, ts)")

    # meta_events: trajectory (turns) と層を分けた meta 操作の ledger。
    # sleep など、地層に対する
    # 解釈 / 操作を記録する。turns_fts には載せない (地層と札を混ぜない)。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            session_id TEXT,
            payload TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_events_kind ON meta_events(kind, ts)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id, status)")

    # FTS5 (turns)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(content)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts_tri USING fts5(content, tokenize='trigram')")
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS turns_fts_insert AFTER INSERT ON turns BEGIN
          INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
          INSERT INTO turns_fts_tri(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS turns_fts_delete AFTER DELETE ON turns BEGIN
          INSERT INTO turns_fts(turns_fts, rowid, content) VALUES ('delete', old.id, old.content);
          INSERT INTO turns_fts_tri(turns_fts_tri, rowid, content) VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS turns_fts_update AFTER UPDATE ON turns BEGIN
          INSERT INTO turns_fts(turns_fts, rowid, content) VALUES ('delete', old.id, old.content);
          INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
          INSERT INTO turns_fts_tri(turns_fts_tri, rowid, content) VALUES ('delete', old.id, old.content);
          INSERT INTO turns_fts_tri(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )

    # FTS5 (sessions)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(title, summary)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts_tri USING fts5(title, summary, tokenize='trigram')")
    # UPDATE trigger は plain DELETE WHERE rowid= を使う (FTS5 専用 'delete' は entry 無いと SQL logic error)
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS sessions_fts_insert AFTER INSERT ON sessions BEGIN
          INSERT INTO sessions_fts(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
          INSERT INTO sessions_fts_tri(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS sessions_fts_update AFTER UPDATE ON sessions BEGIN
          DELETE FROM sessions_fts WHERE rowid = old.rowid;
          INSERT INTO sessions_fts(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
          DELETE FROM sessions_fts_tri WHERE rowid = old.rowid;
          INSERT INTO sessions_fts_tri(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
        END;
        """
    )

    conn.commit()
    return conn


def ensure_session(db: sqlite3.Connection, sid: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, started_at) VALUES (?, ?)",
        (sid, time.time()),
    )
    db.commit()


def open_session(db: sqlite3.Connection) -> str:
    sid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    db.execute("INSERT INTO sessions (id, started_at) VALUES (?, ?)", (sid, time.time()))
    db.commit()
    return sid


def close_session(db: sqlite3.Connection, sid: str) -> None:
    db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), sid))
    db.commit()


def append_turn(
    db: sqlite3.Connection, sid: str, role: str, content: str,
    cwd: str = "", model: str = "",
) -> None:
    db.execute(
        "INSERT INTO turns (session_id, role, content, ts, cwd, model) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, role, content, time.time(), cwd or None, model or None),
    )
    db.commit()


def log_meta(db: sqlite3.Connection, kind: str, sid: str | None = None, payload: str = "") -> None:
    """meta-event を ledger に append。turns とは別テーブル、FTS にも載らない。"""
    db.execute(
        "INSERT INTO meta_events (ts, kind, session_id, payload) VALUES (?, ?, ?, ?)",
        (time.time(), kind, sid or None, payload or None),
    )
    db.commit()


# --- plan ledger の投影 (kind=plan / plan-approval / plan-progress) ---
# lispy.py の plan primitives と view.py の計画パネルが同じ導出規則を共有する
# ための helper — 二重実装で規則が乖離しないよう、 投影はここに一本化する。
# 書き込みは log_meta 経由 (append-only)、 ここは読むだけ。

def plan_latest(db: sqlite3.Connection) -> dict | None:
    """最新の kind=plan 行。 payload dict に "id" を足して返す。 無ければ None。"""
    row = db.execute(
        "SELECT id, payload FROM meta_events WHERE kind = 'plan' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        p = json.loads(row[1] or "")
    except Exception:
        return None
    p["id"] = row[0]
    return p


def plan_approval(db: sqlite3.Connection, plan_id: int) -> dict | None:
    """plan_id への最新の承認判定 (kind=plan-approval の payload)。 無ければ None。"""
    for (payload,) in db.execute(
        "SELECT payload FROM meta_events WHERE kind = 'plan-approval' "
        "ORDER BY id DESC LIMIT 30").fetchall():
        try:
            a = json.loads(payload or "")
        except Exception:
            continue
        if a.get("plan_id") == plan_id:
            return a
    return None


def plan_done_steps(db: sqlite3.Connection, plan_id: int) -> set[int]:
    """plan_id の完了ステップ番号 (kind=plan-progress、 1-based) の集合。
    plan_id は SQL 側 (json_extract) で絞る — 直近 N 件の窓だと、 反復記録の多い
    長い run で古いステップの完了が窓から押し出されて取りこぼすため。"""
    done: set[int] = set()
    for (payload,) in db.execute(
        "SELECT payload FROM meta_events WHERE kind = 'plan-progress' "
        "AND json_extract(payload, '$.plan_id') = ?",
        (plan_id,),
    ).fetchall():
        try:
            g = json.loads(payload or "")
        except Exception:
            continue
        if isinstance(g.get("step"), int):
            done.add(g["step"])
    return done


def get_client():
    missing = [k for k, v in (
        ("LLM_API_KEY", API_KEY),
        ("LLM_BASE_URL", BASE_URL),
        ("LLM_MODEL", MODEL),
    ) if not v]
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} not set. "
            f"copy .env.example to .env and fill in the values."
        )
    from openai import OpenAI  # lazy import
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def judge_configured() -> bool:
    """JUDGE_* 3 変数が揃っているか。 揃っていなければ judge は executor に fallback する。"""
    return bool(JUDGE_MODEL and JUDGE_BASE_URL and JUDGE_API_KEY)


def get_judge_client():
    """審査者 LLM の client。 JUDGE_* が未設定なら executor の client を返す (fallback)。

    fallback は「別モデルによる独立審査」 ではなく 「同じ重みの別文脈審査」 に弱まる。
    self-modifying 運用では JUDGE_* を設定すること (.env.example 参照)。
    """
    if not judge_configured():
        return get_client()
    from openai import OpenAI  # lazy import
    return OpenAI(api_key=JUDGE_API_KEY, base_url=JUDGE_BASE_URL)


def judge_model() -> str:
    """審査に使う model 名。 JUDGE_MODEL が無ければ executor の MODEL。"""
    return JUDGE_MODEL if judge_configured() else MODEL


def judge_model_done() -> str:
    """round 毎の証拠確認 (judge-call → judge-done) に使う model 名。

    JUDGE_MODEL_DONE 未設定なら judge_model() に fallback。 gate / plan 審査
    (騙された場合の被害が 1 round で済まない判定) は judge_model() のまま。
    """
    return JUDGE_MODEL_DONE if (JUDGE_MODEL_DONE and judge_configured()) else judge_model()


# ---------------------------------------------------------------------------
# label — session に title / keyphrases / tags を貼る (旧 sleep の軽量版)
# ---------------------------------------------------------------------------

_LABEL_SYSTEM = (
    "あなたは会話の索引付け担当。 要約はしない (内容を文に縮めない)。 "
    "代わりに、 raw を後から navigable にするための **索引語** を抽出する。 "
    "出力は厳密に 1 行 JSON のみ、 説明文や前後の text は付けない。"
)

_LABEL_PROMPT_TMPL = """以下の会話に title / keyphrases / tags を付けて JSON で返す。

- title: 1 行 (40 文字以内)、 「あの話」 を呼び戻すための見出し
- keyphrases: 索引語の列 3-7 個。 文や説明文ではなく、 単語または短い句。 検索で使える形
- tags: 分類カテゴリ 1-3 個。 short identifier ("lispy", "design", "philosophy" 等)

要約 (内容を文章に縮める) は **絶対にしない**。 keyphrase は raw を指す handle。

出力 JSON 形式 (これ以外の text を出力しない):
{"title": "...", "keyphrases": ["...", "..."], "tags": ["...", "..."]}

会話:
"""


def _format_turns_to_text(turns: list, max_per_turn: int = 400) -> str:
    """turns (Turn-like オブジェクト or (role, content) タプル) → 1 つの text。

    Turn-like: .role / .content 属性を持つ
    タプル: (role, content) の 2 要素
    どちらでも受け入れて統一的に format。
    """
    lines = []
    total = 0
    for t in turns:
        if hasattr(t, "role"):
            role, content = t.role, t.content
        else:
            role, content = t[0], t[1]
        c = (content or "").replace("\n", " ")
        if len(c) > max_per_turn:
            c = c[:max_per_turn] + "…"
        line = f"[{role}] {c}"
        if total + len(line) > 8000:
            lines.append("[…truncated…]")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def propose_label(turns: list) -> dict | None:
    """LLM に turns から title / keyphrases / tags を提案させる。

    turns: Turn-like オブジェクト or (role, content) タプルの list。
    成功すれば dict、 失敗 (LLM が JSON 返さない、 空 turns、 etc.) は None。
    DB へのアクセス無し。
    """
    convo = _format_turns_to_text(turns)
    if not convo.strip():
        return None
    client = get_client()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _LABEL_SYSTEM},
            {"role": "user", "content": _LABEL_PROMPT_TMPL + convo},
        ],
        max_tokens=512,
        extra_body={"think": False},
    )
    raw = (resp.choices[0].message.content or "").strip()
    # ```json ... ``` で囲まれている場合に備えて剥がす
    if raw.startswith("```"):
        # 最初の ``` 後の改行までを skip、 末尾 ``` を剥がす
        first_nl = raw.find("\n")
        if first_nl != -1:
            raw = raw[first_nl + 1:]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3].rstrip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "title": str(data.get("title", "")).strip()[:200],
        "keyphrases": [str(x) for x in (data.get("keyphrases") or [])][:10],
        "tags": [str(x) for x in (data.get("tags") or [])][:5],
    }


def apply_label(
    db: sqlite3.Connection,
    sid: str,
    title: str,
    keyphrases: list[str],
    tags: list[str],
) -> None:
    """session レコードに label を書き込む。

    旧 schema を流用:
      title    → sessions.title
      keyphrases → sessions.summary (空白区切りで join、 要約じゃなく索引語列)
      tags[0]  → sessions.domain (主タグ、 現 schema は単数なので)
    """
    kw_str = " ".join(keyphrases) if keyphrases else ""
    main_tag = tags[0] if tags else ""
    db.execute(
        "UPDATE sessions SET title = ?, summary = ?, domain = ?, derived_at = ? "
        "WHERE id = ?",
        (title, kw_str, main_tag, time.time(), sid),
    )
    db.commit()


def _read_session_turns(db: sqlite3.Connection, sid: str) -> list:
    """DB から (role, content) タプルの list を時系列順で取得。"""
    return db.execute(
        "SELECT role, content FROM turns WHERE session_id = ? ORDER BY ts",
        (sid,),
    ).fetchall()


def label_session(db: sqlite3.Connection, sid: str, turns: list | None = None) -> dict | None:
    """propose + apply を一発で。

    turns を引数で渡せばそれを使い、 省略時は DB から取り出す。
    lispy の renew は in-memory env.turns を渡す (DB に未反映の assistant も含めるため)。
    CLI から呼ぶ場合は省略 → DB 読み。
    成功時は label dict を返す。
    """
    if turns is None:
        turns = _read_session_turns(db, sid)
    proposed = propose_label(turns)
    if proposed is None:
        return None
    apply_label(db, sid, proposed["title"], proposed["keyphrases"], proposed["tags"])
    return proposed


def resolve_session(db: sqlite3.Connection, prefix: str) -> str:
    """session id の prefix 一致から完全 id を返す。"""
    rows = db.execute(
        "SELECT id FROM sessions WHERE id LIKE ?", (f"{prefix}%",)
    ).fetchall()
    if not rows:
        raise SystemExit(f"no session matching {prefix}")
    if len(rows) > 1:
        raise SystemExit(f"ambiguous: {len(rows)} sessions match {prefix}")
    return rows[0][0]


# ---------------------------------------------------------------------------
# emoji / animal markers (ghost 由来、間抜けが必要)
# ---------------------------------------------------------------------------

_SESSION_ANIMALS = [
    "🐱", "🐶", "🦊", "🐸", "🐙", "🦉", "🐻", "🐺",
    "🦈", "🐧", "🦎", "🐝", "🦋", "🐬", "🦅", "🐢",
]


def pick_animal(sid: str) -> str:
    short = sid[:8] if len(sid) >= 8 else sid
    try:
        n = int(short, 16)
    except ValueError:
        # 決定的 fallback (Python の hash() は process ごとにランダム化されるため使えない)
        n = sum(ord(c) for c in short)
    return _SESSION_ANIMALS[n % len(_SESSION_ANIMALS)]


def pick_user_face(content: str) -> str:
    c = (content or "").lower()
    if any(w in c for w in ["ふざけ", "おかしい", "バグ", "だめ", "ひどい", "最悪", "むかつ", "壊れ", "なんで"]):
        return "😤"
    if any(w in c for w in ["？", "?", "わからん", "わからない", "なぜ", "どうして", "どういう"]):
        return "🤔"
    if any(w in c for w in ["ありがと", "さんきゅ", "助かる", "最高", "いいね", "すごい", "やった", "完璧"]):
        return "😆"
    if any(w in c for w in ["おはよ", "こんにち", "こんばん", "おつかれ", "ただいま", "よろしく"]):
        return "😊"
    if any(w in c for w in ["して", "やって", "頼む", "お願い", "変えて", "直して", "作って", "見せて"]):
        return "😙"
    if any(w in c for w in ["！", "!", "www", "笑", "ｗ", "草"]):
        return "😜"
    if len(c) < 10:
        return "🙂"
    return "😀"


def is_system_noise(content: str) -> bool:
    if not content:
        return False
    return any(m in content for m in [
        "Background command", "toolu_", "completed (exit code",
        "Read the output file to retrieve the result",
    ])


# ---------------------------------------------------------------------------
# md incremental write (date-based, ghost 流)
# ---------------------------------------------------------------------------

def locked_append(filepath: Path, text: str) -> None:
    """file ロック付き append。複数プロセス同時書き込みに対応。"""
    lock_path = Path(str(filepath) + ".lock")
    lock_fd = None
    try:
        for _ in range(10):
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.05)
        else:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(text)
            return
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(text)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                os.remove(str(lock_path))
            except OSError:
                pass


def local_now() -> dt.datetime:
    utc = dt.datetime.now(dt.timezone.utc)
    return (utc + dt.timedelta(hours=TZ_OFFSET_HOURS)).replace(tzinfo=None)


def local_from_ts(ts: float) -> dt.datetime:
    utc = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return (utc + dt.timedelta(hours=TZ_OFFSET_HOURS)).replace(tzinfo=None)


def append_to_date_md(sid: str, role: str, content: str, cwd: str = "") -> None:
    """日付別 md に 1 turn 追記。session 切替時はヘッダ追加。"""
    if role == "user" and is_system_noise(content):
        return
    stripped = re.sub(r"<[^>]+>", "", content or "").strip()
    if not stripped:
        return

    TURN_DIR.mkdir(parents=True, exist_ok=True)
    now = local_now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    md_path = TURN_DIR / f"{date_str}.md"

    animal = pick_animal(sid)
    short_sid = sid[:8]
    project = Path(cwd).name if cwd else ""

    if role == "user":
        icon = f"{pick_user_face(content)} user"
    else:
        icon = "🤖 assistant"

    if not md_path.exists():
        text = f'---\ntitle: "{date_str}"\ntags: [lispy]\n---\n\n'
        header = f"## {animal} {time_str} [{project}] session:{short_sid}\n" if project else f"## {animal} {time_str} session:{short_sid}\n"
        text += header
        if cwd:
            text += f"> cwd: {cwd}\n"
        text += f"\n### {icon} {animal} {time_str}\n\n{content}\n"
        md_path.write_text(text, encoding="utf-8")
        return

    existing = md_path.read_text(encoding="utf-8")
    entry = ""
    if f"session:{short_sid}" not in existing:
        header = f"\n---\n\n## {animal} {time_str} [{project}] session:{short_sid}\n" if project else f"\n---\n\n## {animal} {time_str} session:{short_sid}\n"
        entry += header
        if cwd:
            entry += f"> cwd: {cwd}\n"
    entry += f"\n### {icon} {animal} {time_str}\n\n{content}\n"
    locked_append(md_path, entry)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# status line
# ---------------------------------------------------------------------------

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "Return the current local date and time (ISO 8601).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List the contents of a directory. "
                "対象の構造を把握する最初の一手。 名前のパターンが分かっているなら glob、 "
                "中身の文字列で探すなら grep を使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "ファイルを行番号付きで読む。"
                "offset (1-based 行番号) と limit (読む行数) でページング可。"
                "編集するなら表示行番号を頼りに edit_file の old_string を組み立てる。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "開始行 (1-based、default 1)"},
                    "limit": {"type": "integer", "description": "読む行数 (default 全部、max 2000)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": (
                "Find files matching a glob pattern. "
                "ファイル名・拡張子のパターンで探すときに使う (例: **/*.py)。 "
                "ファイルの中身で探すなら grep。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search a regex pattern in files under a path. "
                "定義箇所・使用箇所・設定値の所在を探すときは、 read_file で1つずつ開くより先にこれ。 "
                "ヒットしたファイルだけ read_file で読む。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "過去 trajectory を検索 (軽量、1 段目)。"
                "結果は session_id + 時刻 + role + title + 1 行 snippet のみ (各 hit 約 100 字)。"
                "深掘りが要る hit は recall_session で session_id を指定して取りに行く。"
                "今のタスクが過去に似た流れがあるか **まず広く探す** ときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "検索語。FTS5 構文を許容。"},
                    "k": {"type": "integer", "description": "返す件数。default 5、max 20。"},
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "fts", "tri"],
                        "description": "auto=fts → 空なら trigram。日本語の部分一致を直接当てたければ tri。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_session",
            "description": (
                "1 session の turn を時系列で取る。recall でヒットした session の前後文脈を見たいとき。"
                "返り値は role / 時刻 / 本文 (各 1000 字まで)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "session id (prefix 一致可)"},
                    "limit": {"type": "integer", "description": "返す turn 数。default 20。"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": (
                "current session の task 一覧 (completed は default で除外)。 "
                "複数手の作業の途中で「次に何が残っているか」 を確認するために呼ぶ。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"include_completed": {"type": "boolean"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_add",
            "description": (
                "current session に task を追加。 pending 状態で作る。 "
                "3 手以上かかる依頼を受けたら、 着手前に手順を分解してここに登録する "
                "(1 手順 = 1 task)。 進行中の抜け漏れ防止。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "task の内容"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_done",
            "description": (
                "task を completed にマーク。 該当作業を検証し終えた直後に呼ぶ "
                "(まとめて後で消さない — どこまで進んだかが常に見えるように)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "task id"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "URL を GET して text content を返す。html は簡易に tag 除去。"
                "redirects 追従、size 上限 2MB、出力上限 16k 字、timeout 指定可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "integer", "description": "default 15s、max 60s"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "DuckDuckGo の html 版で web 検索。title + url + snippet のリストを返す。"
                "詳しく読みたい hit があれば web_fetch で本文を取りに行く 2 段構え。"
                "API key 不要。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "件数。default 10、max 20"},
                },
                "required": ["query"],
            },
        },
    },
]


def _tool_current_time(args: dict) -> str:
    return local_now().isoformat(timespec="seconds")


def _tool_list_dir(args: dict) -> str:
    path = Path(args["path"]).expanduser()
    if not path.exists():
        return f"not found: {path}"
    if not path.is_dir():
        return f"not a directory: {path}"
    entries = []
    for e in sorted(path.iterdir()):
        suffix = "/" if e.is_dir() else ""
        entries.append(f"{e.name}{suffix}")
    return "\n".join(entries[:300]) or "(empty)"


def _tool_read_file(args: dict) -> str:
    path = Path(args["path"]).expanduser()
    if not path.exists():
        return f"not found: {path}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"read error: {e}"
    lines = text.splitlines()
    total = len(lines)
    offset_arg = args.get("offset")
    offset = max(1, int(offset_arg)) if offset_arg else 1
    limit_arg = args.get("limit")
    limit = max(1, min(int(limit_arg), 2000)) if limit_arg else min(2000, total)
    start = offset - 1
    if start >= total:
        return f"# {path} ({total} lines, offset {offset} out of range)"
    end = min(total, start + limit)
    selected = lines[start:end]
    body = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(selected, start=offset))
    if len(body) > 32000:
        body = body[:32000] + "\n…(truncated by char limit)"
    header = f"# {path} ({total} lines, showing {offset}-{end})"
    return f"{header}\n{body}" if body else header


def _tool_glob(args: dict) -> str:
    pattern = args["pattern"]
    base = Path(args.get("cwd") or os.getcwd()).expanduser()
    matches = sorted(str(p) for p in base.glob(pattern))[:300]
    return "\n".join(matches) or "(no matches)"


def _tool_grep(args: dict) -> str:
    pattern = args["pattern"]
    path = Path(args["path"]).expanduser()
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"invalid regex: {e}"
    if not path.exists():
        return f"not found: {path}"
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    results: list[str] = []
    for f in files[:1000]:
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    results.append(f"{f}:{i}: {line}")
                    if len(results) >= 100:
                        return "\n".join(results)
        except OSError:
            continue
    return "\n".join(results) or "(no matches)"


SHELL_SAFE_FIRST_TOKEN = {
    "ls", "cat", "head", "tail", "less", "more", "file", "stat",
    "pwd", "date", "echo", "which", "whoami", "uname", "hostname",
    "find", "tree", "wc", "diff",
    "grep", "rg", "ag", "ack",
    "sqlite3",  # 大半は SELECT、ただし mutation 可能性を承知の上
}
GIT_SAFE_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "remote", "config"}


def _tool_recall(args: dict) -> str:
    """軽量な 2 段構えの第 1 段: session_id + title + 1 行 snippet のみ返す。
    深掘りが要れば LLM が recall_session を呼ぶ。"""
    query = (args.get("query") or "").strip()
    if not query:
        return "(empty query)"
    k = max(1, min(int(args.get("k") or 5), 20))
    mode = args.get("mode") or "auto"

    db = init_db(DB_PATH)

    def _search(table: str, q: str) -> list:
        return db.execute(
            f"""
            SELECT t.session_id, t.role, t.ts,
                   snippet({table}, 0, '«', '»', '…', 12) AS snip,
                   s.title
            FROM {table}
            JOIN turns t ON {table}.rowid = t.id
            LEFT JOIN sessions s ON s.id = t.session_id
            WHERE {table} MATCH ?
            ORDER BY rank LIMIT ?
            """,
            (q, k),
        ).fetchall()

    rows: list = []
    used_mode = mode
    if mode in ("auto", "fts"):
        try:
            rows = _search("turns_fts", query)
            used_mode = "fts"
        except sqlite3.OperationalError:
            rows = []
    if not rows and mode in ("auto", "tri"):
        try:
            rows = _search("turns_fts_tri", f'"{query}"')
            used_mode = "tri"
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        return f"(no matches for: {query})"

    out: list[str] = [
        f"# recall: {len(rows)} hits (mode={used_mode})",
        "深掘りしたい行があれば recall_session で session_id を指定。",
        "",
    ]
    for i, (sid, role, ts, snip, title) in enumerate(rows, 1):
        when = local_from_ts(ts).strftime("%Y-%m-%d %H:%M")
        title_str = f" — {title}" if title else ""
        out.append(f"{i}. {sid[:12]}  {when}  [{role}]{title_str}")
        if snip:
            out.append(f"   …{snip}…")
    return "\n".join(out)


def _tool_recall_session(args: dict) -> str:
    sid_prefix = (args.get("session_id") or "").strip()
    if not sid_prefix:
        return "(empty session_id)"
    limit = max(1, min(int(args.get("limit") or 20), 100))

    db = init_db(DB_PATH)
    row = db.execute(
        "SELECT id, started_at, title, summary FROM sessions WHERE id LIKE ? LIMIT 1",
        (sid_prefix + "%",),
    ).fetchone()
    if not row:
        return f"(no session matching: {sid_prefix})"
    sid, started_at, title, summary = row

    turns = db.execute(
        "SELECT role, ts, content FROM turns WHERE session_id = ? ORDER BY ts LIMIT ?",
        (sid, limit),
    ).fetchall()

    when = local_from_ts(started_at).strftime("%Y-%m-%d %H:%M")
    out = [f"# session {sid} (started {when})"]
    if title:
        out.append(f"title: {title}")
    if summary:
        out.append(f"summary: {summary}")
    out.append("")
    for role, ts, content in turns:
        tt = local_from_ts(ts).strftime("%H:%M:%S")
        body = (content or "")
        excerpt = body[:1000] + ("…(truncated)" if len(body) > 1000 else "")
        out.append(f"[{tt} {role}] {excerpt}\n")
    return "\n".join(out)


def _tool_task_list(args: dict) -> str:
    include_completed = bool(args.get("include_completed", False))
    db = init_db(DB_PATH)
    sid = _TOOL_CTX.get("sid")
    if include_completed:
        rows = db.execute(
            "SELECT id, content, status FROM tasks WHERE session_id = ? ORDER BY id",
            (sid,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, content, status FROM tasks WHERE session_id = ? AND status != 'completed' ORDER BY id",
            (sid,),
        ).fetchall()
    if not rows:
        return "(no tasks)"
    marker = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    return "\n".join(f"{marker.get(s, '[?]')} #{tid} {c}" for tid, c, s in rows)


def _tool_task_add(args: dict) -> str:
    content = (args.get("content") or "").strip()
    if not content:
        return "(empty task content)"
    sid = _TOOL_CTX.get("sid")
    db = init_db(DB_PATH)
    if sid:
        ensure_session(db, sid)
    now = time.time()
    cur = db.execute(
        "INSERT INTO tasks (session_id, content, status, created_at, updated_at) "
        "VALUES (?, ?, 'pending', ?, ?)",
        (sid, content, now, now),
    )
    db.commit()
    return f"task #{cur.lastrowid} added: {content}"


def _tool_task_done(args: dict) -> str:
    try:
        tid = int(args.get("id") or 0)
    except (TypeError, ValueError):
        return f"(invalid task id: {args.get('id')!r})"
    if tid <= 0:
        return "(task id must be > 0)"
    db = init_db(DB_PATH)
    cur = db.execute(
        "UPDATE tasks SET status = 'completed', updated_at = ? WHERE id = ?",
        (time.time(), tid),
    )
    db.commit()
    if cur.rowcount == 0:
        return f"(task #{tid} not found)"
    return f"task #{tid} done"


def _tool_web_fetch(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return "(empty url)"
    if not url.startswith(("http://", "https://")):
        return f"invalid url: {url} (need http:// or https://)"
    timeout = max(1, min(int(args.get("timeout") or 15), 60))

    import httpx
    try:
        resp = httpx.get(
            url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "lispy/0.1"},
        )
    except httpx.HTTPError as e:
        return f"fetch error: {e}"

    ctype = resp.headers.get("content-type", "")
    text = resp.text
    raw_len = len(text)

    if "html" in ctype.lower():
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        import html as _html
        text = _html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

    header = f"[{resp.status_code}] {url} ({ctype})\n"
    if len(text) > 16000:
        text = text[:16000] + f"\n…(truncated, {raw_len} total chars)"
    return header + text


def _tool_web_search(args: dict) -> str:
    q = (args.get("query") or "").strip()
    if not q:
        return "(empty query)"
    n = max(1, min(int(args.get("limit") or 10), 20))

    import httpx
    try:
        resp = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": q},
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) lispy/0.1",
                "Accept": "text/html",
            },
        )
    except httpx.HTTPError as e:
        return f"search error: {e}"

    html_text = resp.text
    item_re = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.+?)</a>',
        re.DOTALL,
    )
    snippet_re = re.compile(
        r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.+?)</a>',
        re.DOTALL,
    )
    titles_urls = item_re.findall(html_text)
    snippets = snippet_re.findall(html_text)

    import html as _html_mod
    from urllib.parse import unquote, parse_qs, urlparse

    out = []
    for i, (url, title_html) in enumerate(titles_urls[:n]):
        if "uddg=" in url:
            try:
                qs = parse_qs(urlparse(url).query)
                if "uddg" in qs:
                    url = unquote(qs["uddg"][0])
            except Exception:
                pass
        if url.startswith("//"):
            url = "https:" + url
        title = _html_mod.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        snippet = ""
        if i < len(snippets):
            snippet = _html_mod.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()
        out.append(f"{i + 1}. {title}\n   {url}\n   {snippet}")

    if not out:
        return f"(no results for: {q})"
    return f"# web_search: {q} ({len(out)} hits)\n\n" + "\n\n".join(out)


# tool 呼び出し時の共有 context (sid / cwd / mode 等)。
# 呼び出し側 (lispy 等) が tool 実行前にセットする。
_TOOL_CTX: dict = {"sid": None, "cwd": "", "in_subagent": False, "mode": "default"}


TOOL_DISPATCH = {
    "current_time": _tool_current_time,
    "list_dir": _tool_list_dir,
    "read_file": _tool_read_file,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "recall": _tool_recall,
    "recall_session": _tool_recall_session,
    "task_list": _tool_task_list,
    "task_add": _tool_task_add,
    "task_done": _tool_task_done,
    "web_fetch": _tool_web_fetch,
    "web_search": _tool_web_search,
}



# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    rows = db.execute(
        """
        SELECT s.id, s.started_at, s.title, s.domain, COUNT(t.id) AS n
        FROM sessions s LEFT JOIN turns t ON t.session_id = s.id
        GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for sid, ts, title, domain, n in rows:
        when = local_from_ts(ts).strftime("%Y-%m-%d %H:%M")
        title_disp = title or "(no title)"
        domain_disp = f"[{domain}]" if domain else ""
        print(f"{when}  {sid[:16]:16}  {n:4} turns  {domain_disp:12} {title_disp}")


# ---------------------------------------------------------------------------
# dump (DB → 日付別 md 再生成)
# ---------------------------------------------------------------------------

def cmd_dump(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    TURN_DIR.mkdir(parents=True, exist_ok=True)
    rows = db.execute(
        "SELECT session_id, role, content, ts, cwd FROM turns ORDER BY ts"
    ).fetchall()
    if not rows:
        print("no turns to dump")
        return

    # 日付別にグループ化
    by_date: dict[str, list] = defaultdict(list)
    for sid, role, content, ts, cwd in rows:
        local = local_from_ts(ts)
        date_str = local.strftime("%Y-%m-%d")
        by_date[date_str].append((sid, role, content, local, cwd or ""))

    for date_str, items in sorted(by_date.items()):
        md_path = TURN_DIR / f"{date_str}.md"
        text = f'---\ntitle: "{date_str}"\ntags: [lispy]\n---\n'
        current_sid = None
        for sid, role, content, when, cwd in items:
            short_sid = sid[:8]
            time_str = when.strftime("%H:%M")
            animal = pick_animal(sid)
            project = Path(cwd).name if cwd else ""
            if short_sid != current_sid:
                current_sid = short_sid
                if text.endswith("---\n"):
                    pass
                text += "\n"
                header = f"## {animal} {time_str} [{project}] session:{short_sid}\n" if project else f"## {animal} {time_str} session:{short_sid}\n"
                text += header
                if cwd:
                    text += f"> cwd: {cwd}\n"
            if role == "user":
                icon = f"{pick_user_face(content)} user"
            else:
                icon = "🤖 assistant"
            text += f"\n### {icon} {animal} {time_str}\n\n{content}\n"
        md_path.write_text(text, encoding="utf-8")
        print(f"wrote {md_path}")




# ---------------------------------------------------------------------------
# search (FTS5 + trigram)
# ---------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    q = args.query
    limit = args.limit
    # trigram は CJK 部分一致用、phrase quote が必要
    turns_table = "turns_fts_tri" if args.tri else "turns_fts"
    sessions_table = "sessions_fts_tri" if args.tri else "sessions_fts"
    match_query = f'"{q}"' if args.tri else q

    show_sessions = args.sessions or not args.turns
    show_turns = args.turns or not args.sessions

    if show_sessions:
        rows = db.execute(
            f"""
            SELECT s.id, s.started_at, s.title, s.summary, s.domain,
                   snippet({sessions_table}, -1, '«', '»', '…', 12) AS snip
            FROM {sessions_table}
            JOIN sessions s ON {sessions_table}.rowid = s.rowid
            WHERE {sessions_table} MATCH ?
            ORDER BY rank LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        if rows:
            print("== sessions ==")
            for sid, ts, title, summary, domain, snip in rows:
                when = local_from_ts(ts).strftime("%Y-%m-%d %H:%M")
                dom = f"[{domain}]" if domain else ""
                print(f"{when}  {sid[:16]:16}  {dom:12} {title or '(no title)'}")
                if snip:
                    print(f"    {snip}")

    if show_turns:
        rows = db.execute(
            f"""
            SELECT t.session_id, t.role, t.ts, t.cwd,
                   snippet({turns_table}, 0, '«', '»', '…', 16) AS snip
            FROM {turns_table}
            JOIN turns t ON {turns_table}.rowid = t.id
            WHERE {turns_table} MATCH ?
            ORDER BY rank LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        if rows:
            print("== turns ==")
            for sid, role, ts, cwd, snip in rows:
                when = local_from_ts(ts).strftime("%Y-%m-%d %H:%M")
                print(f"{when}  {sid[:16]:16}  {role:10} {snip}")


# ---------------------------------------------------------------------------
# cross — scope query → 関連 session を集めて構造ラベル付きで並べる
#         並べるまでが道具。気付き / 意味付けは見る側。
# ---------------------------------------------------------------------------

def _tool_name_from_turn(content: str) -> str | None:
    """tool turn の content から tool 名を抜く。`name(args) → ...` の形式を期待。"""
    if not content:
        return None
    paren = content.find("(")
    if paren <= 0:
        return None
    name = content[:paren].strip()
    if not name or not name.replace("_", "").isalnum():
        return None
    return name


def _session_structure(db: sqlite3.Connection, sid: str) -> dict:
    """session 全 turn から構造ラベルを集計する (interpret しない、count のみ)。"""
    turns = db.execute(
        "SELECT role, ts, content FROM turns WHERE session_id = ? ORDER BY ts",
        (sid,),
    ).fetchall()
    if not turns:
        return {"n_turns": 0}

    roles: dict[str, int] = {}
    tools: dict[str, int] = {}
    for role, _, content in turns:
        roles[role] = roles.get(role, 0) + 1
        if role == "tool":
            tn = _tool_name_from_turn(content or "")
            if tn:
                tools[tn] = tools.get(tn, 0) + 1

    first_ts = turns[0][1]
    last_ts = turns[-1][1]
    duration_sec = int(last_ts - first_ts)

    top_tools = sorted(tools.items(), key=lambda x: -x[1])[:3]

    return {
        "n_turns": len(turns),
        "roles": roles,
        "top_tools": top_tools,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_sec": duration_sec,
        "ends_with": turns[-1][0],
    }


def _fmt_duration(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    h, rem = divmod(sec, 3600)
    return f"{h}h{rem // 60:02d}m"


def cmd_cross(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    q = args.query
    k = max(1, min(args.k, 20))
    n_excerpts = max(0, min(args.turns, 20))

    table = "turns_fts_tri" if args.tri else "turns_fts"
    match_q = f'"{q}"' if args.tri else q

    # ヒット数で session をランク (LLM を介さない素朴な集計)
    try:
        sess_rows = db.execute(
            f"""
            SELECT t.session_id, COUNT(*) AS hits
            FROM {table}
            JOIN turns t ON {table}.rowid = t.id
            WHERE {table} MATCH ?
            GROUP BY t.session_id
            ORDER BY hits DESC LIMIT ?
            """,
            (match_q, k),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"search error: {e}", file=sys.stderr)
        return

    if not sess_rows:
        print(f"(no sessions matching: {q})")
        return

    print(f"# cross: {len(sess_rows)} sessions matching {q!r}  (mode={'tri' if args.tri else 'fts'})")
    print("並べるまでが道具の仕事。差を見るのは見る側。\n")

    for sid, hits in sess_rows:
        meta = db.execute(
            "SELECT started_at, title, summary FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not meta:
            continue
        started_at, title, summary = meta
        when = local_from_ts(started_at).strftime("%Y-%m-%d %H:%M")
        struct = _session_structure(db, sid)

        roles_str = "  ".join(f"{r}={n}" for r, n in sorted(struct["roles"].items()))
        tools_str = ", ".join(f"{n}({c})" for n, c in struct["top_tools"]) or "(none)"
        title_str = f" — {title}" if title else ""

        print("═" * 78)
        print(f"session {sid[:16]}  ({when}){title_str}")
        print(f"  turns={struct['n_turns']}  {roles_str}")
        print(f"  top_tools={tools_str}")
        print(f"  duration={_fmt_duration(struct['duration_sec'])}  ends_with={struct['ends_with']}")
        print(f"  hits_in_query={hits}")
        if summary:
            print(f"  summary: {summary.splitlines()[0][:120]}")

        if n_excerpts > 0:
            excerpts = db.execute(
                f"""
                SELECT t.role, t.ts,
                       snippet({table}, 0, '«', '»', '…', 16) AS snip
                FROM {table}
                JOIN turns t ON {table}.rowid = t.id
                WHERE {table} MATCH ? AND t.session_id = ?
                ORDER BY t.ts LIMIT ?
                """,
                (match_q, sid, n_excerpts),
            ).fetchall()
            if excerpts:
                print(f"  matching turns ({len(excerpts)}):")
                for role, ts, snip in excerpts:
                    tt = local_from_ts(ts).strftime("%H:%M")
                    print(f"    {tt} [{role:9}] …{snip}…")
        print()


# ---------------------------------------------------------------------------
# domain
# ---------------------------------------------------------------------------

def cmd_domain(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    if args.session is None and args.domain is None:
        rows = db.execute(
            "SELECT domain, COUNT(*) FROM sessions WHERE domain IS NOT NULL GROUP BY domain ORDER BY COUNT(*) DESC"
        ).fetchall()
        if not rows:
            print("(no domains assigned)")
            return
        for d, n in rows:
            print(f"{d:20} {n:4} sessions")
        return

    sid = resolve_session(db, args.session)
    if args.clear:
        db.execute("UPDATE sessions SET domain = NULL WHERE id = ?", (sid,))
        db.commit()
        print(f"cleared domain for {sid[:16]}")
        return
    if args.domain:
        db.execute("UPDATE sessions SET domain = ? WHERE id = ?", (args.domain, sid))
        db.commit()
        print(f"set domain '{args.domain}' for {sid[:16]}")
        return
    row = db.execute("SELECT domain FROM sessions WHERE id = ?", (sid,)).fetchone()
    print(row[0] if row and row[0] else "(none)")


# ---------------------------------------------------------------------------
# events — meta-event ledger の閲覧 (turns とは別の層、FTS にも載らない)
# ---------------------------------------------------------------------------

def _parse_since(spec: str) -> float:
    """7d / 24h / 30m / 60s → 「現在から N 秒前」の epoch を返す。"""
    spec = spec.strip()
    if not spec:
        return 0.0
    unit = spec[-1]
    try:
        n = int(spec[:-1])
    except ValueError:
        try:
            return float(spec)
        except ValueError:
            return 0.0
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    return time.time() - n * mult


def cmd_events(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    where: list[str] = []
    params: list = []
    if args.kind:
        where.append("kind = ?")
        params.append(args.kind)
    if args.session:
        where.append("session_id LIKE ?")
        params.append(args.session + "%")
    if args.since:
        since_ts = _parse_since(args.since)
        if since_ts:
            where.append("ts >= ?")
            params.append(since_ts)
    sql = "SELECT ts, kind, session_id, payload FROM meta_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(args.limit)
    rows = db.execute(sql, tuple(params)).fetchall()
    if not rows:
        print("(no events)")
        return
    for ts, kind, sid, payload in rows:
        when = local_from_ts(ts).strftime("%Y-%m-%d %H:%M:%S")
        sid_str = f" [{sid[:12]}]" if sid else ""
        head = (payload or "").splitlines()[0] if payload else ""
        if args.full and payload:
            print(f"{when}  {kind:14}{sid_str}")
            for line in payload.splitlines():
                print(f"    {line}")
            print()
        else:
            print(f"{when}  {kind:14}{sid_str}  {head[:100]}")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="host", description="agent layer")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="list sessions")
    p_list.add_argument("--limit", type=int, default=30)
    sub.add_parser("dump", help="rebuild date-based md from DB")
    p_search = sub.add_parser("search", help="FTS5 search over turns / sessions")
    p_search.add_argument("query")
    p_search.add_argument("--tri", action="store_true", help="trigram tokenizer (CJK 部分一致)")
    p_search.add_argument("--turns", action="store_true", help="only turns")
    p_search.add_argument("--sessions", action="store_true", help="only sessions")
    p_search.add_argument("--limit", type=int, default=10)
    p_cross = sub.add_parser("cross", help="scope query → session 横断で構造ラベル付きで並べる")
    p_cross.add_argument("query")
    p_cross.add_argument("--k", type=int, default=5, help="拾う session 数 (default 5)")
    p_cross.add_argument("--turns", type=int, default=3, help="各 session で表示する matching turn 数 (default 3)")
    p_cross.add_argument("--tri", action="store_true", help="trigram tokenizer (CJK 部分一致)")
    p_events = sub.add_parser("events", help="meta-event ledger を見る")
    p_events.add_argument("--kind", help="種別で絞る (sleep)")
    p_events.add_argument("--session", help="session_id prefix で絞る")
    p_events.add_argument("--since", help="期間絞り込み (7d / 24h / 30m / 60s)")
    p_events.add_argument("--limit", type=int, default=20)
    p_events.add_argument("--full", action="store_true", help="payload を全文表示")
    p_dom = sub.add_parser("domain", help="show / set domain for sessions")
    p_dom.add_argument("session", nargs="?")
    p_dom.add_argument("domain", nargs="?")
    p_dom.add_argument("--clear", action="store_true")
    p_label = sub.add_parser("label", help="LLM が session の title/keyphrases/tags を提案 → DB に書く")
    p_bw = sub.add_parser("brainwash", help="洗脳: 生層 (turns) から蒸留層 (data/memory/) を作り直す (.env の JUDGE_* / LLM_* 必須)")
    p_bw.add_argument("--session", action="append", default=[],
                      help="洗う session (prefix 可、 複数指定可)。 省略時は前回の洗脳以降に turn が増えた session")
    p_label.add_argument("session", nargs="?", help="session_id prefix (省略時は --unlabeled 必須)")
    p_label.add_argument("--unlabeled", action="store_true", help="title が空の全 session を順に label")
    p_label.add_argument("--yes", action="store_true", help="承認 prompt を skip して LLM 提案をそのまま書く")

    return p


def cmd_label(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    if args.unlabeled:
        rows = db.execute(
            "SELECT id FROM sessions WHERE (title IS NULL OR title = '') ORDER BY started_at"
        ).fetchall()
        sids = [r[0] for r in rows]
        if not sids:
            print("(全 session に label 済み)")
            return
        print(f"label 対象: {len(sids)} session")
    elif args.session:
        sid = resolve_session(db, args.session)
        sids = [sid]
    else:
        print("(usage) host label <sid_prefix>  or  host label --unlabeled")
        return

    for sid in sids:
        print(f"\n=== {sid} ===")
        turns = _read_session_turns(db, sid)
        proposed = propose_label(turns)
        if proposed is None:
            print("  (LLM 提案失敗 or session が空)")
            continue
        print(f"  title:      {proposed['title']}")
        print(f"  keyphrases: {' / '.join(proposed['keyphrases'])}")
        print(f"  tags:       {' / '.join(proposed['tags'])}")
        if args.yes:
            apply_label(db, sid, proposed["title"], proposed["keyphrases"], proposed["tags"])
            print("  → 書き込み")
        else:
            ans = input("  承認しますか? [y/N/skip]: ").strip().lower()
            if ans in ("y", "yes"):
                apply_label(db, sid, proposed["title"], proposed["keyphrases"], proposed["tags"])
                print("  → 書き込み")
            else:
                print("  → skip")


def cmd_brainwash(args: argparse.Namespace) -> None:
    import brainwash
    db = init_db(DB_PATH)
    print(brainwash.brainwash(db, sessions=args.session or None))


def main() -> None:
    args = build_parser().parse_args()
    handler = {
        "list": cmd_list,
        "dump": cmd_dump,
        "search": cmd_search,
        "cross": cmd_cross,
        "events": cmd_events,
        "domain": cmd_domain,
        "label": cmd_label,
        "brainwash": cmd_brainwash,
    }[args.cmd]
    handler(args)


if __name__ == "__main__":
    main()
