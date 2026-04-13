"""OPPO App Store implementation.

Uses OAuth2 for authentication, similar to Huawei.
"""

import logging
import time
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from ..utils import md5_file
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://developer.oppomobile.com/api"


class OppoStore(BaseStore):
    name = StoreName.OPPO
    display_name = "OPPO App Store (OPPO应用商店)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.client_id: str = store_config.get("client_id", "")
        self.client_secret: str = store_config.get("client_secret", "")
        self._access_token: str = ""
        self._token_expires_at: float = 0

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("client_id"):
            missing.append("stores.oppo.client_id")
        if not self.config.get("client_secret"):
            missing.append("stores.oppo.client_secret")
        return missing

    def authenticate(self) -> bool:
        """Get OAuth2 access token."""
        if self._access_token and time.time() < self._token_expires_at - 300:
            return True

        logger.info(f"[{self.display_name}] Authenticating via OAuth2...")
        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/oauth/v2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        data = resp.json()
        if "access_token" not in data:
            logger.error(f"OPPO auth failed: {data}")
            return False

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 86400)
        return True

    def _upload_apk(self, apk_info: ApkInfo) -> str:
        """Upload APK to OPPO. Returns file ID."""
        import os
        with open(apk_info.path, "rb") as f:
            resp = self._request_with_retry(
                "POST",
                f"{BASE_URL}/store/v1/apk/upload",
                data={"access_token": self._access_token},
                files={"file": (apk_info.path.name, f, "application/vnd.android.package-archive")},
            )
        data = resp.json()
        if data.get("result", {}).get("result_code") != 0:
            raise RuntimeError(f"OPPO upload failed: {data}")

        return data.get("result", {}).get("data", {}).get("file_key", "")

    def _update_app(self, file_key: str) -> dict[str, Any]:
        """Submit app update to OPPO."""
        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/store/v1/apk/update",
            data={
                "access_token": self._access_token,
                "app_id": self.app_info.package_name,
                "file_key": file_key,
                "update_desc": self.app_info.changelog,
            },
        )
        return resp.json()  # type: ignore[return-value]

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            file_key = self._upload_apk(apk_info)
            result = self._update_app(file_key)

            result_code = result.get("result", {}).get("result_code", -1)
            if result_code == 0:
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
                    message=f"Update failed: {result.get('result', {}).get('result_msg', 'unknown')}",
                    details=result,
                )
        except Exception as e:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=str(e),
            )
