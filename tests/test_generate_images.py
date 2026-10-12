"""generate_images.py のテスト。

実行方法（プロジェクトのルートで）:
    python -m pytest tests -v
"""
import csv
import io
import json
import os
import subprocess
import sys

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate_images as g  # noqa: E402
from fake_comfyui import FakeComfyUI, make_png  # noqa: E402

HEADER = ["scene_num", "scene_name", "seed", "file_name", "positive_prompt"]


def write_csv(path, rows, header=HEADER, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def load_real_workflow(name):
    return g.load_workflow(os.path.join(ROOT, name))


# ============================================================
# 出力ファイル名
# ============================================================

class TestOutputFilename:
    def test_csvのfile_nameを使う(self):
        assert g.build_output_filename("scene_01.jpg", "pid") == "scene_01.jpg"

    def test_空ならprompt_idにフォールバック(self):
        assert g.build_output_filename("", "pid") == "scene_pid.png"
        assert g.build_output_filename("   ", "pid") == "scene_pid.png"
        assert g.build_output_filename(None, "pid") == "scene_pid.png"

    def test_パスを含んでもファイル名部分のみ(self):
        assert g.build_output_filename("../x/y.jpg", "pid") == "y.jpg"
        assert g.build_output_filename("..\\x\\y.jpg", "pid") == "y.jpg"
        assert g.build_output_filename("..", "pid") == "scene_pid.png"

    def test_複数枚は連番(self):
        assert g.build_output_filenames("a.jpg", 1, "p") == ["a.jpg"]
        assert g.build_output_filenames("a.jpg", 2, "p") == ["a_01.jpg", "a_02.jpg"]

    def test_出力済み判定(self, tmp_path):
        assert not g.output_exists(tmp_path, "a.jpg")
        (tmp_path / "a_01.jpg").write_bytes(b"x")
        assert g.output_exists(tmp_path, "a.jpg")
        (tmp_path / "b.png").write_bytes(b"x")
        assert g.output_exists(tmp_path, "b.png")
        assert not g.output_exists(tmp_path, "")


# ============================================================
# 画像保存
# ============================================================

class TestSaveImage:
    def test_同じ形式ならそのまま保存しメタデータを保持(self, tmp_path):
        data = make_png()
        path = tmp_path / "a.png"
        g.save_image(data, str(path))
        assert path.read_bytes() == data
        assert Image.open(path).info.get("prompt") == '{"dummy": "workflow"}'

    def test_jpgに変換し透過部分は白(self, tmp_path):
        data = make_png(color=(255, 0, 0, 0))   # 完全に透明
        path = tmp_path / "a.jpg"
        g.save_image(data, str(path))
        img = Image.open(path)
        assert img.format == "JPEG"
        r, gg, b = img.getpixel((8, 8))
        assert min(r, gg, b) > 240

    def test_webpに変換(self, tmp_path):
        path = tmp_path / "a.webp"
        g.save_image(make_png(), str(path))
        assert Image.open(path).format == "WEBP"

    def test_未知の拡張子は元の形式のまま(self, tmp_path):
        data = make_png()
        path = tmp_path / "a.img"
        g.save_image(data, str(path))
        assert path.read_bytes() == data

    def test_一時ファイルが残らない(self, tmp_path):
        g.save_image(make_png(), str(tmp_path / "a.jpg"))
        assert sorted(os.listdir(tmp_path)) == ["a.jpg"]


# ============================================================
# CSV読み込み
# ============================================================

class TestLoadCsv:
    def test_実CSVを読み込める(self):
        scenes = g.load_scenes_from_csv(os.path.join(ROOT, "hayfever_scenes.csv"))
        assert len(scenes) == 28
        assert scenes[0]["file_name"] == "scene_01_opener.jpg"
        assert isinstance(scenes[0]["seed"], int)

    def test_BOM付きCSV(self, tmp_path):
        path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "a.jpg", "p"]], encoding="utf-8-sig")
        scenes = g.load_scenes_from_csv(path)
        assert len(scenes) == 1 and scenes[0]["num"] == "1"

    def test_不正な行はスキップ(self, tmp_path):
        path = write_csv(tmp_path / "s.csv", [
            [1, "ok", 10, "a.jpg", "prompt"],
            [2, "bad seed", "abc", "b.jpg", "prompt"],
            [3, "empty prompt", 30, "c.jpg", ""],
            ["", "", "", "", ""],
            [4, "ok2", " 40 ", " d.jpg ", "prompt"],
        ])
        scenes = g.load_scenes_from_csv(path)
        assert [s["num"] for s in scenes] == ["1", "4"]
        assert scenes[1]["seed"] == 40 and scenes[1]["file_name"] == "d.jpg"

    def test_必須カラム不足(self, tmp_path):
        path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "p"]],
                         header=["scene_num", "scene_name", "seed", "positive_prompt"])
        assert g.load_scenes_from_csv(path) == []

    def test_ファイルなし(self, tmp_path):
        assert g.load_scenes_from_csv(str(tmp_path / "none.csv")) == []

    def test_file_name重複検出(self):
        scenes = [{"num": "1", "file_name": "a.jpg"}, {"num": "2", "file_name": "A.JPG"},
                  {"num": "3", "file_name": "b.jpg"}, {"num": "4", "file_name": ""}]
        assert g.find_duplicate_file_names(scenes) == {"a.jpg": ["1", "2"]}

    def test_失敗リストCSVは再入力できる(self, tmp_path):
        scene = {"num": "5", "name": "n", "seed": 7, "file_name": "x.jpg", "positive_prompt": "p, q"}
        path = g.write_failed_csv(str(tmp_path), [(scene, "err")])
        assert g.load_scenes_from_csv(path) == [scene]


