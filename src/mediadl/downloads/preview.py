"""Concise, UI-agnostic download-plan preview summaries."""

from __future__ import annotations

from dataclasses import dataclass

from mediadl.downloads.plan import DownloadPlan


@dataclass(frozen=True, slots=True)
class PlanPreview:
    source: str
    selected_count: int
    selection: str
    sort: str
    output_format: str
    quality: str
    dedupe: str
    destination: str
    estimated_size: str
    free_space: str
    warnings: tuple[str, ...]


def build_preview(plan: DownloadPlan) -> PlanPreview:
    warnings: list[str] = []
    if plan.disk_sufficient is False:
        warnings.append("Estimated download size exceeds currently available disk space.")
    if plan.estimated_size_bytes is None:
        warnings.append("Download size estimate is unavailable for this plan.")

    return PlanPreview(
        source=plan.source.title or plan.source.source_key,
        selected_count=plan.item_count,
        selection=plan.selection_label,
        sort=plan.sort_mode,
        output_format=plan.output_format.upper(),
        quality=plan.quality,
        dedupe=plan.dedupe_mode,
        destination=plan.output_dir,
        estimated_size=_format_bytes(plan.estimated_size_bytes),
        free_space=_format_bytes(plan.available_disk_bytes),
        warnings=tuple(warnings),
    )


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "Unknown"
    if value < 0:
        return "Unknown"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.1f} {unit}"
