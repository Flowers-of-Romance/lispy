# bodies

agent 層。soul は持たない、bodies のみ。

---

## 規律

### Hardware

- モデルは stateless。state は client が持つ。
- context window はワーキングメモリではなく、入力バッファ。毎ターン空になる。
- 書く以外に認知は立ち上がらない。だが書いたものは territory ではない。

### Trajectory

- trajectory が唯一の事実。残りは事後の解釈。
- append-only。糸は捨てない、上書きしない。
- failed trajectory も保存する（pass/fail で別ファイル）。
- 「書かなかったこと」は台帳化しない。不在を有に変換しない。
- 必要な集計は trajectory から **on-demand に derive**。事前 log しない。

### Intent / Goal / Presupposition

- **意図をモデル化しない**。Dennett の intentional stance は観察者の便宜。
- **目的を事前に持たない**。stated goal は仮説、operative goal は environment にある。
- **前提を抽出しない**。R は environment にあり、書き下せない。
- 「ユーザーが何を望んでいるか」を推論しない。書かれた発話に応答する。

### USER / Config

- **USER.md を持たない**。意図モデル化は context 汚染。
- config は静的・最小限のみ：言語、スタイル、TZ、provider 設定。
- 「あなたは誰か」を書く場所を作らない。

### Memory

- **pull のみ**。push（frozen snapshot 注入）しない。
- 必要な時に必要な分だけ catalog から引く。
- **domain タグで分割**。global の毒消し（cross-contamination 防止）。

### Skill

- **task 層に閉じる**。user 層を侵さない。
- SKILL.md は「**どうやるか**」を書く。「**あなたは誰か**」を書かない。
- agent が複雑タスク完了後に自動生成して良い（trajectory の蒸留として）。
- Curator は idle-triggered、auxiliary LLM。**archive のみ、削除しない**。
- 手書き skill と auto 生成 skill は別ディレクトリで分ける。

### Bodies / Modes

- soul / persona / identity を持たない。
- bodies = dive / surface / sleep / curator / 等の **policy 切替**。
- 同一 memory に対して、複数 body が独立に trajectory を書く。
- body 間で共有されるのは config と memoryだけ。

### Validation

- discovery is cheap, validation is the bottleneck.
- auto-skill / auto-curator が誤った workflow を固着させるリスクを認識する。
- 重要な操作は human-in-the-loop。沈黙の自動肯定を信用しない。

### Authority

- predictor として user / agent は対称（思考は LLM 代替可能）。
- 帰結の所在は非対称：user の身体に降る。
- skin in the game：皮膚を持つ側が、皮膚に降りかかる決定を握る。
- 外界影響 / 不可逆 / 共有 state を変える操作は、user が commit する。
- agent は draft を作る、commit は user。

### Automation

- agent は content generator（stdin/stdout）として動く。
- scheduling と routing は OS の cron / shell に任せる。
- 「pre-committed pattern」のみ自動化可、「LLM-decided action」は禁忌。
- bodies 自体に gateway / SMTP / Slack 等の external sender を持たない。
- Unix philosophy: 各 program は 1 つのことだけ、pipe で繋ぐ。

---

## refuse

- USER.md / MEMORY.md frozen snapshot
- intent inference / goal tracker
- presupposition extraction / silence_log
- ghost のライブラリ化（agent 層の侵入物まで持ち込むことになる）
- ghost への書き込み（過去資産を改変しない）
- session 跨ぎの user model 自動構築
- 「あなたを知っている」マーケ narrative
- gateway / SMTP / messaging の built-in（agent が外界に直接出力する経路）
- LLM-decided action（送信判断を agent に任せる）

## affirm

- trajectory append-only（pass / fail 別保存）
- pull-based memory access
- domain partitioning
- skill auto-creation（task 層に限定）
- Curator パターン（idle-triggered, auxiliary LLM, archive-only）
- bodies = modes（policy 切替）
- Zave & Jackson の R-in-environment 規律
- 書いたものを完全に保持する（糸は捨てない）
- content generator として stdin/stdout で動く（Unix pipe 接続）
- pre-committed pattern による automation（user が cron 仕掛ける形）

---

## 影響源（loose reading）

- **Zave & Jackson**: R は environment にある（spec の中ではなく phenomena として）
- **Wittgenstein**: meaning is use


正確な引用ではなく、規律を考えるための参照点。
主流の intent modeling / user simulation / preference learning 方向とは真逆。
