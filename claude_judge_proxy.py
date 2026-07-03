#!/usr/bin/env python3
"""claude_judge_proxy — サブスクの Claude (Claude Code headless) を lispy の judge に使う。

lispy の judge (define-gate / skill 審査 / judge-call / judge-done) は OpenAI 互換
client で JUDGE_BASE_URL を叩く。 サブスク (Pro/Max) の認証は API キーでなく OAuth
なので直接は使えない — 代わりにこの proxy が OpenAI 互換リクエストを受けて
`claude -p` (Claude Code CLI, サブスク認証) に委譲し、 chat.completions 形式で返す。

使い方:
    python3 claude_judge_proxy.py            # 127.0.0.1:8402 で待つ
    # .env:
    #   JUDGE_MODEL=opus
    #   JUDGE_BASE_URL=http://127.0.0.1:8402/v1
    #   JUDGE_API_KEY=subscription            (proxy は検証しない。 3 変数を揃えるためのダミー)

注意:
  - judge 用途 (単発・tool なし・非 stream) に限定した最小実装
  - 消費はサブスクの利用枠 (Claude Code と共有)。 auto-step の judge-done は round 毎に
    呼ぶので、 長い自走では消費が嵩む
  - claude CLI が非ゼロ exit なら 500 を返す → lispy 側は fail-closed (REJECT) に倒れる
"""
import json
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8402
TIMEOUT = 300  # judge は数百 token だが、 混雑時の余裕をみる


def _flatten(content) -> str:
    """OpenAI の content (str | [{type:text,...}]) を平文に。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


def _call_claude(model: str, system: str, prompt: str) -> str:
    cmd = ["claude", "-p", "--model", model]
    if system:
        cmd += ["--system-prompt", system]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"claude exit {r.returncode}").strip()[:500])
    return r.stdout.strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静かに (stderr 汚染しない)
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if "chat/completions" not in self.path:
            return self._json(404, {"error": {"message": f"unknown path: {self.path}"}})
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw)
        except Exception as e:
            return self._json(400, {"error": {"message": f"bad request: {e}"}})

        model = str(body.get("model") or "opus")
        sys_parts, user_parts = [], []
        for m in body.get("messages", []):
            text = _flatten(m.get("content", ""))
            if m.get("role") == "system":
                sys_parts.append(text)
            else:
                # judge は system+user のみの想定だが、 assistant 履歴が来ても平文で繋ぐ
                role = m.get("role", "user")
                user_parts.append(text if role == "user" else f"[{role}]\n{text}")

        try:
            out = _call_claude(model, "\n\n".join(sys_parts), "\n\n".join(user_parts))
        except Exception as e:
            return self._json(500, {"error": {"message": f"claude -p failed: {e}"}})

        self._json(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": out},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def do_GET(self):
        if "models" in self.path:
            return self._json(200, {"object": "list", "data": [
                {"id": "opus", "object": "model", "owned_by": "claude-subscription"},
                {"id": "sonnet", "object": "model", "owned_by": "claude-subscription"},
            ]})
        self._json(200, {"ok": True, "proxy": "claude_judge_proxy", "backend": "claude -p (subscription)"})


if __name__ == "__main__":
    print(f"claude_judge_proxy on http://{HOST}:{PORT}  (backend: claude -p, サブスク認証)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
