"""Abstract base class for all store implementations."""

import abc
import logging
from typing import Any

import requests

from ..models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName

logger = logging.getLogger(__name__)


class BaseStore(abc.ABC):
    """Base class that all store implementations must extend."""

    name: StoreName
    display_name: str

    def __init__(self, store_config: dict[str, Any], app_info: AppInfo):
        self.config = store_config
        self.app_info = app_info
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "AppStorePublisher/0.1.0"})

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)

    def validate_config(self) -> list[str]:
        """Return list of missing required config keys. Empty = OK."""
        return []

    @abc.abstractmethod
    def authenticate(self) -> bool:
        """Authenticate with the store. Return True on success."""
        ...

    @abc.abstractmethod
    def upload_apk(self, apk_info: ApkInfo) -> PublishResult:
        """Upload an APK and submit the update. Return result."""
        ...

    def publish(self, apk_info: ApkInfo) -> PublishResult:
        """Full publish flow: validate → auth → upload."""
        if not self.enabled:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.SKIPPED,
                message=f"{self.display_name} is disabled in config",
            )

        missing = self.validate_config()
        if missing:
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=f"Missing config: {', '.join(missing)}",
            )

        try:
            if not self.authenticate():
                return PublishResult(
                    store=self.name,
                    apk_path=apk_info.path,
                    status=PublishStatus.FAILED,
                    message="Authentication failed",
                )
            return self.upload_apk(apk_info)
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error publishing to {self.display_name}: {e}")
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=f"Network error: {e}",
            )
        except Exception as e:
            logger.exception(f"Unexpected error publishing to {self.display_name}")
            return PublishResult(
                store=self.name,
                apk_path=apk_info.path,
                status=PublishStatus.FAILED,
                message=f"Error: {e}",
            )

    def _request_with_retry(
        self, method: str, url: str, max_retries: int = 3, **kwargs: Any
    ) -> requests.Response:
        """Make an HTTP request with exponential backoff retry."""
        import time

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = self.session.request(method, url, timeout=120, **kwargs)
                resp.raise_for_status()
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                wait = 1.0 * (2 ** attempt)
                logger.warning(
                    f"[{self.display_name}] Attempt {attempt+1}/{max_retries} failed: {e}. "
                    f"Retrying in {wait:.1f}s..."
                )
                time.sleep(wait)
            except requests.exceptions.HTTPError as e:
                # Don't retry 4xx client errors
                if e.response is not None and 400 <= e.response.status_code < 500:
                    raise
                last_exc = e
                wait = 1.0 * (2 ** attempt)
                logger.warning(f"[{self.display_name}] HTTP {e.response.status_code if e.response else '?'}: retrying...")
                time.sleep(wait)
        raise last_exc  # type: ignore[misc]
