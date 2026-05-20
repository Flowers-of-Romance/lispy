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

import subprocess
from pathlib import Path
from typing import Any


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


def _is_shell_allowed(cmd: str) -> bool:
    cmd = cmd.strip()
    for prefix in SHELL_ALLOWED_PREFIXES:
        if cmd == prefix or cmd.startswith(prefix + " "):
            return True
    return False


def _confirm(prompt: str) -> bool:
    """REPL 内での y/N 確認。 input() が失敗 (non-tty 等) なら False (skip 扱い)。"""
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
        if not _confirm(f"  [edit.shell] '{cmd[:80]}' を実行しますか? [y/N]: "):
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
# file write / edit
# ---------------------------------------------------------------------------

def _backup(path: Path) -> None:
    """上書き前に .bak を作る (既存内容を保護)。"""
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_bytes(path.read_bytes())


def write_file(path: Any, text: Any, *, yolo: bool = False) -> str:
    """ファイル overwrite。 既存があれば .bak に backup。"""
    p = Path(str(path)).expanduser()
    if p.exists() and not (yolo or _RUNTIME_YOLO):
        if not _confirm(f"  [edit.write-file] '{p}' を上書きしますか? [y/N]: "):
            return f"(skipped: {p})"
    _backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    s = str(text)
    p.write_text(s, encoding="utf-8")
    return f"(wrote {len(s)} bytes to {p})"


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
    if not (yolo or _RUNTIME_YOLO):
        preview_old = old_s[:60].replace("\n", "\\n")
        preview_new = str(new)[:60].replace("\n", "\\n")
        if not _confirm(
            f"  [edit.edit-file] '{p}' の '{preview_old}' を '{preview_new}' に? [y/N]: "
        ):
            return f"(skipped: {p})"
    _backup(p)
    p.write_text(content.replace(old_s, str(new)), encoding="utf-8")
    return f"(edited {p})"


def append_file(path: Any, text: Any) -> str:
    """末尾追記。 改行制御は呼び出し側 (file-append と被るが edit 領域の自然な対称として)。
    確認 prompt は不要 (副作用が局所的)。"""
    p = Path(str(path)).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    s = str(text)
    with open(p, "a", encoding="utf-8") as f:
        f.write(s)
    return f"(appended {len(s)} bytes to {p})"


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def install_primitives(env, *, yolo: bool = False) -> None:
    """lispy.py の build_default_env から呼ぶ。 env.bindings に edit primitives を注入。

    yolo=True にすると確認 prompt を全 skip (auto-test 等で使用)。
    """
    env.bindings["shell"]      = lambda cmd: shell(cmd, yolo=yolo)
    env.bindings["write-file"] = lambda path, text: write_file(path, text, yolo=yolo)
    env.bindings["edit-file"]  = lambda path, old, new: edit_file(path, old, new, yolo=yolo)
    env.bindings["append-file"] = lambda path, text: append_file(path, text)


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
                "上書きは y/N 確認が出る。 新規作成 (path が存在しない) のときは確認なし。"
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
                "y/N 確認が出る。 .bak backup あり。"
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
            "description": "ファイル末尾に追記。 副作用が局所的なので確認なし。",
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
]


def _tool_shell(args: dict) -> str:
    return shell(args.get("cmd", ""))


def _tool_write_file(args: dict) -> str:
    return write_file(args.get("path", ""), args.get("text", ""))


def _tool_edit_file(args: dict) -> str:
    return edit_file(args.get("path", ""), args.get("old", ""), args.get("new", ""))


def _tool_append_file(args: dict) -> str:
    return append_file(args.get("path", ""), args.get("text", ""))


EDIT_TOOL_DISPATCH = {
    "shell": _tool_shell,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "append_file": _tool_append_file,
}
