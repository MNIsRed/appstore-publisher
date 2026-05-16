"""OPPO App Store implementation based on OPPO API upload docs."""

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://oop-openapi-cn.heytapmobi.com"

RELEASE_REQUIRED_FIELDS = [
    "pkg_name",
    "version_code",
    "apk_url",
    "app_name",
    "second_category_id",
    "third_category_id",
    "summary",
    "detail_desc",
    "update_desc",
    "privacy_source_url",
    "icon_url",
    "pic_url",
    "online_type",
    "test_desc",
    "copyright_url",
    "business_username",
    "business_email",
    "business_mobile",
    "age_level",
    "adaptive_equipment",
]

OPPO_ADAPTIVE_EQUIPMENT_VALUES = {4, 5, 6}

RELEASE_STATUS_TEXT = {
    0: "未设置分阶段发布",
    1: "分阶段发布中",
    2: "暂停分阶段发布",
    3: "取消分阶段发布",
    4: "分阶段发布结束",
}

AUDIT_STATUS_TEXT = {
    "0": "未发布",
    "1": "审核中",
    "2": "审核通过",
    "3": "测试不通过",
    "4": "运营审核中",
    "5": "运营打回",
    "6": "运营通过",
    "7": "定时发布",
    "00": "资质审核中",
    "11": "资质审核通过",
    "-11": "资质审核不通过",
    "-22": "报备提交成功",
    "22": "已冻结",
    "111": "上线",
    "222": "下线",
    "444": "审核不通过",
    "x": "其他",
}

AUDIT_REVIEWING_STATUSES = {"1", "4", "00"}
AUDIT_PASSED_STATUSES = {"2", "6", "7", "11", "111"}
AUDIT_REJECTED_STATUSES = {"3", "5", "-11", "444"}