# ============================================================
# ワークフローの書き換え
# ============================================================

class TestUpdatePrompt:
    def test_Qwenワークフロー(self):
        wf = load_real_workflow("Qwen-Image-2512_16_9.json")
        info = g.update_prompt_data(wf, "NEW PROMPT", 123)
        assert info["prompt_targets"] == [("238:227", "text")]
        assert wf["238:227"]["inputs"]["text"] == "NEW PROMPT"
        assert wf["238:230"]["inputs"]["seed"] == 123
        # ネガティブプロンプトは変更しない / 不要な value 入力を追加しない
        assert wf["238:228"]["inputs"]["text"] != "NEW PROMPT"
        assert "value" not in wf["238:227"]["inputs"]

    def test_ernieワークフローは接続元の文字列ノードを書き換える(self):
        wf = load_real_workflow("ernie_image_turbo.json")
        info = g.update_prompt_data(wf, "NEW PROMPT", 5)
        assert info["prompt_targets"] == [("88:94", "value")]
        assert wf["88:94"]["inputs"]["value"] == "NEW PROMPT"
        # CLIPTextEncode の接続（プロンプト拡張の経路）は壊さない
        assert wf["88:67"]["inputs"]["text"] == ["88:97", 0]
        assert wf["88:70"]["inputs"]["seed"] == 5

    def test_Qwen2_1ワークフローはprompt入力を書き換える(self):
        wf = load_real_workflow("qwen_image_2_1_t2i.json")
        info = g.update_prompt_data(wf, "NEW PROMPT", 7)
        assert info["prompt_targets"] == [("459:452", "prompt")]
        assert wf["459:452"]["inputs"]["prompt"] == "NEW PROMPT"
        # ネガティブプロンプトは変更しない
        assert wf["459:452"]["inputs"]["negative_prompt"] == ""
        assert wf["459:458"]["inputs"]["seed"] == 7
        assert info["seed_count"] == 1

    def test_全ワークフローで書き換え先が見つかる(self):
        for name in os.listdir(ROOT):
            if name.endswith(".json"):
                g.update_prompt_data(load_real_workflow(name), "x", 1)

    def test_prompt_node指定(self):
        wf = load_real_workflow("Qwen-Image-2512_16_9.json")
        g.update_prompt_data(wf, "NEW", 1, prompt_node="238:228")
        assert wf["238:228"]["inputs"]["text"] == "NEW"
        with pytest.raises(ValueError):
            g.update_prompt_data(wf, "NEW", 1, prompt_node="nope")
        with pytest.raises(ValueError):
            g.update_prompt_data(wf, "NEW", 1, prompt_node="238:230")

    def test_接続されたシード(self):
        wf = {
            "1": {"class_type": "KSampler", "inputs": {"seed": ["2", 0], "positive": ["3", 0]}},
            "2": {"class_type": "PrimitiveInt", "inputs": {"value": 0}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
        }
        info = g.update_prompt_data(wf, "new", 99)
        assert wf["2"]["inputs"]["value"] == 99
        assert info["seed_count"] == 1

    def test_書き換え先がない(self):
        with pytest.raises(ValueError):
            g.update_prompt_data({"1": {"class_type": "SaveImage", "inputs": {}}}, "x", 1)

    def test_API形式でないJSON(self, tmp_path):
        path = tmp_path / "ui.json"
        path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        with pytest.raises(ValueError):
            g.load_workflow(str(path))


# ============================================================
# 応答の解析・メッセージ処理
# ============================================================

class TestResponses:
    def test_保存画像を優先(self):
        entry = {"outputs": {
            "9": {"images": [{"filename": "p.png", "type": "temp"}]},
            "60": {"images": [{"filename": "s.png", "type": "output"}]},
            "99": {"text": ["x"]},
        }}
        assert [i["filename"] for i in g.extract_images(entry)] == ["s.png"]
        assert [i["filename"] for i in g.extract_images(entry, "9")] == ["p.png"]

    def test_保存画像がなければプレビュー(self):
        entry = {"outputs": {"9": {"images": [{"filename": "p.png", "type": "temp"}]}}}
        assert [i["filename"] for i in g.extract_images(entry)] == ["p.png"]

    def test_履歴のエラー(self):
        entry = {"status": {"status_str": "error", "messages": [
            ["execution_error", {"node_type": "KSampler", "node_id": "3",
                                 "exception_type": "E", "exception_message": "boom"}]]}}
        with pytest.raises(g.GenerationError, match="boom"):
            g.check_history_entry(entry)

    def test_検証エラーの要約(self):
        body = json.dumps({"error": {"message": "failed validation", "details": ""},
                           "node_errors": {"3": {"class_type": "KSampler",
                                                 "errors": [{"message": "bad", "details": "ckpt"}]}}})
        text = g.summarize_prompt_error(body)
        assert "failed validation" in text and "node 3 (KSampler): bad ckpt" in text

    def test_max0の進捗でも落ちない(self):
        client = g.ComfyUIClient("x")
        client.active_prompt_id = "p"
        client.handle_message({"type": "progress", "data": {"prompt_id": "p", "value": 0, "max": 0}})

    def test_エラー後の終了通知で上書きしない(self):
        client = g.ComfyUIClient("x")
        client.handle_message({"type": "execution_error", "data": {"prompt_id": "p",
                                                                  "exception_message": "boom"}})
        client.handle_message({"type": "executing", "data": {"prompt_id": "p", "node": None}})
        assert "boom" in client._finished["p"]

    def test_不正なメッセージでも例外を外に出さない(self):
        client = g.ComfyUIClient("x")
        client._on_message(None, "not json")
        client._on_message(None, b"\x00\x01")

    def test_クライアントIDは毎回異なる(self):
        assert g.ComfyUIClient("x").client_id != g.ComfyUIClient("x").client_id

    def test_引数の既定値(self):
        args = g.parse_args(["a.csv"])
        # ワークフローは既定値を持たない（未指定時は実行時に選択）
        assert args.workflow is None and os.path.isabs(args.output)
        assert g.parse_args(["a.csv", "-w", "x.json"]).workflow == "x.json"
        assert args.timeout == 300 and not args.skip_existing
        with pytest.raises(SystemExit):
            g.parse_args(["a.csv", "--timeout", "0"])


# ============================================================
# ワークフローの選択
# ============================================================

def make_inputs(*answers):
    """input() の代わりに、指定した回答を順に返す関数を作る。"""
    it = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return fake_input


class TestSelectWorkflow:
    def test_JSONのみをファイル名順に一覧(self, tmp_path):
        for name in ["b.json", "A.json", "c.txt", "d.JSON"]:
            (tmp_path / name).write_text("{}", encoding="utf-8")
        (tmp_path / "dir.json").mkdir()
        assert g.list_workflow_files(str(tmp_path)) == ["A.json", "b.json", "d.JSON"]

    def test_番号で選択(self, tmp_path, capsys):
        for name in ["a.json", "b.json"]:
            (tmp_path / name).write_text("{}", encoding="utf-8")
        path = g.select_workflow(str(tmp_path), make_inputs("2"))
        assert path == os.path.join(str(tmp_path), "b.json")
        out = capsys.readouterr().out
        assert "1: a.json" in out and "2: b.json" in out

    def test_不正な入力は再入力(self, tmp_path, capsys):
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        path = g.select_workflow(str(tmp_path), make_inputs("", "x", "0", "2", " 1 "))
        assert path == os.path.join(str(tmp_path), "a.json")
        assert capsys.readouterr().out.count("番号を入力してください") == 4

    def test_入力終了ならNone(self, tmp_path):
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        assert g.select_workflow(str(tmp_path), make_inputs()) is None

    def test_JSONがなければNone(self, tmp_path):
        assert g.select_workflow(str(tmp_path), make_inputs("1")) is None

    def test_未指定時はカレントフォルダから選択して実行(self, server, tmp_path, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        for name in ["ernie_image_turbo.json", "qwen_image_2_1_t2i.json"]:
            (work / name).write_bytes(open(os.path.join(ROOT, name), "rb").read())
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 11, "first.png", "prompt one"]])
        monkeypatch.chdir(work)
        monkeypatch.setattr("builtins.input", make_inputs("2"))
        options = g.parse_args([csv_path, "-s", server.address, "-o", str(tmp_path / "out")])
        assert g.run(options) == g.EXIT_OK
        assert server.prompts[0]["459:452"]["inputs"]["prompt"] == "prompt one"

    def test_選択されなければ準備エラー(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 11, "first.png", "p"]])
        options = g.parse_args([csv_path, "-o", str(tmp_path / "out")])
        assert g.run(options) == g.EXIT_SETUP_ERROR


