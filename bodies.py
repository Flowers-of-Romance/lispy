"""bodies — agent layer.

subcommands:
  chat (default)  対話。trajectory を SQLite + 日付別 md に append。要 LLM_API_KEY。
  record-turn     Claude Code hook handler。stdin から hook JSON を読む。
  list            session 一覧。
  dump            DB から日付別 md を再生成（migration / 再構築用）。
  sleep           未要約 session に title + summary を生成（aux LLM）。
  search          FTS5 + trigram で turns / sessions を検索。
  domain          session に domain tag を付ける / 一覧。

stateless model + client state。content generator として stdin/stdout で動く。
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

DB_PATH = Path(os.environ.get("BODIES_DB", str(_ROOT / "bodies.db")))
TURN_DIR = Path(os.environ.get("BODIES_TURN_DIR", str(_ROOT / "data" / "turns")))
DUMP_DIR = Path(os.environ.get("BODIES_DUMP_DIR", str(_ROOT / "data" / "sessions")))
SKILLS_DIR = Path(os.environ.get("BODIES_SKILLS_DIR", str(_ROOT / "skills")))
SKILLS_MANUAL = SKILLS_DIR / "manual"
SKILLS_AUTO = SKILLS_DIR / "auto"
TZ_OFFSET_HOURS = int(os.environ.get("BODIES_TZ_OFFSET", "9"))
MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-opus-4.7")
BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
API_KEY = os.environ.get("LLM_API_KEY")
AUX_MODEL = os.environ.get("BODIES_AUX_MODEL", MODEL)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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
    # sleep / skill_archive / skill_new / automint など、地層に対する
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


def get_client():
    if not API_KEY:
        raise SystemExit("set LLM_API_KEY in env (.env)")
    from openai import OpenAI  # lazy import (record-turn では不要)
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


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
        text = f'---\ntitle: "{date_str}"\ntags: [bodies]\n---\n\n'
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
# skills (Hermes から: task 層 only、user 層を侵さない規律)
# ---------------------------------------------------------------------------

def parse_skill_frontmatter(text: str) -> tuple[dict, str]:
    """SKILL.md の YAML 風 frontmatter を flat に parse して (meta, body) を返す。

    対応するのは flat な key: value のみ。tags: [a,b,c] は文字列のまま入る。
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm = text[4:end]
    body = text[end + 5:]
    meta: dict = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


def list_skills() -> list[dict]:
    """skills/manual と skills/auto 配下の SKILL.md を全部走査。

    Returns [{name, description, source, path, tags}, ...]
    """
    results = []
    for source, root in [("manual", SKILLS_MANUAL), ("auto", SKILLS_AUTO)]:
        if not root.exists():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                meta, _ = parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
            except OSError:
                continue
            results.append({
                "name": meta.get("name") or skill_md.parent.name,
                "description": meta.get("description", ""),
                "source": source,
                "path": str(skill_md),
                "tags": meta.get("tags", ""),
            })
    return results


def read_skill(name: str) -> str | None:
    """名前から SKILL.md 本体を返す。manual を auto より優先。"""
    for root in [SKILLS_MANUAL, SKILLS_AUTO]:
        path = root / name / "SKILL.md"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                pass
    return None


