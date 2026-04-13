"""Store implementations."""

from typing import Any

from ..models import AppInfo, StoreName
from .base import BaseStore
from .huawei import HuaweiStore
from .honor import HonorStore
from .oppo import OppoStore
from .vivo import VivoStore
from .xiaomi import XiaomiStore
from .yingyongbao import YingyongbaoStore

# Map StoreName → Store class
STORE_REGISTRY: dict[StoreName, type[BaseStore]] = {
    StoreName.YINGYONGBAO: YingyongbaoStore,
    StoreName.HUAWEI: HuaweiStore,
    StoreName.HONOR: HonorStore,
    StoreName.VIVO: VivoStore,
    StoreName.OPPO: OppoStore,
    StoreName.XIAOMI: XiaomiStore,
}

# Map StoreName → config key
STORE_CONFIG_KEYS: dict[StoreName, str] = {
    StoreName.YINGYONGBAO: "yingyongbao",
    StoreName.HUAWEI: "huawei",
    StoreName.HONOR: "honor",
    StoreName.VIVO: "vivo",
    StoreName.OPPO: "oppo",
    StoreName.XIAOMI: "xiaomi",
}


def create_store(
    store_name: StoreName,
    all_store_configs: dict[str, Any],
    app_info: AppInfo,
) -> BaseStore:
    """Instantiate a store from config."""
    config_key = STORE_CONFIG_KEYS[store_name]
    store_config = all_store_configs.get(config_key, {})
    cls = STORE_REGISTRY[store_name]
    return cls(store_config, app_info)


__all__ = [
    "BaseStore",
    "HuaweiStore",
    "HonorStore",
    "OppoStore",
    "VivoStore",
    "XiaomiStore",
    "YingyongbaoStore",
    "STORE_REGISTRY",
    "STORE_CONFIG_KEYS",
    "create_store",
]
