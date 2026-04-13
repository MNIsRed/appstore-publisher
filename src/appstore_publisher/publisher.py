"""Main publisher orchestrator."""

import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .channel_detector import group_by_channel
from .config import get_app_info, get_store_config
from .models import ApkInfo, AppInfo, PublishResult, PublishStatus, StoreName
from .stores import STORE_REGISTRY, create_store

logger = logging.getLogger(__name__)
console = Console()


def publish_apks(
    apk_paths: list[Path],
    config: dict[str, Any],
    dry_run: bool = False,
) -> list[PublishResult]:
    """Publish APKs to their respective stores.

    Args:
        apk_paths: List of APK file paths to publish.
        config: Parsed TOML config dict.
        dry_run: If True, detect channels and show plan without uploading.

    Returns:
        List of PublishResult for each store publish attempt.
    """
    app_info = get_app_info(config)
    store_configs = config.get("stores", {})

    # Group APKs by channel
    channel_groups = group_by_channel(apk_paths)

    if not channel_groups:
        console.print("[yellow]No channel APKs found. Files must match *-{channel}.apk pattern.[/yellow]")
        return []

    # Show detected channels
    results: list[PublishResult] = []

    if dry_run:
        console.print("\n[bold cyan]📋 Dry Run — Detected Plan:[/bold cyan]")
        for store_name, apks in channel_groups.items():
            for apk in apks:
                console.print(f"  [green]✓[/green] {apk.path.name} → [bold]{store_name.value}[/bold]")
        console.print()
        return results

    # Publish to each store
    total = sum(len(apks) for apks in channel_groups.values())
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Publishing...", total=total)

        for store_name, apks in channel_groups.items():
            store = create_store(store_name, store_configs, app_info)

            for apk in apks:
                progress.update(task, description=f"[{store.display_name}] {apk.path.name}")
                result = store.publish(apk)
                results.append(result)
                progress.advance(task)

    return results


def print_results(results: list[PublishResult]) -> None:
    """Print publish results as a rich table."""
    from rich.table import Table

    if not results:
        return

    table = Table(title="📊 Publish Results")
    table.add_column("Store", style="cyan")
    table.add_column("APK", style="white")
    table.add_column("Status")
    table.add_column("Message", style="dim")

    for r in results:
        status_emoji = {
            PublishStatus.SUCCESS: "✅",
            PublishStatus.FAILED: "❌",
            PublishStatus.SKIPPED: "⏭️",
            PublishStatus.PENDING: "⏳",
            PublishStatus.UPLOADING: "🔄",
        }
        emoji = status_emoji.get(r.status, "❓")
        status_color = {
            PublishStatus.SUCCESS: "green",
            PublishStatus.FAILED: "red",
            PublishStatus.SKIPPED: "yellow",
        }.get(r.status, "white")

        table.add_row(
            r.store.value,
            r.apk_path.name,
            f"[{status_color}]{emoji} {r.status.value}[/{status_color}]",
            r.message,
        )

    console.print()
    console.print(table)
    console.print()

    # Summary
    success = sum(1 for r in results if r.status == PublishStatus.SUCCESS)
    failed = sum(1 for r in results if r.status == PublishStatus.FAILED)
    skipped = sum(1 for r in results if r.status == PublishStatus.SKIPPED)

    parts = []
    if success:
        parts.append(f"[green]{success} succeeded[/green]")
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    if skipped:
        parts.append(f"[yellow]{skipped} skipped[/yellow]")

    console.print(f"  Summary: {' | '.join(parts)}")
