"""Channel detection from APK filenames."""

import re
from pathlib import Path
from typing import Optional

from .models import ApkInfo, StoreName

# Aliases for Tencent Yingyongbao
_TENCENT_ALIASES = {"yingyongbao", "tencent", "qq"}

# Channel name → StoreName mapping
CHANNEL_MAP: dict[str, StoreName] = {
    "vivo": StoreName.VIVO,
    "oppo": StoreName.OPPO,
    "huawei": StoreName.HUAWEI,
    "honor": StoreName.HONOR,
    "xiaomi": StoreName.XIAOMI,
    "mi": StoreName.XIAOMI,
    "yingyongbao": StoreName.YINGYONGBAO,
    "tencent": StoreName.YINGYONGBAO,
    "qq": StoreName.YINGYONGBAO,
}

_CHANNEL_TOKEN_PATTERN = re.compile(r"[-_]+")


def detect_channel(filename: str) -> Optional[StoreName]:
    """Detect the target store channel from an APK filename.

    Examples:
        release-vivo.apk       → StoreName.VIVO
        app-oppo-signed.apk    → StoreName.OPPO
        app-honor-release.apk  → StoreName.HONOR
        com.example.app.apk    → None (no channel)
    """
    stem = filename.removesuffix(".apk").removesuffix(".APK")
    for token in _CHANNEL_TOKEN_PATTERN.split(stem.lower()):
        store = CHANNEL_MAP.get(token)
        if store:
            return store
    return None


def extract_apk_info(apk_path: Path) -> ApkInfo:
    """Extract channel info from an APK file path."""
    channel = detect_channel(apk_path.name)
    return ApkInfo(
        path=apk_path,
        channel=channel,
    )


def group_by_channel(apk_files: list[Path]) -> dict[StoreName, list[ApkInfo]]:
    """Group APK files by detected channel. Skips files with no channel."""
    result: dict[StoreName, list[ApkInfo]] = {}
    for apk in apk_files:
        info = extract_apk_info(apk)
        if info.channel is None:
            continue
        result.setdefault(info.channel, []).append(info)
    return result
