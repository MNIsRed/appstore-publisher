"""Tencent Yingyongbao (应用宝) store implementation."""

import hashlib
import base64
import logging
from typing import Any

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from ..utils import load_pem_key
from .base import BaseStore

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open.qq.com"


class YingyongbaoStore(BaseStore):
    name = StoreName.YINGYONGBAO
    display_name = "Tencent Yingyongbao (应用宝)"

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        super().__init__(store_config, app_info)
        self.user_id: str = store_config.get("user_id", "")
        self.private_key_path: str = store_config.get("private_key_path", "")
        self._private_key_pem: str = ""

    def validate_config(self) -> list[str]:
        missing = []
        if not self.config.get("user_id"):
            missing.append("stores.yingyongbao.user_id")
        if not self.config.get("private_key_path"):
            missing.append("stores.yingyongbao.private_key_path")
        return missing

    def _sign_params(self, params: dict[str, Any]) -> str:
        """Generate RSA signature for params.

        Yingyongbao uses: sort params, concatenate values, MD5, then RSA sign.
        """
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        if not self._private_key_pem:
            from pathlib import Path
            self._private_key_pem = load_pem_key(Path(self.private_key_path))

        # Sort and concatenate param values
        sorted_params = sorted(params.items())
        values = "".join(f"{k}={v}" for k, v in sorted_params)
        md5_digest = hashlib.md5(values.encode("utf-8")).digest()

        private_key = serialization.load_pem_private_key(
            self._private_key_pem.encode("utf-8"), password=None
        )
        signature = private_key.sign(
            md5_digest,
            padding.PKCS1v15(),
            hashes.MD5(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def authenticate(self) -> bool:
        # Yingyongbao uses request-time signing, no separate auth step
        if not self._private_key_pem:
            from pathlib import Path
            try:
                self._private_key_pem = load_pem_key(Path(self.private_key_path))
            except Exception as e:
                logger.error(f"Failed to load private key: {e}")
                return False
        return True

    def _get_upload_url(self) -> dict[str, Any]:
        """Get COS pre-signed upload URL from Yingyongbao."""
        params = {
            "user_id": self.user_id,
            "app_name": self.app_info.app_name,
            "pkg_name": self.app_info.package_name,
        }
        sign = self._sign_params(params)
        params["sign"] = sign

        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/get_file_upload_info",
            json=params,
        )
        data = resp.json()
        if data.get("ret") != 0:
            raise RuntimeError(f"Yingyongbao get_upload_url failed: {data.get('msg', 'unknown error')}")
        return data  # type: ignore[return-value]

    def _upload_to_cos(self, upload_info: dict[str, Any], apk_info: ApkInfo) -> None:
        """Upload APK file to Tencent COS using pre-signed URL."""
        cos_url = upload_info["url"]
        with open(apk_info.path, "rb") as f:
            self._request_with_retry(
                "PUT",
                cos_url,
                data=f,
                headers={"Content-Type": "application/octet-stream"},
            )

    def _update_app(self, upload_info: dict[str, Any]) -> dict[str, Any]:
        """Submit the app update to Yingyongbao."""
        params = {
            "user_id": self.user_id,
            "pkg_name": self.app_info.package_name,
            "file_name": upload_info.get("file_name", ""),
            "desc": self.app_info.changelog,
        }
        sign = self._sign_params(params)
        params["sign"] = sign

        resp = self._request_with_retry(
            "POST",
            f"{BASE_URL}/update_app",
            json=params,
        )
        return resp.json()  # type: ignore[return-value]

    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        logger.info(f"[{self.display_name}] Uploading {apk_info.path.name}...")

        try:
            # Step 1: Get upload URL
            upload_info = self._get_upload_url()

            # Step 2: Upload to COS
            self._upload_to_cos(upload_info, apk_info)

            # Step 3: Submit update
            result = self._update_app(upload_info)

            if result.get("ret") == 0:
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.SUCCESS,
                    message="Published successfully (instant, no review)",
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
