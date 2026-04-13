"""Xiaomi App Store (小米应用商店) implementation.

Uses username + access_password + RSA signing.
"""

import hashlib
import json
import logging
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://developer.xiaomi.com"


class XiaomiStore(BaseStore):
    name = StoreName.XIAOMI
    display_name = "Xiaomi App Store (小米应用商店)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.username: str = store_config.get("username", "")
        self.access_password: str = store_config.get("access_password", "")

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("username"):
            missing.append("stores.xiaomi.username")
        if not self.config.get("access_password"):
            missing.append("stores.xiaomi.access_password")
        return missing

    def authenticate(self) -> bool:
        # Xiaomi uses request-time signing with username + password
        return bool(self.username and self.access_password)

    def _sign_params(self, params: dict[str, Any]) -> str:
        """Generate signature for Xiaomi API.

        Xiaomi uses: sort params, concatenate, append access_password, SHA1.
        """
        sorted_params = sorted(params.items())
        sign_str = "&".join(f"{k}={v}" for k, v in sorted_params) + self.access_password
        return hashlib.sha1(sign_str.encode("utf-8")).hexdigest()

    def _query_app(self) -> dict[str, Any]:
        """Query current app info from Xiaomi."""
        params = {
            "userName": self.username,
            "packageName": self.app_info.package_name,
        }
        params["signature"] = self._sign_params(params)

        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/dev/query",
            json=params,
        )
        return resp.json()  # type: ignore[return-value]

    def _push_update(self, apk_info: ApkInfo) -> dict[str, Any]:
        """Push APK update to Xiaomi using multipart form data."""
        import os

        app_info_json = json.dumps({
            "packageName": self.app_info.package_name,
            "appName": self.app_info.app_name,
            "updateExplanation": self.app_info.changelog,
        })

        params = {
            "userName": self.username,
            "synchroType": "1",  # 1 = update existing app
        }
        params["signature"] = self._sign_params(params)

        with open(apk_info.path, "rb") as apk_f:
            files = {
                "apk": (apk_info.path.name, apk_f, "application/vnd.android.package-archive"),
            }
            data = {
                **params,
                "appInfo": app_info_json,
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
            result = self._push_update(apk_info)

            error_code = result.get("result", -1)
            if error_code == 0:
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
