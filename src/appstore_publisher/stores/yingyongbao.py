"""Tencent Yingyongbao (应用宝) store implementation."""

import hashlib
import hmac
import logging
import time
from typing import Any

from ..utils import md5_file
from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://p.open.qq.com/open_file/developer_api"

AUDIT_STATUS_TEXT = {
    1: "审核中",
    2: "审核驳回",
    3: "审核通过",
    8: "开发者主动撤销",
}


class YingyongbaoStore(BaseStore):
    name = StoreName.YINGYONGBAO
    display_name = "Tencent Yingyongbao (应用宝)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.user_id: str = store_config.get("user_id", "")
        self.access_secret: str = store_config.get("access_secret", "")
        self.app_id: str = store_config.get("app_id", "")
        self.release_config: dict[str, Any] = store_config.get("release", {})

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("user_id"):
            missing.append("stores.yingyongbao.user_id")
        if not self.config.get("access_secret"):
            missing.append("stores.yingyongbao.access_secret")
        if not self.config.get("app_id"):
            missing.append("stores.yingyongbao.app_id")
        return missing

    def _sign_params(self, params: dict[str, Any]) -> str:
        """Generate Tencent API HMAC-SHA256 signature."""
        sign_params = {
            key: value
            for key, value in params.items()
            if key != "sign" and value is not None and value != ""
        }
        sign_str = "&".join(f"{key}={sign_params[key]}" for key in sorted(sign_params))
        return hmac.new(
            self.access_secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params = {
            "user_id": self.user_id,
            "timestamp": str(int(time.time())),
        }
        params.update(extra)
        params["sign"] = self._sign_params(params)
        return params

    def _post_form(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}{path}",
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return resp.json()  # type: ignore[return-value]

    def authenticate(self) -> bool:
        # 应用宝使用每次请求签名，无单独鉴权接口。
        return bool(self.user_id and self.access_secret and self.app_id)

    def _query_app_detail(self, package_name: str) -> dict[str, Any]:
        params = self._build_params(
            {
                "pkg_name": package_name,
                "app_id": self.app_id,
            }
        )
        result = self._post_form("/query_app_detail", params)
        if result.get("ret") != 0:
            raise RuntimeError(f"Yingyongbao query_app_detail failed: {result.get('msg') or result}")
        data = result.get("data") or result.get("result") or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"Yingyongbao app detail has unexpected shape: {result}")
        return data

    def _get_upload_url(self, package_name: str, apk_info: ApkInfo) -> dict[str, Any]:
        """Get COS pre-signed upload URL from Yingyongbao."""
        params = self._build_params(
            {
                "pkg_name": package_name,
                "app_id": self.app_id,
                "file_type": "apk",
                "file_name": apk_info.path.name,
            }
        )
        result = self._post_form("/get_file_upload_info", params)
        if result.get("ret") != 0:
            raise RuntimeError(f"Yingyongbao get_file_upload_info failed: {result.get('msg') or result}")
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        if not data.get("pre_sign_url") and not data.get("url"):
            raise RuntimeError(f"Yingyongbao upload info missing pre_sign_url: {result}")
        if not data.get("serial_number") and not data.get("file_serial_number"):
            raise RuntimeError(f"Yingyongbao upload info missing serial_number: {result}")
        return data

    def _upload_to_cos(self, upload_info: dict[str, Any], apk_info: ApkInfo) -> None:
        """Upload APK file to Tencent COS using pre-signed URL."""
        cos_url = upload_info.get("pre_sign_url") or upload_info.get("url")
        with open(apk_info.path, "rb") as f:
            self._request_with_retry(
                "PUT",
                cos_url,
                data=f,
                headers={"Content-Type": "application/octet-stream"},
            )

    def _build_update_params(
        self,
        package_name: str,
        app_detail: dict[str, Any],
        upload_info: dict[str, Any],
        apk_md5: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for key, value in app_detail.items():
            if isinstance(value, (str, int, float)) and value not in ("", None):
                params[key] = value

        params.update(
            {
                "pkg_name": package_name,
                "app_id": self.app_id,
            }
        )

        serial_number = upload_info.get("serial_number") or upload_info.get("file_serial_number")
        apk_bit_type = str(self.release_config.get("apk_bit_type", "32_64")).lower()
        if apk_bit_type == "64":
            params.update(
                {
                    "apk64_flag": 1,
                    "apk64_file_serial_number": serial_number,
                    "apk64_file_md5": apk_md5,
                }
            )
        else:
            params.update(
                {
                    "apk32_flag": 1,
                    "apk32_file_serial_number": serial_number,
                    "apk32_file_md5": apk_md5,
                }
            )

        if self.app_info.changelog:
            params["feature"] = self.app_info.changelog
            params.pop("version_desc", None)
        for key, value in self.release_config.items():
            if key != "apk_bit_type" and value not in ("", None):
                params[key] = value

        required = ["pkg_name", "app_id"]
        missing = [key for key in required if not params.get(key)]
        if missing:
            raise RuntimeError(f"Yingyongbao release missing required fields: {', '.join(missing)}")
        return self._build_params(params)

    def _update_app(self, params: dict[str, Any]) -> dict[str, Any]:
        """Submit the app update to Yingyongbao."""
        return self._post_form("/update_app", params)

    def get_review_status(self, package_name: str = "") -> dict[str, Any]:
        """Query Yingyongbao app update review status."""
        package_name = package_name or self.app_info.package_name
        if not package_name:
            raise RuntimeError("Yingyongbao review status missing package name: app.package_name")

        params = self._build_params(
            {
                "pkg_name": package_name,
                "app_id": self.app_id,
            }
        )
        result = self._post_form("/query_app_update_status", params)
        if result.get("ret") != 0:
            raise RuntimeError(f"Yingyongbao query_app_update_status failed: {result.get('msg') or result}")

        try:
            audit_status = int(result.get("audit_status"))
        except (TypeError, ValueError):
            audit_status = -1

        return {
            "package_name": package_name,
            "version_name": result.get("version_name", ""),
            "version_code": result.get("version_code", ""),
            "audit_result": 0 if audit_status == 1 else (1 if audit_status == 3 else 2),
            "audit_status": AUDIT_STATUS_TEXT.get(audit_status, "未知状态"),
            "audit_message": result.get("audit_reason", ""),
            "raw_status": audit_status,
            "raw": result,
        }

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            package_name = self.app_info.package_name or apk_info.package_name
            if not package_name:
                raise RuntimeError("Yingyongbao release missing package name: app.package_name")

            app_detail = self._query_app_detail(package_name)
            upload_info = self._get_upload_url(package_name, apk_info)
            self._upload_to_cos(upload_info, apk_info)
            update_params = self._build_update_params(
                package_name,
                app_detail,
                upload_info,
                md5_file(apk_info.path),
            )
            result = self._update_app(update_params)

            if result.get("ret") == 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Published successfully (submitted to Yingyongbao review)",
                    details=result,
                )
            else:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Update failed: {result.get('msg', 'unknown')}",
                    details=result,
                )
        except Exception as e:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=str(e),
            )
