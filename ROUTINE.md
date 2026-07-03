# ROUTINE — AIトレンドダイジェスト生成手順（cloud agent 用）

このリポジトリだけを checkout したゼロコンテキストの状態で、朝夕2回このルーティンを実行する。
あなた（LLM）の仕事は **要約テキストの執筆のみ**。取得・日付窓計算・XML 生成・検証・剪定は
すべて `scripts/make_digest.py` が決定的に処理する。手順を逸脱しないこと。

## 実行手順

### ① 収集（決定的処理・そのまま実行）

```bash
python3 scripts/make_digest.py collect
```

- 4つの X List（S3 の RSS）を取得し、前回ダイジェスト以降の新規投稿を `digest_work/input.json` に集約する。
- ネットワーク障害や投稿数不足のフィードは `skip=true` になる（プロセスは失敗しない）。

### ② 入力を読む

`digest_work/input.json` を読む。構造:

```jsonc
{
  "generated_at": "ISO8601",
  "feeds": [
    {
      "slug": "ai-tech",
      "name": "AI・Tech",
      "skip": false,              // true のフィードは要約しない
      "reason": null,             // skip 理由（取得失敗 / 投稿数不足）
      "post_count": 23,
      "posts": [
        {
          "author": "username",   // @ は付いていない。引用時は @username と表記
          "text": "本文全文（[like=.. repost=..] は除去済み。t.co 短縮 URL は残存）",
          "likes": 12,
          "reposts": 3,
          "url": "https://x.com/username/status/....",  // 元ポストへのリンク
          "at": "ISO8601(JST)"
        }
      ]
    }
  ]
}
```

### ③ 要約を執筆し `digest_work/summaries.json` に保存

`skip=false` の **各フィードについて** 日本語トレンドダイジェストを執筆する。
保存形式は次のとおり（`skip=true` の slug は含めない。全フィード skip の場合は下記「異常時」参照）:

```json
{
  "ai-tech": { "title": "...", "html": "..." },
  "toshi-trade": { "title": "...", "html": "..." }
}
```

- `title`: `【<カテゴリ名>】<その回の最重要トピック一言>（M/D 朝|朝夕どちらか）` 形式。
  例: `【AI・Tech】新型オープンモデルが軒並みベンチ更新（7/3 朝）`。午前実行なら「朝」、午後実行なら「夕」。
  カテゴリ名は input.json の `name` を使う。
- `html`: CDATA に格納される本文。空文字は不可（render が exit 1 する）。

#### 要約品質指針

- **主要トピックを3〜5個**、見出し（`<h3>`）付きで整理する。
- 各トピックに **象徴的なポストの引用**（`@author` 名と要旨）と **元ポストへのリンク**（`url`）を添える。
- 各トピックに **「なぜ重要か」を1行**（`<p>`）添える。
- **エンゲージメント（likes / reposts）が高いポストを優先** して取り上げる。
- 使う HTML は `<h3> <ul> <li> <a> <p> <blockquote>` 程度のシンプルな構成にとどめる。
- リンクは `<a href="URL">@author</a>` の形。引用要旨は `<blockquote>` を使ってよい。

#### html の構成例（1トピック分）

```html
<h3>1. 新型オープンモデルがベンチ更新</h3>
<ul>
  <li><a href="https://x.com/example/status/123">@example</a>: 新モデルが主要ベンチで既存を上回ったと報告（like 240 / repost 55）</li>
</ul>
<p>なぜ重要か: オープンモデルの性能競争が加速し、自前運用の選択肢が広がるため。</p>
```

### ④ レンダリング（決定的処理・そのまま実行）

```bash
python3 scripts/make_digest.py render
```

- `digest_work/summaries.json` を読み、各 slug の `digest/<slug>.xml` に item を追加する。
- 同じ回（同日・同 am/pm）の item は **置換**（冪等）。件数超過は古い順に自動剪定。
- 生成 XML は書き込み前に検証される。検証失敗や html 空文字なら非ゼロ終了し、ファイルは書き換えない。

### ⑤ コミット & プッシュ

```bash
git add digest/
git commit -m "chore(digest): AIトレンドダイジェスト更新"
git push
```

- **`digest_work/` はコミットしない**（`.gitignore` 済み）。作業用中間ファイルであり配信対象ではない。
- `digest/*.xml` のみをコミット対象にする。

## 異常時の扱い

- **全フィードが `skip=true`** の場合: `summaries.json` を書かず、`render` も実行せず、コミットもしない（何もせず終了）。
- 一部フィードのみ skip の場合: skip 以外のフィードだけ要約して通常どおり ④⑤ を実行する。
- `render` が非ゼロ終了した場合: 原因（stderr）を確認し、修正できなければコミットしない。
