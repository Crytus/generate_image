"""テスト用の簡易 ComfyUI サーバー（HTTP API と WebSocket の最小限の実装）。

プロンプト文字列に含まれるキーワードで挙動を切り替える。
    ERROR    : execution_error を通知し、履歴もエラーにする
    CACHED   : WebSocket で完了通知を送らず、履歴だけを書き込む（キャッシュ利用時の再現）
    SLOW     : いつまでも完了しない（タイムアウトの確認用）
    BATCH2   : 保存画像を2枚返す
    LATE     : 完了前に別の prompt_id の完了通知を送る（取り違えの確認用）
"""
import base64
import hashlib
import io
import json
import socket
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, PngImagePlugin

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def make_png(mode="RGBA", color=(255, 0, 0, 128), size=(16, 16)):
    """ワークフロー情報を埋め込んだ PNG を作る（ComfyUI の出力を模したもの）。"""
    img = Image.new(mode, size, color)
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", '{"dummy": "workflow"}')
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


class FakeComfyUI:
    def __init__(self):
        self.history = {}
        self.ws_clients = []          # (client_id, socket)
        self.ws_lock = threading.Lock()
        self.prompts = []             # 受信したワークフロー
        self.deleted = []
        self.interrupted = []
        self.running = []
        self.view_requests = []
        self.image_bytes = make_png()
        self._threads = []

        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self):
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/ws":
                    fake._handle_ws(self, parse_qs(url.query).get("clientId", [""])[0])
                elif url.path.startswith("/history/"):
                    pid = url.path[len("/history/"):]
                    self._json({pid: fake.history[pid]} if pid in fake.history else {})
                elif url.path == "/view":
                    fake.view_requests.append(parse_qs(url.query))
                    body = fake.image_bytes
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/queue":
                    self._json({"queue_running": [[0, pid, {}, {}, []] for pid in fake.running],
                                "queue_pending": []})
                else:
                    self._json({}, 404)

            def do_POST(self):
                url = urlparse(self.path)
                body = self._read_json()
                if url.path == "/prompt":
                    workflow = body["prompt"]
                    text = json.dumps(workflow)
                    if "INVALID" in text:
                        self._json({"error": {"message": "Prompt outputs failed validation",
                                              "details": ""},
                                    "node_errors": {"3": {"class_type": "KSampler", "errors": [
                                        {"message": "Value not in list", "details": "ckpt"}]}}}, 400)
                        return
                    pid = str(uuid.uuid4())
                    fake.prompts.append(workflow)
                    t = threading.Thread(target=fake._execute, args=(pid, body["client_id"], text),
                                         daemon=True)
                    fake._threads.append(t)
                    t.start()
                    self._json({"prompt_id": pid, "number": len(fake.prompts), "node_errors": {}})
                elif url.path == "/queue":
                    fake.deleted.extend(body.get("delete", []))
                    self._json({})
                elif url.path == "/interrupt":
                    fake.interrupted.append(body.get("prompt_id"))
                    self._json({})
                else:
                    self._json({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.address = f"127.0.0.1:{self.server.server_address[1]}"

    # --- サーバー制御 ---

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        with self.ws_lock:
            for _, sock in self.ws_clients:
                try:
                    sock.close()
                except OSError:
                    pass
        self.server.server_close()

    def drop_websockets(self):
        """WebSocket 接続を強制切断する（切断時の動作確認用）。"""
        with self.ws_lock:
            clients, self.ws_clients = self.ws_clients, []
        for _, sock in clients:
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass

    # --- WebSocket ---

    def _handle_ws(self, handler, client_id):
        key = handler.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        handler.send_response(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.wfile.flush()
        sock = handler.connection
        with self.ws_lock:
            self.ws_clients.append((client_id, sock))
        self._send(client_id, {"type": "status", "data": {"status": {}, "sid": client_id}})
        # クライアントが閉じるまで受信を続ける
        try:
            while True:
                data = sock.recv(1024)
                if not data or data[0] & 0x0F == 0x8:
                    break
        except OSError:
            pass
        with self.ws_lock:
            self.ws_clients = [(c, s) for c, s in self.ws_clients if s is not sock]
        handler.close_connection = True

    def _send(self, client_id, msg):
        payload = json.dumps(msg).encode("utf-8")
        if len(payload) < 126:
            header = struct.pack("!BB", 0x81, len(payload))
        elif len(payload) < 65536:
            header = struct.pack("!BBH", 0x81, 126, len(payload))
        else:
            header = struct.pack("!BBQ", 0x81, 127, len(payload))
        with self.ws_lock:
            targets = [s for c, s in self.ws_clients if c == client_id]
            for sock in targets:
                try:
                    sock.sendall(header + payload)
                except OSError:
                    pass

    # --- 実行の模擬 ---

    def _execute(self, pid, client_id, text):
        time.sleep(0.1)
        if "SLOW" in text:
            self.running.append(pid)
            return

        self._send(client_id, {"type": "execution_start", "data": {"prompt_id": pid}})

        if "LATE" in text:
            # 別ジョブの完了通知や画像出力が紛れ込んでも取り違えないこと
            other = str(uuid.uuid4())
            self._send(client_id, {"type": "executed", "data": {
                "prompt_id": other, "node": "60",
                "output": {"images": [{"filename": "other.png", "subfolder": "", "type": "output"}]}}})
            self._send(client_id, {"type": "execution_success", "data": {"prompt_id": other}})

        if "ERROR" in text:
            err = {"prompt_id": pid, "node_id": "3", "node_type": "KSampler",
                   "exception_type": "torch.OutOfMemoryError",
                   "exception_message": "CUDA out of memory"}
            self._send(client_id, {"type": "execution_error", "data": err})
            self._send(client_id, {"type": "executing", "data": {"node": None, "prompt_id": pid}})
            self.history[pid] = {"outputs": {}, "status": {
                "status_str": "error", "completed": False,
                "messages": [["execution_start", {}], ["execution_error", err]]}}
            return

        images = [{"filename": f"{pid}_00001_.png", "subfolder": "", "type": "output"}]
        if "BATCH2" in text:
            images.append({"filename": f"{pid}_00002_.png", "subfolder": "", "type": "output"})
        entry = {
            "outputs": {
                "60": {"images": images},
                "9": {"images": [{"filename": "preview.png", "subfolder": "", "type": "temp"}]},
                "99": {"text": ["not an image"]},
            },
            "status": {"status_str": "success", "completed": True, "messages": []},
        }

        if "CACHED" in text:
            # キャッシュ利用時を模して、完了通知を送らず履歴だけを書き込む
            self.history[pid] = entry
            return

        for i in range(1, 5):
            self._send(client_id, {"type": "progress",
                                   "data": {"value": i, "max": 4, "prompt_id": pid, "node": "3"}})
        self._send(client_id, {"type": "execution_success", "data": {"prompt_id": pid}})
        # 実際の ComfyUI と同様、完了通知の少し後に履歴が書き込まれる
        time.sleep(0.3)
        self.history[pid] = entry
        self._send(client_id, {"type": "executing", "data": {"node": None, "prompt_id": pid}})
