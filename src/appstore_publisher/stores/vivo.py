"""vivo App Store implementation.

Uses a single endpoint with HMAC-SHA256 signed requests.
"""

import datetime
import logging
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from ..utils import hmac_sha256_sign, md5_file
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://developer-api.vivo.com.cn/router/rest"


class VivoStore(BaseStore):
    name = StoreName.VIVO
    display_name = "vivo App Store (vivo应用商店)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.access_key: str = store_config.get("access_key", "")
        self.access_secret: str = store_config.get("access_secret", "")

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("access_key"):
            missing.append("stores.vivo.access_key")
        if not self.config.get("access_secret"):
            missing.append("stores.vivo.access_secret")
        return missing

    def authenticate(self) -> bool:
        # Vivo uses request-time HMAC signing, no separate auth step
        return bool(self.access_key and self.access_secret)

    def _build_common_params(self, method: str) -> dict[str, Any]:
        """Build common parameters for Vivo API requests."""
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "method": method,
            "access_key": self.access_key,
            "timestamp": timestamp,
            "sign_method": "hmac_sha256",
            "version": "1.0",
        }

    def _signed_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make a signed request to Vivo API."""
        sign = hmac_sha256_sign(params, self.access_secret)
        params["sign"] = sign

        resp = self._request_with_retry(
            "POST",
            BASE_URL,
            json=params,
        )
        return resp.json()  # type: ignore[return-value]

    def _upload_file(self, apk_info: ApkInfo) -> str:
        """Upload APK file to Vivo. Returns file URL."""
        import os
        params = self._build_common_params("app.upload.file")
        params["apk_md5"] = md5_file(apk_info.path)
        params["apk_size"] = os.path.getsize(apk_info.path)

        # Get signed URL
        result = self._signed_request("app.upload.file", params)

        if result.get("code") != 0:
            raise RuntimeError(f"Vivo upload init failed: {result.get('message', 'unknown')}")

        upload_url = result.get("data", {}).get("upload_url", "")
        if not upload_url:
            raise RuntimeError(f"No upload URL in Vivo response: {result}")

        # Upload to the provided URL
        with open(apk_info.path, "rb") as f:
            self._request_with_retry(
                "POST",
                upload_url,
                files={"file": (apk_info.path.name, f, "application/vnd.android.package-archive")},
            )

        return result.get("data", {}).get("file_url", "")

    def _update_app(self, file_url: str) -> dict[str, Any]:
        """Submit app update to Vivo."""
        params = self._build_common_params("app.update.app")
        params["apk_url"] = file_url
        params["app_desc"] = self.app_info.changelog
        params["online_type"] = "1"  # 1 = immediate online

        return self._signed_request("app.update.app", params)

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            file_url = self._upload_file(apk_info)
            result = self._update_app(file_url)

            if result.get("code") == 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Published successfully",
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
