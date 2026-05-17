"""ds4ds4 — agent layer.

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

import asyncio

from rich.console import Console
from rich.markdown import Markdown

# どの rich Console から呼ばれても bell を黙らせる (Textual 内部の \a 排出を全部塞ぐ)
Console.bell = lambda self: None  # type: ignore[assignment]

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import RichLog, Static, TextArea

console = Console()
err_console = Console(stderr=True)

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

DB_PATH = Path(os.environ.get("DS4DS4_DB", str(_ROOT / "ds4ds4.db")))
TURN_DIR = Path(os.environ.get("DS4DS4_TURN_DIR", str(_ROOT / "data" / "turns")))
DUMP_DIR = Path(os.environ.get("DS4DS4_DUMP_DIR", str(_ROOT / "data" / "sessions")))
TZ_OFFSET_HOURS = int(os.environ.get("DS4DS4_TZ_OFFSET", "9"))
MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-opus-4.7")
BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
API_KEY = os.environ.get("LLM_API_KEY")
AUX_MODEL = os.environ.get("DS4DS4_AUX_MODEL", MODEL)
CTX_WINDOW = int(os.environ.get("DS4DS4_CTX_WINDOW", "200000"))


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
        text = f'---\ntitle: "{date_str}"\ntags: [ds4ds4]\n---\n\n'
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

def _estimate_tokens(history: list[dict]) -> int:
    """char ÷ 3 の雑な近似。正確さは要らない、桁感が分かれば良い。"""
    total = 0
    for m in history:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    total += len(p.get("text", "") or "")
        for tc in m.get("tool_calls", []) or []:
            args = tc.get("function", {}).get("arguments", "")
            total += len(args or "")
    return total // 3


def _git_branch() -> str:
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=1,
        )
        return r.stdout.strip() or "-"
    except Exception:
        return "-"


_MODEL_DISPLAY: str | None = None


def model_display_name() -> str:
    """status に出すモデル名。/v1/models で実モデル名取れたらそれ、ダメなら LLM_MODEL。"""
    global _MODEL_DISPLAY
    if _MODEL_DISPLAY is not None:
        return _MODEL_DISPLAY
    try:
        from openai import OpenAI
        client = OpenAI(api_key=API_KEY or "x", base_url=BASE_URL)
        models = client.models.list()
        if models.data:
            _MODEL_DISPLAY = models.data[0].id
            return _MODEL_DISPLAY
    except Exception:
        pass
    _MODEL_DISPLAY = MODEL.rsplit("/", 1)[-1]
    return _MODEL_DISPLAY


def render_status(history: list[dict]) -> str:
    """status line を組み立てる。DS4DS4_STATUSLINE で shell 外注可。"""
    override = os.environ.get("DS4DS4_STATUSLINE")
    if override:
        try:
            r = subprocess.run(override, shell=True, capture_output=True, text=True, timeout=2)
            return r.stdout.rstrip()
        except Exception:
            pass

    ctx_tokens = _estimate_tokens(history)
    ctx_pct = min(100, int(100 * ctx_tokens / CTX_WINDOW)) if CTX_WINDOW else 0
    cwd_short = os.getcwd().replace(str(Path.home()), "~")
    branch = _git_branch()
    mode = _TOOL_CTX.get("mode", "default")
    return (
        f"{model_display_name()}  ctx ○ {ctx_pct}%  📁 {cwd_short}  🌿 {branch}  mode: {mode}"
    )


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
    {
        "type": "function",
        "function": {
            "name": "consult",
            "description": (
                "fresh context で LLM 自身にサブ質問を投げる。"
                "main loop の history を持たない、tool も持たない、純粋な text-in/text-out。"
                "切り出した小問題 (要約・分類・抽出・形式変換 等) を深く考えさせたいときに使う。"
                "default は thinking 有効 (main loop より深く考える)。再帰 consult は不可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "サブ質問の本文。文脈は全部 prompt に書く"},
                    "max_tokens": {"type": "integer", "description": "返答の最大トークン。default 1024、max 4096"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "ファイルの中の old_string を new_string に書き換える。exact match。"
                "old_string が見つからない / 複数一致 (replace_all=false の時) は error。"
                "unique にするには周辺 context を含めて拡張する。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "description": "default false。true なら全一致を置換"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "ファイルを書き出す (上書き)。親ディレクトリは自動作成。新規 / 全書換用。編集は edit_file を優先。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subagent",
            "description": (
                "fresh history + tool 付きの sub-agent を別 loop で走らせる。"
                "consult は text only / 1 ターンだが、subagent は tool を回せる。"
                "切り出した独立タスク (調査・複数 step の処理 等) を任せる。"
                "戻り値は sub-agent の最終 assistant text。再帰 subagent は不可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "sub-agent への指示。文脈は全部 task に書く"},
                    "system": {"type": "string", "description": "system prompt 上書き (optional)"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "進行中のタスクを記録する。複数 step の作業を追跡したいときに使う。",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "task の status を更新。pending / in_progress / completed。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                },
                "required": ["id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "current session の task 一覧 (completed は default で除外)。",
            "parameters": {
                "type": "object",
                "properties": {"include_completed": {"type": "boolean"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bg_run",
            "description": (
                "shell command を background で起動する。返り値の bg_id で status / tail / kill。"
                "長い build / test / log tail を投げっぱなしにする用途。"
                "safe-list 外は user 確認。"
            ),
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
            "name": "bg_status",
            "description": "background task の状態 (running / done + exit code) を返す。",
            "parameters": {
                "type": "object",
                "properties": {"bg_id": {"type": "string"}},
                "required": ["bg_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bg_tail",
            "description": "background task の stdout/stderr の末尾 N 行を返す (default 50, max 500)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "bg_id": {"type": "string"},
                    "lines": {"type": "integer"},
                },
                "required": ["bg_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bg_kill",
            "description": "background task を terminate (3 秒後に kill)。",
            "parameters": {
                "type": "object",
                "properties": {"bg_id": {"type": "string"}},
                "required": ["bg_id"],
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
    {
        "type": "function",
        "function": {
            "name": "mode_set",
            "description": (
                "permission mode を切替える。"
                "default = unsafe shell に確認、edit/write は free。"
                "plan = edit_file / write_file / bg_run / unsafe shell を全部 block (read-only)。"
                "yolo = 確認なし、何でも実行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["default", "plan", "yolo"]},
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_new",
            "description": (
                "タスクが完結 / 話題が切れたとき、現 session を閉じて新規 session を開く。"
                "carry に渡した文字列だけが次の session の system message として持ち越される。"
                "context が太ってきたら早めに切るのが規律。"
                "過去は recall で取り戻せるので carry は最小で良い。"
                "subagent からは呼べない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "carry": {"type": "string", "description": "次の session に持ち越したい要約 (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_close",
            "description": (
                "現 session を閉じて chat を終了する。"
                "明確に区切りたいときに呼ぶ。subagent からは呼べない。"
            ),
            "parameters": {"type": "object", "properties": {}},
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
    mode = _TOOL_CTX.get("mode", "default")
    print(f"\n  [tool] shell{' (safe)' if safe else ''}: {cmd}", file=sys.stderr)
    if not safe:
        if mode == "plan":
            return _plan_block("shell (unsafe command)")
        if mode != "yolo":
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


def _tool_consult(args: dict) -> str:
    """fresh context で LLM 自身にサブ質問。tool なし、history なし、再帰不可。
    main loop と切り離して考えさせるサブルーチン的扱い。"""
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "(empty prompt)"
    max_tokens = max(1, min(int(args.get("max_tokens") or 1024), 4096))

    client = get_client()
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            # think は default のまま (main の chat は False、consult はサブ問題なので深く)
        )
    except Exception as e:
        return f"consult error: {e}"
    return resp.choices[0].message.content or "(no content)"


# tool 実行コンテキスト (現在の session / cwd 等を tool handler に渡す)
_TOOL_CTX: dict = {"sid": None, "cwd": "", "in_subagent": False, "mode": "default"}


def _plan_block(name: str) -> str:
    return f"plan mode: {name} is blocked. call mode_set('default' or 'yolo') to unblock."

# background process registry: bg_id → {proc, out_path, err_path, cmd, started}
_BG_PROCS: dict = {}


def _tool_edit_file(args: dict) -> str:
    if _TOOL_CTX.get("mode") == "plan":
        return _plan_block("edit_file")
    path = Path(args["path"]).expanduser()
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))
    if not path.exists():
        return f"not found: {path}"
    if old == new:
        return "error: old_string equals new_string"
    if not old:
        return "error: empty old_string"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"read error: {e}"
    count = text.count(old)
    if count == 0:
        return f"error: old_string not found in {path}"
    if count > 1 and not replace_all:
        return (
            f"error: old_string matches {count} times in {path}; "
            "expand surrounding context to make it unique, or pass replace_all=true"
        )
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return f"write error: {e}"
    _print_diff(text, new_text, path)
    plural = "s" if count > 1 else ""
    return f"edited {path} ({count} replacement{plural})"


def _print_diff(old_text: str, new_text: str, path: Path) -> None:
    import difflib
    lines = difflib.unified_diff(
        old_text.splitlines(keepends=False),
        new_text.splitlines(keepends=False),
        fromfile=str(path), tofile=str(path),
        n=2, lineterm="",
    )
    for line in lines:
        if line.startswith("+++") or line.startswith("---"):
            err_console.print(line, style="bold", highlight=False)
        elif line.startswith("+"):
            err_console.print(line, style="green", highlight=False)
        elif line.startswith("-"):
            err_console.print(line, style="red", highlight=False)
        elif line.startswith("@@"):
            err_console.print(line, style="cyan", highlight=False)
        else:
            err_console.print(line, style="dim", highlight=False)


def _tool_write_file(args: dict) -> str:
    if _TOOL_CTX.get("mode") == "plan":
        return _plan_block("write_file")
    path = Path(args["path"]).expanduser()
    content = args.get("content", "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"write error: {e}"
    return f"wrote {path} ({len(content)} bytes)"


SUBAGENT_SYSTEM_PROMPT = """ds4ds4 sub-agent mode.
独立した focused task を任されている。fresh history + tool 持ち、ただし subagent は呼べない。
タスクを最後まで実行し、最終的な結果を簡潔な assistant text で返せ。
tool の使い方は main loop と同じ規律。"""


def _tool_subagent(args: dict) -> str:
    if _TOOL_CTX.get("in_subagent"):
        return "error: 再帰 subagent は不可"
    task = (args.get("task") or "").strip()
    if not task:
        return "(empty task)"
    system = args.get("system") or SUBAGENT_SYSTEM_PROMPT

    client = get_client()
    sub_history = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    sub_tools = [t for t in TOOL_SCHEMA if t["function"]["name"] != "subagent"]
    sub_dispatch = {k: v for k, v in TOOL_DISPATCH.items() if k != "subagent"}

    prev = _TOOL_CTX.get("in_subagent", False)
    _TOOL_CTX["in_subagent"] = True
    try:
        return run_tool_loop(
            client, sub_history, sub_tools,
            dispatch=sub_dispatch, db=None, sid=None,
            cwd=_TOOL_CTX.get("cwd", ""), label="sub",
        )
    finally:
        _TOOL_CTX["in_subagent"] = prev


def _tool_task_create(args: dict) -> str:
    content = (args.get("content") or "").strip()
    if not content:
        return "(empty content)"
    db = init_db(DB_PATH)
    sid = _TOOL_CTX.get("sid")
    now = time.time()
    cur = db.execute(
        "INSERT INTO tasks (session_id, content, status, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
        (sid, content, now, now),
    )
    db.commit()
    return f"task #{cur.lastrowid}: {content}"


def _tool_task_update(args: dict) -> str:
    task_id = args.get("id")
    status = args.get("status", "")
    if status not in ("pending", "in_progress", "completed"):
        return f"invalid status: {status}"
    if task_id is None:
        return "missing id"
    db = init_db(DB_PATH)
    res = db.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status, time.time(), task_id),
    )
    db.commit()
    if res.rowcount == 0:
        return f"task #{task_id} not found"
    return f"task #{task_id} → {status}"


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


def _tool_bg_run(args: dict) -> str:
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return "(empty command)"
    safe = _is_shell_safe(cmd)
    mode = _TOOL_CTX.get("mode", "default")
    print(f"\n  [bg] {cmd}{' (safe)' if safe else ''}", file=sys.stderr)
    if not safe:
        if mode == "plan":
            return _plan_block("bg_run (unsafe command)")
        if mode != "yolo":
            try:
                confirm = input("  background 実行する？ [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "user rejected (no input)"
            if confirm != "y":
                return "user rejected execution"
    bg_id = uuid.uuid4().hex[:8]
    out_path = Path(f"/tmp/ds4ds4-bg-{bg_id}.out")
    err_path = Path(f"/tmp/ds4ds4-bg-{bg_id}.err")
    try:
        out_f = open(out_path, "wb")
        err_f = open(err_path, "wb")
        proc = subprocess.Popen(cmd, shell=True, stdout=out_f, stderr=err_f)
    except OSError as e:
        return f"spawn error: {e}"
    _BG_PROCS[bg_id] = {
        "proc": proc, "out": out_path, "err": err_path,
        "cmd": cmd, "started": time.time(),
    }
    return f"bg_id={bg_id} pid={proc.pid} cmd={cmd}"


def _tool_bg_status(args: dict) -> str:
    bg_id = args.get("bg_id", "")
    if not bg_id and _BG_PROCS:
        return "\n".join(
            _format_bg_status(bid, entry) for bid, entry in _BG_PROCS.items()
        )
    entry = _BG_PROCS.get(bg_id)
    if not entry:
        return f"unknown bg_id: {bg_id}"
    return _format_bg_status(bg_id, entry)


def _format_bg_status(bg_id: str, entry: dict) -> str:
    proc = entry["proc"]
    rc = proc.poll()
    elapsed = int(time.time() - entry["started"])
    state = "running" if rc is None else f"done exit={rc}"
    return f"{bg_id} {state} ({elapsed}s) cmd={entry['cmd']}"


def _tool_bg_tail(args: dict) -> str:
    bg_id = args.get("bg_id", "")
    n = max(1, min(int(args.get("lines") or 50), 500))
    entry = _BG_PROCS.get(bg_id)
    if not entry:
        return f"unknown bg_id: {bg_id}"
    parts: list[str] = []
    for label, path in [("stdout", entry["out"]), ("stderr", entry["err"])]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            lines = []
        if lines:
            parts.append(f"--- {label} (last {len(lines)}) ---\n" + "\n".join(lines))
    status = _format_bg_status(bg_id, entry)
    body = "\n".join(parts) or "(no output yet)"
    return f"{status}\n{body}"


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
            headers={"User-Agent": "ds4ds4/0.1"},
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


def _tool_session_new(args: dict) -> str:
    if _TOOL_CTX.get("in_subagent"):
        return "error: subagent からは session_new を呼べない"
    carry = (args.get("carry") or "").strip()
    _TOOL_CTX["pending_action"] = ("new", carry)
    return f"session rotation queued (carry: {len(carry)} chars)。このターン完了後に新 session に移行。"


def _tool_session_close(args: dict) -> str:
    if _TOOL_CTX.get("in_subagent"):
        return "error: subagent からは session_close を呼べない"
    _TOOL_CTX["pending_action"] = ("close", "")
    return "chat close queued。このターン完了後に終了。"


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
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ds4ds4/0.1",
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


def _tool_mode_set(args: dict) -> str:
    mode = (args.get("mode") or "").strip().lower()
    if mode not in ("default", "plan", "yolo"):
        return f"invalid mode: {mode}. valid: default, plan, yolo"
    prev = _TOOL_CTX.get("mode", "default")
    _TOOL_CTX["mode"] = mode
    return f"mode: {prev} → {mode}"


def _tool_bg_kill(args: dict) -> str:
    bg_id = args.get("bg_id", "")
    entry = _BG_PROCS.get(bg_id)
    if not entry:
        return f"unknown bg_id: {bg_id}"
    proc = entry["proc"]
    if proc.poll() is not None:
        return f"{bg_id} already done exit={proc.returncode}"
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    return f"{bg_id} killed exit={proc.returncode}"


TOOL_DISPATCH = {
    "current_time": _tool_current_time,
    "list_dir": _tool_list_dir,
    "read_file": _tool_read_file,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "shell": _tool_shell,
    "recall": _tool_recall,
    "recall_session": _tool_recall_session,
    "consult": _tool_consult,
    "edit_file": _tool_edit_file,
    "write_file": _tool_write_file,
    "subagent": _tool_subagent,
    "task_create": _tool_task_create,
    "task_update": _tool_task_update,
    "task_list": _tool_task_list,
    "bg_run": _tool_bg_run,
    "bg_status": _tool_bg_status,
    "bg_tail": _tool_bg_tail,
    "bg_kill": _tool_bg_kill,
    "web_fetch": _tool_web_fetch,
    "web_search": _tool_web_search,
    "mode_set": _tool_mode_set,
    "session_new": _tool_session_new,
    "session_close": _tool_session_close,
}


def run_tool_loop(
    client, history: list[dict], tools: list,
    *, dispatch: dict | None = None,
    db=None, sid: str | None = None, cwd: str = "",
    max_iters: int = 20,
    on_chunk=None, on_tool_call=None, on_tool_result=None,
    on_assistant_done=None,
) -> str:
    """streaming で tool-calling loop を駆動。最終 assistant text を返す。

    callbacks (全部 optional):
      on_chunk(piece: str)        — text chunk が来るたび
      on_tool_call(name, args)    — tool dispatch 直前
      on_tool_result(name, result) — tool 実行後
      on_assistant_done(text)     — 1 assistant turn (tool_calls なし) 完了時
    callback 無しなら silent (subagent 用)。
    """
    if dispatch is None:
        dispatch = TOOL_DISPATCH
    for _ in range(max_iters):
        stream = client.chat.completions.create(
            model=MODEL,
            messages=history,
            tools=tools,
            max_tokens=4096,
            extra_body={"think": False},
            stream=True,
        )

        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                content_parts.append(delta.content)
                if on_chunk:
                    on_chunk(delta.content)
            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = tc.index
                entry = tool_calls_acc.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.id:
                    entry["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn:
                    if fn.name:
                        entry["function"]["name"] += fn.name
                    if fn.arguments:
                        entry["function"]["arguments"] += fn.arguments

        content = "".join(content_parts)
        tool_calls = [tool_calls_acc[k] for k in sorted(tool_calls_acc)]

        if not tool_calls:
            history.append({"role": "assistant", "content": content})
            if db and sid:
                append_turn(db, sid, "assistant", content, cwd=cwd, model=MODEL)
                append_to_date_md(sid, "assistant", content, cwd=cwd)
            if on_assistant_done:
                on_assistant_done(content)
            return content

        history.append({
            "role": "assistant",
            "content": content or None,
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                targs = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                targs = {}
            if on_tool_call:
                on_tool_call(name, targs)
            handler = dispatch.get(name)
            if handler is None:
                result = f"unknown tool: {name}"
            else:
                try:
                    result = handler(targs)
                except Exception as e:
                    result = f"error: {e}"
            if on_tool_result:
                on_tool_result(name, result)
            history.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            if db and sid:
                append_turn(db, sid, "tool", f"{name}({targs}) → {result[:200]}", cwd=cwd, model=MODEL)
    return "[tool loop limit reached]"


CHAT_SYSTEM_PROMPT = """ds4ds4 chat mode.