class OppoStore(BaseStore):
    name = StoreName.OPPO
    display_name = "OPPO App Store (OPPO应用商店)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.client_id: str = store_config.get("client_id", "")
        self.client_secret: str = store_config.get("client_secret", "")
        self.release_config: dict[str, Any] = store_config.get("release", {})
        self._access_token: str = ""
        self._token_expires_at: float = 0

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("client_id"):
            missing.append("stores.oppo.client_id")
        if not self.config.get("client_secret"):
            missing.append("stores.oppo.client_secret")
        return missing

    def _release_int(self, key: str, default: int) -> int:
        value = self.release_config.get(key, default)
        if value in (None, ""):
            return default
        return int(value)

    def authenticate(self) -> bool:
        """Get OPPO OpenAPI access token."""
        if self._access_token and time.time() < self._token_expires_at - 300:
            return True

        logger.info(f"[{self.display_name}] Authenticating via OPPO OpenAPI...")
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/developer/v1/token",
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        data = resp.json()
        token = data.get("data", {}).get("access_token")
        if data.get("errno") != 0 or not token:
            logger.error(f"OPPO auth failed: {data}")
            return False

        self._access_token = token
        # OPPO returns expire_in as a Unix timestamp.
        self._token_expires_at = float(data.get("data", {}).get("expire_in", time.time() + 172800))
        return True

    def _sign_params(self, params: dict[str, Any]) -> str:
        """Sign request params with HMAC-SHA256 as required by OPPO OpenAPI."""
        sign_parts = []
        for key, value in sorted(params.items()):
            if key == "api_sign" or value is None:
                continue
            sign_parts.append(f"{key}={value}")
        sign_text = "&".join(sign_parts)
        return hmac.new(
            self.client_secret.encode("utf-8"),
            sign_text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_signed_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        signed = dict(params or {})
        signed["access_token"] = self._access_token
        signed["timestamp"] = str(int(time.time()))
        signed["api_sign"] = self._sign_params(signed)
        return signed

    def _openapi_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an OPPO OpenAPI endpoint with common params and signature."""
        signed = self._build_signed_params(params)
        if method.upper() == "GET":
            resp = self._request_with_retry("GET", f"{BASE_URL}{path}", params=signed)
        else:
            resp = self._request_with_retry("POST", f"{BASE_URL}{path}", data=signed)
        data = resp.json()
        if data.get("errno") != 0:
            raise RuntimeError(f"OPPO API {path} failed: {data}")
        return data  # type: ignore[return-value]

    def _get_app_info(self, package_name: str) -> dict[str, Any]:
        """Fetch current OPPO app details so release can reuse existing metadata."""
        data = self._openapi_request(
            "GET",
            "/resource/v1/app/info",
            {"pkg_name": package_name},
        )
        app_data = data.get("data") or {}
        if not app_data:
            raise RuntimeError(f"OPPO app info is empty for pkg_name={package_name}")
        return app_data  # type: ignore[return-value]

    def _get_upload_config(self) -> dict[str, str]:
        """Get one-time upload URL and sign."""
        data = self._openapi_request("GET", "/resource/v1/upload/get-upload-url")
        upload_data = data.get("data") or {}
        upload_url = upload_data.get("upload_url")
        upload_sign = upload_data.get("sign")
        if not upload_url or not upload_sign:
            raise RuntimeError(f"OPPO upload config missing upload_url/sign: {data}")
        return {"upload_url": upload_url, "sign": upload_sign}

    def _upload_apk(self, apk_info: ApkInfo) -> dict[str, Any]:
        """Upload APK to OPPO file storage and return UploadObj."""
        upload_config = self._get_upload_config()
        with open(apk_info.path, "rb") as f:
            resp = self._request_with_retry(
                "POST",
                upload_config["upload_url"],
                data={"type": "apk", "sign": upload_config["sign"]},
                files={"file": (apk_info.path.name, f, "application/vnd.android.package-archive")},
            )
        data = resp.json()
        if data.get("errno") != 0:
            raise RuntimeError(f"OPPO upload failed: {data}")

        upload_data = data.get("data") or {}
        if not upload_data.get("url") or not upload_data.get("md5"):
            raise RuntimeError(f"OPPO upload response missing url/md5: {data}")
        return upload_data  # type: ignore[return-value]

    def _build_release_params(
        self,
        apk_info: ApkInfo,
        uploaded_apk: dict[str, Any],
        current_app: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge config, current OPPO metadata, and uploaded APK into release params."""
        package_name = self.app_info.package_name or apk_info.package_name
        version_code = apk_info.version_code or self.release_config.get("version_code")

        params: dict[str, Any] = {
            "pkg_name": package_name,
            "version_code": str(version_code or ""),
            "apk_url": json.dumps(
                [
                    {
                        "url": uploaded_apk["url"],
                        "md5": uploaded_apk["md5"],
                        "cpu_code": self._release_int("cpu_code", 0),
                    }
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "app_name": self.app_info.app_name or current_app.get("app_name", ""),
            "update_desc": self.app_info.changelog,
            "online_type": self._release_int("online_type", int(current_app.get("online_type") or 1)),
            "test_desc": self.release_config.get("test_desc", current_app.get("test_desc") or "自动化 API 传包"),
        }

        params["adaptive_equipment"] = self._normalize_adaptive_equipment(
            self.release_config.get("adaptive_equipment", current_app.get("adaptive_equipment"))
        )

        reusable_fields = [
            "second_category_id",
            "third_category_id",
            "summary",
            "detail_desc",
            "privacy_source_url",
            "icon_url",
            "pic_url",
            "landscape_pic_url",
            "copyright_url",
            "icp_url",
            "special_url",
            "special_file_url",
            "business_username",
            "business_email",
            "business_mobile",
            "age_level",
        ]
        for field in reusable_fields:
            value = self.release_config.get(field, current_app.get(field))
            if value not in (None, ""):
                params[field] = value

        # adaptive_type 不是必填项，且后台详情可能返回旧格式；只有用户显式配置时才透传。
        if self.release_config.get("adaptive_type") not in (None, ""):
            params["adaptive_type"] = self.release_config["adaptive_type"]

        if params["online_type"] == 2:
            params["sche_online_time"] = self.release_config.get("sche_online_time")

        missing = [
            field
            for field in RELEASE_REQUIRED_FIELDS
            if params.get(field) in (None, "", [])
        ]
        if missing:
            raise RuntimeError(
                "OPPO release missing required fields: "
                + ", ".join(missing)
                + ". 请先在 OPPO 后台完善应用资料，或在 stores.oppo.release 中补齐。"
            )

        return params

    def _normalize_adaptive_equipment(self, value: Any) -> int:
        """OPPO 适配设备只接受 4、5、6；后台旧值异常时默认手机。"""
        if isinstance(value, list | tuple):
            value = value[0] if value else None
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                try:
                    parsed = json.loads(value)
                    value = parsed[0] if isinstance(parsed, list) and parsed else value
                except json.JSONDecodeError:
                    pass
            elif "," in value:
                value = value.split(",", 1)[0].strip()
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = 4
        if normalized not in OPPO_ADAPTIVE_EQUIPMENT_VALUES:
            normalized = self._release_int("adaptive_equipment", 4)
        if normalized not in OPPO_ADAPTIVE_EQUIPMENT_VALUES:
            normalized = 4
        return normalized

    def _normalize_audit_status(self, value: Any) -> str:
        """Normalize OPPO audit_status while preserving string statuses such as 00."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    def _audit_result_from_status(self, audit_status: str) -> int:
        """Convert OPPO audit_status to the common polling result code."""
        if audit_status in AUDIT_REVIEWING_STATUSES:
            return 0
        if audit_status in AUDIT_PASSED_STATUSES:
            return 1
        if audit_status in AUDIT_REJECTED_STATUSES:
            return 2
        return -1

    def _update_app(self, params: dict[str, Any]) -> dict[str, Any]:
        """Submit app version update to OPPO."""
        return self._openapi_request(
            "POST",
            "/resource/v1/app/upd",
            params,
        )

    def get_review_status(self, package_name: str = "") -> dict[str, Any]:
        """Query OPPO app update review status from app detail."""
        package_name = package_name or self.app_info.package_name
        if not package_name:
            raise RuntimeError("OPPO review status missing package name: app.package_name")

        current_app = self._get_app_info(package_name)
        audit_status = self._normalize_audit_status(current_app.get("audit_status"))
        audit_status_name = str(current_app.get("audit_status_name") or "").strip()
        audit_status_text = (
            audit_status_name
            or AUDIT_STATUS_TEXT.get(audit_status)
            or "未知状态"
        )
        audit_result = self._audit_result_from_status(audit_status)

        try:
            release_status = int(current_app.get("release_status", -1))
        except (TypeError, ValueError):
            release_status = -1

        return {
            "package_name": package_name,
            "release_id": current_app.get("version_id", ""),
            "version_name": current_app.get("version_name", ""),
            "version_code": current_app.get("version_code", ""),
            "audit_result": audit_result,
            "audit_status": audit_status_text,
            "audit_message": (
                current_app.get("audit_reason")
                or current_app.get("reject_reason")
                or current_app.get("check_reason")
                or ""
            ),
            "raw_status": audit_status,
            "audit_status_name": audit_status_name,
            "update_info_check": current_app.get("update_info_check", ""),
            "release_status": release_status,
            "release_status_text": RELEASE_STATUS_TEXT.get(release_status, ""),
            "raw": current_app,
        }

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            package_name = self.app_info.package_name or apk_info.package_name
            if not package_name:
                raise RuntimeError("OPPO release missing package name: app.package_name")
            if not apk_info.version_code and not self.release_config.get("version_code"):
                raise RuntimeError("OPPO release missing version_code")

            current_app = self._get_app_info(package_name)
            uploaded_apk = self._upload_apk(apk_info)
            release_params = self._build_release_params(apk_info, uploaded_apk, current_app)
            result = self._update_app(release_params)

            if result.get("errno") == 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Submitted successfully",
                    details={"result": result, "uploaded_apk": uploaded_apk},
                )
            else:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Update failed: {result.get('data', {}).get('message', 'unknown')}",
                    details=result,
                )
        except Exception as e:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=str(e),
            )
