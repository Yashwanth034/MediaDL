"""Persistent direct-video download orchestration with basic duplicate protection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mediadl.core.errors import InputError
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.basic import BasicDedupeService, BasicDuplicateKind
from mediadl.downloads.format_policy import FormatPolicyBuilder
from mediadl.downloads.single import DownloadReceipt, SingleDownloadRequest, SingleDownloadService
from mediadl.engines.ytdlp import PostprocessorHook, ProgressHook
from mediadl.index.repository import IndexRepository
from mediadl.sources.models import MediaItemStub, ScanResult, SourceDescriptor, SourceKind


class DirectDownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    SKIPPED_DUPLICATE = "skipped_duplicate"


@dataclass(frozen=True, slots=True)
class DirectDownloadResult:
    status: DirectDownloadStatus
    title: str
    output_path: str | None = None


class DirectDownloadCoordinator:
    """Give one-off video URLs persistent dedupe/history without creating a collection job."""

    def __init__(
        self,
        downloader: SingleDownloadService,
        index: IndexRepository,
        dedupe: BasicDedupeService,
    ) -> None:
        self.downloader = downloader
        self.index = index
        self.dedupe = dedupe

    def download(
        self,
        source: SourceDescriptor,
        request: SingleDownloadRequest,
        *,
        dedupe_mode: DedupeMode,
        progress_hook: ProgressHook | None = None,
        postprocessor_hook: PostprocessorHook | None = None,
    ) -> DirectDownloadResult:
        if source.kind is not SourceKind.VIDEO:
            raise InputError("Direct download coordinator accepts only a single-video source")
        policy = FormatPolicyBuilder.build(request.output_format, request.quality)

        if dedupe_mode is not DedupeMode.OFF:
            existing = self.dedupe.check_source_profile(
                media_key=source.source_key,
                output_format=request.output_format,
                quality=policy.quality_label,
            )
            if existing.is_duplicate:
                return DirectDownloadResult(
                    DirectDownloadStatus.SKIPPED_DUPLICATE,
                    title=source.source_key,
                    output_path=existing.path,
                )

        if postprocessor_hook is None:
            receipt = self.downloader.download(request, progress_hook=progress_hook)
        else:
            receipt = self.downloader.download(
                request,
                progress_hook=progress_hook,
                postprocessor_hook=postprocessor_hook,
            )
        self._index_receipt(source, receipt)
        output_path = receipt.output_path

        if output_path is None or not output_path.is_file():
            self.dedupe.mark_source_completed(
                media_key=source.source_key,
                output_format=request.output_format,
                quality=receipt.quality,
                output_path=None,
            )
            return DirectDownloadResult(DirectDownloadStatus.DOWNLOADED, receipt.title)

        if dedupe_mode is not DedupeMode.OFF:
            exact = self.dedupe.check_exact_file(output_path)
            if (
                exact.kind is BasicDuplicateKind.EXACT_FILE
                and exact.path is not None
                and _remove_new_duplicate(output_path, Path(exact.path))
            ):
                self.dedupe.mark_source_completed(
                    media_key=source.source_key,
                    output_format=request.output_format,
                    quality=receipt.quality,
                    output_path=exact.path,
                )
                return DirectDownloadResult(
                    DirectDownloadStatus.SKIPPED_DUPLICATE,
                    receipt.title,
                    exact.path,
                )
            precomputed_sha256 = exact.sha256
        else:
            precomputed_sha256 = None

        registered = self.dedupe.register_completed_file(
            media_key=source.source_key,
            output_format=request.output_format,
            quality=receipt.quality,
            path=output_path,
            precomputed_sha256=precomputed_sha256,
        )
        return DirectDownloadResult(
            DirectDownloadStatus.DOWNLOADED,
            receipt.title,
            registered.path or str(output_path),
        )

    def _index_receipt(self, source: SourceDescriptor, receipt: DownloadReceipt) -> None:
        item = MediaItemStub(
            media_key=source.source_key,
            title=receipt.title,
            url=source.url,
            availability="public",
            media_type="video",
        )
        self.index.index_scan(
            ScanResult(
                source=source,
                title=receipt.title,
                items=(item,),
                reported_count=1,
            ),
            complete_scan=True,
        )


def _remove_new_duplicate(downloaded: Path, kept: Path) -> bool:
    try:
        downloaded_resolved = downloaded.resolve(strict=True)
        kept_resolved = kept.resolve(strict=True)
    except OSError:
        return False
    if downloaded_resolved == kept_resolved or not kept_resolved.is_file():
        return False
    try:
        downloaded_resolved.unlink()
    except OSError:
        return False
    return True
