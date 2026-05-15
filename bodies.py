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
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

from openai import OpenAI

DB_PATH = Path(os.environ.get("BODIES_DB", "bodies.db"))
TURN_DIR = Path(os.environ.get("BODIES_TURN_DIR", "data/turns"))
DUMP_DIR = Path(os.environ.get("BODIES_DUMP_DIR", "data/sessions"))
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


def get_client() -> OpenAI:
    if not API_KEY:
        raise SystemExit("set LLM_API_KEY in env (.env)")
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
# chat (standalone agent loop)
# ---------------------------------------------------------------------------

def cmd_chat(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    client = get_client()
    sid = open_session(db)
    history: list[dict] = []
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

            resp = client.chat.completions.create(
                model=MODEL, messages=history, max_tokens=4096
            )
            out = resp.choices[0].message.content or ""
            history.append({"role": "assistant", "content": out})
            append_turn(db, sid, "assistant", out, cwd=cwd, model=MODEL)
            append_to_date_md(sid, "assistant", out, cwd=cwd)
            print(out)
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
        "domain": cmd_domain,
    }[cmd]
    handler(args)


if __name__ == "__main__":
    main()