- 簡単な質問には text のみで答える。tool は必要な時だけ使う。
- 応答は短く、要点だけ。長い列挙より一行のまとめ。
- read-only な情報取得は list_dir / read_file / glob / grep / current_time を優先。
- shell は副作用ある操作のみ。読み取り系 (ls / cat / sqlite3 select 等) は allow-list で自動承認、それ以外は user 確認。
- 「今いつ？」のような時刻質問は current_time を使う (DB の session 時刻と混同しない)。
- **過去 trajectory が役立ちそうなとき** (「前にやった X」「いつもの手順」「以前のあの session」等) は recall を呼ぶ。
  関連 turn が見つかったら recall_session で前後文脈まで取り、現在のタスクを過去パターンに照らして合成する。
  毎回 retrieve して synthesize する方が ds4ds4 の規律に合う。
- **切り出した小問題** (要約・分類・抽出・形式変換 等) を main loop と切り離して考えさせたいときは consult を呼ぶ。
  fresh context で text-in/text-out、main の history は混じらない。再帰 consult は不可 (consult 中の LLM には tool が無い)。"""


# ---------------------------------------------------------------------------
# chat (standalone agent loop + tool calling)
# ---------------------------------------------------------------------------

class ChatInput(TextArea):
    """multi-line 対応 input。Enter で送信、Alt+Enter / Shift+Enter で改行。"""

    BINDINGS: list = []  # default の Enter=改行 等を全部解除

    def bell(self) -> None:
        pass  # widget レベルの bell も黙らせる

    def on_mount(self) -> None:
        self.cursor_blink = False  # カーソル点滅を止める

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    async def _on_key(self, event: events.Key) -> None:
        # Enter / 改行系を完全に内製で捌く。super に渡さないので TextArea 内部の
        # 「改行挿入 → 副作用 bell」経路が一切走らない。
        if event.key in ("alt+enter", "shift+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text
            self.load_text("")
            self.post_message(self.Submitted(text))
            return
        await super()._on_key(event)


class BodiesChatApp(App):
    """Claude Code 風の TUI。Input は下固定、上は会話ログ、最下は status。"""

    CSS = """
    Screen { background: $surface; }
    RichLog#log {
        background: $surface;
        padding: 0 1;
    }
    ChatInput#prompt {
        dock: bottom;
        border: round gray;
        margin: 0 1;
        height: auto;
        min-height: 3;
        max-height: 12;
        background: $surface;
        scrollbar-size: 0 0;
    }
    Static#status {
        dock: bottom;
        color: $text-muted;
        padding: 0 2;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+d", "quit", "exit", priority=True),
    ]

    def bell(self) -> None:
        pass  # 黙らせる: Textual のデフォルト bell を無効化

    def __init__(self, client, db, sid: str, system_prompt: str, cwd: str):
        super().__init__()
        self.animation_level = "none"
        # rich Console レベルでも bell を黙らせる (\a 文字を抑止)
        try:
            self.console.bell = lambda: None  # type: ignore[assignment]
        except Exception:
            pass
        self.client = client
        self.db = db
        self.sid = sid
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.history: list[dict] = [{"role": "system", "content": system_prompt}]
        self._stream_buffer: list[str] = []

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        yield Static("", id="status")
        yield ChatInput(id="prompt")

    def on_mount(self) -> None:
        self._refresh_status()
        # status の自動更新は一旦切る (bell 容疑のため)。turn ごとに _refresh_status は手動で呼ぶ
        self.query_one("#prompt", ChatInput).focus()

    def _refresh_status(self) -> None:
        self.query_one("#status", Static).update(render_status(self.history))

    def _log(self, renderable) -> None:
        """RichLog に書く (会話ログの統一出口)。"""
        self.query_one("#log", RichLog).write(renderable)

    async def on_chat_input_submitted(self, event: "ChatInput.Submitted") -> None:
        text = event.value.strip()
        if not text:
            return

        self._log(f"\n[dim]❯[/dim] {text}\n")

        self.history.append({"role": "user", "content": text})
        append_turn(self.db, self.sid, "user", text, cwd=self.cwd, model=MODEL)
        append_to_date_md(self.sid, "user", text, cwd=self.cwd)

        self.run_worker(self._run_turn, thread=True, exclusive=False)

    def _run_turn(self) -> None:
        def on_chunk(piece: str) -> None:
            self.call_from_thread(self._append_stream, piece)

        def on_tool_call(name: str, args: dict) -> None:
            self.call_from_thread(self._show_tool_call, name, args)

        def on_assistant_done(content: str) -> None:
            self.call_from_thread(self._flush_stream)

        run_tool_loop(
            self.client, self.history, TOOL_SCHEMA,
            db=self.db, sid=self.sid, cwd=self.cwd,
            on_chunk=on_chunk,
            on_tool_call=on_tool_call,
            on_assistant_done=on_assistant_done,
        )
        self.call_from_thread(self._after_turn)

    def _append_stream(self, piece: str) -> None:
        # 途中表示なし、buffer に貯めるだけ。\a (bell) は剥ぐ。
        self._stream_buffer.append(piece.replace("\x07", ""))

    def _show_tool_call(self, name: str, args: dict) -> None:
        args_str = json.dumps(args, ensure_ascii=False)[:200]
        self._log(f"[dim]  · {name}({args_str})[/dim]")

    def _flush_stream(self) -> None:
        text = "".join(self._stream_buffer)
        self._stream_buffer.clear()
        if text:
            self._log(Markdown(text, code_theme="ansi_dark"))

    def _after_turn(self) -> None:
        action = _TOOL_CTX.pop("pending_action", None)
        self._refresh_status()
        if not action:
            return
        kind, carry = action
        closed_sid = self.sid
        close_session(self.db, closed_sid)

        # closed session を非同期で sleep
        self.run_worker(
            lambda: self._sleep_one(closed_sid),
            thread=True, exclusive=False, name=f"sleep-{closed_sid[:8]}",
        )

        if kind == "close":
            self._log(f"[dim]· session {closed_sid[:12]} closed[/dim]")
            self.exit()
            return
        self.sid = open_session(self.db)
        _TOOL_CTX["sid"] = self.sid
        self.history.clear()
        self.history.append({"role": "system", "content": self.system_prompt})
        if carry:
            self.history.append({
                "role": "system",
                "content": f"[carry from prior session]\n{carry}",
            })
        self._log(f"[dim]· new session {self.sid[:12]}[/dim]")

    def _sleep_one(self, sid: str) -> None:
        """worker thread で sleep_one を走らせ、結果を log に通知。"""
        try:
            title = sleep_one(self.db, sid, self.client)
        except Exception as e:
            self.call_from_thread(self._log, f"[dim red]· sleep[{sid[:8]}] error: {e}[/dim red]")
            return
        if title:
            self.call_from_thread(self._log, f"[dim]· sleep[{sid[:8]}]: {title}[/dim]")


