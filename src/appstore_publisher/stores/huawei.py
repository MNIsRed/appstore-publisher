"""Huawei AppGallery store implementation."""

import logging
import time
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://connect-api.cloud.huawei.com"


class HuaweiStore(BaseStore):
    name = StoreName.HUAWEI
    display_name = "Huawei AppGallery (华为应用市场)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.client_id: str = store_config.get("client_id", "")
        self.client_secret: str = store_config.get("client_secret", "")
        self.app_id: str = store_config.get("app_id", "")
        self._access_token: str = ""
        self._token_expires_at: float = 0

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("client_id"):
            missing.append("stores.huawei.client_id")
        if not self.config.get("client_secret"):
            missing.append("stores.huawei.client_secret")
        if not self.config.get("app_id"):
            missing.append("stores.huawei.app_id")
        return missing

    def authenticate(self) -> bool:
        """Get OAuth2 access token (48h TTL)."""
        if self._access_token and time.time() < self._token_expires_at - 300:
            return True  # Token still valid

        logger.info(f"[{self.display_name}] Authenticating via OAuth2...")
        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/api/oauth2/v1/token",
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        data = resp.json()
        if "access_token" not in data:
            logger.error(f"Huawei auth failed: {data}")
            return False

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 172800)  # 48h
        self.session.headers.update(
            {"Authorization": f"Bearer {self._access_token}"}
        )
        return True

    def _get_upload_url(self) -> str:
        """Get a pre-signed upload URL from Huawei."""
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/api/publish/v2/upload-url",
            params={"appId": self.app_id, "suffix": "apk"},
        )
        data = resp.json()
        upload_url = data.get("uploadUrl") or data.get("upload_url")
        if not upload_url:
            raise RuntimeError(f"Failed to get upload URL: {data}")
        return upload_url  # type: ignore[no-any-return]

    def _upload_apk(self, upload_url: str, apk_info: ApkInfo) -> str:
        """Upload APK file and return the file hash/ID."""
        with open(apk_info.path, "rb") as f:
            resp = self._request_with_retry(
                "PUT",
                upload_url,
                data=f,
                headers={
                    "Content-Type": "application/octet-stream",
                },
            )
        data = resp.json()
        return data.get("fileDestUlr", "") or data.get("result", {}).get("fileId", "")

    def _update_file_info(self, file_url: str) -> dict[str, Any]:
        """Submit file info update."""
        resp = self._request_with_retry(
            "PUT",
            f"{BASE_URL}/api/publish/v2/app-file-info",
            params={"appId": self.app_id},
            json={
                "fileType": 5,  # 5 = APK
                "files": [
                    {
                        "fileName": f"{self.app_info.package_name}.apk",
                        "fileDestUrl": file_url,
                    }
                ],
            },
        )
        return resp.json()  # type: ignore[return-value]

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            # Step 1: Get upload URL
            upload_url = self._get_upload_url()

            # Step 2: Upload APK
            file_url = self._upload_apk(upload_url, apk_info)

            # Step 3: Submit file info
            result = self._update_file_info(file_url)

            ret_code = result.get("ret", {}).get("code", -1)
            if ret_code == 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Published successfully (review: 1-3 days)",
                    details=result,
                )
            else:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Update failed: {result.get('ret', {}).get('msg', 'unknown')}",
                    details=result,
                )
        except Exception as e:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=str(e),
            )
