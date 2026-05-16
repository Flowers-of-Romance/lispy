---
name: bodies-session-explore
description: bodies の session / trajectory を探索する手順 (list / search / dump の使い分け)
created: 2026-05-16T04:30:00
source: manual
tags: bodies,navigation,search
---

# bodies-session-explore

## When

ユーザーが bodies に貯まった session や対話を **探索する** とき。
特に「最近何話した？」「あの話どこ？」「あの session 何？」のような検索系の質問。

## How

### 1. 一覧で見る
```bash
bodies list                 # 直近 30 session、新しい順
bodies list --limit 100     # もっと多く
```
出力: 開始時刻 / session_id(16字) / turn 数 / domain / title

### 2. 全文検索 (FTS5)
```bash
bodies search "keyword"               # 両方 (turns + sessions)、ASCII 単語境界
bodies search "keyword" --turns       # 発話本体のみ
bodies search "keyword" --sessions    # title / summary のみ
bodies search "keyword" --limit 20    # 件数
```

### 3. 日本語の部分一致 (trigram、3字以上)
```bash
bodies search "の違い" --tri          # CJK substring
bodies search "デプロイ" --tri
```
trigram は **3 字未満不可** (FTS5 仕様)。"AI" や "の" は当たらない。

### 4. 日付別 md を読む
```bash
ls /Users/jm/bodies/data/turns/                # 日付ごと
cat /Users/jm/bodies/data/turns/2026-05-16.md
```
session 切替は `---` 区切り、各 session は animal 絵文字でマーク (deterministic hash of session_id)。

### 5. 特定 session に絞る
```bash
sqlite3 /Users/jm/bodies/bodies.db "SELECT role, content FROM turns WHERE session_id LIKE 'PREFIX%' ORDER BY ts" | head -50
```
prefix は 8-16 字あれば一意。

### 6. domain tag で絞る
```bash
bodies domain                        # tag 一覧 + 件数
bodies domain SID DOMAIN             # tag 付ける
sqlite3 bodies.db "SELECT id, title FROM sessions WHERE domain='ghost'"
```

## Pitfalls

- **trigram の最小**: 3 字未満は通らない。短いキーワードは default tokenizer の方が当たる
- **bodies.db を直接 write しない**: SELECT は OK、INSERT/UPDATE/DELETE は規律違反 (糸を捨てる行為)
- **session_id の動物絵文字**: 同じ session_id は常に同じ動物だが、session 切れると別 session 別動物
- **session の終了時刻 (ended_at)**: chat なら自動更新、record-turn 経由 (hook) なら NULL のまま
- **検索ヒットの偏り**: 最近の session ほど多くヒットしがち。古い trajectory も `--limit` 増やせば見える

## See also

- `bodies sleep` で未要約 session に title/summary を生成 (要 LLM_API_KEY)
- `bodies dump` で DB から日付別 md を再生成
