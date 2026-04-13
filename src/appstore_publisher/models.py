"""Data models for appstore-publisher."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class StoreName(str, Enum):
    YINGYONGBAO = "yingyongbao"
    HUAWEI = "huawei"
    HONOR = "honor"
    VIVO = "vivo"
    OPPO = "oppo"
    XIAOMI = "xiaomi"


class PublishStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ApkInfo:
    """Information about an APK file to publish."""
    path: Path
    channel: Optional[StoreName] = None
    version_name: str = ""
    version_code: int = 0
    package_name: str = ""


@dataclass
class PublishResult:
    """Result of publishing to a single store."""
    store: StoreName
    apk_path: Path
    status: PublishStatus = PublishStatus.PENDING
    message: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class AppInfo:
    """Common app information."""
    package_name: str = ""
    app_name: str = ""
    changelog: str = "Bug fixes and improvements"
