"""CSVに記述したシーン情報をもとに、ComfyUI の API で画像を一括生成するスクリプト。

使用方法:
    python generate_images.py <scenes.csv> [オプション]

詳細は readme.md を参照。
"""
import argparse
import copy
import csv
import io
import json
import os
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from urllib import error, parse, request

from PIL import Image
import websocket

# === 既定値 ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SERVER = "localhost:8188"
DEFAULT_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
DEFAULT_TIMEOUT = 300           # 1シーンあたりのタイムアウト（秒）
HISTORY_POLL_INTERVAL = 5       # WebSocketの通知が来ない場合に履歴を確認する間隔（秒）
WS_CONNECT_TIMEOUT = 10         # WebSocket接続のタイムアウト（秒）
HTTP_TIMEOUT = 30               # HTTPリクエストのタイムアウト（秒）

REQUIRED_COLUMNS = ("scene_num", "scene_name", "seed", "file_name", "positive_prompt")

# プロンプト文字列を書き換える対象の入力名（優先順）
# "prompt" は TextEncodeQwenImage21（Qwen-Image 2.1）などで使われる
PROMPT_KEYS = ("text", "value", "prompt")
# シード値を持つ入力名
SEED_KEYS = ("seed", "noise_seed")
# positive 入力を持つサンプラーノード
SAMPLER_CLASSES = ("KSampler", "KSamplerAdvanced", "SamplerCustom")

# 拡張子と Pillow の保存形式の対応
EXT_TO_FORMAT = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".gif": "GIF",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}

# 終了コード
EXIT_OK = 0
EXIT_FAILED_SCENES = 1
EXIT_SETUP_ERROR = 2


class GenerationError(Exception):
    """画像生成に失敗したことを表す例外。"""


def log(message=""):
    """進捗バーの行を壊さないよう、改行してから出力する。"""
    ProgressBar.finish_line()
    print(message, flush=True)


