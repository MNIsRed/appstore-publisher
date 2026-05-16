"""Web GUI server for AppStore Publisher.

Uses only Python built-in libraries (http.server, json, etc.).
No external dependencies required.
"""

import copy
import glob
import json
import logging
import os
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from ..channel_detector import detect_channel

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

_poll_lock = threading.Lock()
_poll_stop_event = threading.Event()
_poll_threads: dict[str, threading.Thread] = {}

POLLABLE_STORES = ("yingyongbao", "huawei", "honor", "vivo", "oppo")
SUPPORTED_POLL_STORES = set(POLLABLE_STORES)

# Store display names for UI
STORE_DISPLAY = {
    "yingyongbao": "应用宝",
    "huawei": "华为",
    "honor": "荣耀",
    "vivo": "vivo",
    "oppo": "OPPO",
    "xiaomi": "小米",
}


def _new_poll_store_status(store_name: str, enabled: bool = True) -> dict[str, Any]:
    supported = store_name in SUPPORTED_POLL_STORES
    return {
        "store": store_name,
        "store_display": STORE_DISPLAY.get(store_name, store_name),
        "enabled": enabled,
        "supported": supported,
        "running": False,
        "done": False,
        "state": "idle" if supported else "unsupported",
        "message": "未开始轮询" if supported else "暂未接入审核状态查询",
        "started_at": "",
        "stopped_at": "",
        "last_result": None,
        "history": [],
        "observed_passed_at": "",
    }


_poll_status: dict[str, Any] = {
    "running": False,
    "done": False,
    "message": "未开始轮询",
    "started_at": "",
    "stopped_at": "",
    "interval_seconds": 10800,
    "stores": {
        store_name: _new_poll_store_status(store_name)
        for store_name in POLLABLE_STORES
    },
}


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
        detected = detect_channel(filename)
        channel = detected.value if detected else None

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


def _run_upload(apk_dir: str, changelog: str, version: str, version_code: str, target_stores: list[str]) -> None:
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
                detected = detect_channel(filename)
                if detected and detected.value == store_name:
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
                    version_name=version,
                    version_code=int(version_code) if version_code.isdigit() else 0,
                    package_name=config.get("app", {}).get("package_name", ""),
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


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _poll_store_status(store_name: str) -> dict[str, Any]:
    stores = _poll_status.setdefault("stores", {})
    if store_name not in stores:
        stores[store_name] = _new_poll_store_status(store_name)
    return stores[store_name]


def _refresh_poll_enabled(config: dict[str, Any]) -> None:
    for store_name in POLLABLE_STORES:
        status = _poll_store_status(store_name)
        status["enabled"] = config.get(store_name, {}).get("enabled") is not False
        status["store_display"] = STORE_DISPLAY.get(store_name, store_name)
        status["supported"] = store_name in SUPPORTED_POLL_STORES


def _sync_poll_summary() -> None:
    stores = _poll_status.get("stores", {})
    running_stores = [s for s in stores.values() if s.get("running")]
    touched_stores = [
        s for s in stores.values()
        if s.get("started_at") or s.get("history") or s.get("state") in {"done", "failed", "stopped"}
    ]

    _poll_status["running"] = bool(running_stores)
    _poll_status["done"] = bool(touched_stores) and not running_stores
    if running_stores:
        names = "、".join(str(s.get("store_display") or s.get("store")) for s in running_stores)
        _poll_status["message"] = f"{names} 轮询中"
    elif touched_stores:
        _poll_status["message"] = "全部轮询任务已结束"
        _poll_status["stopped_at"] = max(
            (str(s.get("stopped_at") or "") for s in touched_stores),
            default=_poll_status.get("stopped_at", ""),
        )
    else:
        _poll_status["message"] = "未开始轮询"

    # 兼容旧前端/调用方：保留荣耀的旧字段。
    honor_status = stores.get("honor", {})
    _poll_status["last_result"] = honor_status.get("last_result")
    _poll_status["history"] = honor_status.get("history", [])
    _poll_status["observed_passed_at"] = honor_status.get("observed_passed_at", "")