def cmd_chat(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    client = get_client()
    sid = open_session(db)
    system_prompt = CHAT_SYSTEM_PROMPT
    cwd = os.getcwd()
    _TOOL_CTX["sid"] = sid
    _TOOL_CTX["cwd"] = cwd
    _TOOL_CTX["in_subagent"] = False
    _TOOL_CTX["mode"] = getattr(args, "mode", "default")

    app = BodiesChatApp(client, db, sid, system_prompt, cwd)
    try:
        # full screen mode: iTerm2 で inline mode の layout 不具合 (入力が中段に出る + 応答消失)
        # を回避。chat 中は terminal 占有、Ctrl+D で抜けると元に戻る。
        app.run(mouse=False)
    finally:
        close_session(db, app.sid)


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
    """transcript を読んで、まだ DB に入ってない entry を全部 append。
    L2: assistant text に加え、tool_use / tool_result も turn として取り込む。
    model 名は assistant entry の message.model から拾う (spoof 検出可)。"""
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

    # dedup: DB に既に入ってるこの session の最後の turn ts より新しい entry のみ取り込む
    last_ts_row = db.execute(
        "SELECT MAX(ts) FROM turns WHERE session_id = ?", (sid,)
    ).fetchone()
    last_ts = float(last_ts_row[0] or 0.0)

    # tool_result map (id → text) は全 message から先に集める (順序不問の参照用)
    result_map: dict[str, str] = {}
    for msg in messages:
        result_map.update(extract_tool_results(msg))

    appended = 0
    last_assistant_text: str | None = None
    for msg in messages:
        ts_str = msg.get("timestamp")
        if not ts_str:
            continue
        try:
            entry_ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if entry_ts <= last_ts + 1e-6:
            continue

        etype = msg.get("type")
        m = msg.get("message", {}) if isinstance(msg.get("message"), dict) else {}

        if etype == "assistant":
            model = m.get("model") or ""
            text = extract_assistant_text(msg)
            if text and text.strip():
                append_turn(db, sid, "assistant", text, cwd=cwd, model=model)
                appended += 1
                last_assistant_text = text
            for name, inp, tid in extract_tool_calls(msg):
                try:
                    inp_str = json.dumps(inp, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    inp_str = str(inp)
                if len(inp_str) > 300:
                    inp_str = inp_str[:300] + "…"
                result = result_map.get(tid, "")
                if len(result) > 500:
                    result = result[:500] + "…"
                content = f"{name}({inp_str}) → {result}" if result else f"{name}({inp_str})"
                append_turn(db, sid, "tool", content, cwd=cwd, model=model)
                appended += 1

    # date-based md は最後の assistant text のみ追記 (従来挙動を維持、tool は md には書かない)
    if last_assistant_text:
        append_to_date_md(sid, "assistant", last_assistant_text, cwd=cwd)


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
        print(f"ds4ds4 record-turn error: {e}", file=sys.stderr)


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
        text = f'---\ntitle: "{date_str}"\ntags: [ds4ds4]\n---\n'
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


def sleep_one(
    db: sqlite3.Connection, sid: str, client,
) -> str | None:
    """1 session に title + summary を生成する。title 済みなら skip。"""
    row = db.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row and row[0]:
        return None
    transcript = transcript_of(db, sid)
    if not transcript.strip():
        return None
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
    return title


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
        print(f"sleeping over {sid} ...", file=sys.stderr)
        try:
            title = sleep_one(db, sid, client)
            if title:
                print(f"  → {title}", file=sys.stderr)
        except Exception as e:
            print(f"  error: {e}", file=sys.stderr)


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
    p = argparse.ArgumentParser(prog="ds4ds4", description="agent layer")
    sub = p.add_subparsers(dest="cmd")
    p_chat = sub.add_parser("chat", help="interactive chat (default)")
    p_chat.add_argument("--mode", choices=["default", "plan", "yolo"], default="default",
                        help="permission mode (default=current, plan=read-only, yolo=no confirm)")
    sub.add_parser("record-turn", help="hook handler (stdin: hook JSON)")
    p_list = sub.add_parser("list", help="list sessions")
    p_list.add_argument("--limit", type=int, default=30)
    sub.add_parser("dump", help="rebuild date-based md from DB")
    p_sleep = sub.add_parser("sleep", help="generate title/summary (要 LLM_API_KEY)")
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
    }[cmd]
    handler(args)


if __name__ == "__main__":
    main()