def skill_listing_for_prompt() -> str:
    """chat の system prompt に積む 1 行 listing を生成。"""
    skills = list_skills()
    if not skills:
        return ""
    lines = ["", "## Available skills (read_file で本体を引ける):"]
    for s in skills:
        loc = "manual" if s["source"] == "manual" else "auto"
        lines.append(f"- **{s['name']}** ({loc}): {s['description']}  →  {s['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# tools (chat 用、最小核)
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
            "description": "List the contents of a directory.",
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
            "description": "Read the contents of a text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max lines to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
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
            "description": "Search a regex pattern in files under a path.",
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
            "name": "shell",
            "description": "Run a shell command. Side effects allowed; user confirms before execution.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
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
    limit = args.get("limit")
    if limit:
        lines = text.splitlines()[: int(limit)]
        text = "\n".join(lines)
    return text[:16000]


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


def _is_shell_safe(cmd: str) -> bool:
    """allow-list 該当なら True (確認不要)。"""
    tokens = cmd.strip().split()
    if not tokens:
        return False
    first = tokens[0]
    if first in SHELL_SAFE_FIRST_TOKEN:
        return True
    if first == "git" and len(tokens) >= 2 and tokens[1] in GIT_SAFE_SUBCOMMANDS:
        # ただし git config --set / --add / --unset は除外
        if tokens[1] == "config" and any(t.startswith(("--set", "--add", "--unset", "--replace", "--remove")) for t in tokens[2:]):
            return False
        return True
    return False


def _tool_shell(args: dict) -> str:
    cmd = args["command"]
    safe = _is_shell_safe(cmd)
    print(f"\n  [tool] shell{' (safe)' if safe else ''}: {cmd}", file=sys.stderr)
    if not safe:
        try:
            confirm = input("  実行する？ [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "user rejected (no input)"
        if confirm != "y":
            return "user rejected execution"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "timeout (60s)"
    out = proc.stdout or ""
    if proc.stderr:
        out += "\n--- stderr ---\n" + proc.stderr
    if proc.returncode != 0:
        out += f"\n(exit {proc.returncode})"
    return out[:8000]


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


TOOL_DISPATCH = {
    "current_time": _tool_current_time,
    "list_dir": _tool_list_dir,
    "read_file": _tool_read_file,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "shell": _tool_shell,
    "recall": _tool_recall,
    "recall_session": _tool_recall_session,
}


CHAT_SYSTEM_PROMPT = """bodies chat mode.

- 簡単な質問には text のみで答える。tool は必要な時だけ使う。
- 応答は短く、要点だけ。長い列挙より一行のまとめ。
- read-only な情報取得は list_dir / read_file / glob / grep / current_time を優先。
- shell は副作用ある操作のみ。読み取り系 (ls / cat / sqlite3 select 等) は allow-list で自動承認、それ以外は user 確認。
- 「今いつ？」のような時刻質問は current_time を使う (DB の session 時刻と混同しない)。
- **過去 trajectory が役立ちそうなとき** (「前にやった X」「いつもの手順」「以前のあの session」等) は recall を呼ぶ。
  関連 turn が見つかったら recall_session で前後文脈まで取り、現在のタスクを過去パターンに照らして合成する。
  事前に skill として固めるより、毎回 retrieve して synthesize する方が bodies の規律に合う。"""


# ---------------------------------------------------------------------------
# chat (standalone agent loop + tool calling)
# ---------------------------------------------------------------------------

def cmd_chat(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    client = get_client()
    sid = open_session(db)
    system_prompt = CHAT_SYSTEM_PROMPT + skill_listing_for_prompt()
    history: list[dict] = [{"role": "system", "content": system_prompt}]
    cwd = os.getcwd()

    try:
        while True:
            try:
                user = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user:
                continue
            history.append({"role": "user", "content": user})
            append_turn(db, sid, "user", user, cwd=cwd, model=MODEL)
            append_to_date_md(sid, "user", user, cwd=cwd)

            # tool loop: tool_calls が無くなるまで往復
            for _ in range(20):  # 安全のため最大 20 ループ
                resp = client.chat.completions.create(
                    model=MODEL,
                    messages=history,
                    tools=TOOL_SCHEMA,
                    max_tokens=4096,
                    extra_body={"think": False},  # chat は CoT 不要 (sleep は default=thinking high)
                )
                msg = resp.choices[0].message
                tool_calls = msg.tool_calls or []
                content = msg.content or ""

                if not tool_calls:
                    history.append({"role": "assistant", "content": content})
                    append_turn(db, sid, "assistant", content, cwd=cwd, model=MODEL)
                    append_to_date_md(sid, "assistant", content, cwd=cwd)
                    print(content)
                    break

                # tool_calls あり: assistant message を tool_calls 付きで履歴に追加
                history.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                })
                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        targs = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        targs = {}
                    print(f"  [tool] {name}({targs})", file=sys.stderr)
                    handler = TOOL_DISPATCH.get(name)
                    if handler is None:
                        result = f"unknown tool: {name}"
                    else:
                        try:
                            result = handler(targs)
                        except Exception as e:
                            result = f"error: {e}"
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                    # tool 実行も trajectory に残す (role=tool)
                    append_turn(db, sid, "tool", f"{name}({targs}) → {result[:200]}", cwd=cwd, model=MODEL)
            else:
                # 20 周しても落ち着かない場合
                print("[tool loop limit reached]", file=sys.stderr)
    finally:
        close_session(db, sid)


# ---------------------------------------------------------------------------
# record-turn (Claude Code hook handler)
# ---------------------------------------------------------------------------

def extract_assistant_text(message: dict) -> str | None:
    if message.get("type") != "assistant":
        return None
    msg = message.get("message", {})
    parts = msg.get("content", [])
    if isinstance(parts, str):
        return parts
    texts = []
    for p in parts:
        if isinstance(p, str):
            texts.append(p)
        elif isinstance(p, dict) and p.get("type") == "text":
            texts.append(p.get("text", ""))
    return "\n".join(texts) if texts else None


def extract_tool_calls(message: dict) -> list[tuple[str, dict, str]]:
    if message.get("type") != "assistant":
        return []
    parts = message.get("message", {}).get("content", [])
    if not isinstance(parts, list):
        return []
    out = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "tool_use":
            out.append((p.get("name", ""), p.get("input", {}), p.get("tool_use_id", "")))
    return out


def extract_tool_results(message: dict) -> dict[str, str]:
    if message.get("type") != "user":
        return {}
    parts = message.get("message", {}).get("content", [])
    if not isinstance(parts, list):
        return {}
    out = {}
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "tool_result":
            tid = p.get("tool_use_id", "")
            c = p.get("content", "")
            if isinstance(c, list):
                c = "\n".join(
                    x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"
                )
            out[tid] = str(c)
    return out


def format_tool_for_md(name: str, input_dict: dict, result: str) -> str:
    MAX = 500
    truncated = (result or "").strip()[:MAX]
    if result and len(result) > MAX:
        truncated += "\n… (truncated)"
    if name == "Bash":
        cmd = input_dict.get("command", "").replace("\n", " ").strip()
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"- Bash `{cmd}`\n```\n{truncated}\n```\n"
    if name == "Read":
        return f"- Read `{input_dict.get('file_path', '')}`\n"
    if name == "Edit":
        return f"- Edit `{input_dict.get('file_path', '')}`\n"
    if name == "Write":
        return f"- Write `{input_dict.get('file_path', '')}`\n"
    if name in ("Glob", "Grep"):
        return f"- {name} `{input_dict.get('pattern', '')}`\n"
    return f"- {name}\n"


def handle_user_prompt(hook: dict) -> None:
    prompt = hook.get("prompt", "")
    if not prompt.strip():
        return
    sid = hook.get("session_id", "unknown")
    cwd = hook.get("cwd", "")
    db = init_db(DB_PATH)
    ensure_session(db, sid)
    append_turn(db, sid, "user", prompt, cwd=cwd)
    append_to_date_md(sid, "user", prompt, cwd=cwd)


def handle_agent_response(hook: dict) -> None:
    resp = hook.get("prompt_response") or hook.get("response", "")
    if not resp or not resp.strip():
        return
    sid = hook.get("session_id", "unknown")
    cwd = hook.get("cwd", "")
    db = init_db(DB_PATH)
    ensure_session(db, sid)
    append_turn(db, sid, "assistant", resp, cwd=cwd)
    append_to_date_md(sid, "assistant", resp, cwd=cwd)


def handle_stop(hook: dict) -> None:
    transcript_path = hook.get("transcript_path", "")
    if not transcript_path or not Path(transcript_path).exists():
        return
    sid = hook.get("session_id", "unknown")
    cwd = hook.get("cwd", "")

    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").strip().split("\n")
    except Exception:
        return
    messages = []
    for line in lines:
        if not line.strip():
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    db = init_db(DB_PATH)
    ensure_session(db, sid)

    # SQLite: 最後の assistant テキストのみ
    for msg in reversed(messages):
        text = extract_assistant_text(msg)
        if text and text.strip():
            append_turn(db, sid, "assistant", text, cwd=cwd)
            append_to_date_md(sid, "assistant", text, cwd=cwd)
            break

    # md: tool call を callout で追記
    result_map: dict[str, str] = {}
    for msg in messages:
        result_map.update(extract_tool_results(msg))
    parts: list[str] = []
    for msg in messages:
        for name, inp, tid in extract_tool_calls(msg):
            parts.append(format_tool_for_md(name, inp, result_map.get(tid, "")))
    if not parts:
        return

    now = local_now()
    md_path = TURN_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    if md_path.exists():
        lines_out = ["> [!info]- 🔧 Tool calls"]
        for p in parts:
            for line in p.rstrip("\n").split("\n"):
                lines_out.append(f"> {line}")
        locked_append(md_path, "\n" + "\n".join(lines_out) + "\n")


def cmd_record_turn(args: argparse.Namespace) -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        hook = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    event = hook.get("hook_event_name", "")
    try:
        if event in ("UserPromptSubmit", "BeforeAgent", "PromptSubmit"):
            handle_user_prompt(hook)
        elif event == "Stop":
            handle_stop(hook)
        elif event in ("AfterAgent", "AgentComplete"):
            handle_agent_response(hook)
    except Exception as e:
        print(f"bodies record-turn error: {e}", file=sys.stderr)


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
        text = f'---\ntitle: "{date_str}"\ntags: [bodies]\n---\n'
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
# sleep (aux LLM → title + summary)
# ---------------------------------------------------------------------------

SLEEP_PROMPT = """以下の対話の **タイトル（1 行、30 字以内）** と **要約（3-5 行、日本語）** を返せ。
タイトルは内容を表す名詞句。要約は何を話して何に至ったかを書く。

返答フォーマット:
TITLE: <タイトル>
SUMMARY:
<要約 1 行目>
<要約 2 行目>
...

---対話開始---
{transcript}
---対話終了---"""


SKILL_AUTOMINT_PROMPT = """以下の対話を読み、これを **再利用可能な skill (手順書)** として記録する価値があるか判定せよ。

判定基準 (全て満たすと yes):
- 5+ の tool 呼び出しがある複雑タスクである
- 同種のタスクで再利用できる workflow が抽出可能
- 失敗→成功の経験や、自明でないコツがある
- 単なる質疑応答や雑談ではない

bodies 規律: skill は task 層に閉じる。「あなたは誰か」は書かない、「どうやるか」だけ書く。

返答フォーマット (no の場合は DECISION: no の 1 行のみ):
DECISION: yes
NAME: <kebab-case の skill 名、20 字以内、英数字とハイフンのみ>
DESCRIPTION: <when to use を 1 行で、日本語可>
TAGS: <comma-separated、3 個まで>
BODY:
<SKILL.md 本体、## When / ## How / ## Pitfalls の見出しを含む markdown>

---対話開始---
{transcript}
---対話終了---"""


SLEEP_MIN_TOOL_CALLS_FOR_SKILL = 5


def transcript_of(db: sqlite3.Connection, sid: str, max_chars: int = 60000) -> str:
    turns = db.execute(
        "SELECT role, content FROM turns WHERE session_id = ? ORDER BY ts",
        (sid,),
    ).fetchall()
    buf, total = [], 0
    for role, content in turns:
        chunk = f"[{role}] {content}\n\n"
        if total + len(chunk) > max_chars:
            buf.append("...（後略）")
            break
        buf.append(chunk)
        total += len(chunk)
    return "".join(buf)


def parse_sleep_response(text: str) -> tuple[str, str]:
    title = ""
    summary: list[str] = []
    mode = None
    for line in text.splitlines():
        if line.startswith("TITLE:"):
            title = line[6:].strip()
            mode = "title"
        elif line.startswith("SUMMARY:"):
            mode = "summary"
        elif mode == "summary":
            summary.append(line)
    return title, "\n".join(summary).strip()


def parse_skill_automint_response(text: str) -> dict | None:
    """SKILL_AUTOMINT_PROMPT の返答を parse。yes の場合のみ dict を返す。"""
    decision = ""
    meta = {"name": "", "description": "", "tags": ""}
    body_lines: list[str] = []
    mode = None
    for line in text.splitlines():
        if line.startswith("DECISION:"):
            decision = line.split(":", 1)[1].strip().lower()
        elif line.startswith("NAME:"):
            meta["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("DESCRIPTION:"):
            meta["description"] = line.split(":", 1)[1].strip()
        elif line.startswith("TAGS:"):
            meta["tags"] = line.split(":", 1)[1].strip()
        elif line.startswith("BODY:"):
            mode = "body"
        elif mode == "body":
            body_lines.append(line)
    if decision != "yes" or not meta["name"]:
        return None
    name = re.sub(r"[^a-z0-9\-]", "", meta["name"].lower())[:30]
    if not name:
        return None
    return {
        "name": name,
        "description": meta["description"],
        "tags": meta["tags"],
        "body": "\n".join(body_lines).strip(),
    }


def maybe_autocreate_skill(db: sqlite3.Connection, sid: str, client) -> Path | None:
    """session の trajectory を見て skill 化候補なら skills/auto/ に書き出す。"""
    n_tool = db.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id = ? AND role = 'tool'",
        (sid,),
    ).fetchone()[0]
    if n_tool < SLEEP_MIN_TOOL_CALLS_FOR_SKILL:
        return None
    transcript = transcript_of(db, sid)
    if not transcript.strip():
        return None

    try:
        resp = client.chat.completions.create(
            model=AUX_MODEL,
            messages=[{"role": "user", "content": SKILL_AUTOMINT_PROMPT.format(transcript=transcript)}],
            max_tokens=2048,
        )
    except Exception as e:
        print(f"  skill mint error: {e}", file=sys.stderr)
        return None
    text = resp.choices[0].message.content or ""
    parsed = parse_skill_automint_response(text)
    if not parsed:
        return None

    target = SKILLS_AUTO / parsed["name"] / "SKILL.md"
    if target.exists():
        return None  # 衝突したら skip (Curator が後で見る)
    target.parent.mkdir(parents=True, exist_ok=True)

    # frontmatter を整える (LLM 出力に frontmatter が無い場合は付ける)
    body = parsed["body"]
    if not body.startswith("---\n"):
        frontmatter = (
            "---\n"
            f"name: {parsed['name']}\n"
            f"description: {parsed['description']}\n"
            f"created: {local_now().isoformat(timespec='seconds')}\n"
            f"source: auto\n"
            f"source_session: {sid}\n"
            f"tags: {parsed['tags']}\n"
            "---\n\n"
        )
        body = frontmatter + body
    target.write_text(body, encoding="utf-8")
    return target


def cmd_sleep(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    client = get_client()
    rows = db.execute(
        "SELECT id FROM sessions WHERE title IS NULL OR summary IS NULL ORDER BY started_at"
    ).fetchall()
    if not rows:
        print("no sessions to summarize")
        return
    for (sid,) in rows:
        transcript = transcript_of(db, sid)
        if not transcript.strip():
            continue
        print(f"sleeping over {sid} ...", file=sys.stderr)
        resp = client.chat.completions.create(
            model=AUX_MODEL,
            messages=[{"role": "user", "content": SLEEP_PROMPT.format(transcript=transcript)}],
            max_tokens=1024,
        )
        text = resp.choices[0].message.content or ""
        title, summary = parse_sleep_response(text)
        db.execute(
            "UPDATE sessions SET title = ?, summary = ?, derived_at = ? WHERE id = ?",
            (title or None, summary or None, time.time(), sid),
        )
        db.commit()
        log_meta(db, "sleep", sid=sid, payload=title or "(no title)")
        print(f"  → {title}", file=sys.stderr)

        # skill mint (tool call が多い session のみ)
        if not args.no_skill:
            skill_path = maybe_autocreate_skill(db, sid, client)
            if skill_path:
                log_meta(db, "automint", sid=sid, payload=str(skill_path))
                print(f"  + auto skill: {skill_path}", file=sys.stderr)


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
# skill (CLI)
# ---------------------------------------------------------------------------

def cmd_skill(args: argparse.Namespace) -> None:
    sub = args.skill_cmd or "list"

    if sub == "list":
        skills = list_skills()
        if not skills:
            print("(no skills)")
            print(f"  add one: mkdir -p {SKILLS_MANUAL}/<name> && $EDITOR {SKILLS_MANUAL}/<name>/SKILL.md")
            return
        for s in skills:
            tag = f"[{s['tags']}]" if s["tags"] else ""
            print(f"{s['source']:6}  {s['name']:24}  {tag:20} {s['description']}")
        return

    if sub == "show":
        body = read_skill(args.name)
        if body is None:
            print(f"skill not found: {args.name}", file=sys.stderr)
            raise SystemExit(1)
        print(body)
        return

    if sub == "new":
        target = SKILLS_MANUAL / args.name / "SKILL.md"
        if target.exists():
            print(f"already exists: {target}", file=sys.stderr)
            raise SystemExit(1)
        target.parent.mkdir(parents=True, exist_ok=True)
        # `--from-stdin` 明示時のみ stdin から読み込む。そうでなければテンプレ生成。
        if args.from_stdin:
            body = sys.stdin.read()
        else:
            body = f"""---
name: {args.name}
description: {args.description or '(describe when to use this skill)'}
created: {local_now().isoformat(timespec='seconds')}
source: manual
tags: {args.tags or ''}
---

# {args.name}

## When

(when to use this skill)

## How

1. step 1
2. step 2

## Pitfalls

- (common mistake to avoid)
"""
        target.write_text(body, encoding="utf-8")
        log_meta(init_db(DB_PATH), "skill_new", payload=f"{args.name} → {target}")
        print(f"created {target}")
        return

    if sub == "archive":
        # auto skill を archive サブディレクトリに移す (manual は対象外、user が手で消すべき)
        src = SKILLS_AUTO / args.name / "SKILL.md"
        if not src.exists():
            print(f"auto skill not found: {args.name}", file=sys.stderr)
            raise SystemExit(1)
        archive_dir = SKILLS_AUTO / ".archive" / args.name
        archive_dir.mkdir(parents=True, exist_ok=True)
        src.rename(archive_dir / "SKILL.md")
        try:
            src.parent.rmdir()  # 空の親ディレクトリを掃除
        except OSError:
            pass
        log_meta(init_db(DB_PATH), "skill_archive", payload=args.name)
        print(f"archived: {args.name}")
        return


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
    p = argparse.ArgumentParser(prog="bodies", description="agent layer")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="interactive chat (default)")
    sub.add_parser("record-turn", help="hook handler (stdin: hook JSON)")
    p_list = sub.add_parser("list", help="list sessions")
    p_list.add_argument("--limit", type=int, default=30)
    sub.add_parser("dump", help="rebuild date-based md from DB")
    p_sleep = sub.add_parser("sleep", help="generate title/summary + auto skill (要 LLM_API_KEY)")
    p_sleep.add_argument("--no-skill", action="store_true", help="skip auto skill mint")
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
    p_events = sub.add_parser("events", help="meta-event ledger (sleep / skill 操作 等) を見る")
    p_events.add_argument("--kind", help="種別で絞る (sleep / skill_new / skill_archive / automint)")
    p_events.add_argument("--session", help="session_id prefix で絞る")
    p_events.add_argument("--since", help="期間絞り込み (7d / 24h / 30m / 60s)")
    p_events.add_argument("--limit", type=int, default=20)
    p_events.add_argument("--full", action="store_true", help="payload を全文表示")
    p_dom = sub.add_parser("domain", help="show / set domain for sessions")
    p_dom.add_argument("session", nargs="?")
    p_dom.add_argument("domain", nargs="?")
    p_dom.add_argument("--clear", action="store_true")

    p_skill = sub.add_parser("skill", help="manage skills (manual / auto)")
    p_skill_sub = p_skill.add_subparsers(dest="skill_cmd")
    p_skill_sub.add_parser("list", help="list all skills")
    p_skill_show = p_skill_sub.add_parser("show", help="display a skill body")
    p_skill_show.add_argument("name")
    p_skill_new = p_skill_sub.add_parser("new", help="create a new manual skill")
    p_skill_new.add_argument("name")
    p_skill_new.add_argument("--description", default="")
    p_skill_new.add_argument("--tags", default="")
    p_skill_new.add_argument("--from-stdin", action="store_true", help="read body from stdin instead of template")
    p_skill_arc = p_skill_sub.add_parser("archive", help="archive an auto skill")
    p_skill_arc.add_argument("name")
    return p


def main() -> None:
    args = build_parser().parse_args()
    cmd = args.cmd or "chat"
    handler = {
        "chat": cmd_chat,
        "record-turn": cmd_record_turn,
        "list": cmd_list,
        "dump": cmd_dump,
        "sleep": cmd_sleep,
        "search": cmd_search,
        "cross": cmd_cross,
        "events": cmd_events,
        "domain": cmd_domain,
        "skill": cmd_skill,
    }[cmd]
    handler(args)


if __name__ == "__main__":
    main()
