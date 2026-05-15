"""bodies — minimal tracer.

stateless model + client が抱える trajectory + 標準入出力。
content generator として stdin/stdout で動く。
"""
import os
import sqlite3
import time
import uuid

from openai import OpenAI

DB_PATH = os.environ.get("BODIES_DB", "bodies.db")
MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-opus-4.7")
BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
API_KEY = os.environ.get("LLM_API_KEY")


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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


def append_turn(db: sqlite3.Connection, session_id: str, role: str, content: str) -> None:
    db.execute(
        "INSERT INTO turns (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (session_id, role, content, time.time()),
    )
    db.commit()


def main() -> None:
    if not API_KEY:
        raise SystemExit("set LLM_API_KEY in env (.env)")

    db = init_db(DB_PATH)
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    session_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    history: list[dict] = []

    while True:
        try:
            user = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue

        history.append({"role": "user", "content": user})
        append_turn(db, session_id, "user", user)

        resp = client.chat.completions.create(
            model=MODEL,
            messages=history,
            max_tokens=4096,
        )
        out = resp.choices[0].message.content or ""
        history.append({"role": "assistant", "content": out})
        append_turn(db, session_id, "assistant", out)
        print(out)


if __name__ == "__main__":
    main()
