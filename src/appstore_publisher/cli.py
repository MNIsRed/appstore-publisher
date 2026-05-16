"""CLI entry point for appstore-publisher."""

import glob
import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .config import find_config, get_app_info, get_store_config, load_config
from .publisher import print_results, publish_apks

console = Console()


def _mask_secret(value: object, keep_start: int = 4, keep_end: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep_start + keep_end:
        return "***"
    return f"{text[:keep_start]}***{text[-keep_end:]}"


@click.group()
@click.version_option(version=__version__)
@click.option(
    "-c", "--config",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to config.toml file. Auto-detected if not specified.",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose/debug logging.")
@click.pass_context
def cli(ctx: click.Context, config: Path | None, verbose: bool) -> None:
    """App Store Publisher — auto-publish APKs to Chinese Android stores."""
    # Setup logging
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load config
    if config is not None:
        if not config.is_file():
            console.print(f"[red]Error:[/red] Config file not found: {config}")
            ctx.exit(1)
        config_path = config
    else:
        config_path = find_config()
        if config_path is None:
            console.print(
                "[red]Error:[/red] No config.toml found. "
                "Create one from config.example.toml or use --config."
            )
            ctx.exit(1)

    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["config"] = load_config(config_path)

    if verbose:
        console.print(f"[dim]Using config: {config_path}[/dim]")


@cli.command()
@click.argument("apk_files", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, help="Detect channels and show plan without uploading.")
@click.pass_context
def publish(ctx: click.Context, apk_files: tuple[Path, ...], dry_run: bool) -> None:
    """Publish APK files to their corresponding app stores.

    APK_FILES can be file paths or glob patterns.

    Examples:

      appstore-publisher publish ./release-*.apk

      appstore-publisher publish release-vivo.apk release-oppo.apk

      appstore-publisher publish --dry-run ./apks/
    """
    config = ctx.obj["config"]

    # Expand globs and directories
    resolved_paths: list[Path] = []
    for pattern in apk_files:
        if pattern.is_dir():
            resolved_paths.extend(sorted(pattern.glob("*.apk")))
        elif any(c in str(pattern) for c in ["*", "?", "["]):
            expanded = glob.glob(str(pattern))
            resolved_paths.extend(Path(p) for p in sorted(expanded))
        elif pattern.is_file():
            resolved_paths.append(pattern)
        else:
            console.print(f"[yellow]Warning:[/yellow] File not found: {pattern}")

    if not resolved_paths:
        console.print("[red]Error:[/red] No APK files found.")
        ctx.exit(1)

    # Show what we found
    app_info = get_app_info(config)
    if app_info.package_name:
        console.print(f"[dim]App: {app_info.app_name or app_info.package_name}[/dim]")
    console.print(f"[dim]Found {len(resolved_paths)} APK file(s)[/dim]")

    if dry_run:
        console.print("[yellow]🔍 Dry run mode — no uploads will be performed[/yellow]\n")

    # Publish
    results = publish_apks(resolved_paths, config, dry_run=dry_run)
    print_results(results)

    # Exit with error code if any failed
    if any(r.status.value == "failed" for r in results):
        ctx.exit(1)


@cli.command()
@click.pass_context
def channels(ctx: click.Context) -> None:
    """Show available channels and their store names."""
    from .channel_detector import CHANNEL_MAP
    from rich.table import Table

    table = Table(title="📱 Supported Channels")
    table.add_column("Filename suffix", style="cyan")
    table.add_column("Store", style="white")
    table.add_column("Config key", style="dim")

    seen: set = set()
    for suffix, store in CHANNEL_MAP.items():
        if store not in seen:
            seen.add(store)
            table.add_row(suffix, store.value, store.value)

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]Files matching *-{channel}.apk or *-{channel}-signed.apk[/dim]")


@cli.group()
@click.pass_context
def diagnose(ctx: click.Context) -> None:
    """Run store-specific diagnostics without uploading APKs."""


@diagnose.command("vivo")
@click.option("--package-name", default="", help="Package name to query. Defaults to app.package_name.")
@click.pass_context
def diagnose_vivo(ctx: click.Context, package_name: str) -> None:
    """Diagnose vivo app.query.details access and signed request parameters."""
    from rich.panel import Panel
    from rich.table import Table

    from .models import StoreName
    from .stores import create_store
    from .stores.vivo import METHOD_APP_DETAIL, VivoStore

    config = ctx.obj["config"]
    app_info = get_app_info(config)
    store_configs = config.get("stores", {})
    store_config = get_store_config(config, StoreName.VIVO)
    store = create_store(StoreName.VIVO, store_configs, app_info)
    if not isinstance(store, VivoStore):
        console.print("[red]Error:[/red] vivo store implementation is unavailable.")
        ctx.exit(1)

    target_package = package_name or app_info.package_name
    if not target_package:
        console.print("[red]Error:[/red] Missing package name. Set app.package_name or pass --package-name.")
        ctx.exit(1)

    missing = store.validate_config()
    if missing:
        console.print("[red]Error:[/red] Missing config: " + ", ".join(missing))
        ctx.exit(1)

    preview = store.build_diagnostic_app_detail_request(target_package)
    params = preview["params"]

    table = Table(title="vivo app.query.details request preview")
    table.add_column("Field", style="cyan")
    table.add_column("Value", overflow="fold")
    table.add_row("url", str(preview["url"]))
    table.add_row("packageName", target_package)
    table.add_row("access_key", _mask_secret(store_config.get("access_key", "")))
    table.add_row("method", str(params.get("method", "")))
    table.add_row("sign_method", str(params.get("sign_method", "")))
    table.add_row("v", str(params.get("v", "")))
    table.add_row("timestamp", str(params.get("timestamp", "")))
    table.add_row("signed_keys", ", ".join(preview["signed_keys"]))
    console.print(table)

    try:
        result = store._signed_request(
            METHOD_APP_DETAIL,
            store._build_app_detail_params(target_package),
        )
    except Exception as e:
        console.print(Panel(str(e), title="Request exception", style="red"))
        ctx.exit(1)

    response_table = Table(title="vivo raw response")
    response_table.add_column("Field", style="cyan")
    response_table.add_column("Value", overflow="fold")
    for key in ("code", "subCode", "msg", "subMsg"):
        response_table.add_row(key, str(result.get(key, "")))
    console.print(response_table)

    data = result.get("data")
    if isinstance(data, dict):
        console.print(f"[green]OK[/green] vivo returned data keys: {', '.join(sorted(data.keys()))}")
    else:
        console.print("[yellow]vivo did not return app detail data.[/yellow]")
        if str(result.get("code", "")) == "10018":
            console.print(
                "[yellow]Hint:[/yellow] vivo returned code=10018. "
                "The Open API document uses code=23 for illegal signatures, so this response usually points to "
                "access-key permission, platform/account namespace, or method authorization rather than a local HMAC mismatch."
            )


def main() -> None:
    """Entry point for the CLI."""
    cli(obj={})


if __name__ == "__main__":
    main()
