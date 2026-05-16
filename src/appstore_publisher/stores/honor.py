"""Honor (荣耀) App Market store implementation."""

import logging
import os
import time
from typing import Any

from ..utils import sha256_file
from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from .base import BaseStore

logger = logging.getLogger(__name__)

AUTH_URL = "https://iam.developer.honor.com/auth/token"
BASE_URL = "https://appmarket-openapi-drcn.cloud.honor.com/openapi/v1/publish"

AUDIT_RESULT_TEXT = {
    0: "审核中",
    1: "审核通过",
    2: "审核不通过",
    3: "其他非审核状态",
    4: "编辑中，未提交审核",
}


class HonorStore(BaseStore):
    name = StoreName.HONOR
    display_name = "Honor App Market (荣耀应用市场)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.client_id: str = store_config.get("client_id", "")
        self.client_secret: str = store_config.get("client_secret", "")
        self.app_id: str = store_config.get("app_id", "")
        self.release_config: dict[str, Any] = store_config.get("release", {})
        self._access_token: str = ""
        self._token_expires_at: float = 0

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("client_id"):
            missing.append("stores.honor.client_id")
        if not self.config.get("client_secret"):
            missing.append("stores.honor.client_secret")
        return missing

    def _release_int(self, key: str, default: int) -> int:
        value = self.release_config.get(key, default)
        if value in (None, ""):
            return default
        return int(value)

    def _new_feature(self) -> str:
        new_feature = str(self.app_info.changelog or "").strip()
        if len(new_feature) < 3:
            new_feature = "修复已知问题，优化应用体验。"
        return new_feature[:500]

    def _release_language(self) -> str:
        return str(self.release_config.get("language") or "zh-CN")

    def authenticate(self) -> bool:
        """Get OAuth2 access token."""
        if self._access_token and time.time() < self._token_expires_at - 300:
            return True

        logger.info(f"[{self.display_name}] Authenticating via OAuth2...")
        resp = self._request_with_retry(
            "POST",
            AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = resp.json()
        if "access_token" not in data:
            logger.error(f"Honor auth failed: {data}")
            return False

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 172800)
        self.session.headers.update(
            {"Authorization": f"Bearer {self._access_token}"}
        )
        return True

    def _get_app_id(self, package_name: str) -> str:
        if self.app_id:
            return self.app_id
        if not package_name:
            raise RuntimeError("Honor app_id is empty and package name is missing")
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/get-app-id",
            params={"pkgName": package_name},
        )
        result = resp.json()
        data = result.get("data") or result.get("result") or result
        if isinstance(data, list):
            app_item = next(
                (
                    item
                    for item in data
                    if isinstance(item, dict) and item.get("packageName") == package_name
                ),
                data[0] if data else {},
            )
            app_id = app_item.get("appId") if isinstance(app_item, dict) else None
        else:
            app_id = data.get("appId") or data.get("app_id") if isinstance(data, dict) else None
        if not app_id:
            raise RuntimeError(f"Honor get-app-id failed: {result}")
        self.app_id = str(app_id)
        return self.app_id

    def _get_app_detail(self, app_id: str) -> dict[str, Any]:
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/get-app-detail",
            params={"appId": app_id},
        )
        result = resp.json()
        data = result.get("data") or result.get("result") or {}
        return data if isinstance(data, dict) else {}

    def _get_app_current_release(self, app_id: str) -> dict[str, Any]:
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/get-app-current-release",
            params={"appId": app_id},
        )
        result = resp.json()
        data = result.get("data") or result.get("result") or {}
        if not self._is_success(result):
            raise RuntimeError(f"Honor get-app-current-release failed: {result}")
        return data if isinstance(data, dict) else {}

    def _get_upload_info(self, app_id: str, apk_info: ApkInfo) -> dict[str, Any]:
        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/get-file-upload-url",
            params={"appId": app_id},
            json=[
                {
                    "fileName": apk_info.path.name,
                    "fileType": 100,
                    "fileSize": os.path.getsize(apk_info.path),
                    "fileSha256": sha256_file(apk_info.path),
                }
            ],
        )
        result = resp.json()
        data = result.get("data") or result.get("result") or result
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise RuntimeError(f"Honor upload URL response has unexpected shape: {result}")
        upload_url = data.get("uploadUrl") or data.get("upload_url") or data.get("url")
        object_id = data.get("objectId") or data.get("object_id")
        if not upload_url:
            raise RuntimeError(f"Failed to get Honor upload URL: {result}")
        if not object_id:
            raise RuntimeError(f"Honor upload URL response missing objectId: {result}")
        data["uploadUrl"] = upload_url
        data["objectId"] = object_id
        return data

    def _upload_apk(self, app_id: str, upload_info: dict[str, Any], apk_info: ApkInfo) -> str:
        with open(apk_info.path, "rb") as f:
            resp = self._request_with_retry(
                "POST",
                f"{BASE_URL}/file-upload",
                params={"appId": app_id, "objectId": upload_info["objectId"]},
                files={
                    "file": (
                        apk_info.path.name,
                        f,
                        "application/vnd.android.package-archive",
                    )
                },
            )
        result = resp.json()
        data = result.get("data") or result.get("result") or result
        object_id = data.get("objectId") if isinstance(data, dict) else None
        return str(object_id or upload_info["objectId"])

    def _update_file_info(self, app_id: str, object_id: str) -> dict[str, Any]:
        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/update-file-info",
            params={"appId": app_id},
            json={
                "bindingFileList": [
                    {"objectId": object_id}
                ],
            },
        )
        return resp.json()  # type: ignore[return-value]

    def _update_language_info(
        self, app_id: str, app_detail: dict[str, Any]
    ) -> dict[str, Any]:
        language_infos = (
            app_detail.get("languageInfo")
            or app_detail.get("languageInfoList")
            or app_detail.get("languageInfos")
            or []
        )
        if isinstance(language_infos, dict):
            language_infos = [language_infos]
        if not isinstance(language_infos, list) or not language_infos:
            raise RuntimeError(
                "Honor get-app-detail response missing languageInfo; cannot update newFeature"
            )

        target_language = self._release_language()
        language_info = next(
            (
                item
                for item in language_infos
                if isinstance(item, dict) and item.get("languageId") == target_language
            ),
            None,
        )
        if language_info is None:
            language_info = next(
                (item for item in language_infos if isinstance(item, dict)), None
            )
        if not isinstance(language_info, dict):
            raise RuntimeError("Honor get-app-detail response has no usable languageInfo item")

        language_id = language_info.get("languageId") or target_language
        app_name = language_info.get("appName")
        intro = language_info.get("intro")
        if not app_name or not intro:
            raise RuntimeError(
                "Honor languageInfo missing appName or intro; cannot safely update newFeature"
            )

        body = {
            "languageInfoList": [
                {
                    "languageId": language_id,
                    "appName": app_name,
                    "intro": intro,
                    "briefIntro": language_info.get("briefIntro", ""),
                    "newFeature": self._new_feature(),
                }
            ],
            "setAll": 0,
        }
        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/update-language-info",
            params={"appId": app_id},
            json=body,
        )
        return resp.json()  # type: ignore[return-value]

    def _submit_audit(self, app_id: str) -> dict[str, Any]:
        release_type = self._release_int("release_type", 1)
        body: dict[str, Any] = {
            "forceUpdate": self._release_int("force_update", 0),
            "releaseType": release_type,
        }
        if self.release_config.get("test_account"):
            body["testAccount"] = self.release_config["test_account"]
        if self.release_config.get("test_password"):
            body["testPassword"] = self.release_config["test_password"]
        if self.release_config.get("test_comment"):
            body["testComment"] = self.release_config["test_comment"]
        if release_type == 2:
            release_time = self.release_config.get("release_time")
            if not release_time:
                raise RuntimeError("Honor release_type=2 requires stores.honor.release.release_time")
            body["releaseTime"] = release_time
        if release_type == 3:
            phased_release_info = self.release_config.get("phased_release_info")
            if not phased_release_info:
                raise RuntimeError("Honor release_type=3 requires stores.honor.release.phased_release_info")
            body["phasedReleaseInfo"] = phased_release_info

        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/submit-audit",
            params={"appId": app_id},
            json=body,
        )
        return resp.json()  # type: ignore[return-value]

    def _is_success(self, result: dict[str, Any]) -> bool:
        code = result.get("code", result.get("ret"))
        return code in (0, "0", "200", 200) or result.get("ret", {}).get("code") == 0

    def get_review_status(self, package_name: str = "") -> dict[str, Any]:
        """Query Honor latest release version and audit status."""
        app_id = self._get_app_id(package_name or self.app_info.package_name)
        data = self._get_app_current_release(app_id)
        audit_result = data.get("auditResult")
        try:
            audit_result_int = int(audit_result)
        except (TypeError, ValueError):
            audit_result_int = -1

        return {
            "app_id": app_id,
            "release_id": data.get("releaseId", ""),
            "version_name": data.get("versionName", ""),
            "version_code": data.get("versionCode", ""),
            "audit_result": audit_result_int,
            "audit_status": AUDIT_RESULT_TEXT.get(audit_result_int, "未知状态"),
            "audit_message": data.get("auditMessage", ""),
            "audit_attachment": data.get("auditAttachment") or [],
            "raw": data,
        }

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            package_name = self.app_info.package_name or apk_info.package_name
            if not package_name:
                raise RuntimeError("Honor release missing package name: app.package_name")
            app_id = self._get_app_id(package_name)
            app_detail = self._get_app_detail(app_id)

            upload_info = self._get_upload_info(app_id, apk_info)
            object_id = self._upload_apk(app_id, upload_info, apk_info)
            file_result = self._update_file_info(app_id, object_id)
            if not self._is_success(file_result):
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"File info update failed: {file_result.get('msg', 'unknown')}",
                    details=file_result,
                )
            language_result = self._update_language_info(app_id, app_detail)
            if not self._is_success(language_result):
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Language info update failed: {language_result.get('msg', 'unknown')}",
                    details=language_result,
                )

            result = self._submit_audit(app_id)
            if self._is_success(result):
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Published successfully (submitted to Honor review)",
                    details=result,
                )
            else:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Submit audit failed: {result.get('msg', result.get('ret', {}).get('msg', 'unknown'))}",
                    details=result,
                )
        except Exception as e:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=str(e),
            )
