# duet — 2つの AI モデルが作り、ルールはハーネスが検証する

> このページは英語 README から訳した短い入門です。**正式な内容は[英語の README](../../README.md)** です。すべての実行記録は [docs/QA.md](../QA.md) にあります。

AI エージェントが1体だけだと、自分の宿題を自分で採点して「合格」と報告します。duet は**2つ目のモデル**を加え、**テストをハーネス自身が実行し**、各ワークフローにルールを課します。そのルールは、どちらかのモデルの自己申告ではなく、実際に起きたことに照らして検証されます。

## インストール

```bash
curl -fsSL https://raw.githubusercontent.com/shubharya-os/duet/main/install.sh | sh
```

または Claude Code の中でプラグインとして：

```
/plugin marketplace add shubharya-os/duet
/plugin install duet@duet
```

手持ちの Claude と ChatGPT のサブスクリプションをそのまま使います — **API キーは不要**。サブスクリプションが1つでも動きます（例: `--pair claude:opus+claude:sonnet`）。

## 9つのコマンド、それぞれに検証されるルール

```bash
duet build "家計簿の Web アプリ"         # ゼロから: 受け入れ基準 → テスト → コード
duet fix "カンマを含む行がエクスポートで消える" # テストは元のコードで失敗し、修正後に通ること
duet add "report に --json オプション"   # その機能がないと失敗するテストが必要
duet refactor "parser.py を2つに分割"     # 既存のテストは1バイトも変えてはいけない
duet plan "ストレージを Postgres に移行"  # 議論の末、変更してよいのは PLAN.md だけ
duet review                               # 別のモデルが diff を読み取り専用でレビュー
duet make "…" --like URL                  # 参考アプリを調査し、構築し、同等性を採点、クリーンルーム
duet jury                                 # 模擬ユーザー・顧客・投資家・審査員。すべての指摘は製品を引用
duet page check index.html                # Web サイトのゲート：デスクトップとスマホ幅での表示崩れを検出
```

`fix` は完成したテストを**元のコードのスナップショット上で再実行**します。バグのあるコードでもテストが通るなら、そのテストはバグを捉えていないので、両者の承認は取り消されます。

## 実績

一文と空のディレクトリだけから、duet は家計簿 Web アプリ（シングルページ UI、JSON API、SQLite、CSV 出力）を **35 分**で構築しました。その後、手作業で攻撃しました: SQL インジェクションはただの文字列として保存され、保存型 XSS は実際のブラウザでテキストとして表示され、CSV 数式インジェクションは無害化され、`0.1 + 0.2` の合計はちょうど `0.30` でした。

## デフォルトにする

```bash
duet skill default    # 元に戻す: duet skill undefault
```

以後、Claude Code・Codex・Gemini CLI・Antigravity・OpenCode は、実際のコード変更（バグ修正・機能追加・リファクタリング）を duet に任せ、質問や1行の修正はそのまま直接行います。
