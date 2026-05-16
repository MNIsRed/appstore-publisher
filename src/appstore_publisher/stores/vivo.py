"""vivo App Store implementation."""

import logging
import time
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from ..utils import hmac_sha256_sign, md5_file
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://developer-api.vivo.com.cn/router/rest"

METHOD_UPLOAD_APK = "app.upload.apk"
METHOD_UPDATE_APP = "app.update.submit"
METHOD_APP_DETAIL = "app.query.details"

STATUS_TEXT = {
    1: "草稿",
    2: "待审核",
    3: "审核通过",
    4: "审核不通过",
    5: "撤销审核",
}

ONLINE_STATUS_TEXT = {
    0: "未上架",
    1: "已上架",
    2: "已下架",
    3: "待发布",
}


class VivoStore(BaseStore):
    name = StoreName.VIVO
    display_name = "vivo App Store (vivo应用商店)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.access_key: str = store_config.get("access_key", "")
        self.access_secret: str = store_config.get("access_secret", "")
        self.release_config: dict[str, Any] = store_config.get("release", {})

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
        return {
            "method": method,
            "access_key": self.access_key,
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "version": "1.0",
            "sign_method": "hmac-sha256",
            "target_app_key": "developer",
        }

    def _sign_params(self, params: dict[str, Any]) -> str:
        sign_params = {
            key: value
            for key, value in params.items()
            if key not in {"sign", "file"} and value is not None
        }
        return hmac_sha256_sign(sign_params, self.access_secret)

    def _signed_request(
        self,
        method: str,
        params: dict[str, Any],
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a signed request to Vivo API."""
        params["sign"] = self._sign_params(params)

        kwargs: dict[str, Any]
        if files:
            kwargs = {"data": params, "files": files}
        else:
            kwargs = {"data": params}
        resp = self._request_with_retry(
            "POST",
            BASE_URL,
            **kwargs,
        )
        return resp.json()  # type: ignore[return-value]

    def build_diagnostic_app_detail_request(self, package_name: str) -> dict[str, Any]:
        """Build a sanitized vivo app.query.details request preview for diagnostics."""
        params = self._build_app_detail_params(package_name)
        params["sign"] = self._sign_params(params)
        return {
            "url": BASE_URL,
            "method": "POST",
            "content_type": "application/x-www-form-urlencoded",
            "params": {
                key: ("***" if key == "sign" else value)
                for key, value in params.items()
            },
            "signed_keys": sorted(key for key in params.keys() if key != "sign"),
        }

    def _build_app_detail_params(self, package_name: str) -> dict[str, Any]:
        params = self._build_common_params(METHOD_APP_DETAIL)
        params["packageName"] = package_name
        params["v"] = "1.0"
        return params

    def _assert_success(self, result: dict[str, Any], action: str) -> None:
        code = str(result.get("code", "0"))
        sub_code = str(result.get("subCode", "0"))
        if code != "0" or sub_code not in {"", "0", "None"}:
            message = result.get("subMsg") or result.get("msg") or result
            if isinstance(message, str) and "禁止访问" in message:
                message = (
                    f"{message}。vivo 返回该错误通常表示 access_key 未被授权访问当前接口、"
                    "接口能力未对该账号开通，或账号/应用归属不匹配；若签名错误，vivo 文档通常返回 code=23。"
                )
            raise RuntimeError(f"Vivo {action} failed: {message}")

    def _upload_apk_file(self, apk_info: ApkInfo, package_name: str) -> dict[str, Any]:
        """Upload APK file to Vivo and return uploaded file data."""
        file_md5 = md5_file(apk_info.path)
        params = self._build_common_params(METHOD_UPLOAD_APK)
        params.update(
            {
                "packageName": package_name,
                "fileMd5": file_md5,
            }
        )
        if self.release_config.get("stage_type"):
            params["stageType"] = self.release_config["stage_type"]

        with open(apk_info.path, "rb") as f:
            result = self._signed_request(
                METHOD_UPLOAD_APK,
                params,
                files={
                    "file": (
                        apk_info.path.name,
                        f,
                        "application/vnd.android.package-archive",
                    )
                },
            )
        self._assert_success(result, "APK upload")
        data = result.get("data") or {}
        serial_number = data.get("serialnumber") or data.get("serialNumber")
        if not serial_number:
            raise RuntimeError(f"Vivo APK upload missing serialnumber: {result}")
        data["serialnumber"] = serial_number
        return data  # type: ignore[return-value]

    def _update_app(
        self,
        package_name: str,
        apk_info: ApkInfo,
        upload_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit app update to Vivo."""
        version_code = apk_info.version_code or upload_data.get("versionCode")
        if not version_code:
            raise RuntimeError("Vivo release missing version_code")

        params = self._build_common_params(METHOD_UPDATE_APP)
        params.update(
            {
                "packageName": package_name,
                "onlineType": int(self.release_config.get("online_type", 1)),
                "remark": self.app_info.changelog,
            }
        )
        if params["onlineType"] == 2 and self.release_config.get("sche_online_time"):
            params["onlineTime"] = self.release_config["sche_online_time"]

        return self._signed_request(METHOD_UPDATE_APP, params)

    def _get_app_detail(self, package_name: str) -> dict[str, Any]:
        """Fetch vivo app details."""
        params = self._build_app_detail_params(package_name)
        result = self._signed_request(METHOD_APP_DETAIL, params)
        self._assert_success(result, "app detail")
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"Vivo app detail has unexpected shape: {result}")
        return data

    def get_review_status(self, package_name: str = "") -> dict[str, Any]:
        """Query vivo latest app review status."""
        package_name = package_name or self.app_info.package_name
        if not package_name:
            raise RuntimeError("Vivo review status missing package name: app.package_name")

        data = self._get_app_detail(package_name)
        try:
            status = int(data.get("status"))
        except (TypeError, ValueError):
            status = -1
        try:
            online_status = int(data.get("onlineStatus"))
        except (TypeError, ValueError):
            online_status = -1

        if status == 2:
            audit_result = 0
        elif status == 3:
            audit_result = 1
        elif status == 4:
            audit_result = 2
        else:
            audit_result = -1

        status_text = STATUS_TEXT.get(status, "未知状态")
        if online_status in ONLINE_STATUS_TEXT:
            status_text = f"{status_text} / {ONLINE_STATUS_TEXT[online_status]}"

        return {
            "package_name": package_name,
            "version_name": data.get("versionName", ""),
            "version_code": data.get("versionCode", ""),
            "audit_result": audit_result,
            "audit_status": status_text,
            "audit_message": data.get("notes", ""),
            "raw_status": status,
            "online_status": online_status,
            "raw": data,
        }

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            package_name = self.app_info.package_name or apk_info.package_name
            if not package_name:
                raise RuntimeError("Vivo release missing package name: app.package_name")
            if not apk_info.version_code:
                raise RuntimeError("Vivo release missing version_code")

            upload_data = self._upload_apk_file(apk_info, package_name)
            result = self._update_app(package_name, apk_info, upload_data)

            if str(result.get("code", "0")) == "0" and str(result.get("subCode", "0")) in {"", "0", "None"}:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Published successfully (submitted to vivo review)",
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
