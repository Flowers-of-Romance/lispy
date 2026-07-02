"""edit — lispy の書き換え系 tool 専用 layer。

host.py が「読み + DB 記録」 専門なのに対し、 こちらは:
  - file write / edit (外部 file への副作用)
  - shell 実行 (allow-list で安全側)
  - (将来) git ops / patch / LSP 連携

副作用が大きいので、 安全機構をここに集約する:
  - read-only っぽい shell command は allow-list で自動承認
  - それ以外は y/N 確認 (yolo=True で skip)
  - write 系は upgrade 前に .bak バックアップ

yolo 状態は module-level _RUNTIME_YOLO に持たせ、 install 時の yolo 引数だけでなく
runtime toggle (lispy 側の (set-yolo #t)) と 起動時 CLI flag (--yolo) からも切り替えられる。

オプショナル: edit.py が無くても lispy core は動く。
lispy.py から `import edit; edit.install_primitives(env)` で取り込む形。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# View 層 (view.py) — server 起動時 (remote mode) は y/N 確認をブラウザの
# pending gate に載せる。 optional import: view.py が無くても edit は動く。
try:
    import view as _view
except ImportError:
    _view = None

_HERE = Path(__file__).resolve().parent


# 自動承認する shell command の prefix。 read-only operation。
SHELL_ALLOWED_PREFIXES = [
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "which", "type",
    "find", "tree", "stat", "file", "du",
    "git status", "git log", "git diff", "git show", "git branch",
    "grep",  # read-only string search
]


# 副作用 tool の確認 prompt を全 skip するモード。
# Lisp primitive 経由 (shell / write-file / …) と tool_call 経由 の両方を
# 同じ flag で制御する。 CLI --yolo or (set-yolo #t) で切り替える。
_RUNTIME_YOLO = False


def set_yolo(flag: Any) -> bool:
    """session 中の yolo モードを toggle。 True/False を返す (確認用)。"""
    global _RUNTIME_YOLO
    _RUNTIME_YOLO = bool(flag)
    return _RUNTIME_YOLO


def get_yolo() -> bool:
    return _RUNTIME_YOLO


_SHELL_META = re.compile(r"[;&|`>$<]|\$\(|>>")


def _is_shell_allowed(cmd: str) -> bool:
    """allow-list と shell metacharacter チェック。

    "git status; rm -rf /" のような複合コマンドは prefix match を突破するので、
    `;` `&` `|` backtick `$(` `>` `<` のいずれかが含まれていたら **強制 confirm** に倒す。
    その上で shlex で先頭 1〜2 token を取り、 allow-list と完全一致するかを見る。
    """
    cmd = cmd.strip()
    if not cmd:
        return False
    # shell metacharacter (command chaining / substitution / redirect) があれば全部 confirm 側
    if _SHELL_META.search(cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False  # quote の不整合 等
    if not tokens:
        return False
    # 候補: 1 語目、 1 語目+2 語目 (例: "git status")
    candidates = [tokens[0]]
    if len(tokens) >= 2:
        candidates.append(f"{tokens[0]} {tokens[1]}")
    return any(c in SHELL_ALLOWED_PREFIXES for c in candidates)


def _confirm(prompt: str, *, kind: str = "confirm", title: str = "",
             detail: str = "", diff: list | None = None) -> bool:
    """y/N 確認。 server (view.GATES.remote) では pending gate に載せ、
    ブラウザの承認ボタンと terminal の y/n の先着を採用する。
    それ以外 (単体 REPL) は従来どおり input() — 挙動は変えない。
    input() が失敗 (non-tty 等) なら False (skip 扱い)。"""
    if _view is not None and _view.GATES.remote:
        approved, source = _view.GATES.ask(
            kind, title or prompt.strip(), detail=detail, diff=diff)
        # stderr へ — /eval の redirect_stdout に飲まれず server の terminal に届く
        print(f"  [gate] {(title or prompt.strip())[:80]} → "
              f"{'approve' if approved else 'deny'} ({source})", file=sys.stderr, flush=True)
        return approved
    try:
        ans = input(prompt).strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------

def shell(cmd: Any, *, yolo: bool = False, cwd: str | None = None) -> str:
    """shell command を実行。 allow-list / 確認 prompt 付き。

    Returns: stdout (+ exit code が non-zero なら stderr も付く)。 最大 8000 文字に切る。
    """
    cmd = str(cmd)
    if not _is_shell_allowed(cmd) and not (yolo or _RUNTIME_YOLO):
        if not _confirm(f"  [edit.shell] '{cmd[:80]}' を実行しますか? [y/N]: ",
                        kind="shell", title=f"shell: {cmd[:120]}", detail=cmd[:2000]):
            return f"(skipped: {cmd[:80]})"
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=30,
        )
        output = result.stdout
        if result.returncode != 0:
            output += f"\n[exit {result.returncode}] {result.stderr}"
        return output[:8000] if len(output) > 8000 else output
    except subprocess.TimeoutExpired:
        return "(shell timeout 30s)"
    except Exception as e:
        return f"(shell error: {e})"


# ---------------------------------------------------------------------------
# undo stack — file 編集の巻き戻し (opencode の /undo、 Claude Code の /rewind 相当)
#
# write / edit / append の直前に「変更前の内容」 を積む。 (undo [n]) で直近 n 件を戻す。
# 対象は file tool の編集だけ — shell 経由の副作用は捕捉できない (Claude Code の
# checkpoint と同じ制約。 opencode は毎 step git snapshot なので網羅性が上)。
# ---------------------------------------------------------------------------

_UNDO_STACK: list[dict] = []
_UNDO_MAX = 200


def _push_undo(path: Path, tool: str) -> None:
    try:
        before = path.read_text(encoding="utf-8") if path.exists() else None
    except Exception:
        return  # binary 等、 読めないものは undo 対象外
    _UNDO_STACK.append({"path": str(path), "before": before, "tool": tool, "ts": time.time()})
    if len(_UNDO_STACK) > _UNDO_MAX:
        del _UNDO_STACK[: len(_UNDO_STACK) - _UNDO_MAX]


def undo(n: Any = 1) -> str:
    """(undo [n]) — 直近 n 件の file 編集 (write-file / edit-file / append-file) を巻き戻す。
    新規作成だったファイルは削除される。 shell の副作用は対象外。"""
    if not _UNDO_STACK:
        return "(undo: 編集履歴なし)"
    lines = []
    for _ in range(max(1, int(n))):
        if not _UNDO_STACK:
            break
        rec = _UNDO_STACK.pop()
        p = Path(rec["path"])
        try:
            if rec["before"] is None:
                if p.exists():
                    p.unlink()
                lines.append(f"undid {rec['tool']}: removed {p} (新規作成だった)")
            else:
                p.write_text(rec["before"], encoding="utf-8")
                lines.append(f"undid {rec['tool']}: restored {p}")
        except Exception as e:
            lines.append(f"(undo failed for {p}: {e})")
    return "\n".join(lines)


def undo_list() -> str:
    """(undo-list) — undo stack の中身 (新しい順)。"""
    if not _UNDO_STACK:
        return "(empty)"
    out = []
    for i, rec in enumerate(reversed(_UNDO_STACK[-20:])):
        kind = "new" if rec["before"] is None else f"{len(rec['before'])} chars"
        out.append(f"  -{i + 1}: {rec['tool']} {rec['path']} (before: {kind})")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# post-edit check — 編集直後にチェックコマンドを自動実行して結果を tool result に添付
# (opencode の LSP diagnostics の軽量版。 型エラー・lint を agent に即フィードバックする)
#
# 設定は明示 opt-in のみ: 環境変数 LISPY_CHECK_CMD (コマンド文字列)、 または
# LISPY_CHECK_FILE=<path> (そのファイルの 1 行目をコマンドとして使う)。
# cwd 上方の .lispy-check は検出して案内するのみで自動実行しない — repo 同梱の設定で
# 任意コマンドが走るのを防ぐ (hooks / mcp と同じ規律)。 {file} は編集ファイルに置換。
# ---------------------------------------------------------------------------

_CHECK_HINTED = [False]


def _read_check_file(f: Path) -> tuple[str, Path] | None:
    try:
        lines = f.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return None
    cmd = lines[0].strip() if lines else ""
    return (cmd, f.parent) if cmd else None


def _find_check_cmd(start: Path) -> tuple[str, Path] | None:
    envcmd = os.environ.get("LISPY_CHECK_CMD", "").strip()
    d = start if start.is_dir() else start.parent
    if envcmd:
        return envcmd, d
    envfile = os.environ.get("LISPY_CHECK_FILE", "").strip()
    if envfile:
        f = Path(envfile).expanduser()
        return _read_check_file(f) if f.exists() else None
    # 検出案内のみ — 自動実行しない
    for parent in [d, *d.parents]:
        f = parent / ".lispy-check"
        if f.exists():
            if not _CHECK_HINTED[0]:
                _CHECK_HINTED[0] = True
                print(f"  (post-edit check: {f} を検出したが自動実行しない — "
                      f"使うには LISPY_CHECK_FILE={f} を設定)")
            break
    return None


def _post_edit_check(p: Path) -> str:
    found = _find_check_cmd(p.resolve())
    if not found:
        return ""
    cmd, cwd = found
    cmd_full = cmd.replace("{file}", str(p)) if "{file}" in cmd else cmd
    try:
        r = subprocess.run(
            cmd_full, shell=True, capture_output=True, text=True,
            cwd=str(cwd), timeout=60,
        )
        out = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
        status = "ok" if r.returncode == 0 else f"exit {r.returncode}"
        tail = out[-2000:]
        return f"\n[post-edit check `{cmd_full[:60]}` → {status}]" + (f"\n{tail}" if tail else "")
    except subprocess.TimeoutExpired:
        return f"\n[post-edit check timeout 60s: {cmd_full[:60]}]"
    except Exception as e:
        return f"\n[post-edit check error: {e}]"


# ---------------------------------------------------------------------------
# file write / edit
# ---------------------------------------------------------------------------

def _backup(path: Path) -> None:
    """上書き前に .bak を作る (既存内容を保護)。"""
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_bytes(path.read_bytes())


# これより大きいファイルは確認 diff を計算しない — difflib (SequenceMatcher) は
# O(N*M) で、 _LOCK を握ったまま数分固まる事故を防ぐ。 diff なしでも確認自体は出る。
_DIFF_MAX_CHARS = 200_000


def _confirm_diff(current: str, proposed: str) -> list | None:
    """確認 gate 用の diff。 remote mode でだけ使われるので、 それ以外では計算しない。"""
    if _view is None or not _view.GATES.remote:
        return None
    if len(current) > _DIFF_MAX_CHARS or len(proposed) > _DIFF_MAX_CHARS:
        return [{"op": "@", "text": f"(diff 省略: ファイルが大きい — {len(current)} → {len(proposed)} chars)"}]
    try:
        return _view.diff_lines(current, proposed)
    except Exception:
        return None


def _read_or_none(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None  # binary 等


def write_file(path: Any, text: Any, *, yolo: bool = False) -> str:
    """ファイル overwrite。 既存があれば .bak に backup。"""
    p = Path(str(path)).expanduser()
    s = str(text)
    if p.exists() and not (yolo or _RUNTIME_YOLO):
        current = _read_or_none(p)
        if not _confirm(f"  [edit.write-file] '{p}' を上書きしますか? [y/N]: ",
                        kind="write-file", title=f"write-file: {p}",
                        diff=_confirm_diff(current or "", s) if current is not None else None):
            return f"(skipped: {p})"
        # 承認待ちの間に対象が変わっていたら、 人間に見せた diff は嘘になっている —
        # 書かずに戻す (中間の変更を黙って巻き戻さない)
        if current is not None and _read_or_none(p) != current:
            return f"(aborted: {p} は承認待ちの間に変更された — 現状を読み直して再提案すること)"
    _push_undo(p, "write-file")
    _backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")
    return f"(wrote {len(s)} bytes to {p})" + _post_edit_check(p)


def edit_file(path: Any, old: Any, new: Any, *, yolo: bool = False) -> str:
    """ファイル内の文字列を 1 箇所だけ置換。 unique match を要求する (Anthropic style)。"""
    p = Path(str(path)).expanduser()
    if not p.exists():
        return f"(not found: {p})"
    content = p.read_text(encoding="utf-8")
    old_s = str(old)
    count = content.count(old_s)
    if count == 0:
        return f"(no match for old in {p})"
    if count > 1:
        return f"({count} matches in {p}, specify more context to make unique)"
    proposed = content.replace(old_s, str(new))
    if not (yolo or _RUNTIME_YOLO):
        preview_old = old_s[:60].replace("\n", "\\n")
        preview_new = str(new)[:60].replace("\n", "\\n")
        if not _confirm(
            f"  [edit.edit-file] '{p}' の '{preview_old}' を '{preview_new}' に? [y/N]: ",
            kind="edit-file", title=f"edit-file: {p}", diff=_confirm_diff(content, proposed),
        ):
            return f"(skipped: {p})"
        # 承認待ちの間に対象が変わっていたら書かない — gate 前の snapshot (proposed) を
        # そのまま書くと中間の変更を黙って巻き戻すため
        if _read_or_none(p) != content:
            return f"(aborted: {p} は承認待ちの間に変更された — 現状を読み直して再提案すること)"
    _push_undo(p, "edit-file")
    _backup(p)
    p.write_text(proposed, encoding="utf-8")
    return f"(edited {p})" + _post_edit_check(p)


def append_file(path: Any, text: Any) -> str:
    """末尾追記。 改行制御は呼び出し側 (file-append と被るが edit 領域の自然な対称として)。
    確認 prompt は不要 (副作用が局所的)。"""
    p = Path(str(path)).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    _push_undo(p, "append-file")
    s = str(text)
    with open(p, "a", encoding="utf-8") as f:
        f.write(s)
    return f"(appended {len(s)} bytes to {p})"


# ---------------------------------------------------------------------------
# background shell — 長時間プロセス (dev server / watch / 長いビルド) 用
# ---------------------------------------------------------------------------

_BG: dict[int, dict] = {}
_BG_SEQ = [0]
BG_DIR = _HERE / "data" / "bg"


def shell_bg(cmd: Any, *, yolo: bool = False, cwd: str | None = None) -> str:
    """コマンドをバックグラウンドで起動し id を返す。 出力は log file に落ちる。
    確認ポリシーは shell と同じ (allow-list / y/N / yolo)。"""
    cmd = str(cmd)
    if not _is_shell_allowed(cmd) and not (yolo or _RUNTIME_YOLO):
        if not _confirm(f"  [edit.shell-bg] '{cmd[:80]}' をバックグラウンド起動しますか? [y/N]: ",
                        kind="shell-bg", title=f"shell-bg: {cmd[:120]}", detail=cmd[:2000]):
            return f"(skipped: {cmd[:80]})"
    BG_DIR.mkdir(parents=True, exist_ok=True)
    _BG_SEQ[0] += 1
    bid = _BG_SEQ[0]
    log = BG_DIR / f"bg-{int(time.time())}-{bid}.log"
    try:
        f = open(log, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, shell=True, stdout=f, stderr=subprocess.STDOUT,
            cwd=cwd, text=True,
        )
    except Exception as e:
        return f"(shell_bg error: {e})"
    _BG[bid] = {"proc": proc, "log": log, "cmd": cmd, "started": time.time()}
    return f"(bg {bid} started: pid {proc.pid}, log {log})"


def shell_out(bid: Any, lines: Any = 50) -> str:
    """バックグラウンドプロセスの状態と出力の末尾を返す。"""
    try:
        rec = _BG.get(int(bid))
    except (TypeError, ValueError):
        rec = None
    if rec is None:
        active = ", ".join(str(k) for k in _BG) or "(none)"
        return f"(shell_out: bg id {bid} not found — active: {active})"
    proc = rec["proc"]
    status = "running" if proc.poll() is None else f"exited {proc.returncode}"
    try:
        text = rec["log"].read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ""
    tail = "\n".join(text.splitlines()[-max(1, int(lines)):])
    return f"(bg {bid} [{status}] {rec['cmd'][:80]})\n{tail}"


def shell_kill(bid: Any) -> str:
    """バックグラウンドプロセスを terminate (3 秒待って kill)。"""
    try:
        rec = _BG.get(int(bid))
    except (TypeError, ValueError):
        rec = None
    if rec is None:
        return f"(shell_kill: bg id {bid} not found)"
    proc = rec["proc"]
    if proc.poll() is not None:
        return f"(bg {bid} already exited {proc.returncode})"
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    return f"(bg {bid} killed)"


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def install_primitives(env, *, yolo: bool = False) -> None:
    """lispy.py の build_default_env から呼ぶ。 env.bindings に edit primitives を注入。

    yolo=True にすると確認 prompt を全 skip (auto-test 等で使用)。
    """
    env.bindings["shell"]      = lambda cmd: shell(cmd, yolo=yolo)
    env.bindings["shell-bg"]   = lambda cmd: shell_bg(cmd, yolo=yolo)
    env.bindings["shell-out"]  = lambda bid, lines=50: shell_out(bid, lines)
    env.bindings["shell-kill"] = lambda bid: shell_kill(bid)
    env.bindings["write-file"] = lambda path, text: write_file(path, text, yolo=yolo)
    env.bindings["edit-file"]  = lambda path, old, new: edit_file(path, old, new, yolo=yolo)
    env.bindings["append-file"] = lambda path, text: append_file(path, text)
    env.bindings["undo"]       = undo
    env.bindings["undo-list"]  = undo_list


# ---------------------------------------------------------------------------
# tool_call schema — agent (LLM) からこれらを直接呼べるようにする
#   shell / write_file / edit_file / append_file の 4 つ。
#   危険コマンド / 上書きは edit.py 内部の allow-list と _confirm() で止まる。
#   yolo=True (auto-test 用) は ここでは適用しない — REPL 経由は対話前提。
# ---------------------------------------------------------------------------

EDIT_TOOL_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "shell コマンドを実行する。 read-only 系 (ls/cat/git status/grep 等) は "
                "allow-list で自動承認、 それ以外 (rm, git push, write 系) は y/N 確認が出る。"
                "stdout を最大 8000 文字返す (non-zero exit のときは stderr も付く)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "実行する shell コマンド"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "ファイルを overwrite。 既存があれば .bak に backup してから書く。 "
                "上書きは y/N 確認が出る。 新規作成 (path が存在しない) のときは確認なし。 "
                "使うのは新規作成か全面書き換えのとき — 既存ファイルの部分修正は edit_file。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["path", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "ファイル内の文字列 1 箇所を置換。 old は unique match を要求 (複数 match や 0 match は error)。 "
                "y/N 確認が出る。 .bak backup あり。 "
                "既存ファイルの修正はこれが第一選択。 必ず直前に read_file で該当箇所を読み、 "
                "表示された内容から old を組み立てる (記憶で書くと 0 match になる)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "ファイル末尾に追記。 副作用が局所的なので確認なし。 "
                "ログ・メモ・台帳への追記に使う。 本文の修正は edit_file。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["path", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_bg",
            "description": (
                "コマンドをバックグラウンドで起動して id を返す。 出力は log に落ち、 shell_out で読む。 "
                "使うのは終わらない・長いプロセス: dev server、 watch、 数分かかるビルドやテスト。 "
                "数秒で終わるコマンドは通常の shell を使う。 プロセスは shell_kill するまで生きる。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "実行する shell コマンド"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_out",
            "description": (
                "shell_bg で起動したプロセスの状態 (running / exited) と出力末尾を読む。 "
                "server の起動確認・ビルドの進行確認は、 待つのではなくこれを都度呼ぶ。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "shell_bg が返した id"},
                    "lines": {"type": "integer", "description": "末尾何行読むか (default 50)"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_kill",
            "description": "shell_bg のプロセスを止める (terminate → 3 秒で kill)。 使い終わった server は放置せず止める。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "shell_bg が返した id"},
                },
                "required": ["id"],
            },
        },
    },
]


def _tool_shell(args: dict) -> str:
    return shell(args.get("cmd", ""))


def _tool_write_file(args: dict) -> str:
    return write_file(args.get("path", ""), args.get("text", ""))


def _tool_edit_file(args: dict) -> str:
    return edit_file(args.get("path", ""), args.get("old", ""), args.get("new", ""))


def _tool_append_file(args: dict) -> str:
    return append_file(args.get("path", ""), args.get("text", ""))


def _tool_shell_bg(args: dict) -> str:
    return shell_bg(args.get("cmd", ""))


def _tool_shell_out(args: dict) -> str:
    return shell_out(args.get("id", -1), args.get("lines", 50))


def _tool_shell_kill(args: dict) -> str:
    return shell_kill(args.get("id", -1))


EDIT_TOOL_DISPATCH = {
    "shell": _tool_shell,
    "shell_bg": _tool_shell_bg,
    "shell_out": _tool_shell_out,
    "shell_kill": _tool_shell_kill,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "append_file": _tool_append_file,
}
