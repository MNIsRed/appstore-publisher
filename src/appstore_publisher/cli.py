"""CLI entry point for appstore-publisher."""

import glob
import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .config import find_config, get_app_info, load_config
from .publisher import print_results, publish_apks

console = Console()


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


def main() -> None:
    """Entry point for the CLI."""
    cli(obj={})


if __name__ == "__main__":
    main()