def _append_poll_history(store_name: str, item: dict[str, Any]) -> None:
    store_status = _poll_store_status(store_name)
    item.setdefault("store", store_name)
    item.setdefault("store_display", STORE_DISPLAY.get(store_name, store_name))
    history = store_status.setdefault("history", [])
    history.insert(0, item)
    del history[50:]


def _poll_status_snapshot() -> dict[str, Any]:
    config = load_config()
    with _poll_lock:
        _refresh_poll_enabled(config)
        _sync_poll_summary()
        return copy.deepcopy(_poll_status)


def _stop_polling() -> dict[str, Any]:
    _poll_stop_event.set()
    with _poll_lock:
        if _poll_status.get("running"):
            _poll_status["message"] = "正在停止轮询任务..."
            for store_status in _poll_status.get("stores", {}).values():
                if store_status.get("running"):
                    store_status["message"] = "正在停止轮询任务..."
        else:
            _poll_status["message"] = "当前没有运行中的轮询任务"
    return {"ok": True, "message": "停止指令已发送"}


def _create_poll_store(store_name: str, config: dict[str, Any], app_info: Any) -> Any:
    from ..models import StoreName
    from ..stores import create_store

    return create_store(StoreName(store_name), config, app_info)


def _validate_poll_config(store_name: str, store: Any, store_config: dict[str, Any], package_name: str) -> None:
    if store_name == "honor" and not store_config.get("app_id") and not package_name:
        raise RuntimeError("请先配置荣耀 App ID，或配置应用包名用于自动查询 App ID")
    if store_name in {"yingyongbao", "vivo", "oppo"} and not package_name:
        raise RuntimeError(f"请先配置 {STORE_DISPLAY.get(store_name, store_name)} 轮询所需的应用包名")

    missing = store.validate_config()
    if missing:
        raise RuntimeError("缺少配置: " + ", ".join(missing))
    if not store.authenticate():
        raise RuntimeError(f"{STORE_DISPLAY.get(store_name, store_name)} 鉴权失败，请检查密钥配置")


def _run_store_poll(store_name: str, interval_seconds: int) -> None:
    """后台轮询指定市场最新版本与审核状态。"""
    try:
        from ..models import AppInfo

        config = load_config()
        app_config = config.get("app", {})
        store_config = config.get(store_name, {})
        app_info = AppInfo(
            package_name=app_config.get("package_name", ""),
            app_name=app_config.get("app_name", ""),
        )
        store = _create_poll_store(store_name, config, app_info)
        package_name = app_info.package_name

        _validate_poll_config(store_name, store, store_config, package_name)

        while not _poll_stop_event.is_set():
            checked_at = _now_text()
            result = store.get_review_status(package_name)
            audit_result = result.get("audit_result")
            is_in_review = audit_result == 0
            is_passed = audit_result == 1

            item = {
                "checked_at": checked_at,
                "status": "reviewing" if is_in_review else "finished",
                "message": result.get("audit_status", "未知状态"),
                "result": result,
            }

            with _poll_lock:
                store_status = _poll_store_status(store_name)
                store_status["last_result"] = result
                store_status["message"] = item["message"]
                _append_poll_history(store_name, item)
                if is_passed and not store_status.get("observed_passed_at"):
                    store_status["observed_passed_at"] = checked_at
                _sync_poll_summary()

            if not is_in_review:
                with _poll_lock:
                    store_status = _poll_store_status(store_name)
                    store_status["running"] = False
                    store_status["done"] = True
                    store_status["state"] = "done"
                    store_status["stopped_at"] = checked_at
                    if is_passed:
                        store_status["message"] = "审核通过，轮询已结束"
                    else:
                        store_status["message"] = "当前不在审核中，轮询已结束"
                    _sync_poll_summary()
                return

            if _poll_stop_event.wait(interval_seconds):
                break

        with _poll_lock:
            store_status = _poll_store_status(store_name)
            store_status["running"] = False
            store_status["done"] = True
            store_status["state"] = "stopped"
            store_status["stopped_at"] = _now_text()
            store_status["message"] = "轮询已停止"
            _sync_poll_summary()
    except Exception as e:
        with _poll_lock:
            store_status = _poll_store_status(store_name)
            store_status["running"] = False
            store_status["done"] = True
            store_status["state"] = "failed"
            store_status["stopped_at"] = _now_text()
            store_status["message"] = f"轮询失败: {e}"
            _append_poll_history(store_name, {
                "checked_at": store_status["stopped_at"],
                "status": "failed",
                "message": str(e),
                "result": None,
            })
            _sync_poll_summary()