class ProgressBar:
    """1行で上書き表示する進捗バー。"""
    _active = False
    _lock = threading.Lock()

    @classmethod
    def show(cls, value, max_value, label=""):
        if not max_value or max_value <= 0:
            return
        bar_length = 30
        filled = int(bar_length * value // max_value)
        bar = "█" * filled + "-" * (bar_length - filled)
        percent = value / max_value * 100
        with cls._lock:
            print(f"\r📡 [{bar}] {percent:6.2f}% {label}", end="", flush=True)
            cls._active = True

    @classmethod
    def finish_line(cls):
        with cls._lock:
            if cls._active:
                print(flush=True)
                cls._active = False


# === ComfyUI クライアント ===

class ComfyUIClient:
    """ComfyUI の HTTP API と WebSocket をまとめて扱うクライアント。"""

    def __init__(self, server_address, verbose=False):
        self.server_address = server_address
        # 複数起動しても通知が混ざらないよう、起動ごとに一意なIDを使う
        self.client_id = str(uuid.uuid4())
        self.verbose = verbose

        self.ws = None
        self._ws_ready = threading.Event()
        self._connected = False
        self._closing = False
        self._last_ws_error = None

        # prompt_id -> エラーメッセージ（成功時は None）
        self._finished = {}
        # 完了通知の保持件数の上限（古いものから破棄）
        self._finished_order = deque()
        self._finished_limit = 1000
        self._cond = threading.Condition()

        # 進捗表示の対象 prompt_id
        self.active_prompt_id = None

    # --- HTTP ---

    def _url(self, path, query=None):
        url = f"http://{self.server_address}{path}"
        if query:
            url += "?" + parse.urlencode(query)
        return url

    def _get_json(self, path, query=None):
        with request.urlopen(self._url(path, query), timeout=HTTP_TIMEOUT) as res:
            return json.loads(res.read().decode("utf-8"))

    def _post_json(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(self._url(path), data=data,
                              headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            body = res.read().decode("utf-8")
            return json.loads(body) if body.strip() else None

    def queue_prompt(self, workflow):
        """ワークフローをキューに登録し、prompt_id を返す。"""
        try:
            body = self._post_json("/prompt", {"client_id": self.client_id, "prompt": workflow})
        except error.HTTPError as e:
            # 400 の場合はワークフローの検証エラーが本文に入っている
            detail = e.read().decode("utf-8", errors="replace")
            raise GenerationError(f"workflow送信エラー (HTTP {e.code}): {summarize_prompt_error(detail)}") from e
        except (error.URLError, OSError) as e:
            raise GenerationError(f"workflow送信エラー: {e}") from e

        prompt_id = (body or {}).get("prompt_id")
        if not prompt_id:
            raise GenerationError(f"prompt_id が返されませんでした: {body}")
        return prompt_id

    def get_history(self, prompt_id):
        """実行履歴を取得する。まだ完了していない場合は None。"""
        body = self._get_json(f"/history/{parse.quote(prompt_id)}")
        return (body or {}).get(prompt_id)

    def get_image(self, image_info):
        """/view から画像データを取得する。"""
        url = self._url("/view", {
            "filename": image_info.get("filename", ""),
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        })
        if self.verbose:
            log(f"✅ 画像取得: {url}")
        with request.urlopen(url, timeout=HTTP_TIMEOUT) as res:
            return res.read()

    def cancel(self, prompt_id):
        """キューから削除し、実行中であれば中断する。"""
        try:
            self._post_json("/queue", {"delete": [prompt_id]})
            queue = self._get_json("/queue")
            running_ids = [item[1] for item in queue.get("queue_running", [])
                           if isinstance(item, list) and len(item) > 1]
            if prompt_id in running_ids:
                self._post_json("/interrupt", {"prompt_id": prompt_id})
                log(f"⏹️ 実行中のジョブを中断しました: {prompt_id}")
            else:
                log(f"🗑️ キューからジョブを削除しました: {prompt_id}")
        except Exception as e:
            log(f"⚠️ ジョブのキャンセルに失敗しました: {e}")

    # --- WebSocket ---

    def connect(self, timeout=WS_CONNECT_TIMEOUT):
        """WebSocket に接続する。接続できなければ ConnectionError。"""
        self._ws_ready.clear()
        self._connected = False
        self._closing = False
        self._last_ws_error = None
        self.ws = websocket.WebSocketApp(
            f"ws://{self.server_address}/ws?clientId={self.client_id}",
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()
        self._ws_ready.wait(timeout)
        if not self._connected:
            self._closing = True
            self.ws.close()
            reason = self._last_ws_error or "タイムアウト"
            raise ConnectionError(f"ComfyUI ({self.server_address}) に接続できません: {reason}")

    def ensure_connected(self):
        """切断されていれば再接続を試みる。失敗しても履歴ポーリングで続行する。"""
        if self._connected:
            return
        log("🔄 WebSocket 再接続中...")
        try:
            self.connect()
        except ConnectionError as e:
            log(f"⚠️ {e}（履歴の確認で続行します）")

    def close(self):
        self._closing = True
        if self.ws:
            self.ws.close()

    def _on_open(self, ws):
        self._connected = True
        log("✅ WebSocket接続成功")
        self._ws_ready.set()

    def _on_error(self, ws, err):
        self._last_ws_error = err
        if self._connected and not self._closing:
            log(f"❌ WebSocket エラー: {err}")
        self._ws_ready.set()

    def _on_close(self, ws, code, reason):
        was_connected = self._connected
        self._connected = False
        if was_connected and not self._closing:
            log("🔌 WebSocket切断（履歴の確認で続行します）")
        self._ws_ready.set()

    def _on_message(self, ws, message):
        # ハンドラ内の例外で待機処理が止まらないよう、すべて捕捉する
        try:
            if isinstance(message, bytes):
                return  # プレビュー画像などのバイナリは使わない
            self.handle_message(json.loads(message))
        except Exception as e:
            log(f"⚠️ メッセージ処理エラー: {e}")

    def handle_message(self, msg):
        msg_type = msg.get("type")
        data = msg.get("data") or {}
        prompt_id = data.get("prompt_id")

        if self.verbose:
            log(f"📩 {msg_type}: {data}")

        if msg_type == "progress":
            if prompt_id == self.active_prompt_id:
                ProgressBar.show(data.get("value", 0), data.get("max", 0),
                                 f"- prompt_id: {prompt_id}")
        elif msg_type == "execution_success":
            self._mark_finished(prompt_id, None)
        elif msg_type == "execution_error":
            self._mark_finished(prompt_id, format_execution_error(data))
        elif msg_type == "execution_interrupted":
            self._mark_finished(prompt_id, "実行が中断されました")
        elif msg_type == "executing" and data.get("node") is None and prompt_id:
            # 実行終了の通知。先にエラー等が記録されていれば上書きしない
            self._mark_finished(prompt_id, None, overwrite=False)

    def _mark_finished(self, prompt_id, err, overwrite=True):
        if not prompt_id:
            return
        with self._cond:
            if prompt_id in self._finished and not overwrite:
                return
            if prompt_id not in self._finished:
                self._finished_order.append(prompt_id)
                if len(self._finished_order) > self._finished_limit:
                    self._finished.pop(self._finished_order.popleft(), None)
            self._finished[prompt_id] = err
            self._cond.notify_all()

    # --- 完了待ち ---

    def wait_for_completion(self, prompt_id, timeout):
        """ジョブの完了を待ち、履歴エントリを返す。

        WebSocket の通知を待ちつつ、一定間隔で /history も確認する。
        （キャッシュ利用時や WebSocket 切断時でも完了を検出するため）
        失敗時は GenerationError、時間切れは TimeoutError。
        """
        deadline = time.monotonic() + timeout
        next_poll = time.monotonic() + HISTORY_POLL_INTERVAL

        while True:
            with self._cond:
                if prompt_id in self._finished:
                    ws_error = self._finished.pop(prompt_id)
                    break

            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + HISTORY_POLL_INTERVAL
                try:
                    entry = self.get_history(prompt_id)
                except Exception as e:
                    entry = None
                    log(f"⚠️ 履歴の取得に失敗しました: {e}")
                if entry is not None:
                    return check_history_entry(entry)

            if now >= deadline:
                raise TimeoutError(f"タイムアウト ({timeout}秒)")

            with self._cond:
                if prompt_id not in self._finished:
                    self._cond.wait(max(0.0, min(deadline, next_poll) - now))

        ProgressBar.finish_line()
        if ws_error:
            raise GenerationError(ws_error)
        return check_history_entry(self._fetch_history_with_retry(prompt_id))

    def _fetch_history_with_retry(self, prompt_id, retries=20, interval=0.5):
        # 完了通知の直後は履歴がまだ書き込まれていないことがあるため再試行する
        last_error = None
        for _ in range(retries):
            try:
                entry = self.get_history(prompt_id)
                if entry is not None:
                    return entry
            except Exception as e:
                last_error = e
            time.sleep(interval)
        raise GenerationError(f"実行履歴を取得できませんでした: {last_error or prompt_id}")


# === 応答の解析 ===

def summarize_prompt_error(body_text):
    """/prompt の 400 応答から要点を取り出す。"""
    try:
        body = json.loads(body_text)
    except ValueError:
        return body_text[:500]
    parts = []
    err = body.get("error")
    if isinstance(err, dict):
        parts.append(err.get("message", "") + (f" ({err['details']})" if err.get("details") else ""))
    for node_id, node_err in (body.get("node_errors") or {}).items():
        for e in node_err.get("errors", []):
            parts.append(f"node {node_id} ({node_err.get('class_type')}): "
                         f"{e.get('message')} {e.get('details', '')}".strip())
    return " / ".join(p for p in parts if p) or body_text[:500]


def format_execution_error(data):
    return (f"実行エラー: {data.get('node_type', '?')} (node {data.get('node_id', '?')}) - "
            f"{data.get('exception_type', '')}: {data.get('exception_message', '').strip()}")


def check_history_entry(entry):
    """履歴エントリがエラーなら GenerationError、成功ならそのまま返す。"""
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        for msg in status.get("messages", []):
            if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
                raise GenerationError(format_execution_error(msg[1]))
        raise GenerationError("実行エラー（詳細不明）")
    return entry


def extract_images(entry, output_node=None):
    """履歴エントリから画像情報を取り出す。

    SaveImage 等の保存画像（type=output）を優先し、なければプレビュー画像（type=temp）を使う。
    output_node を指定した場合はそのノードの画像のみを対象にする。
    """
    outputs = entry.get("outputs") or {}
    saved, temp = [], []
    for node_id in sorted(outputs, key=_node_sort_key):
        if output_node is not None and node_id != output_node:
            continue
        for image in outputs[node_id].get("images") or []:
            if not isinstance(image, dict) or "filename" not in image:
                continue
            (saved if image.get("type", "output") == "output" else temp).append(image)
    return saved or temp


def _node_sort_key(node_id):
    # "60" や "238:231" のようなIDを数値順に並べる
    return [int(p) if p.isdigit() else p for p in str(node_id).replace(":", " ").split()]


# === ワークフローの書き換え ===

def find_prompt_target(workflow, start_node_id):
    """positive の接続元をたどり、プロンプト文字列を持つ (ノードID, 入力名) を探す。

    CLIPTextEncode の text が他ノードから接続されている場合（文字列ノードや
    スイッチ経由など）も、上流にたどって最初に見つかった文字列入力を対象にする。
    """
    visited = set()
    queue = deque([start_node_id])
    while queue:
        node_id = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = workflow.get(node_id)
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        for key in PROMPT_KEYS:
            if isinstance(inputs.get(key), str):
                return node_id, key
        for value in inputs.values():
            if _is_link(value):
                queue.append(str(value[0]))
    return None


def _is_link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[1], int)


def _set_seed(workflow, inputs, key, seed):
    """シード値を設定する。他ノードから接続されている場合は接続元の値を書き換える。"""
    value = inputs[key]
    if not _is_link(value):
        inputs[key] = seed
        return True
    ref = workflow.get(str(value[0]))
    if isinstance(ref, dict):
        ref_inputs = ref.get("inputs", {})
        for ref_key in SEED_KEYS + ("value",):
            if ref_key in ref_inputs and not _is_link(ref_inputs[ref_key]):
                ref_inputs[ref_key] = seed
                return True
    return False


def update_prompt_data(workflow, positive_text, new_seed, prompt_node=None):
    """ワークフローのシードとポジティブプロンプトを書き換える。

    書き換え対象が見つからない場合は ValueError。
    """
    prompt_targets = set()
    seed_count = 0

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        class_type = node.get("class_type")

        if class_type in SAMPLER_CLASSES or class_type == "RandomNoise":
            for key in SEED_KEYS:
                if key in inputs and _set_seed(workflow, inputs, key, new_seed):
                    seed_count += 1

        if prompt_node is None and class_type in SAMPLER_CLASSES and _is_link(inputs.get("positive")):
            target = find_prompt_target(workflow, str(inputs["positive"][0]))
            if target:
                prompt_targets.add(target)

    if prompt_node is not None:
        node = workflow.get(prompt_node)
        if not isinstance(node, dict):
            raise ValueError(f"指定されたプロンプトノードがありません: {prompt_node}")
        inputs = node.get("inputs", {})
        key = next((k for k in PROMPT_KEYS if isinstance(inputs.get(k), str)), None)
        if key is None:
            raise ValueError(f"ノード {prompt_node} に文字列の text / value / prompt 入力がありません")
        prompt_targets.add((prompt_node, key))

    if not prompt_targets:
        raise ValueError("ワークフロー内にポジティブプロンプトの書き換え先が見つかりません"
                         "（--prompt-node で指定してください）")

    for node_id, key in prompt_targets:
        workflow[node_id]["inputs"][key] = positive_text

    return {"prompt_targets": sorted(prompt_targets), "seed_count": seed_count}


def list_workflow_files(directory):
    """フォルダ内の JSON ファイルをファイル名順に返す。"""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted((n for n in names
                   if n.lower().endswith(".json") and os.path.isfile(os.path.join(directory, n))),
                  key=str.lower)


def select_workflow(directory, input_func=None):
    """フォルダ内の JSON ファイルを一覧表示し、番号で選択させる。

    選択したファイルのパスを返す。候補がない・入力が終了した場合は None。
    """
    if input_func is None:
        input_func = input
    files = list_workflow_files(directory)
    if not files:
        log(f"❌ ワークフロー（.json）が見つかりません: {directory}")
        log("   -w オプションでワークフローを指定してください")
        return None

    log(f"📋 ワークフローを選択してください ({directory})")
    for i, name in enumerate(files, start=1):
        log(f"  {i:>2}: {name}")

    while True:
        try:
            answer = input_func(f"番号を入力 (1-{len(files)}): ").strip()
        except EOFError:
            log("\n❌ ワークフローが選択されませんでした")
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(files):
            return os.path.join(directory, files[int(answer) - 1])
        log(f"⚠️ 1〜{len(files)} の番号を入力してください")


def load_workflow(path):
    with open(path, "r", encoding="utf-8") as f:
        workflow = json.load(f)
    if not isinstance(workflow, dict) or not any(
            isinstance(n, dict) and "class_type" in n for n in workflow.values()):
        raise ValueError("API形式のワークフローではありません（ComfyUI の Export (API) で保存してください）")
    return workflow


# === 画像保存 ===

def build_output_filename(file_name, fallback_id):
    """CSVのfile_nameから出力ファイル名を決定する。
    file_nameが空の場合はprompt_idを使ったファイル名にフォールバックする。
    """
    # パス区切りを含む場合でも出力ディレクトリ外に書き出さないようファイル名部分のみ使用
    name = os.path.basename((file_name or "").strip())
    if name and name not in (".", ".."):
        return name
    return f"scene_{fallback_id}.png"


def build_output_filenames(file_name, count, fallback_id):
    """出力画像が複数ある場合は「名前_01.拡張子」のように連番を付ける。"""
    base = build_output_filename(file_name, fallback_id)
    if count <= 1:
        return [base]
    stem, ext = os.path.splitext(base)
    return [f"{stem}_{i:02d}{ext}" for i in range(1, count + 1)]


def output_exists(output_dir, file_name):
    """既に出力済みか（連番付きの1枚目も含めて）判定する。"""
    if not (file_name or "").strip():
        return False
    base = build_output_filename(file_name, "")
    stem, ext = os.path.splitext(base)
    return any(os.path.exists(os.path.join(output_dir, n)) for n in (base, f"{stem}_01{ext}"))


def save_image(image_data, fpath):
    """画像データを保存する。

    拡張子の形式と元画像の形式が同じ場合は、受信したデータをそのまま書き込む
    （ComfyUI が PNG に埋め込むワークフロー情報を保持するため）。
    異なる場合は拡張子に合わせて変換する。JPEG の場合は透過部分を白で塗りつぶす。
    書き込みは一時ファイル経由で行い、途中で失敗しても壊れたファイルを残さない。
    """
    ext = os.path.splitext(fpath)[1].lower()
    target_format = EXT_TO_FORMAT.get(ext)
    img = Image.open(io.BytesIO(image_data))
    tmp_path = fpath + ".tmp"
    try:
        if target_format is None or target_format == img.format:
            if target_format is None:
                log(f"⚠️ 未知の拡張子のため元の形式 ({img.format}) のまま保存します: {fpath}")
            with open(tmp_path, "wb") as f:
                f.write(image_data)
        else:
            save_kwargs = {}
            if target_format == "JPEG":
                img = _to_rgb(img)
                save_kwargs["quality"] = 95
            img.save(tmp_path, format=target_format, **save_kwargs)
        os.replace(tmp_path, fpath)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _to_rgb(img):
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


# === CSVからシーン情報を読み込み ===

def load_scenes_from_csv(csv_path: str) -> list:
    """CSVからシーン情報をリストで読み込む。
    CSVフォーマット: scene_num,scene_name,seed,file_name,positive_prompt
    不正な行は警告を出して読み飛ばす。ファイル自体が読めない場合は空リストを返す。
    """
    scenes = []
    log(f"📂 CSVファイル読み込み中: {csv_path}")
    try:
        # Excel で保存した UTF-8 (BOM付き) にも対応
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = [name.strip() for name in (reader.fieldnames or [])]
            reader.fieldnames = fieldnames
            missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
            if missing:
                log(f"❌ CSVの必須カラムが不足しています: {', '.join(missing)}")
                return []

            for row in reader:
                line = reader.line_num
                if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                    continue  # 空行
                try:
                    seed = int((row["seed"] or "").strip())
                except ValueError:
                    log(f"⚠️ {line}行目: seed が整数ではないためスキップします ({row['seed']!r})")
                    continue
                prompt = (row["positive_prompt"] or "").strip()
                if not prompt:
                    log(f"⚠️ {line}行目: positive_prompt が空のためスキップします")
                    continue
                scenes.append({
                    "num": (row["scene_num"] or "").strip(),
                    "name": (row["scene_name"] or "").strip(),
                    "seed": seed,
                    "file_name": (row["file_name"] or "").strip(),
                    "positive_prompt": prompt,
                })
        log(f"✅ {len(scenes)} シーン読み込み完了")
    except FileNotFoundError:
        log(f"❌ CSVファイルが見つかりません: {csv_path}")
    except Exception as e:
        log(f"❌ CSV読み込みエラー: {e}")
    return scenes


def find_duplicate_file_names(scenes):
    """出力ファイル名の重複を返す（Windows を考慮して大文字小文字を区別しない）。"""
    seen = {}
    for scene in scenes:
        if not scene["file_name"]:
            continue
        key = build_output_filename(scene["file_name"], "").lower()
        seen.setdefault(key, []).append(scene["num"])
    return {name: nums for name, nums in seen.items() if len(nums) > 1}


def write_failed_csv(output_dir, failed):
    """失敗したシーンをCSVに書き出す。そのまま再実行の入力として使える。"""
    path = os.path.join(output_dir, f"failed_scenes_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(REQUIRED_COLUMNS + ("error",))
        for scene, reason in failed:
            writer.writerow([scene["num"], scene["name"], scene["seed"],
                             scene["file_name"], scene["positive_prompt"], reason])
    return path


# === シーン単位の生成 ===

def generate_scene(client, workflow_template, scene, options):
    """1シーン分の画像を生成して保存し、保存したパスのリストを返す。"""
    workflow = copy.deepcopy(workflow_template)
    update_prompt_data(workflow, scene["positive_prompt"], scene["seed"], options.prompt_node)

    prompt_id = client.queue_prompt(workflow)
    log(f"  ⇨ prompt_id: {prompt_id}")
    client.active_prompt_id = prompt_id
    try:
        entry = client.wait_for_completion(prompt_id, options.timeout)
    except (TimeoutError, KeyboardInterrupt):
        # 時間切れ・Ctrl+C の場合は ComfyUI 側のジョブも取り消す
        ProgressBar.finish_line()
        client.cancel(prompt_id)
        raise
    finally:
        client.active_prompt_id = None

    images = extract_images(entry, options.output_node)
    if not images:
        raise GenerationError("出力画像がありません（ワークフローに SaveImage 等の画像出力ノードがあるか確認してください）")

    saved = []
    for image_info, name in zip(images, build_output_filenames(scene["file_name"], len(images), prompt_id)):
        fpath = os.path.join(options.output, name)
        image_data = client.get_image(image_info)
        save_image(image_data, fpath)
        log(f"💾 画像保存完了: {fpath} ({len(image_data)} bytes)")
        saved.append(fpath)
    return saved


# === メイン ===

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="CSVのシーン情報をもとに ComfyUI で画像を一括生成します。")
    parser.add_argument("csv", help="シーン情報のCSVファイル")
    parser.add_argument("-w", "--workflow",
                        help="ワークフローJSON（API形式）。未指定時はカレントフォルダの JSON から番号で選択")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR,
                        help="画像の保存先フォルダ (既定: スクリプトと同じ場所の output)")
    parser.add_argument("-s", "--server", default=DEFAULT_SERVER,
                        help=f"ComfyUI のアドレス (既定: {DEFAULT_SERVER})")
    parser.add_argument("-t", "--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"1シーンあたりのタイムアウト秒数 (既定: {DEFAULT_TIMEOUT})")
    parser.add_argument("--skip-existing", action="store_true",
                        help="出力ファイルが既に存在するシーンをスキップする")
    parser.add_argument("--prompt-node",
                        help="プロンプトを書き換えるノードID（自動検出がうまくいかない場合に指定）")
    parser.add_argument("--output-node",
                        help="画像を取得する出力ノードID（未指定時は保存画像をすべて取得）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="WebSocket メッセージなどの詳細ログを表示する")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout は1以上を指定してください")
    return args


def run(options, client_factory=ComfyUIClient):
    # --- 準備 ---
    if not options.workflow:
        options.workflow = select_workflow(os.getcwd())
        if not options.workflow:
            return EXIT_SETUP_ERROR

    try:
        workflow = load_workflow(options.workflow)
        log(f"📩 workflow読み込み完了: {options.workflow}")
    except FileNotFoundError:
        log(f"❌ ワークフローが見つかりません: {options.workflow}")
        return EXIT_SETUP_ERROR
    except ValueError as e:
        log(f"❌ ワークフローを読み込めません: {e}")
        return EXIT_SETUP_ERROR

    # 書き換え先を事前に確認しておく（全シーン失敗を防ぐ）
    try:
        info = update_prompt_data(copy.deepcopy(workflow), "", 0, options.prompt_node)
    except ValueError as e:
        log(f"❌ {e}")
        return EXIT_SETUP_ERROR
    targets = ", ".join(f"{n}.{k}" for n, k in info["prompt_targets"])
    log(f"🛠️ プロンプト書き換え先: {targets} / シード書き換え箇所: {info['seed_count']}")
    if info["seed_count"] == 0:
        log("⚠️ シードの書き換え先が見つかりません。CSVの seed は反映されません")

    scenes = load_scenes_from_csv(options.csv)
    if not scenes:
        log("⚠️ シーン情報が空です。処理を終了します。")
        return EXIT_SETUP_ERROR

    for name, nums in find_duplicate_file_names(scenes).items():
        log(f"⚠️ file_name が重複しています: {name} (scene {', '.join(nums)}) — 後のシーンで上書きされます")

    os.makedirs(options.output, exist_ok=True)

    client = client_factory(options.server, verbose=options.verbose)
    try:
        client.connect()
    except ConnectionError as e:
        log(f"❌ {e}")
        return EXIT_SETUP_ERROR

    # --- 生成 ---
    total = len(scenes)
    success_count = 0
    skipped_count = 0
    failed = []

    try:
        for index, scene in enumerate(scenes, start=1):
            log(f"\n{'=' * 60}")
            log(f"▶ Scene {scene['num']} ({index}/{total}) {scene['name']}")
            log(f"  Prompt: {scene['positive_prompt'][:60]}...")
            log(f"  Seed: {scene['seed']}")
            log(f"  出力ファイル: {scene['file_name'] or '(未指定: prompt_id を使用)'}")
            log("=" * 60)

            if options.skip_existing and output_exists(options.output, scene["file_name"]):
                log("⏭️ 出力ファイルが存在するためスキップします")
                skipped_count += 1
                continue

            client.ensure_connected()
            start = time.monotonic()
            try:
                generate_scene(client, workflow, scene, options)
                success_count += 1
                log(f"✅ Scene {scene['num']} 完了 ({time.monotonic() - start:.0f}秒)")
            except (GenerationError, TimeoutError) as e:
                log(f"❌ Scene {scene['num']}: {e}")
                failed.append((scene, str(e)))
            except Exception as e:
                # 想定外のエラー（画像取得の通信エラー等）でも次のシーンへ進む
                log(f"❌ Scene {scene['num']}: 予期しないエラー: {e!r}")
                failed.append((scene, repr(e)))
    except KeyboardInterrupt:
        log("\n🛑 中断されました")
        raise
    finally:
        client.close()

    # --- 結果 ---
    log(f"\n{'=' * 60}")
    log(f"🎬 生成完了: 成功 {success_count} / スキップ {skipped_count} / 失敗 {len(failed)} (全{total}シーン)")
    log(f"保存先: {options.output}")
    if failed:
        log("失敗したシーン:")
        for scene, reason in failed:
            log(f"  - Scene {scene['num']} ({scene['file_name']}): {reason}")
        log(f"📝 失敗リスト: {write_failed_csv(options.output, failed)}")
        log("   （このCSVを指定して再実行できます）")
    log("=" * 60)

    return EXIT_FAILED_SCENES if failed else EXIT_OK


def main(argv=None):
    # 出力をリダイレクトした場合でも絵文字で UnicodeEncodeError にならないようにする
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    options = parse_args(argv)
    try:
        return run(options)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
