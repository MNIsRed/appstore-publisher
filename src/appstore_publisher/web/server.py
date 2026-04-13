"""Web GUI server for AppStore Publisher.

Uses only Python built-in libraries (http.server, json, etc.).
No external dependencies required.
"""

import glob
import json
import logging
import os
import re
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "appstore-publisher"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATIC_DIR = Path(__file__).parent / "static"

# Upload status shared state
_upload_status: dict[str, Any] = {
    "running": False,
    "progress": [],
    "done": False,
}

# Store display names for UI
STORE_DISPLAY = {
    "yingyongbao": "应用宝",
    "huawei": "华为",
    "honor": "荣耀",
    "vivo": "vivo",
    "oppo": "OPPO",
    "xiaomi": "小米",
}

CHANNEL_MAP = {
    "yingyongbao": "yingyongbao",
    "tencent": "yingyongbao",
    "qq": "yingyongbao",
    "huawei": "huawei",
    "honor": "honor",
    "vivo": "vivo",
    "oppo": "oppo",
    "xiaomi": "xiaomi",
}

CHANNEL_PATTERN = re.compile(r".*[-_](?P<channel>[a-z]+)(?:[-_]signed)?\.apk$", re.IGNORECASE)


def load_config() -> dict[str, Any]:
    """Load config from JSON file."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    return {
        "yingyongbao": {"enabled": False},
        "huawei": {"enabled": False},
        "honor": {"enabled": False},
        "vivo": {"enabled": False},
        "oppo": {"enabled": False},
        "xiaomi": {"enabled": False},
    }


def save_config(config: dict[str, Any]) -> None:
    """Save config to JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def scan_apks(directory: str) -> dict[str, Any]:
    """Scan a directory for APK files and detect channels."""
    if not directory or not os.path.isdir(directory):
        return {"apks": [], "error": "目录不存在"}

    apk_files = sorted(glob.glob(os.path.join(directory, "*.apk")))
    result = []
    for apk_path in apk_files:
        filename = os.path.basename(apk_path)
        match = CHANNEL_PATTERN.match(filename)
        channel = None
        if match:
            ch = match.group("channel").lower()
            channel = CHANNEL_MAP.get(ch)

        file_size = os.path.getsize(apk_path)
        result.append({
            "filename": filename,
            "path": apk_path,
            "channel": channel,
            "channel_display": STORE_DISPLAY.get(channel, "未知") if channel else "未检测到",
            "size": file_size,
            "size_display": f"{file_size / 1024 / 1024:.1f} MB",
        })

    return {"apks": result}


def _run_upload(apk_dir: str, changelog: str, version: str, target_stores: list[str]) -> None:
    """Run upload in background thread."""
    global _upload_status

    _upload_status["running"] = True
    _upload_status["done"] = False
    _upload_status["progress"] = [
        {"store": s, "store_display": STORE_DISPLAY.get(s, s), "status": "pending", "message": "等待中"}
        for s in target_stores
    ]

    config = load_config()

    for i, store_name in enumerate(target_stores):
        _upload_status["progress"][i]["status"] = "uploading"
        _upload_status["progress"][i]["message"] = "上传中..."

        try:
            # Find APK for this channel
            apk_files = sorted(glob.glob(os.path.join(apk_dir, "*.apk")))
            matched_apk = None
            for apk_path in apk_files:
                filename = os.path.basename(apk_path)
                match = CHANNEL_PATTERN.match(filename)
                if match:
                    ch = match.group("channel").lower()
                    detected = CHANNEL_MAP.get(ch)
                    if detected == store_name:
                        matched_apk = apk_path
                        break

            if not matched_apk:
                _upload_status["progress"][i]["status"] = "failed"
                _upload_status["progress"][i]["message"] = f"未找到 {STORE_DISPLAY.get(store_name, store_name)} 渠道的 APK 文件"
                continue

            # Try to use existing store implementation
            try:
                from ..models import AppInfo, ApkInfo, StoreName
                from ..stores import create_store

                # Map store name string to StoreName enum
                store_enum = StoreName(store_name)
                store_config = config.get(store_name, {})

                app_info = AppInfo(
                    package_name=config.get("app", {}).get("package_name", ""),
                    app_name=config.get("app", {}).get("app_name", ""),
                    changelog=changelog or "Bug fixes and improvements",
                )

                apk_info = ApkInfo(
                    path=Path(matched_apk),
                    channel=store_enum,
                )

                store = create_store(store_enum, config, app_info)
                result = store.publish(apk_info)

                if result.status.value == "success":
                    _upload_status["progress"][i]["status"] = "success"
                    _upload_status["progress"][i]["message"] = result.message or "上传成功"
                elif result.status.value == "skipped":
                    _upload_status["progress"][i]["status"] = "skipped"
                    _upload_status["progress"][i]["message"] = result.message or "已跳过"
                else:
                    _upload_status["progress"][i]["status"] = "failed"
                    _upload_status["progress"][i]["message"] = result.message or "上传失败"

            except ImportError as e:
                _upload_status["progress"][i]["status"] = "failed"
                _upload_status["progress"][i]["message"] = f"导入错误: {e}"
            except Exception as e:
                _upload_status["progress"][i]["status"] = "failed"
                _upload_status["progress"][i]["message"] = f"上传出错: {e}"

        except Exception as e:
            _upload_status["progress"][i]["status"] = "failed"
            _upload_status["progress"][i]["message"] = str(e)

    _upload_status["running"] = False
    _upload_status["done"] = True


class AppStoreHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the AppStore Publisher Web GUI."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging."""
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        """Send JSON response."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        """Read and parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body)  # type: ignore[no-any-return]

    def _serve_index(self) -> None:
        """Serve the main HTML page."""
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            with open(index_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, "index.html not found")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_index()
        elif path == "/api/config":
            config = load_config()
            self._send_json(config)
        elif path == "/api/scan":
            params = parse_qs(parsed.query)
            directory = params.get("dir", [""])[0]
            result = scan_apks(directory)
            self._send_json(result)
        elif path == "/api/status":
            self._send_json(_upload_status)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            try:
                body = self._read_body()
                save_config(body)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
        elif path == "/api/upload":
            try:
                body = self._read_body()
                apk_dir = body.get("apk_dir", "")
                changelog = body.get("changelog", "")
                version = body.get("version", "")
                target_stores = body.get("target_stores", [])

                if not apk_dir:
                    self._send_json({"ok": False, "error": "请指定 APK 目录"}, status=400)
                    return
                if not target_stores:
                    self._send_json({"ok": False, "error": "请至少选择一个目标商店"}, status=400)
                    return
                if _upload_status.get("running"):
                    self._send_json({"ok": False, "error": "已有上传任务在运行中"}, status=400)
                    return

                # Start upload in background thread
                thread = threading.Thread(
                    target=_run_upload,
                    args=(apk_dir, changelog, version, target_stores),
                    daemon=True,
                )
                thread.start()

                self._send_json({"ok": True, "message": "上传任务已启动"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
        else:
            self.send_error(404, "Not Found")


def run_server(host: str = "127.0.0.1", port: int = 8580) -> None:
    """Start the web server."""
    server = HTTPServer((host, port), AppStoreHandler)
    url = f"http://{host}:{port}"
    print(f"🐱 AppStore Publisher Web GUI 已启动!")
    print(f"   打开浏览器访问: {url}")
    print(f"   按 Ctrl+C 停止服务器")

    # Try to open browser
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")
        server.server_close()


if __name__ == "__main__":
    run_server()
