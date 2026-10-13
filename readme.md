# generate_image

CSVファイルに記述したシーン情報（プロンプト・シード・ファイル名）をもとに、ComfyUI の API を使って画像を一括生成するスクリプトです。

## 必要環境

- Python 3.8 以上
- 起動済みの ComfyUI（デフォルト: `localhost:8188`）
- Python パッケージ
  - `Pillow`
  - `websocket-client`
  - `pytest`（テストを実行する場合のみ）

## インストール

### どこからでも `generate_image` コマンドで使う（推奨）

[uv](https://docs.astral.sh/uv/) を使い、プロジェクトのルートで以下を実行します。
専用の仮想環境に依存パッケージが入り、`generate_image` コマンドが `%USERPROFILE%\.local\bin` に配置されます。
編集可能モード（`-e`）なので、`generate_images.py` を修正すると即座に反映されます。

```
uv tool install -e .
uv tool update-shell        # 初回のみ。.local\bin を PATH に追加（ターミナルの再起動が必要）
```

`pyproject.toml` の依存パッケージやコマンド定義を変更した場合は、`uv tool install -e . --reinstall` で再インストールしてください。
アンインストールは `uv tool uninstall generate-image` です。

### 開発用（プロジェクトの仮想環境にインストール）

```
pip install -e .            # 本体のみ
pip install -e ".[test]"    # テスト用の pytest も含める
```

インストールせずに使う場合は、依存パッケージだけを入れてください。

```
pip install pillow websocket-client pytest
```

## ファイル構成

| ファイル | 説明 |
| --- | --- |
| `generate_images.py` | 画像一括生成スクリプト本体 |
| `pyproject.toml` | パッケージ設定（`uv tool install -e .` / `pip install -e .` 用） |
| `*.json` | ComfyUI のワークフロー（API形式でエクスポートしたもの） |
| `test_scenes.csv` | シーン情報CSVのサンプル |
| `output/` | 生成画像の保存先（実行したフォルダに作成。なければ自動作成） |
| `tests/` | テスト一式（偽の ComfyUI サーバーを含む） |

## 使い方

```
python generate_images.py <CSVファイルのパス> [オプション]
```

インストール済みの場合は、どのフォルダからでも `generate_image` コマンドで実行できます（オプションは同じです）。

```
generate_image <CSVファイルのパス> [オプション]
```

例:

```
python generate_images.py test_scenes.csv -w Qwen-Image-2512_16_9.json
python generate_images.py test_scenes.csv -w ernie_image_turbo.json --skip-existing
python generate_images.py test_scenes.csv -w qwen_image_2_1_t2i.json
python generate_images.py test_scenes.csv
```

### ワークフローの選択

`-w` を指定しない場合は、カレントフォルダにある JSON ファイルの一覧を表示し、番号で選択します。

```
📋 ワークフローを選択してください (E:\work\generate_image)
   1: ernie_image_turbo.json
   2: Qwen-Image-2512_16_9.json
   3: Qwen-Image-2512_4_3.json
   4: qwen_image_2_1_t2i.json
番号を入力 (1-4): 4
```

- 範囲外の番号や数字以外を入力した場合は、再入力を求めます。
- カレントフォルダに JSON ファイルがない場合や、入力が終了した場合（パイプ入力の終端など）は、終了コード `2` で終了します。
- ワークフローではない JSON を選んだ場合は、読み込み時にエラーになります。

### オプション

| オプション | 説明 | 既定値 |
| --- | --- | --- |
| `-w`, `--workflow` | ワークフローJSON（API形式） | なし（カレントフォルダの JSON から番号で選択） |
| `-o`, `--output` | 画像の保存先フォルダ | 実行したフォルダの `output` |
| `-s`, `--server` | ComfyUI のアドレス | `localhost:8188` |
| `-t`, `--timeout` | 1シーンあたりのタイムアウト（秒） | `300` |
| `--skip-existing` | 出力ファイルが既にあるシーンをスキップ | 無効（上書き） |
| `--prompt-node` | プロンプトを書き換えるノードIDを指定 | 自動検出 |
| `--output-node` | 画像を取得する出力ノードIDを指定 | 保存画像をすべて取得 |
| `-v`, `--verbose` | WebSocket メッセージなどの詳細ログを表示 | 無効 |

ワークフローやCSVの相対パスは、実行時のカレントフォルダ基準で解釈されます。既定の保存先はスクリプトの場所が基準なので、どのフォルダから実行しても動作します。

### 終了コード

| コード | 意味 |
| --- | --- |
| `0` | すべてのシーンが成功（またはスキップ） |
| `1` | 失敗したシーンがある |
| `2` | 準備段階のエラー（ワークフロー・CSVが読めない、ComfyUI に接続できない等） |
| `130` | Ctrl+C で中断 |

## CSVフォーマット

1行目はヘッダ行で、以下のカラムが必須です。文字コードは UTF-8（Excel で保存した BOM 付きも可）。

| カラム | 説明 |
| --- | --- |
| `scene_num` | シーン番号（ログ表示用） |
| `scene_name` | シーン名（ログ表示用） |
| `seed` | 生成に使用するシード値（整数） |
| `file_name` | 出力ファイル名（例: `scene_01_opener.jpg`） |
| `positive_prompt` | ポジティブプロンプト |

例:

```csv
scene_num,scene_name,seed,file_name,positive_prompt
1,オープニング,1001999,scene_01_opener.jpg,"soft morning light filtering through cherry blossom branches, ..."
```

- `seed` が整数でない行や `positive_prompt` が空の行は、警告を表示してスキップします。
- `file_name` が重複している場合は警告を表示します（後のシーンで上書きされます）。

## 出力ファイル名

- 生成画像は保存先フォルダに、CSV の `file_name` の名前で保存されます。
- 保存形式は拡張子から判定されます（`.png` / `.jpg` / `.jpeg` / `.webp` / `.bmp` / `.gif` / `.tif` / `.tiff`）。
  - 元画像と同じ形式（ComfyUI の標準は PNG）の場合は、受信したデータをそのまま保存します。PNG に埋め込まれたワークフロー情報も保持されるため、ComfyUI にドラッグ＆ドロップすれば生成条件を復元できます。
  - JPEG に変換する場合、透過部分は白で塗りつぶします（品質 95）。変換時はワークフロー情報は失われます。
- 1回の生成で画像が複数枚出力された場合は `名前_01.jpg`、`名前_02.jpg` のように連番を付けます。
- `file_name` にパスが含まれていてもファイル名部分のみを使用し、保存先フォルダの外には保存しません。
- `file_name` が空の場合は `scene_<prompt_id>.png` という名前で保存されます。
- 同名のファイルが既に存在する場合は上書きされます（`--skip-existing` でスキップ可能）。

## 失敗時の動作と再実行

- ComfyUI 側で実行エラー（VRAM 不足、モデルが見つからない等）が起きた場合は、待たずに失敗として次のシーンへ進みます。
- ワークフローの検証エラーは、どのノードの何が問題かを表示します。
- タイムアウトしたジョブは ComfyUI のキューから削除し、実行中であれば中断します。Ctrl+C で中断した場合も同様です。
- 失敗したシーンがある場合、保存先フォルダに `failed_scenes_YYYYmmdd_HHMMSS.csv` を出力します。このCSVをそのまま指定すれば、失敗したシーンだけを再実行できます。

```
python generate_images.py output\failed_scenes_20261009_210000.csv
```

- 途中で止まった処理を再開する場合は、元のCSVに `--skip-existing` を付けて実行すると、生成済みのシーンを飛ばせます。

## ワークフローの要件

- ComfyUI の「Export (API)」でエクスポートしたJSONであること
- 画像を出力するノード（`SaveImage` / `PreviewImage` 等）を含むこと

スクリプトはワークフローを以下のように書き換えます（元のJSONファイルは変更しません）。

### シード

- `KSampler` / `KSamplerAdvanced` / `SamplerCustom` / `RandomNoise` ノードの `seed` / `noise_seed` を CSV の `seed` に置き換えます。
- シードが他のノード（`PrimitiveInt` 等）から接続されている場合は、接続元の値を書き換えます。

### プロンプト

- サンプラーの `positive` 入力から接続元をたどり、最初に見つかった文字列の `text` / `value` / `prompt` 入力を CSV の `positive_prompt` に置き換えます。
  - 例: `Qwen-Image-2512_*.json` では `CLIPTextEncode` の `text`
  - 例: `ernie_image_turbo.json` では、`CLIPTextEncode` の手前にある `PrimitiveStringMultiline` の `value`（プロンプト拡張の経路を保ったまま書き換えます）
  - 例: `qwen_image_2_1_t2i.json` では `TextEncodeQwenImage21` の `prompt`（`negative_prompt` は変更しません）
- 自動検出で意図しないノードが選ばれる場合は、`--prompt-node` でノードIDを指定してください。
- 起動時に書き換え先のノードIDを表示します。書き換え先が見つからない場合は、生成を始める前にエラーで終了します。

### 取得する画像

- `SaveImage` などの保存画像（type=output）を優先し、なければ `PreviewImage` などのプレビュー画像を使用します。
- 特定のノードの画像だけを取得したい場合は、`--output-node` でノードIDを指定してください。

## 処理の流れ

1. ワークフローJSONを読み込み、プロンプト・シードの書き換え先を確認する
2. CSVからシーン情報を読み込む
3. ComfyUI の WebSocket（`/ws`）に接続する（クライアントIDは起動ごとに一意）
4. シーンごとに以下を繰り返す
   1. ワークフローのコピーのシード・プロンプトを書き換え、`/prompt` に送信
   2. WebSocket で進捗を受信し、プログレスバーを表示
   3. 完了通知（`execution_success` 等）を待つ。並行して `/history` も定期的に確認し、キャッシュ利用時や WebSocket 切断時でも完了を検出する
   4. `/history/{prompt_id}` から出力画像の情報を取得し、`/view` から画像を取得して保存
   5. WebSocket が切断されていれば、次のシーンの前に再接続を試みる
5. 結果（成功・スキップ・失敗件数）を表示し、失敗があれば失敗リストを出力して終了

## Windows での文字化け・エラーについて

ログには絵文字を使用していますが、出力をファイルやパイプにリダイレクトした場合も UTF-8 で出力するため、`UnicodeEncodeError` は発生しません。

## テスト

```
python -m pytest tests -v
```

`tests/fake_comfyui.py` の偽 ComfyUI サーバー（HTTP API と WebSocket）を使うため、ComfyUI を起動しなくても実行できます。
