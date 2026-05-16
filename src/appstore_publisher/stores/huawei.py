"""Huawei AppGallery store implementation."""

import logging
import os
import re
import time
from typing import Any

from ..utils import sha256_file
from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://connect-api.cloud.huawei.com"

HUAWEI_BRIEF_INFO_PATHS = (
    "/api/publish/v2/app-brief-info-list",
    "/api/publish/v2/app-info/brief-info-list",
    "/api/publish/v2/app-info-list",
    "/api/publish/v2/app-info",
)

HUAWEI_REVIEWING_WORDS = ("审核中", "待审核", "reviewing", "in_review", "auditing", "processing")
HUAWEI_PASSED_WORDS = ("审核通过", "已上架", "released", "approved", "passed", "online")
HUAWEI_REJECTED_WORDS = ("审核驳回", "审核失败", "rejected", "failed")


class HuaweiStore(BaseStore):
    name = StoreName.HUAWEI
    display_name = "Huawei AppGallery (华为应用市场)"

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
            missing.append("stores.huawei.client_id")
        if not self.config.get("client_secret"):
            missing.append("stores.huawei.client_secret")
        if not self.config.get("app_id"):
            missing.append("stores.huawei.app_id")
        return missing

    def _release_int(self, key: str, default: int) -> int:
        value = self.release_config.get(key, default)
        if value in (None, ""):
            return default
        return int(value)

    def _release_remark(self) -> str:
        remark = str(self.release_config.get("remark") or "").strip()
        if len(remark) < 10:
            remark = "通过 API 自动提交应用更新审核。"
        return remark[:300]

    def _release_float(self, key: str, default: float) -> float:
        value = self.release_config.get(key, default)
        if value in (None, ""):
            return default
        return float(value)

    def _release_language(self) -> str:
        return str(self.release_config.get("language") or "zh-CN")

    def _new_features(self) -> str:
        new_features = str(self.app_info.changelog or "").strip()
        if len(new_features) < 3:
            new_features = "修复已知问题，优化应用体验。"
        return new_features[:500]

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
            {
                "Authorization": f"Bearer {self._access_token}",
                "client_id": self.client_id,
            }
        )
        return True

    def _is_harmony_package(self, apk_info: ApkInfo) -> bool:
        package_type = str(self.release_config.get("package_type", "")).lower()
        return package_type == "app" or apk_info.path.suffix.lower() == ".app"

    def _get_upload_info_for_obs(self, apk_info: ApkInfo) -> dict[str, Any]:
        """Get a pre-signed OBS upload URL for HarmonyOS .app packages."""
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/api/publish/v2/upload-url/for-obs",
            params={
                "appId": self.app_id,
                "fileName": apk_info.path.name,
                "sha256": sha256_file(apk_info.path),
                "contentLength": os.path.getsize(apk_info.path),
                "releaseType": self._release_int("release_type", 1),
            },
        )
        data = resp.json()
        if data.get("ret", {}).get("code", 0) != 0:
            raise RuntimeError(f"Failed to get Huawei upload URL: {data}")
        url_info = data.get("urlInfo") or data.get("result") or data
        upload_url = url_info.get("url") or url_info.get("uploadUrl") or url_info.get("upload_url")
        if not upload_url:
            raise RuntimeError(f"Failed to get upload URL: {data}")
        url_info["url"] = upload_url
        return url_info  # type: ignore[no-any-return]

    def _get_upload_info(self, apk_info: ApkInfo) -> dict[str, Any]:
        """Get a classic package upload URL for Android APK/AAB packages."""
        suffix = apk_info.path.suffix.lower().lstrip(".") or "apk"
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/api/publish/v2/upload-url",
            params={
                "appId": self.app_id,
                "releaseType": self._release_int("release_type", 1),
                "suffix": suffix,
            },
        )
        data = resp.json()
        if data.get("ret", {}).get("code", 0) != 0:
            raise RuntimeError(f"Failed to get Huawei upload URL: {data}")
        upload_url = data.get("uploadUrl") or data.get("upload_url")
        auth_code = data.get("authCode") or data.get("auth_code")
        if not upload_url or not auth_code:
            raise RuntimeError(f"Failed to get Huawei upload URL/authCode: {data}")
        return {
            "url": upload_url,
            "authCode": auth_code,
            "method": "POST",
        }

    def _upload_for_obs(self, upload_info: dict[str, Any], apk_info: ApkInfo) -> str:
        """Upload a HarmonyOS package and return the object ID."""
        headers = dict(upload_info.get("headers") or {})
        headers.setdefault("Content-Type", "application/octet-stream")
        with open(apk_info.path, "rb") as f:
            resp = self._request_with_retry(
                str(upload_info.get("method") or "PUT"),
                upload_info["url"],
                data=f,
                headers=headers,
            )
        object_id = (
            upload_info.get("objectId")
            or upload_info.get("object_id")
            or upload_info.get("fileDestUrl")
            or upload_info.get("fileDestUlr")
        )
        if object_id:
            return str(object_id)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        object_id = data.get("objectId") or data.get("fileDestUrl") or data.get("fileDestUlr")
        if not object_id:
            raise RuntimeError(f"Huawei upload response missing objectId: {data or upload_info}")
        return str(object_id)

    def _upload_apk(self, upload_info: dict[str, Any], apk_info: ApkInfo) -> dict[str, Any]:
        """Upload an Android APK/AAB package and return Huawei file info."""
        with open(apk_info.path, "rb") as f:
            resp = self._request_with_retry(
                "POST",
                upload_info["url"],
                data={
                    "authCode": upload_info["authCode"],
                    "fileCount": "1",
                    "parseType": "0",
                },
                # 华为上传服务要求表单字段名为 file，同时会根据 filename 后缀识别包类型。
                files={"file": ("file.apk", f, "application/vnd.android.package-archive")},
            )
        data = resp.json()
        result = data.get("result") or {}
        upload_rsp = result.get("UploadFileRsp") or result.get("uploadFileRsp") or {}
        file_info_list = upload_rsp.get("fileInfoList") or result.get("fileInfoList") or []
        file_info = file_info_list[0] if file_info_list else {}
        file_dest_url = (
            file_info.get("fileDestUlr")
            or file_info.get("fileDestUrl")
            or result.get("fileDestUlr")
            or result.get("fileDestUrl")
            or result.get("fileId")
        )
        if not file_dest_url:
            raise RuntimeError(f"Huawei upload response missing fileDestUrl: {data}")
        file_info["fileDestUrl"] = file_dest_url
        return file_info  # type: ignore[return-value]

    def _update_package_info_for_obs(self, object_id: str, apk_info: ApkInfo) -> dict[str, Any]:
        """Submit HarmonyOS package info update."""
        resp = self._request_with_retry(
            "PUT",
            f"{BASE_URL}/api/publish/v3/app-package-info",
            params={"appId": self.app_id},
            json={
                "fileName": apk_info.path.name,
                "objectId": object_id,
            },
        )
        return resp.json()  # type: ignore[return-value]

    def _update_file_info(self, file_info: dict[str, Any], apk_info: ApkInfo) -> dict[str, Any]:
        """Submit Android APK/AAB file info update."""
        body: dict[str, Any] = {
            "fileType": 5,
            "files": [
                {
                    "fileName": apk_info.path.name,
                    "fileDestUrl": file_info["fileDestUrl"],
                }
            ],
        }
        if file_info.get("size"):
            body["files"][0]["size"] = file_info["size"]

        resp = self._request_with_retry(
            "PUT",
            f"{BASE_URL}/api/publish/v2/app-file-info",
            params={
                "appId": self.app_id,
                "releaseType": self._release_int("release_type", 1),
            },
            json=body,
        )
        return resp.json()  # type: ignore[return-value]

    def _extract_package_ids(self, package_result: dict[str, Any]) -> list[str]:
        versions = package_result.get("pkgVersion") or package_result.get("pkgVersions") or []
        if isinstance(versions, str):
            versions = [versions]
        return [str(item) for item in versions if item not in (None, "")]

    def _get_compile_status(self, package_ids: list[str]) -> dict[str, Any]:
        resp = self._request_with_retry(
            "GET",
            f"{BASE_URL}/api/publish/v2/package/compile/status",
            params={
                "appId": self.app_id,
                "pkgIds": ",".join(package_ids),
            },
        )
        return resp.json()  # type: ignore[return-value]

    def _wait_for_compile_ready(self, package_result: dict[str, Any]) -> None:
        package_ids = self._extract_package_ids(package_result)
        if not package_ids:
            # app-file-info 偶发不返回 pkgVersion；给华为后台一次解析缓冲，避免立即提交。
            time.sleep(self._release_float("compile_initial_wait", 10.0))
            return

        timeout = self._release_float("compile_timeout", 180.0)
        interval = self._release_float("compile_poll_interval", 10.0)
        deadline = time.time() + timeout
        last_result: dict[str, Any] = {}

        while time.time() < deadline:
            result = self._get_compile_status(package_ids)
            last_result = result
            ret_code = result.get("ret", {}).get("code", 0)
            if ret_code != 0:
                raise RuntimeError(f"Huawei compile status query failed: {result}")

            states = result.get("pkgStateList") or result.get("packageState") or []
            if states and all(int(item.get("successStatus", 1)) == 0 for item in states):
                return
            time.sleep(interval)

        raise RuntimeError(f"Huawei package compile not ready before submit: {last_result}")

    def _update_language_info(self) -> dict[str, Any]:
        resp = self._request_with_retry(
            "PUT",
            f"{BASE_URL}/api/publish/v2/app-language-info",
            params={"appId": self.app_id},
            json={
                "lang": self._release_language(),
                "newFeatures": self._new_features(),
            },
        )
        return resp.json()  # type: ignore[return-value]

    def _submit_release_v2(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "appId": self.app_id,
            "releaseType": self._release_int("release_type", 1),
            "remark": self._release_remark(),
        }
        if self.release_config.get("release_time"):
            params["releaseTime"] = self.release_config["release_time"]

        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/api/publish/v2/app-submit",
            params=params,
        )
        return resp.json()  # type: ignore[return-value]

    def _submit_release_v3(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "releaseType": self._release_int("release_type", 1),
            "releasePhase": self._release_int("release_phase", 0),
            "remark": self._release_remark(),
        }
        if self.release_config.get("release_time"):
            body["releaseTime"] = self.release_config["release_time"]
        if self.release_config.get("phased_release_description"):
            body["phasedReleaseDescription"] = self.release_config["phased_release_description"]

        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/api/publish/v3/app-submit",
            params={"appId": self.app_id},
            json=body,
        )
        return resp.json()  # type: ignore[return-value]

    def _brief_info_paths(self) -> list[str]:
        configured = self.release_config.get("brief_info_path")
        if configured:
            if isinstance(configured, str):
                return [configured]
            if isinstance(configured, list):
                return [str(item) for item in configured if item]
        return list(HUAWEI_BRIEF_INFO_PATHS)

    def _query_brief_info_list(self) -> dict[str, Any]:
        last_error: Exception | None = None
        params = {
            "appId": self.app_id,
            "releaseType": self._release_int("release_type", 1),
        }
        for path in self._brief_info_paths():
            try:
                resp = self._request_with_retry(
                    "GET",
                    f"{BASE_URL}{path}",
                    params=params,
                    max_retries=1,
                )
                data = resp.json()
                ret = data.get("ret", {})
                if isinstance(ret, dict) and ret.get("code", 0) not in (0, "0", None):
                    last_error = RuntimeError(f"Huawei brief info query failed: {data}")
                    continue
                items = self._extract_brief_items(data)
                if not items:
                    last_error = RuntimeError(
                        f"Huawei brief info query returned no version items from {path}: {data}"
                    )
                    continue
                return data  # type: ignore[return-value]
            except Exception as e:
                last_error = e
        raise RuntimeError(f"Huawei brief info query failed: {last_error}")

    def _is_brief_item(self, item: dict[str, Any]) -> bool:
        version_keys = {
            "versionCode",
            "version_code",
            "pkgVersionCode",
            "versionNumber",
            "versionName",
            "version_name",
            "pkgVersion",
            "onShelfVersionCode",
            "onShelfVersionNumber",
        }
        status_keys = {
            "auditStatus",
            "reviewStatus",
            "status",
            "releaseStatus",
            "releaseState",
            "state",
            "appStatus",
            "auditStatusName",
            "reviewStatusName",
            "statusName",
            "stateName",
        }
        return any(key in item for key in version_keys | status_keys)

    def _extract_brief_items(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = [
            data.get("appBriefInfoList"),
            data.get("briefInfoList"),
            data.get("appInfoList"),
            data.get("appInfo"),
            data.get("list"),
            data.get("items"),
            data.get("data"),
            data.get("result"),
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return [
                    item
                    for item in candidate
                    if isinstance(item, dict) and self._is_brief_item(item)
                ]
            if isinstance(candidate, dict):
                nested = self._extract_brief_items(candidate)
                if nested:
                    return nested
        return [data] if data and self._is_brief_item(data) else []

    def _version_code_of(self, item: dict[str, Any]) -> int:
        value = (
            item.get("versionCode")
            or item.get("version_code")
            or item.get("pkgVersionCode")
            or item.get("versionNumberCode")
            or item.get("version")
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _version_name_of(self, item: dict[str, Any]) -> str:
        value = (
            item.get("versionName")
            or item.get("version_name")
            or item.get("versionNumber")
            or item.get("pkgVersion")
            or ""
        )
        return str(value)

    def _on_shelf_version_code_of(self, item: dict[str, Any]) -> int:
        value = item.get("onShelfVersionCode") or item.get("onshelfVersionCode")
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _on_shelf_version_name_of(self, item: dict[str, Any]) -> str:
        return str(item.get("onShelfVersionNumber") or item.get("onshelfVersionNumber") or "")

    def _time_of(self, item: dict[str, Any]) -> int:
        for key in ("updateTime", "updatedTime", "submitTime", "createTime", "releaseTime"):
            value = item.get(key)
            if isinstance(value, int | float):
                return int(value)
            if isinstance(value, str):
                digits = re.sub(r"\D", "", value)
                if digits:
                    try:
                        return int(digits[:14])
                    except ValueError:
                        pass
        return 0

    def _select_latest_brief_item(
        self,
        items: list[dict[str, Any]],
        target_version_name: str = "",
        target_version_code: int | str = "",
    ) -> dict[str, Any]:
        if not items:
            raise RuntimeError("Huawei brief info list is empty")

        target_code = str(target_version_code or "")
        if target_code:
            for item in items:
                if str(self._version_code_of(item)) == target_code:
                    return item

        target_name = str(target_version_name or "")
        if target_name:
            for item in items:
                if self._version_name_of(item) == target_name:
                    return item

        reviewing_items = [
            item
            for item in items
            if self._huawei_audit_result(item)[0] == 0
        ]
        pool = reviewing_items or items
        return max(pool, key=lambda item: (self._version_code_of(item), self._time_of(item)))

    def _huawei_audit_result(self, item: dict[str, Any]) -> tuple[int, str]:
        status_value = (
            item.get("auditStatus")
            or item.get("reviewStatus")
            or item.get("status")
            or item.get("releaseStatus")
            or item.get("releaseState")
            or item.get("state")
            or item.get("appStatus")
            or ""
        )
        text = str(
            item.get("auditStatusName")
            or item.get("reviewStatusName")
            or item.get("statusName")
            or item.get("stateName")
            or status_value
            or "未知状态"
        )
        audit_info = item.get("auditInfo")
        if isinstance(audit_info, dict) and audit_info.get("auditOpinion"):
            text = str(audit_info.get("auditOpinion"))
        normalized = text.lower()
        status_text = str(status_value).lower()
        if any(word in normalized or word in status_text for word in HUAWEI_REVIEWING_WORDS):
            return 0, text
        if any(word in normalized or word in status_text for word in HUAWEI_PASSED_WORDS):
            return 1, text
        if any(word in normalized or word in status_text for word in HUAWEI_REJECTED_WORDS):
            return 2, text
        if str(status_value) in {"1"}:
            return 0, text
        version_code = self._version_code_of(item)
        on_shelf_version_code = self._on_shelf_version_code_of(item)
        if version_code > 0 and on_shelf_version_code > 0:
            if version_code > on_shelf_version_code:
                return 0, "待审核或待上架"
            if version_code == on_shelf_version_code:
                return 1, "当前版本已上架"
        return -1, text

    def get_review_status(
        self,
        package_name: str = "",
        target_version_name: str = "",
        target_version_code: int | str = "",
    ) -> dict[str, Any]:
        """Query Huawei latest or target app review status."""
        data = self._query_brief_info_list()
        items = self._extract_brief_items(data)
        item = self._select_latest_brief_item(items, target_version_name, target_version_code)
        audit_result, audit_status = self._huawei_audit_result(item)

        return {
            "app_id": self.app_id,
            "package_name": package_name or self.app_info.package_name,
            "release_id": item.get("releaseId") or item.get("versionId") or item.get("id", ""),
            "version_name": self._version_name_of(item),
            "version_code": self._version_code_of(item),
            "on_shelf_version_name": self._on_shelf_version_name_of(item),
            "on_shelf_version_code": self._on_shelf_version_code_of(item),
            "audit_result": audit_result,
            "audit_status": audit_status,
            "audit_message": (
                item.get("auditMessage")
                or item.get("reviewMessage")
                or (
                    item.get("auditInfo", {}).get("auditOpinion")
                    if isinstance(item.get("auditInfo"), dict)
                    else ""
                )
                or item.get("reason")
                or item.get("message")
                or ""
            ),
            "raw_status": (
                item.get("auditStatus")
                or item.get("reviewStatus")
                or item.get("status")
                or item.get("releaseStatus")
                or item.get("releaseState")
                or item.get("state")
                or ""
            ),
            "raw": item,
        }

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            if self._is_harmony_package(apk_info):
                upload_info = self._get_upload_info_for_obs(apk_info)
                object_id = self._upload_for_obs(upload_info, apk_info)
                package_result = self._update_package_info_for_obs(object_id, apk_info)
            else:
                upload_info = self._get_upload_info(apk_info)
                file_info = self._upload_apk(upload_info, apk_info)
                package_result = self._update_file_info(file_info, apk_info)
            ret_code = package_result.get("ret", {}).get("code", -1)
            if ret_code != 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Package update failed: {package_result.get('ret', {}).get('msg', 'unknown')}",
                    details=package_result,
                )

            is_harmony_package = self._is_harmony_package(apk_info)
            if not is_harmony_package:
                self._wait_for_compile_ready(package_result)

            language_result = self._update_language_info()
            ret_code = language_result.get("ret", {}).get("code", -1)
            if ret_code != 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message=f"Language info update failed: {language_result.get('ret', {}).get('msg', 'unknown')}",
                    details=language_result,
                )

            result = self._submit_release_v3() if is_harmony_package else self._submit_release_v2()
            ret_code = result.get("ret", {}).get("code", -1)
            if ret_code == 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Published successfully (submitted to Huawei review)",
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
