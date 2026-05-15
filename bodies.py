"""bodies — agent layer.

subcommands:
  chat (default)  対話。trajectory を SQLite に append。
  list            session 一覧。
  dump            data/sessions/*.md に書き出す（LLM 不要）。
  sleep           未要約 session に title + summary を生成（aux LLM）+ dump。

stateless model + client state。content generator として stdin/stdout で動く。
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from openai import OpenAI

DB_PATH = Path(os.environ.get("BODIES_DB", "bodies.db"))
DUMP_DIR = Path(os.environ.get("BODIES_DUMP_DIR", "data/sessions"))
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
            ts REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, ts)")
    conn.commit()
    return conn


def open_session(db: sqlite3.Connection) -> str:
    sid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    db.execute("INSERT INTO sessions (id, started_at) VALUES (?, ?)", (sid, time.time()))
    db.commit()
    return sid


def close_session(db: sqlite3.Connection, sid: str) -> None:
    db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), sid))
    db.commit()


def append_turn(db: sqlite3.Connection, sid: str, role: str, content: str) -> None:
    db.execute(
        "INSERT INTO turns (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (sid, role, content, time.time()),
    )
    db.commit()


def get_client() -> OpenAI:
    if not API_KEY:
        raise SystemExit("set LLM_API_KEY in env (.env)")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

def cmd_chat(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    client = get_client()
    sid = open_session(db)
    history: list[dict] = []

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
            append_turn(db, sid, "user", user)
            resp = client.chat.completions.create(
                model=MODEL, messages=history, max_tokens=4096
            )
            out = resp.choices[0].message.content or ""
            history.append({"role": "assistant", "content": out})
            append_turn(db, sid, "assistant", out)
            print(out)
    finally:
        close_session(db, sid)


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
        when = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        title_disp = title or "(no title)"
        domain_disp = f"[{domain}]" if domain else ""
        print(f"{when}  {sid[:16]:16}  {n:4} turns  {domain_disp:12} {title_disp}")


# ---------------------------------------------------------------------------
# dump (LLM 不要)
# ---------------------------------------------------------------------------

def session_to_md(db: sqlite3.Connection, sid: str) -> str:
    row = db.execute(
        "SELECT started_at, ended_at, title, summary, domain FROM sessions WHERE id = ?",
        (sid,),
    ).fetchone()
    if not row:
        return ""
    started_at, ended_at, title, summary, domain = row
    started = dt.datetime.fromtimestamp(started_at).isoformat(timespec="seconds")
    ended = dt.datetime.fromtimestamp(ended_at).isoformat(timespec="seconds") if ended_at else "(open)"

    lines = []
    lines.append(f"# {title or sid}")
    lines.append("")
    lines.append(f"- session_id: `{sid}`")
    lines.append(f"- started_at: {started}")
    lines.append(f"- ended_at: {ended}")
    if domain:
        lines.append(f"- domain: {domain}")
    lines.append("")
    if summary:
        lines.append("## summary")
        lines.append("")
        lines.append(summary)
        lines.append("")
    lines.append("## turns")
    lines.append("")
    turns = db.execute(
        "SELECT role, content, ts FROM turns WHERE session_id = ? ORDER BY ts",
        (sid,),
    ).fetchall()
    for role, content, ts in turns:
        prefix = "**user**" if role == "user" else "**assistant**" if role == "assistant" else f"**{role}**"
        lines.append(f"### {prefix}")
        lines.append("")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def dump_session(db: sqlite3.Connection, sid: str, out_dir: Path) -> Path:
    md = session_to_md(db, sid)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = db.execute("SELECT started_at FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
    stamp = dt.datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{stamp}-{sid[-8:]}.md"
    out.write_text(md, encoding="utf-8")
    return out


def cmd_dump(args: argparse.Namespace) -> None:
    db = init_db(DB_PATH)
    sids = [r[0] for r in db.execute("SELECT id FROM sessions ORDER BY started_at").fetchall()]
    for sid in sids:
        out = dump_session(db, sid, DUMP_DIR)
        print(f"wrote {out}")


# ---------------------------------------------------------------------------
# sleep (aux LLM)
# ---------------------------------------------------------------------------

SLEEP_PROMPT = """以下の対話の **タイトル（1 行、30 字以内）** と **要約（3-5 行、日本語）** を返せ。
タイトルは内容を表す名詞句。要約は何を話して何に至ったかを書く。

返答フォーマット（JSON ではなく素朴に）:
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
    buf = []
    total = 0
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
    summary_lines: list[str] = []
    mode = None
    for line in text.splitlines():
        if line.startswith("TITLE:"):
            title = line[len("TITLE:"):].strip()
            mode = "title"
        elif line.startswith("SUMMARY:"):
            mode = "summary"
        elif mode == "summary":
            summary_lines.append(line)
    return title, "\n".join(summary_lines).strip()


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
        out = dump_session(db, sid, DUMP_DIR)
        print(f"  → {title} / {out}")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bodies", description="agent layer")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="interactive chat (default)")
    p_list = sub.add_parser("list", help="list sessions")
    p_list.add_argument("--limit", type=int, default=30)
    sub.add_parser("dump", help="dump all sessions to markdown")
    sub.add_parser("sleep", help="generate title/summary for unprocessed sessions")
    return p


def main() -> None:
    args = build_parser().parse_args()
    cmd = args.cmd or "chat"
    handler = {
        "chat": cmd_chat,
        "list": cmd_list,
        "dump": cmd_dump,
        "sleep": cmd_sleep,
    }[cmd]
    handler(args)


if __name__ == "__main__":
    main()