def _run_honor_poll(interval_seconds: int) -> None:
    """后台轮询荣耀最新版本与审核状态。"""
    _run_store_poll("honor", interval_seconds)


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
        elif path == "/api/poll/status":
            self._send_json(_poll_status_snapshot())
        elif path == "/api/poll/stop":
            self._send_json(_stop_polling())
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
                version_code = body.get("version_code", "")
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
                    args=(apk_dir, changelog, version, version_code, target_stores),
                    daemon=True,
                )
                thread.start()

                self._send_json({"ok": True, "message": "上传任务已启动"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
        elif path == "/api/poll/start":
            try:
                global _poll_threads

                body = self._read_body()
                interval_seconds = int(body.get("interval_seconds") or 10800)
                interval_seconds = max(interval_seconds, 60)
                requested_stores = body.get("stores") or ["honor"]
                if not isinstance(requested_stores, list):
                    requested_stores = [requested_stores]
                requested_stores = [
                    str(store_name)
                    for store_name in requested_stores
                    if str(store_name) in POLLABLE_STORES
                ]
                if not requested_stores:
                    self._send_json({"ok": False, "error": "请选择至少一个轮询市场"}, status=400)
                    return

                config = load_config()
                runnable_stores = [
                    store_name
                    for store_name in requested_stores
                    if store_name in SUPPORTED_POLL_STORES
                    and config.get(store_name, {}).get("enabled") is not False
                ]
                if not runnable_stores:
                    self._send_json({"ok": False, "error": "所选市场暂未接入轮询，或尚未启用"}, status=400)
                    return

                with _poll_lock:
                    if _poll_status.get("running"):
                        self._send_json({"ok": False, "error": "已有轮询任务在运行中"}, status=400)
                        return
                    _poll_stop_event.clear()
                    _refresh_poll_enabled(config)
                    _poll_status.update({
                        "running": True,
                        "done": False,
                        "message": "轮询已启动",
                        "started_at": _now_text(),
                        "stopped_at": "",
                        "interval_seconds": interval_seconds,
                    })
                    for store_name in POLLABLE_STORES:
                        enabled = config.get(store_name, {}).get("enabled") is not False
                        _poll_status["stores"][store_name] = _new_poll_store_status(store_name, enabled)
                    for store_name in requested_stores:
                        store_status = _poll_store_status(store_name)
                        if store_name not in SUPPORTED_POLL_STORES:
                            store_status.update({
                                "done": True,
                                "state": "unsupported",
                                "message": "暂未接入审核状态查询",
                            })
                            continue
                        if config.get(store_name, {}).get("enabled") is False:
                            store_status.update({
                                "done": True,
                                "state": "skipped",
                                "message": "该市场未启用",
                            })
                            continue
                        store_status.update({
                            "running": True,
                            "done": False,
                            "state": "running",
                            "message": "轮询已启动，正在查询审核状态",
                            "started_at": _poll_status["started_at"],
                            "stopped_at": "",
                        })
                    _sync_poll_summary()

                poll_runners = {
                    "yingyongbao": lambda interval: _run_store_poll("yingyongbao", interval),
                    "huawei": lambda interval: _run_store_poll("huawei", interval),
                    "honor": _run_honor_poll,
                    "vivo": lambda interval: _run_store_poll("vivo", interval),
                    "oppo": lambda interval: _run_store_poll("oppo", interval),
                }
                _poll_threads = {}
                for store_name in runnable_stores:
                    thread = threading.Thread(
                        target=poll_runners[store_name],
                        args=(interval_seconds,),
                        daemon=True,
                    )
                    _poll_threads[store_name] = thread
                    thread.start()

                names = "、".join(STORE_DISPLAY.get(store_name, store_name) for store_name in runnable_stores)
                self._send_json({"ok": True, "message": f"{names} 审核状态轮询已启动"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
        elif path == "/api/poll/stop":
            self._send_json(_stop_polling())
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
