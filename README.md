# ds4ds4 — 認知の考古学

trajectory を append-only で保存し、必要なときに掘り、並べる。
解釈・意図モデル化は **しない**。骨を組み立てて人を作らない。


---

## 観察された pattern

固定した規律ではなく、これまで alive を保てた配置の覚え書き。
書いた瞬間 rule になる。rule で測り始めると感受性が落ちる。
壊しても良い。ただし「なぜこの配置だったか」を見失わない範囲で。

### 層 (trajectory)

- trajectory = 地層。append-only、上書きしない、失敗も残す。
- hook (UserPromptSubmit / Stop) 経由と chat 経由の turn は同じ ledger に積む。
- meta 操作 (sleep / skill 編集 / automint) は別テーブル `meta_events` に書く
  — **地層と札を混ぜない**。札を地層から検索すると考古学が成立しない。

### 発掘 (pull)

- pull only。push (frozen snapshot を context に押し込む) しない。
- query が来たときに掘る。事前 derive しない。
- domain タグで層を分け cross-contamination を防ぐ。

### 並べる (interpret しない)

- `recall`: 関連 turn を 1 行 snippet で並べる (ピンポイント試掘)
- `recall_session`: 1 session を時系列で取る (トレンチ)
- `cross`: 同じ scope の session を構造ラベル付きで横並びにする
- どれも tool は構造を読み上げるだけ。「これが面白い」とは言わない。
  museum の札 (鉄製、長さ 30cm) は書く、「時代を象徴する一品」とは書かない。

### モデル化を避ける

- USER.md / 「あなたは誰か」を書く場所を持たない。
- 意図 / 目的 / 前提を推論しない。書かれた発話に応答する。


### 帰結は user に降る

- skin in the game: 不可逆操作は user が commit。
- harness は draft を作る、commit は user。
- 外界影響 / 共有 state を変える操作は harness から発火しない (gateway / SMTP 等 built-in 不要)。

---

## CLI

```
ds4ds4 chat                対話モード (deepseek で think:False、recall 経由で過去を pull)
ds4ds4 record-turn         hook handler (Claude Code の UserPromptSubmit / Stop で起動)
ds4ds4 list                session 一覧
ds4ds4 search QUERY        FTS5 / trigram で turn / session を検索
ds4ds4 cross QUERY         scope query → session 横断、構造ラベル付きで並べる
ds4ds4 events              meta-event ledger (sleep / skill 操作 等)
ds4ds4 dump                DB → 日付別 md を再生成
ds4ds4 sleep               未要約 session に title/summary、tool 多い session は automint
ds4ds4 domain ...          session に domain タグ
ds4ds4 skill list/show/new/archive   skill (manual + auto) の手動発掘道具
```

`ds4ds4 chat` 中の tool: `current_time / list_dir / read_file / glob / grep / shell / recall / recall_session`。

---

## refuse

- USER.md / MEMORY.md の frozen snapshot
- intent inference / goal tracker / presupposition extraction
- session 跨ぎの user model 自動構築
- LLM-decided な不可逆 action (送信判断を LLM ループに任せる)
- gateway / SMTP / messaging の built-in
- 「あなたを知っている」マーケ narrative
- skill umbrella の自動 consolidation (= 過去 trace の再解釈ループ)

## affirm

- trajectory append-only (失敗も残す)
- pull-based memory access
- domain partitioning
- meta-event は別 ledger
- 発掘道具は薄く保つ (recall / cross / search / dump)
- skill は手動編集の対象
- content generator として stdin/stdout で動く (Unix pipe 接続)
- pre-committed pattern による automation (cron は user が仕掛ける)

---

## 影響源 (loose reading)

- **archaeology**: 骨は人を語らない。配置から見る人の中に立ち上がる。
  考古学者は「これは悲しい人だった」と書かない。「鉄欠乏が見える」と置く。
- **Polanyi**: tacit knowledge は articulation で消える。残るのは痕跡。
- **Christopher Alexander**: QWAN (無名の質) は規則化で死ぬ。
  pattern は「ここで alive を保てた配置」の記述であり、強制でも禁止でもない。
- **Zave & Jackson**: R は environment にある (spec の中ではなく phenomena として)。
- **Wittgenstein**: meaning is use。

正確な引用ではなく、何が alive で何が dead かを感じるための参照点。
