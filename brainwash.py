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
# 洗脳が管理するディレクトリの marker。 wipe (全 *.md 削除) はこの marker があるときだけ —
# LISPY_MEMORY_DIR に一般のディレクトリ (~/notes 等) を誤って指しても消さないための床。
MEMORY_MARKER = ".lispy-memory"
MAX_TOKENS = int(os.environ.get("BRAINWASH_MAX_TOKENS", "8192"))
MAX_SESSIONS = int(os.environ.get("BRAINWASH_MAX_SESSIONS", "10"))
MAX_CHARS = int(os.environ.get("BRAINWASH_MAX_CHARS", "200000"))
# prompt に載せる既存記憶の上限 (transcript とは別枠)。 per-file / 合計の二段。
MEMORY_MAX_CHARS = int(os.environ.get("BRAINWASH_MEMORY_MAX_CHARS", "100000"))
_MEMFILE_TRUNC = 20000
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


def _wash_history(db) -> list[tuple[float, list[str]]]:
    """kind=brainwash の ledger から (洗脳時刻, 洗った session id 群) を時系列で返す。
    watermark は session ごと — 全体で 1 本の MAX(ts) にすると、 対象を絞った洗脳や
    MAX_SESSIONS で溢れた session が「洗われていないのに watermark の下」 になり
    永久に skip される。"""
    out: list[tuple[float, list[str]]] = []
    rows = db.execute(
        "SELECT ts, payload FROM meta_events WHERE kind = 'brainwash' ORDER BY ts ASC"
    ).fetchall()
    for ts, payload in rows:
        try:
            sids = json.loads(payload or "{}").get("sessions") or []
        except Exception:
            continue
        out.append((float(ts), [str(s) for s in sids]))
    return out


def _pick_sessions(db, sessions: list[str] | None) -> list[str]:
    """洗う session を決める。 指定があれば prefix 解決。 無ければ「最後にその session を
    洗った時刻」 より新しい turn を持つ session を、 新しい順に MAX_SESSIONS 件。
    溢れた session は watermark が進まないので次回に持ち越される (取りこぼさない)。"""
    if sessions:
        return [host.resolve_session(db, s) for s in sessions]
    history = _wash_history(db)

    def last_wash(sid: str) -> float:
        t = 0.0
        for ts, sids in history:
            for s in sids:
                # 旧 payload は 12 字 prefix で記録されていた — prefix 一致で拾う
                if sid == s or (len(s) < len(sid) and sid.startswith(s)):
                    t = max(t, ts)
        return t

    rows = db.execute(
        "SELECT session_id, MAX(ts) AS latest FROM turns "
        "GROUP BY session_id ORDER BY latest DESC"
    ).fetchall()
    picked: list[str] = []
    for sid, latest in rows:
        if float(latest) > last_wash(str(sid)):
            picked.append(str(sid))
            if len(picked) >= MAX_SESSIONS:
                break
    return picked


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
        text = p.read_text(encoding="utf-8")
        if len(text) > _MEMFILE_TRUNC:
            text = text[:_MEMFILE_TRUNC] + f"\n…(truncated, {len(text)} chars)"
        out[str(p.relative_to(MEMORY_DIR))] = text
    return out


def _memory_prompt_text(memory: dict[str, str]) -> str:
    """既存記憶を prompt 用に連結。 合計 MEMORY_MAX_CHARS で打ち切る
    (per-file は _read_memory_files 側で truncate 済み)。"""
    if not memory:
        return "(まだ記憶なし)"
    parts: list[str] = []
    used = 0
    for path, content in memory.items():
        block = f"### {path}\n{content}"
        if used + len(block) > MEMORY_MAX_CHARS:
            parts.append(f"### {path}\n(memory budget 超過 — 省略。次回の洗脳で圧縮すること)")
            continue
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


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
    mem_text = _memory_prompt_text(memory)

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
    if index_lines > INDEX_MAX_LINES:
        # index-first 読みの規律そのものなので warn で通さない — 蒸留層は無変更のまま返す
        return (f"(brainwash: index.md が {index_lines} 行で上限 {INDEX_MAX_LINES} を超過 — "
                f"蒸留層は無変更。index は 1 事実 1 行・詳細はリンク先、の形に圧縮して再実行すること)")

    # 置き換え: 既存 *.md を消して書き直す (蒸留層は生層からの派生物なので全置換でよい)。
    # ただし wipe は marker (.lispy-memory) がある = 洗脳が管理してきたディレクトリに限る。
    # LISPY_MEMORY_DIR が誤って一般ディレクトリ (~/notes 等) を指していた場合の誤削除防止。
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    marker = MEMORY_DIR / MEMORY_MARKER
    if any(MEMORY_DIR.rglob("*.md")) and not marker.exists():
        return (f"(brainwash: {MEMORY_DIR} に marker ({MEMORY_MARKER}) が無いのに .md がある — "
                f"洗脳管理外のディレクトリの可能性があるため wipe しない。 "
                f"LISPY_MEMORY_DIR を専用ディレクトリにするか、 管理下に置くなら "
                f"{marker} を手で作成すること)")
    marker.touch()
    for p in MEMORY_DIR.rglob("*.md"):
        p.unlink()
    for target, content in writes.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    payload = json.dumps({
        # 完全 id で記録する — _pick_sessions の session 別 watermark がこれを照合する
        "sessions": sids,
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
        f" (kept {len(kept)} / dropped {len(dropped)})",
    ]
    for d in dropped[:8]:
        lines.append(f"  [dropped] {d[:120]}")
    if skipped:
        lines.append(f"  [skipped paths] {', '.join(skipped)}")
    return "\n".join(lines)
