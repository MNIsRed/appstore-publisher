"""Config loading and validation."""

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

from .models import AppInfo

CONFIG_FILENAMES = ["config.toml", "config.local.toml"]


def find_config(search_dir: Path | None = None) -> Path | None:
    """Find config.toml in the current directory or parent directories."""
    if search_dir is None:
        search_dir = Path.cwd()
    for directory in [search_dir, *search_dir.parents]:
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(path: Path) -> dict[str, Any]:
    """Load and return parsed TOML config."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def get_app_info(config: dict[str, Any]) -> AppInfo:
    """Extract common app info from config."""
    app = config.get("app", {})
    changelog_section = config.get("changelog", {})
    return AppInfo(
        package_name=app.get("package_name", ""),
        app_name=app.get("app_name", ""),
        changelog=changelog_section.get("default", "Bug fixes and improvements"),
    )


def get_store_config(config: dict[str, Any], store_name: str) -> dict[str, Any]:
    """Get config for a specific store."""
    stores = config.get("stores", {})
    return stores.get(store_name, {})
