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
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS sessions_fts_insert AFTER INSERT ON sessions BEGIN
          INSERT INTO sessions_fts(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
          INSERT INTO sessions_fts_tri(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS sessions_fts_update AFTER UPDATE ON sessions BEGIN
          INSERT INTO sessions_fts(sessions_fts, rowid, title, summary)
            VALUES ('delete', old.rowid, COALESCE(old.title, ''), COALESCE(old.summary, ''));
          INSERT INTO sessions_fts(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
          INSERT INTO sessions_fts_tri(sessions_fts_tri, rowid, title, summary)
            VALUES ('delete', old.rowid, COALESCE(old.title, ''), COALESCE(old.summary, ''));
          INSERT INTO sessions_fts_tri(rowid, title, summary)
            VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.summary, ''));
        END;
        """
    )

    # 既存データの FTS migration (upgrade パス)
    if conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0] == 0:
        if conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] > 0:
            conn.execute("INSERT INTO turns_fts(rowid, content) SELECT id, content FROM turns")
            conn.execute("INSERT INTO turns_fts_tri(rowid, content) SELECT id, content FROM turns")
    if conn.execute("SELECT COUNT(*) FROM sessions_fts").fetchone()[0] == 0:
        if conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE title IS NOT NULL OR summary IS NOT NULL"
        ).fetchone()[0] > 0:
            conn.execute(
                "INSERT INTO sessions_fts(rowid, title, summary) "
                "SELECT rowid, COALESCE(title, ''), COALESCE(summary, '') FROM sessions"
            )
            conn.execute(
                "INSERT INTO sessions_fts_tri(rowid, title, summary) "
                "SELECT rowid, COALESCE(title, ''), COALESCE(summary, '') FROM sessions"
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


TOOL_DISPATCH = {
    "current_time": _tool_current_time,
    "list_dir": _tool_list_dir,
    "read_file": _tool_read_file,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "shell": _tool_shell,
}


CHAT_SYSTEM_PROMPT = """bodies chat mode.

- 簡単な質問には text のみで答える。tool は必要な時だけ使う。
- 応答は短く、要点だけ。長い列挙より一行のまとめ。
- read-only な情報取得は list_dir / read_file / glob / grep / current_time を優先。
- shell は副作用ある操作のみ。読み取り系 (ls / cat / sqlite3 select 等) は allow-list で自動承認、それ以外は user 確認。
- 「今いつ？」のような時刻質問は current_time を使う (DB の session 時刻と混同しない)。"""


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
        print(f"  → {title}", file=sys.stderr)


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
        print(f"archived: {args.name}")
        return


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
    sub.add_parser("sleep", help="generate title/summary for unprocessed sessions")
    p_search = sub.add_parser("search", help="FTS5 search over turns / sessions")
    p_search.add_argument("query")
    p_search.add_argument("--tri", action="store_true", help="trigram tokenizer (CJK 部分一致)")
    p_search.add_argument("--turns", action="store_true", help="only turns")
    p_search.add_argument("--sessions", action="store_true", help="only sessions")
    p_search.add_argument("--limit", type=int, default=10)
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
        "domain": cmd_domain,
        "skill": cmd_skill,
    }[cmd]
    handler(args)


if __name__ == "__main__":
    main()