# ============================================================
# 偽 ComfyUI サーバーを使った結合テスト
# ============================================================

@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(g, "HISTORY_POLL_INTERVAL", 0.3)
    fake = FakeComfyUI().start()
    yield fake
    fake.stop()


def make_options(server, csv_path, out_dir, *extra):
    return g.parse_args([csv_path, "-s", server.address, "-o", str(out_dir),
                         "-w", os.path.join(ROOT, "Qwen-Image-2512_16_9.json"), *extra])


class TestIntegration:
    def test_Qwen2_1ワークフローで生成(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 11, "first.jpg", "prompt one"]])
        out = tmp_path / "out"
        options = g.parse_args([csv_path, "-s", server.address, "-o", str(out),
                                "-w", os.path.join(ROOT, "qwen_image_2_1_t2i.json")])
        assert g.run(options) == g.EXIT_OK
        assert Image.open(out / "first.jpg").format == "JPEG"
        sent = server.prompts[0]
        assert sent["459:452"]["inputs"]["prompt"] == "prompt one"
        assert sent["459:458"]["inputs"]["seed"] == 11

    def test_正常系_file_nameで保存(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [
            [1, "a", 11, "first.jpg", "prompt one"],
            [2, "b", 22, "second.png", "prompt two"],
        ])
        out = tmp_path / "out"   # 存在しないフォルダも自動作成される
        assert g.run(make_options(server, csv_path, out)) == g.EXIT_OK
        assert Image.open(out / "first.jpg").format == "JPEG"
        assert (out / "second.png").read_bytes() == server.image_bytes
        # シードとプロンプトがワークフローに反映されている
        assert [p["238:230"]["inputs"]["seed"] for p in server.prompts] == [11, 22]
        assert [p["238:227"]["inputs"]["text"] for p in server.prompts] == ["prompt one", "prompt two"]
        # /view は保存画像を取得している
        assert all(r["type"] == ["output"] for r in server.view_requests)

    def test_キャッシュ時は履歴から完了を検出(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "c.png", "CACHED"]])
        assert g.run(make_options(server, csv_path, tmp_path)) == g.EXIT_OK
        assert (tmp_path / "c.png").exists()

    def test_実行エラーは即座に失敗し失敗リストを出力(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [
            [1, "a", 1, "e.png", "ERROR"],
            [2, "b", 2, "ok.png", "fine"],
        ])
        assert g.run(make_options(server, csv_path, tmp_path, "-t", "30")) == g.EXIT_FAILED_SCENES
        assert (tmp_path / "ok.png").exists() and not (tmp_path / "e.png").exists()
        failed = [n for n in os.listdir(tmp_path) if n.startswith("failed_scenes_")]
        assert len(failed) == 1
        rows = g.load_scenes_from_csv(str(tmp_path / failed[0]))
        assert [r["file_name"] for r in rows] == ["e.png"]

    def test_タイムアウト時はジョブを取り消す(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "slow.png", "SLOW"]])
        assert g.run(make_options(server, csv_path, tmp_path, "-t", "1")) == g.EXIT_FAILED_SCENES
        assert len(server.deleted) == 1
        assert server.interrupted == server.deleted

    def test_別ジョブの通知を取り違えない(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "mine.png", "LATE"]])
        assert g.run(make_options(server, csv_path, tmp_path)) == g.EXIT_OK
        names = [r["filename"][0] for r in server.view_requests]
        assert "other.png" not in names and len(names) == 1

    def test_複数枚は連番で保存(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "multi.png", "BATCH2"]])
        assert g.run(make_options(server, csv_path, tmp_path)) == g.EXIT_OK
        assert (tmp_path / "multi_01.png").exists() and (tmp_path / "multi_02.png").exists()

    def test_既存ファイルをスキップ(self, server, tmp_path):
        (tmp_path / "exists.png").write_bytes(b"old")
        csv_path = write_csv(tmp_path / "s.csv", [
            [1, "a", 1, "exists.png", "p"],
            [2, "b", 2, "new.png", "p"],
        ])
        assert g.run(make_options(server, csv_path, tmp_path, "--skip-existing")) == g.EXIT_OK
        assert (tmp_path / "exists.png").read_bytes() == b"old"
        assert (tmp_path / "new.png").exists()
        assert len(server.prompts) == 1

    def test_検証エラーは内容を表示して失敗(self, server, tmp_path, capsys):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "x.png", "INVALID"]])
        assert g.run(make_options(server, csv_path, tmp_path)) == g.EXIT_FAILED_SCENES
        assert "Value not in list" in capsys.readouterr().out

    def test_WebSocket切断後も続行(self, server, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "x.png", "p"], [2, "b", 2, "y.png", "p"]])
        options = make_options(server, csv_path, tmp_path)
        original = g.generate_scene

        def drop_then_generate(client, *args):
            # 各シーンの開始前に接続を切る（再接続と履歴ポーリングの確認）
            server.drop_websockets()
            return original(client, *args)

        g.generate_scene = drop_then_generate
        try:
            assert g.run(options) == g.EXIT_OK
        finally:
            g.generate_scene = original
        assert (tmp_path / "x.png").exists() and (tmp_path / "y.png").exists()

    def test_サーバー未起動(self, tmp_path):
        csv_path = write_csv(tmp_path / "s.csv", [[1, "a", 1, "x.png", "p"]])
        options = g.parse_args([csv_path, "-s", "127.0.0.1:1", "-o", str(tmp_path),
                                "-w", os.path.join(ROOT, "Qwen-Image-2512_16_9.json")])
        assert g.run(options) == g.EXIT_SETUP_ERROR


def test_出力をパイプしても絵文字で落ちない(tmp_path):
    """cp932 のパイプ出力でも UnicodeEncodeError にならないこと。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHONIO")}
    env["PYTHONIOENCODING"] = "cp932"
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "generate_images.py"), str(tmp_path / "none.csv"),
         "-s", "127.0.0.1:1", "-o", str(tmp_path),
         "-w", os.path.join(ROOT, "Qwen-Image-2512_16_9.json")],
        capture_output=True, env=env, cwd=str(tmp_path), timeout=60)
    assert result.returncode == g.EXIT_SETUP_ERROR, result.stderr.decode("utf-8", "replace")
    assert b"Traceback" not in result.stderr
    assert "CSVファイルが見つかりません" in result.stdout.decode("utf-8")
