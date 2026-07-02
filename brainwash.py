"""brainwash (洗脳) — 生層 (host.db の turns) から蒸留層 (data/memory/) を作り直す consolidation パス。

二層ストアの書き込み側:
  生層   = host.db の turns (role 付き、 append-only、 検索しない、 検証の土台)
  蒸留層 = ここが作る data/memory/*.md (裏どり済みの事実 + index.md)

3 手:
  VERIFY   — assistant の主張を tool / user turn (= 一次資料) に照合。 裏づけの無い主張は
             落とし、 dropped_claims として数える。 user が明示的に述べた事実・選好は
             user turn 自体が根拠になる
  ORGANIZE — 検証済みの事実を 1 トピック 1 ファイルの md に書き直す (既存の蒸留層とマージ、
             重複は畳む)
  ENRICH   — 関連ファイルを相互リンクし、 index.md (1 事実 1 行、 行数上限) を書く

読み側は検索しない: agent は index.md を read_file で読み、 足りなければリンク先を開く
(index-first)。 埋め込みも FTS も使わない — index が context に収まるサイズであることを
この洗脳が規律として維持する。

洗うのは judge LLM (.env の JUDGE_*、 未設定なら executor に fallback)。 名前が示す通り、
記憶が自分を整理するのではなく、 外の審級が記憶を書き換える。 通常の洗脳と向きが逆で、
根拠なき信念を植え付けるのではなく、 根拠なき主張を洗い落とす。

蒸留層はいつでも生層から作り直せる派生物。 洗脳のプロンプトを改良したら re-wash するだけ。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import host  # noqa: E402

MEMORY_DIR = Path(os.environ.get("LISPY_MEMORY_DIR", str(_HERE / "data" / "memory")))
MAX_TOKENS = int(os.environ.get("BRAINWASH_MAX_TOKENS", "8192"))
MAX_SESSIONS = int(os.environ.get("BRAINWASH_MAX_SESSIONS", "10"))
MAX_CHARS = int(os.environ.get("BRAINWASH_MAX_CHARS", "200000"))
INDEX_MAX_LINES = int(os.environ.get("BRAINWASH_INDEX_MAX_LINES", "100"))
_TURN_TRUNC = 1500

BRAINWASH_SYSTEM = (
    "あなたは agent の長期記憶を作り直す洗脳係。 入力は (a) 会話ログ (role 付き) と "
    "(b) 現在の記憶ファイル群。 出力は記憶ディレクトリ全体の置き換え。 手順:\n"
    "VERIFY — 記憶に値する主張を洗い出す。 assistant の主張は tool turn の実行結果か "
    "user turn の発言という一次資料に照合し、 裏づけの無い主張は落として dropped_claims に列挙する "
    "(assistant が確かめずに書いた推測・尤もらしいだけの記述を通さない)。 "
    "user が明示的に述べた事実・選好・決定は user turn 自体が根拠。\n"
    "ORGANIZE — 検証済みの事実を 1 トピック 1 ファイルの Markdown に書き直す。 "
    "既存の記憶ファイルとマージし、 重複は畳む。 矛盾したら新しい情報を採り、 旧記述は 1 行注記。 "
    "セッション固有の些事 (一時的なエラー、 その場限りのやり取り) は記憶にしない。\n"
    "ENRICH — 関連ファイルを [リンク](path) で相互参照させる。\n"
    "index.md は必須: 1 事実 1 行 + 詳細ファイルへのリンク、 全体で {index_max} 行以内。 "
    "index だけ読めば大半の用が足りるように書く。\n"
    "出力は次の JSON のみ (説明文・コードフェンス禁止):\n"
    '{{"kept_facts": ["..."], "dropped_claims": ["..."], '
    '"files": [{{"path": "index.md", "content": "..."}}, ...]}}\n'
    "files は記憶ディレクトリの完全な置き換え。 path は相対パスの .md のみ。"
)


def _strip_fences(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```[a-zA-Z0-9_+\-]*\s*\n?(.*?)\n?```$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def _last_wash_ts(db) -> float:
    row = db.execute(
        "SELECT MAX(ts) FROM meta_events WHERE kind = 'brainwash'"
    ).fetchone()
    return float(row[0]) if row and row[0] else 0.0


def _pick_sessions(db, sessions: list[str] | None) -> list[str]:
    """洗う session を決める。 指定があれば prefix 解決、 無ければ前回の洗脳以降に
    turn が増えた session (新しい順に MAX_SESSIONS 件)。"""
    if sessions:
        return [host.resolve_session(db, s) for s in sessions]
    since = _last_wash_ts(db)
    rows = db.execute(
        "SELECT session_id, MAX(ts) AS latest FROM turns WHERE ts > ? "
        "GROUP BY session_id ORDER BY latest DESC LIMIT ?",
        (since, MAX_SESSIONS),
    ).fetchall()
    return [r[0] for r in rows]


def _session_transcript(db, sid: str) -> str:
    title_row = db.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
    title = title_row[0] if title_row and title_row[0] else ""
    lines = [f"## session {sid[:12]}" + (f" — {title}" if title else "")]
    rows = db.execute(
        "SELECT role, content FROM turns WHERE session_id = ? ORDER BY ts ASC", (sid,)
    ).fetchall()
    for role, content in rows:
        c = (content or "").strip()
        if len(c) > _TURN_TRUNC:
            c = c[:_TURN_TRUNC] + f" …(truncated, {len(c)} chars)"
        lines.append(f"{role}: {c}")
    return "\n".join(lines)


def _read_memory_files() -> dict[str, str]:
    if not MEMORY_DIR.exists():
        return {}
    out: dict[str, str] = {}
    for p in sorted(MEMORY_DIR.rglob("*.md")):
        out[str(p.relative_to(MEMORY_DIR))] = p.read_text(encoding="utf-8")
    return out


def _safe_rel_path(path: str) -> Path | None:
    """蒸留層内に閉じた相対 .md パスか検証。 traversal は None。"""
    if not path or not path.endswith(".md"):
        return None
    p = Path(path)
    if p.is_absolute():
        return None
    resolved = (MEMORY_DIR / p).resolve()
    try:
        resolved.relative_to(MEMORY_DIR.resolve())
    except ValueError:
        return None
    return resolved


def brainwash(db, sessions: list[str] | None = None) -> str:
    """洗脳を 1 回走らせ、 結果 summary を返す。 judge 不達・出力不正のときは
    蒸留層に触らず message を返す (fail-safe: 壊れた出力で記憶を消さない)。"""
    sids = _pick_sessions(db, sessions)
    if not sids:
        return "(brainwash: 前回の洗脳以降に新しい turn が無い — 洗うものがない)"

    transcripts = []
    budget = MAX_CHARS
    for sid in sids:
        t = _session_transcript(db, sid)
        if len(t) > budget:
            t = t[:budget] + "\n…(transcript budget exceeded)"
        transcripts.append(t)
        budget -= len(t)
        if budget <= 0:
            break

    memory = _read_memory_files()
    mem_text = "\n\n".join(f"### {p}\n{c}" for p, c in memory.items()) or "(まだ記憶なし)"

    user_content = (
        "現在の記憶ファイル群:\n" + mem_text
        + "\n\n---\n\n会話ログ:\n" + "\n\n".join(transcripts)
    )
    system = BRAINWASH_SYSTEM.format(index_max=INDEX_MAX_LINES)

    try:
        client = host.get_judge_client()
        resp = client.chat.completions.create(
            model=host.judge_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_tokens=MAX_TOKENS,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        return f"(brainwash: judge unreachable — {type(e).__name__}: {e})"

    try:
        data = json.loads(_strip_fences(raw))
        kept = [str(x) for x in data.get("kept_facts", [])]
        dropped = [str(x) for x in data.get("dropped_claims", [])]
        files = data.get("files", [])
        assert isinstance(files, list) and files
    except Exception as e:
        return f"(brainwash: 出力を parse できない ({type(e).__name__}: {e}) — 蒸留層は無変更)\n{raw[:400]}"

    # 検証してから書く。 index.md が無い置き換えは受けない (読み経路が消えるので)
    writes: dict[Path, str] = {}
    skipped: list[str] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        target = _safe_rel_path(str(f.get("path", "")))
        if target is None:
            skipped.append(str(f.get("path", "?")))
            continue
        writes[target] = str(f.get("content", ""))
    index_path = (MEMORY_DIR / "index.md").resolve()
    if index_path not in writes:
        return "(brainwash: 出力に index.md が無い — 蒸留層は無変更)"
    index_lines = writes[index_path].count("\n") + 1
    warn = ""
    if index_lines > INDEX_MAX_LINES:
        warn = f"\n  (warning: index.md が {index_lines} 行 — 上限 {INDEX_MAX_LINES} 行を超過、次回の洗脳で圧縮すること)"

    # 置き換え: 既存 *.md を消して書き直す (蒸留層は生層からの派生物なので全置換でよい)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    for p in MEMORY_DIR.rglob("*.md"):
        p.unlink()
    for target, content in writes.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    payload = json.dumps({
        "sessions": [s[:12] for s in sids],
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_claims": [d[:200] for d in dropped],
        "files": [str(t.relative_to(MEMORY_DIR.resolve())) for t in writes],
        "skipped_paths": skipped,
    }, ensure_ascii=False)
    try:
        host.log_meta(db, "brainwash", sid=sids[0], payload=payload)
    except Exception:
        pass

    lines = [
        f"brainwash: {len(sids)} sessions を洗った → {len(writes)} files"
        f" (kept {len(kept)} / dropped {len(dropped)}){warn}",
    ]
    for d in dropped[:8]:
        lines.append(f"  [dropped] {d[:120]}")
    if skipped:
        lines.append(f"  [skipped paths] {', '.join(skipped)}")
    return "\n".join(lines)
