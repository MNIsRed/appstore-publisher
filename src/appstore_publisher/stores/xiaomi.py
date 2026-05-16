"""Xiaomi App Store (小米应用商店) implementation."""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from ..utils import md5_file
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "http://api.developer.xiaomi.com/devupload"
ENCRYPT_GROUP_SIZE = 117


class XiaomiStore(BaseStore):
    name = StoreName.XIAOMI
    display_name = "Xiaomi App Store (小米应用商店)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.username: str = store_config.get("username", "")
        # 小米文档示例里字段名叫 password，但这里应填写自动发布 API 私钥。
        self.private_key: str = (store_config.get("private_key") or store_config.get("access_password", "")).strip()
        self.public_key_path: str = store_config.get("public_key_path") or "keys/dev.api.public.cer"
        self.release_config: dict[str, Any] = store_config.get("release", {})

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("username"):
            missing.append("stores.xiaomi.username")
        if not self.private_key:
            missing.append("stores.xiaomi.private_key")
        if not self.public_key_path:
            missing.append("stores.xiaomi.public_key_path")
        elif not Path(self.public_key_path).is_file():
            missing.append(f"stores.xiaomi.public_key_path not found: {self.public_key_path}")
        return missing

    def authenticate(self) -> bool:
        # Xiaomi uses request-time SIG encryption.
        return bool(self.username and self.private_key)

    def _json_dumps(self, data: dict[str, Any]) -> str:
        """按小米示例生成 JSON 字符串，签名和请求体必须使用同一份文本。"""
        return json.dumps(data, ensure_ascii=False)

    def _encrypt_by_public_key(self, text: str) -> str:
        """Use Xiaomi public certificate to encrypt SIG JSON with RSA PKCS#1 v1.5."""
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import padding

        cert_data = Path(self.public_key_path).read_bytes()
        cert = x509.load_pem_x509_certificate(cert_data)
        public_key = cert.public_key()

        text_bytes = text.encode("utf-8")
        encrypted = bytearray()
        for idx in range(0, len(text_bytes), ENCRYPT_GROUP_SIZE):
            encrypted.extend(
                public_key.encrypt(
                    text_bytes[idx:idx + ENCRYPT_GROUP_SIZE],
                    padding.PKCS1v15(),
                )
            )
        return encrypted.hex()

    def _build_sig(self, request_data_text: str, file_hashes: list[dict[str, str]] | None = None) -> str:
        sig_items = [
            {
                "name": "RequestData",
                "hash": hashlib.md5(request_data_text.encode("utf-8")).hexdigest(),
            }
        ]
        if file_hashes:
            sig_items.extend(file_hashes)

        sig_json = {
            "sig": sig_items,
            "password": self.private_key,
        }
        return self._encrypt_by_public_key(self._json_dumps(sig_json))

    def _query_app(self) -> dict[str, Any]:
        """Query current app info from Xiaomi."""
        request_data = {
            "userName": self.username,
            "packageName": self.app_info.package_name,
        }
        request_data_text = self._json_dumps(request_data)
        encrypted_sig = self._build_sig(request_data_text)

        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/dev/query",
            data={"RequestData": request_data_text, "SIG": encrypted_sig},
        )
        return resp.json()  # type: ignore[return-value]

    def _push_update(self, apk_info: ApkInfo) -> dict[str, Any]:
        """Push APK update to Xiaomi using multipart form data."""
        app_detail = {
            "packageName": self.app_info.package_name or apk_info.package_name,
            "appName": self.app_info.app_name,
            "versionName": apk_info.version_name or self.release_config.get("version_name", ""),
            "updateDesc": self.app_info.changelog,
        }
        control_keys = {"synchro_type"}
        for key, value in self.release_config.items():
            if key in control_keys:
                continue
            if value not in (None, ""):
                app_detail[key] = value

        request_data = {
            "userName": self.username,
            "appInfo": self._json_dumps(app_detail),
            "synchroType": str(self.release_config.get("synchro_type", 1)),
        }
        request_data_text = self._json_dumps(request_data)
        file_hashes = [
            {
                "name": "apk",
                "hash": md5_file(apk_info.path),
            }
        ]
        encrypted_sig = self._build_sig(request_data_text, file_hashes)

        with open(apk_info.path, "rb") as apk_f:
            files = {
                "apk": (apk_info.path.name, apk_f, "application/vnd.android.package-archive"),
            }
            data = {
                "RequestData": request_data_text,
                "SIG": encrypted_sig,
            }

            resp = self._request_with_retry(
                "POST",
                f"{BASE_URL}/dev/push",
                data=data,
                files=files,
            )

        return resp.json()  # type: ignore[return-value]

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            if not (self.app_info.package_name or apk_info.package_name):
                raise RuntimeError("Xiaomi release missing package name: app.package_name")
            result = self._push_update(apk_info)

            result_code = result.get("result", result.get("code", -1))
            if result_code == 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Submitted successfully",
                    details=result,
                )
            else:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Update failed: {result.get('message', 'unknown')}",
                    details=result,
                )
        except Exception as e:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=str(e),
            )
