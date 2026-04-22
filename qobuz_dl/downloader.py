import errno
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pathvalidate import sanitize_filename, sanitize_filepath
from tqdm import tqdm

import qobuz_dl.metadata as metadata
from qobuz_dl.color import OFF, GREEN, RED, YELLOW, CYAN, RESET
from qobuz_dl.exceptions import NonStreamable

QL_DOWNGRADE = "FormatRestrictedByFormatAvailability"
# Quality fallback chain: try each level in order until one works
_QUALITY_FALLBACK_CHAIN = [27, 7, 6, 5]
# used in case of error
DEFAULT_FORMATS = {
    "MP3": [
        "{albumartist}/{album} ({year}) [MP3]",
        "{tracknumber} - {tracktitle}",
    ],
    "Unknown": [
        "{albumartist}/{album}",
        "{tracknumber} - {tracktitle}",
    ],
}

DEFAULT_FOLDER = "{albumartist}/{album} ({year}) [{bit_depth}B-{sampling_rate}kHz]"
DEFAULT_TRACK = "{tracknumber} - {tracktitle}"
DEFAULT_STAGING = "auto"

logger = logging.getLogger(__name__)

_AUTO_STAGING_ROOT = os.path.join(
    os.path.expanduser("~"),
    ".cache",
    "qobuz-dl",
    "staging",
)
_STAGING_DISABLED_VALUES = {"", "0", "off", "none", "false", "disabled"}
_STAGING_MOVE_RETRIES = 6
_STAGING_MOVE_DELAY = 0.35

# Module-level download speed limit in bytes/sec.  0 = unlimited.
# Set by cli.py from --limit-rate flag.
_rate_limit_bps = 0
_TQDM_MININTERVAL = 0.12
_TQDM_MAXINTERVAL = 0.80
_TQDM_SMOOTHING = 0.10
_COMPACT_PROGRESS_COLS = 96
_WIDE_BAR_WIDTH = 25
_COMPACT_BAR_WIDTH = 14

# Descriptive classification for Qobuz restriction codes.
_RESTRICTION_DETAILS = {
    "FormatRestrictedByFormatAvailability": (
        "QUALITY_UNAVAILABLE",
        "Requested quality is not available for this track.",
    ),
    "TrackRestrictedByPurchaseCredentials": (
        "PURCHASE_REQUIRED",
        "Track requires purchase credentials for full download.",
    ),
    "SampleRestrictedByRightHolders": (
        "RIGHTSHOLDER_SAMPLE_ONLY",
        "Only a sample is available due to rightsholder restrictions.",
    ),
    "TrackRestrictedByRightHolders": (
        "RIGHTSHOLDER_BLOCKED",
        "Track download is blocked by rightsholder policy.",
    ),
    "TrackRestrictedByTerritorialAvailability": (
        "GEO_RESTRICTED",
        "Track is unavailable in your current region.",
    ),
}


def _describe_restrictions(track_url_dict):
    """Return a human-readable string describing why a track was skipped."""
    restrictions = track_url_dict.get("restrictions", [])
    if not restrictions:
        return "[NO_URL] API did not return a downloadable URL (sample/demo or unavailable item)."

    # Keep order while removing duplicates across repeated codes.
    seen_codes = set()
    reasons = []
    for restriction in restrictions:
        code = (
            restriction.get("code", "unknown")
            if isinstance(restriction, dict)
            else str(restriction)
        )
        if code in seen_codes:
            continue
        seen_codes.add(code)

        category, description = _RESTRICTION_DETAILS.get(
            code,
            (
                "UNKNOWN_RESTRICTION",
                "Qobuz returned an unrecognized restriction code.",
            ),
        )
        extra_message = ""
        if isinstance(restriction, dict) and restriction.get("message"):
            extra_message = f" message={restriction['message']}"

        reasons.append(
            f"[{category}] {description} (code={code}{extra_message})"
        )

    return "; ".join(reasons)


def _is_windows_mount(path: str) -> bool:
    if os.name == "nt":
        return False
    return os.path.abspath(path).startswith("/mnt/")


def _normalize_staging_setting(value) -> str:
    if value is None:
        return DEFAULT_STAGING

    normalized = str(value).strip()
    if not normalized:
        return DEFAULT_STAGING

    normalized_lower = normalized.lower()
    if normalized_lower in _STAGING_DISABLED_VALUES:
        return ""
    if normalized_lower == DEFAULT_STAGING:
        return DEFAULT_STAGING

    return os.path.abspath(os.path.expanduser(normalized))


def _ellipsis_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    head_len = (max_len - 3) // 2
    tail_len = max_len - 3 - head_len
    return f"{text[:head_len]}...{text[-tail_len:]}"


def _terminal_columns() -> int:
    return shutil.get_terminal_size((120, 24)).columns


def _is_compact_progress_layout() -> bool:
    return _terminal_columns() <= _COMPACT_PROGRESS_COLS


def _progress_bar_width(compact: bool) -> int:
    return _COMPACT_BAR_WIDTH if compact else _WIDE_BAR_WIDTH


def _progress_desc_width(compact: bool) -> int:
    cols = _terminal_columns()
    reserve = 42 if compact else 64
    max_len = 48 if compact else 52
    return max(20, min(max_len, cols - reserve))


def _build_master_bar_format(compact: bool) -> str:
    if compact:
        return (
            GREEN
            + "{desc} "
            + f"|{{bar:{_progress_bar_width(compact)}}}| "
            + "{n_fmt}/{total_fmt}"
            + RESET
            + "\033[K"
        )
    return (
        GREEN
        + "{desc} "
        + f"|{{bar:{_progress_bar_width(compact)}}}| "
        + "{percentage:3.0f}% "
        + "{n_fmt}/{total_fmt}"
        + RESET
        + "\033[K"
    )


def _build_transfer_bar_format(compact: bool, segmented: bool = False) -> str:
    seg_suffix = " [seg]" if segmented else ""
    if compact:
        return (
            CYAN
            + "{n_fmt}/{total_fmt} "
            + f"|{{bar:{_progress_bar_width(compact)}}}| "
            + "{percentage:3.0f}% "
            + "\u2502 {desc}"
            + seg_suffix
            + "\033[K"
        )
    return (
        CYAN
        + "{n_fmt}/{total_fmt} "
        + f"|{{bar:{_progress_bar_width(compact)}}}| "
        + "{percentage:3.0f}% "
        + "{rate_fmt} "
        + "ETA {remaining} "
        + "\u2502 {desc}"
        + seg_suffix
        + "\033[K"
    )


def _build_postprocess_bar_format(compact: bool, color: str, status: str) -> str:
    status_col = f"{status:<9}"
    return (
        color
        + f"|{{bar:{_progress_bar_width(compact)}}}| "
        + f"{status_col} \u2502 {{desc}}"
        + RESET
        + "\033[K"
    )


def _fit_progress_desc(desc: str, compact: Optional[bool] = None) -> str:
    # Keep progress rows table-like: same description column width at all states.
    if compact is None:
        compact = _is_compact_progress_layout()
    desc_width = _progress_desc_width(compact)
    return _ellipsis_middle(desc, desc_width).ljust(desc_width)


def _format_master_progress(
    done: int,
    total: int,
    downloaded: int = 0,
    skipped: int = 0,
    failed: int = 0,
    active: int = 0,
    compact: bool = False,
) -> str:
    width = max(2, len(str(max(total, 0))))
    label_ok, label_sk, label_er, label_act = ("o", "s", "e", "a") if compact else (
        "ok", "sk", "er", "act"
    )
    return (
        f"[{done:0{width}d}/{total:0{width}d}] "
        f"{label_ok}:{downloaded:0{width}d} "
        f"{label_sk}:{skipped:0{width}d} "
        f"{label_er}:{failed:0{width}d} "
        f"{label_act}:{active:0{width}d}"
    )


class WorkerSlotAllocator:
    """Assign reusable tqdm slots to active worker tasks."""

    def __init__(self, slot_count: int):
        self._slot_count = max(1, int(slot_count))
        self._lock = threading.Lock()
        self._available_slots = list(range(self._slot_count))
        self._in_use = set()

    def acquire_slot(self) -> int:
        with self._lock:
            if not self._available_slots:
                # Defensive fallback; should not happen with fixed-size worker pool.
                return 0
            slot = self._available_slots.pop(0)
            self._in_use.add(slot)
            return slot

    def release_slot(self, slot: int) -> None:
        with self._lock:
            if slot in self._in_use:
                self._in_use.remove(slot)
                self._available_slots.append(slot)
                self._available_slots.sort()


class Download:
    def __init__(
        self,
        client,
        item_id: str,
        path: str,
        quality: int,
        embed_art: bool = False,
        albums_only: bool = False,
        downgrade_quality: bool = False,
        cover_og_quality: bool = False,
        no_cover: bool = False,
        folder_format=None,
        track_format=None,
        show_master_progress: bool = True,
        prefetched_meta=None,
        staging_directory=DEFAULT_STAGING,
    ):
        self.client = client
        self.item_id = item_id
        self.path = path
        self.quality = quality
        self.albums_only = albums_only
        self.embed_art = embed_art
        self.downgrade_quality = downgrade_quality
        self.cover_og_quality = cover_og_quality
        self.no_cover = no_cover
        self.folder_format = folder_format or DEFAULT_FOLDER
        self.track_format = track_format or DEFAULT_TRACK
        self.show_master_progress = show_master_progress
        self.prefetched_meta = prefetched_meta
        self.concurrent_downloads = 1  # set by caller; > 1 enables parallel mode
        self._count_lock = threading.Lock()  # protects the tmp-file counter
        self.staging_directory = _normalize_staging_setting(staging_directory)
        self._staging_notice_emitted = False

    def _get_staging_root_for(self, final_root_dir: str) -> Optional[str]:
        setting = self.staging_directory
        if not setting:
            return None

        if setting == DEFAULT_STAGING:
            if not _is_windows_mount(final_root_dir):
                return None
            staging_root = _AUTO_STAGING_ROOT
        else:
            staging_root = setting

        os.makedirs(staging_root, exist_ok=True)
        if not self._staging_notice_emitted:
            logger.info(
                f"{CYAN}Processing files in Linux staging dir: {staging_root}{RESET}"
            )
            self._staging_notice_emitted = True
        return staging_root

    def _resolve_work_root(self, final_root_dir: str) -> Tuple[str, bool]:
        staging_root = self._get_staging_root_for(final_root_dir)
        if not staging_root:
            return final_root_dir, False

        stage_key = hashlib.sha1(final_root_dir.encode("utf-8")).hexdigest()[:12]
        leaf_name = sanitize_filename(os.path.basename(final_root_dir)) or "release"
        work_root = os.path.join(staging_root, f"{leaf_name}-{stage_key}")
        os.makedirs(work_root, exist_ok=True)
        return work_root, True

    @staticmethod
    def _clear_directory(path: str) -> None:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)

    @staticmethod
    def _promote_from_staging(staged_path: str, final_path: str) -> None:
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        last_error = None

        for attempt in range(1, _STAGING_MOVE_RETRIES + 1):
            try:
                if os.path.isfile(final_path):
                    os.remove(final_path)
                shutil.move(staged_path, final_path)
                return
            except PermissionError as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
                if exc.errno not in (errno.EACCES, errno.EPERM, errno.EBUSY):
                    break

            if attempt < _STAGING_MOVE_RETRIES:
                time.sleep(_STAGING_MOVE_DELAY * attempt)

        if last_error:
            raise last_error

    def _promote_release_tree(
        self,
        staged_release_dir: str,
        final_release_dir: str,
    ) -> Tuple[int, int]:
        if not os.path.isdir(staged_release_dir):
            return 0, 0

        moved = 0
        failed = 0
        for current_dir, _, files in os.walk(staged_release_dir):
            rel_dir = os.path.relpath(current_dir, staged_release_dir)
            target_dir = (
                final_release_dir
                if rel_dir == "."
                else os.path.join(final_release_dir, rel_dir)
            )
            for name in files:
                staged_path = os.path.join(current_dir, name)
                final_path = os.path.join(target_dir, name)
                try:
                    self._promote_from_staging(staged_path, final_path)
                    moved += 1
                except Exception as exc:
                    failed += 1
                    logger.error(
                        f"{RED}Error moving '{name}' to destination: {exc}",
                        exc_info=True,
                    )

        for current_dir, _, _ in os.walk(staged_release_dir, topdown=False):
            try:
                if not os.listdir(current_dir):
                    os.rmdir(current_dir)
            except OSError:
                pass

        return moved, failed

    def _get_track_url_with_fallback(self, track_id, fmt_id):
        """Try *fmt_id* first; if format-restricted AND quality_fallback is
        on, walk down the quality chain until a downloadable URL is found.

        Returns (parse_dict, actual_quality) tuple.
        """
        parse = self.client.get_track_url(track_id, fmt_id=fmt_id)

        if not self.downgrade_quality:
            logger.debug(f"Quality fallback disabled for track {track_id}")
            return parse, int(fmt_id)

        # Check if we got a usable response
        if "sample" not in parse and parse.get("sampling_rate") and "url" in parse:
            return parse, int(fmt_id)

        # Only fallback on format restriction, not purchase/geo restrictions
        restrictions = parse.get("restrictions", [])
        is_format_restricted = any(
            r.get("code") == QL_DOWNGRADE
            for r in restrictions if isinstance(r, dict)
        )
        if not is_format_restricted:
            return parse, int(fmt_id)  # not a format issue — don't retry

        # Walk down the quality chain
        current_idx = (
            _QUALITY_FALLBACK_CHAIN.index(int(fmt_id))
            if int(fmt_id) in _QUALITY_FALLBACK_CHAIN
            else -1
        )
        for lower_q in _QUALITY_FALLBACK_CHAIN[current_idx + 1:]:
            logger.debug(f"Trying quality {lower_q} for track {track_id}")
            parse = self.client.get_track_url(track_id, fmt_id=lower_q)
            if "sample" not in parse and parse.get("sampling_rate"):
                logger.info(
                    f"{YELLOW}Quality {fmt_id} unavailable, "
                    f"fell back to {lower_q}"
                )
                return parse, lower_q

        # All qualities exhausted — return last response for skip handling
        return parse, int(fmt_id)

    def download_id_by_type(self, track=True):
        if not track:
            return self.download_release()
        return self.download_track()

    def download_release(self):
        meta = (
            self.prefetched_meta
            if isinstance(self.prefetched_meta, dict)
            else self.client.get_album_meta(self.item_id)
        )

        if not meta.get("streamable"):
            raise NonStreamable("This release is not streamable")

        if self.albums_only and (
            meta.get("release_type") != "album"
            or meta.get("artist").get("name") == "Various Artists"
        ):
            logger.info(f'{OFF}Ignoring Single/EP/VA: {meta.get("title", "n/a")}')
            return {
                "status": "skipped",
                "downloaded": 0,
                "skipped": 0,
                "failed": 0,
                "move_errors": 0,
            }

        album_title = _get_title(meta)

        format_info = self._get_format(meta)
        file_format, quality_met, bit_depth, sampling_rate = format_info

        if not self.downgrade_quality and not quality_met:
            logger.info(
                f"{OFF}Skipping {album_title} as it doesn't meet quality requirement"
            )
            return {
                "status": "skipped",
                "downloaded": 0,
                "skipped": 0,
                "failed": 0,
                "move_errors": 0,
            }

        tracks = meta["tracks"]["items"]
        track_count = len(tracks)

        if self.concurrent_downloads <= 0:
            MAX_SAFE_WORKERS = 6
            calculated_workers = min(os.cpu_count() or 4, track_count, MAX_SAFE_WORKERS)
            max_workers = max(calculated_workers, 1)
            mode_str = "Auto-scale"
        else:
            requested_workers = max(1, int(self.concurrent_downloads))
            max_workers = min(requested_workers, track_count) if track_count else 1
            if requested_workers != max_workers:
                logger.debug(
                    f"Capped workers from {requested_workers} to {max_workers} "
                    f"for {track_count} track(s)"
                )
            mode_str = "Manual"

        thread_info = ""
        if max_workers > 1 or mode_str == "Auto-scale":
            from qobuz_dl.color import CYAN
            thread_info = f"\n{CYAN}Threads allocated: {max_workers} worker(s) for {track_count} track(s) [{mode_str}]"

        logger.info(
            f"\n{YELLOW}Downloading: {album_title}\nQuality: {file_format}"
            f" ({bit_depth}/{sampling_rate}){thread_info}\n"
        )
        album_attr = self._get_album_attr(
            meta, album_title, file_format, bit_depth, sampling_rate
        )
        folder_format, track_format = _clean_format_str(
            self.folder_format, self.track_format, file_format
        )
        sanitized_title = sanitize_filepath(folder_format.format(**album_attr))
        dirn = os.path.join(self.path, sanitized_title)
        os.makedirs(dirn, exist_ok=True)

        work_album_dir, using_release_staging = self._resolve_work_root(dirn)
        if using_release_staging:
            # Ensure a clean staging tree so stale files from older runs are not promoted.
            self._clear_directory(work_album_dir)

        assets_dir = work_album_dir if using_release_staging else dirn

        if self.no_cover:
            logger.info(f"{OFF}Skipping cover")
        else:
            _get_extra(
                meta["image"]["large"],
                assets_dir,
                og_quality=self.cover_og_quality,
            )

        if "goodies" in meta:
            try:
                _get_extra(meta["goodies"][0]["url"], assets_dir, "booklet.pdf")
            except:  # noqa
                pass
        media_numbers = [track["media_number"] for track in meta["tracks"]["items"]]
        is_multiple = True if len([*{*media_numbers}]) > 1 else False



        if max_workers > 1:
            stats = self._download_tracks_parallel(
                tracks,
                dirn,
                meta,
                is_multiple,
                max_workers,
                work_album_dir=work_album_dir,
                defer_promotion=using_release_staging,
            )
        else:
            stats = self._download_tracks_sequential(
                tracks,
                dirn,
                meta,
                is_multiple,
                work_album_dir=work_album_dir,
                defer_promotion=using_release_staging,
            )

        moved_files = 0
        promotion_failures = 0
        if using_release_staging:
            logger.info(f"{CYAN}Finalizing album files...{RESET}")
            moved_files, promotion_failures = self._promote_release_tree(
                work_album_dir,
                dirn,
            )
            if promotion_failures:
                logger.warning(
                    f"{YELLOW}Album finalization completed with "
                    f"{promotion_failures} move error(s)."
                )

        # Print summary
        dl = stats.get("downloaded", 0)
        sk = stats.get("skipped", 0)
        fa = stats.get("failed", 0)
        summary_parts = []
        if dl:
            summary_parts.append(f"{GREEN}✓ {dl} downloaded")
        if sk:
            summary_parts.append(f"{YELLOW}⚠ {sk} skipped")
        if fa:
            summary_parts.append(f"{RED}✗ {fa} failed")
        if moved_files:
            summary_parts.append(f"{CYAN}↷ {moved_files} moved")
        if promotion_failures:
            summary_parts.append(f"{RED}✗ {promotion_failures} move errors")
        logger.info("  ".join(summary_parts) + RESET if summary_parts else f"{GREEN}Completed")

        failed_total = fa + promotion_failures
        if failed_total:
            status = "failed"
        elif sk:
            status = "partial"
        elif dl:
            status = "completed"
        else:
            status = "skipped"

        return {
            "status": status,
            "downloaded": dl,
            "skipped": sk,
            "failed": fa,
            "move_errors": promotion_failures,
        }

    def _download_tracks_sequential(
        self,
        tracks,
        dirn,
        meta,
        is_multiple,
        work_album_dir=None,
        defer_promotion=False,
    ):
        """Original one-at-a-time download loop. Returns stats dict."""
        stats = {"downloaded": 0, "skipped": 0, "failed": 0}
        for count, i in enumerate(tracks):
            try:
                parse, actual_q = self._get_track_url_with_fallback(
                    i["id"], self.quality
                )
                if "sample" not in parse and parse.get("sampling_rate"):
                    is_mp3 = True if int(actual_q) == 5 else False
                    track_status = self._download_and_tag(
                        dirn, count, parse, i, meta, False, is_mp3,
                        i["media_number"] if is_multiple else None,
                        work_root_base=work_album_dir,
                        defer_promotion=defer_promotion,
                    )
                    if track_status not in stats:
                        track_status = "failed"
                    stats[track_status] += 1
                else:
                    reason = _describe_restrictions(parse)
                    title = i.get("title", f"track {i.get('id', '?')}")
                    logger.info(f"{OFF}Skipping '{title}': {reason}")
                    stats["skipped"] += 1
            except Exception as exc:
                track_title = i.get("title", i.get("id", "unknown"))
                logger.error(f"{RED}Failed to download '{track_title}': {exc}")
                stats["failed"] += 1
        return stats

    def _download_tracks_parallel(
        self,
        tracks,
        dirn,
        meta,
        is_multiple,
        max_workers,
        work_album_dir=None,
        defer_promotion=False,
    ):
        """Parallel download using ThreadPoolExecutor. Returns stats dict."""
        counter = [0]  # mutable container for atomic-style increment
        stats = {"downloaded": 0, "skipped": 0, "failed": 0}
        active_state = {"count": 0}
        active_lock = threading.Lock()
        slot_allocator = WorkerSlotAllocator(max_workers)
        track_count = len(tracks)
        compact_ui = _is_compact_progress_layout()
        # Worker slots are fixed to 0..max_workers-1.
        # Render the master bar directly below worker slots.
        worker_position_offset = 0
        master_position = max_workers

        master_bar = None
        if track_count and self.show_master_progress:
            master_bar = tqdm(
                total=track_count,
                position=master_position,
                leave=True,
                dynamic_ncols=True,
                desc=_format_master_progress(0, track_count, compact=compact_ui),
                bar_format=_build_master_bar_format(compact_ui),
                mininterval=_TQDM_MININTERVAL,
                maxinterval=_TQDM_MAXINTERVAL,
            )

        def _worker(i):
            with self._count_lock:
                count = counter[0]
                counter[0] += 1
            with active_lock:
                active_state["count"] += 1
            slot = slot_allocator.acquire_slot()
            slot_position = slot + worker_position_offset

            track_num = i.get("track_number", count + 1)
            track_prefix = f"[{track_num:02d}/{track_count:02d}]"
            track_artist = _safe_get(i, "performer", "name")
            track_title = i.get("title", f"track {i.get('id', '?')}")
            slot_desc_raw = (
                f"{track_prefix} {track_artist} - {track_title}"
                if track_artist else f"{track_prefix} {track_title}"
            )
            slot_desc = _fit_progress_desc(slot_desc_raw, compact_ui)

            try:
                # Show queued placeholder only while there are unstarted tracks.
                stagger = count * 0.5
                remaining_unstarted = max(track_count - (count + 1), 0)
                show_queued = stagger > 0 and remaining_unstarted > 0

                if show_queued:
                    with tqdm(
                        total=1,
                        desc=slot_desc,
                        position=slot_position,
                        leave=False,
                        bar_format=_build_postprocess_bar_format(
                            compact_ui,
                            OFF + CYAN,
                            "queued",
                        ),
                        dynamic_ncols=True,
                        mininterval=_TQDM_MININTERVAL,
                        maxinterval=_TQDM_MAXINTERVAL,
                    ) as queued_bar:
                        logger.debug(
                            f"Worker {count}: queued delay {stagger:.1f}s"
                        )
                        time.sleep(stagger)
                        parse, actual_q = self._get_track_url_with_fallback(
                            i["id"], self.quality
                        )
                        queued_bar.update(1)
                else:
                    if stagger > 0:
                        logger.debug(
                            f"Worker {count}: stagger delay {stagger:.1f}s"
                        )
                        time.sleep(stagger)
                    parse, actual_q = self._get_track_url_with_fallback(
                        i["id"], self.quality
                    )

                if "sample" not in parse and parse.get("sampling_rate"):
                    is_mp3 = True if int(actual_q) == 5 else False
                    return self._download_and_tag(
                        dirn, count, parse, i, meta, False, is_mp3,
                        i["media_number"] if is_multiple else None,
                        position=slot_position,
                        leave=False,
                        work_root_base=work_album_dir,
                        defer_promotion=defer_promotion,
                    )
                else:
                    reason = _describe_restrictions(parse)
                    title = i.get("title", f"track {i.get('id', '?')}")
                    logger.info(f"{OFF}Skipping '{title}': {reason}")
                    return "skipped"
            except Exception as exc:
                track_title = i.get("title", i.get("id", "unknown"))
                logger.error(f"{RED}Failed to download '{track_title}': {exc}")
                return "failed"
            finally:
                slot_allocator.release_slot(slot)
                with active_lock:
                    active_state["count"] -= 1

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_worker, i): i for i in tracks}
                for future in as_completed(futures):
                    status = "failed"
                    try:
                        status = future.result()
                    except Exception:
                        status = "failed"
                    if status not in stats:
                        status = "failed"
                    stats[status] += 1
                    if master_bar is not None:
                        completed = (
                            stats["downloaded"]
                            + stats["skipped"]
                            + stats["failed"]
                        )
                        with active_lock:
                            active_count = max(active_state["count"], 0)
                        master_bar.set_description_str(
                            _format_master_progress(
                                completed,
                                track_count,
                                downloaded=stats["downloaded"],
                                skipped=stats["skipped"],
                                failed=stats["failed"],
                                active=active_count,
                                compact=compact_ui,
                            )
                        )
                        master_bar.update(1)
        finally:
            if master_bar is not None:
                master_bar.close()
        return stats

    def download_track(self):
        parse, actual_q = self._get_track_url_with_fallback(
            self.item_id, self.quality
        )

        if "sample" not in parse and parse["sampling_rate"]:
            meta = (
                self.prefetched_meta
                if isinstance(self.prefetched_meta, dict)
                else self.client.get_track_meta(self.item_id)
            )
            track_title = _get_title(meta)
            artist = _safe_get(meta, "performer", "name")
            logger.info(f"\n{YELLOW}Downloading: {artist} - {track_title}")
            format_info = self._get_format(meta, is_track_id=True, track_url_dict=parse)
            file_format, quality_met, bit_depth, sampling_rate = format_info

            folder_format, track_format = _clean_format_str(
                self.folder_format, self.track_format, str(bit_depth)
            )

            if not self.downgrade_quality and not quality_met:
                logger.info(
                    f"{OFF}Skipping {track_title} as it doesn't "
                    "meet quality requirement"
                )
                return {
                    "status": "skipped",
                    "downloaded": 0,
                    "skipped": 1,
                    "failed": 0,
                    "move_errors": 0,
                }
            track_attr = self._get_track_attr(
                meta, track_title, bit_depth, sampling_rate
            )
            sanitized_title = sanitize_filepath(folder_format.format(**track_attr))

            dirn = os.path.join(self.path, sanitized_title)
            os.makedirs(dirn, exist_ok=True)
            if self.no_cover:
                logger.info(f"{OFF}Skipping cover")
            else:
                _get_extra(
                    meta["album"]["image"]["large"],
                    dirn,
                    og_quality=self.cover_og_quality,
                )
            is_mp3 = True if int(actual_q) == 5 else False
            track_status = self._download_and_tag(
                dirn,
                1,
                parse,
                meta,
                meta,
                True,
                is_mp3,
                False,
            )
            if track_status == "failed":
                return {
                    "status": "failed",
                    "downloaded": 0,
                    "skipped": 0,
                    "failed": 1,
                    "move_errors": 0,
                }
            if track_status == "skipped":
                return {
                    "status": "skipped",
                    "downloaded": 0,
                    "skipped": 1,
                    "failed": 0,
                    "move_errors": 0,
                }
        else:
            reason = _describe_restrictions(parse)
            logger.info(f"{OFF}Skipping track {self.item_id}: {reason}")
            return {
                "status": "skipped",
                "downloaded": 0,
                "skipped": 1,
                "failed": 0,
                "move_errors": 0,
            }
        logger.info(f"{GREEN}Completed")
        return {
            "status": "completed",
            "downloaded": 1,
            "skipped": 0,
            "failed": 0,
            "move_errors": 0,
        }

    def _download_and_tag(
        self,
        root_dir,
        tmp_count,
        track_url_dict,
        track_metadata,
        album_or_track_metadata,
        is_track,
        is_mp3,
        multiple=None,
        position: Optional[int] = None,
        leave: bool = True,
        work_root_base: Optional[str] = None,
        defer_promotion: bool = False,
    ):
        extension = ".mp3" if is_mp3 else ".flac"

        if "url" not in track_url_dict and "url_template" not in track_url_dict:
            logger.info(f"{OFF}Track not available for download")
            return "skipped"

        final_root_dir = root_dir
        os.makedirs(final_root_dir, exist_ok=True)

        if work_root_base:
            work_root_dir = work_root_base
            os.makedirs(work_root_dir, exist_ok=True)
            using_staging = os.path.abspath(work_root_dir) != os.path.abspath(
                final_root_dir
            )
        else:
            work_root_dir, using_staging = self._resolve_work_root(final_root_dir)

        filename = os.path.join(work_root_dir, f".{tmp_count:02}.tmp")

        # Determine the filename
        track_title = track_metadata.get("title")
        artist = _safe_get(track_metadata, "performer", "name")
        filename_attr = self._get_filename_attr(artist, track_metadata, track_title)

        # track_format is a format string
        # e.g. '{tracknumber}. {artist} - {tracktitle}'
        formatted_path = sanitize_filename(self.track_format.format(**filename_attr))

        if multiple:
            formatted_path = sanitize_filename(f"Disc {multiple} - {formatted_path}")

        final_file = os.path.join(final_root_dir, formatted_path)[:250] + extension
        staged_final_file = (
            os.path.join(work_root_dir, formatted_path)[:250] + extension
        )

        if os.path.isfile(final_file):
            file_size = os.path.getsize(final_file)
            # Audio files should be at least 10KB; smaller means corrupt/incomplete
            if file_size > 10240:
                logger.info(f"{OFF}{track_title} was already downloaded")
                return "downloaded"
            else:
                logger.info(
                    f"{YELLOW}{track_title} exists but looks incomplete "
                    f"({file_size} bytes), re-downloading"
                )
                os.remove(final_file)

        processing_error = {"value": False}

        # Clean up any orphaned tmp file from a previous interrupted download
        if os.path.isfile(filename):
            os.remove(filename)

        # Build a rich progress bar description: [03/12] Artist - Track Title
        total_tracks = (
            album_or_track_metadata.get("tracks", {}).get("total")
            if not is_track else None
        )
        track_num = track_metadata.get("track_number", tmp_count + 1)
        if total_tracks and not is_track:
            track_prefix = f"[{track_num:02d}/{total_tracks:02d}]"
        else:
            track_prefix = f"[{track_num:02d}]"
        compact_ui = _is_compact_progress_layout()
        dl_desc_raw = (
            f"{track_prefix} {artist} - {track_title}"
            if artist else f"{track_prefix} {track_title}"
        )
        dl_desc = _fit_progress_desc(dl_desc_raw, compact_ui)

        if "url" in track_url_dict:
            try:
                # 1. FAST PATH: direct URL download
                tqdm_download(
                    track_url_dict["url"],
                    filename,
                    dl_desc,
                    position=position,
                    leave=leave,
                )
            except (ConnectionError, requests.exceptions.ChunkedEncodingError):
                # Akamai block detected — tqdm_download normalizes all streaming
                # errors to ConnectionError, but keep ChunkedEncodingError as
                # a safety net in case it escapes tqdm_download somehow.
                logger.info(
                    f"{YELLOW}Akamai block detected on '{track_title}'. "
                    "Switching to segmented download..."
                )
                # Clean up partial file before retry
                if os.path.isfile(filename):
                    os.remove(filename)
                track_id = track_metadata.get("id")
                track_url_dict = self.client.get_track_url(
                    track_id, int(self.quality), force_segments=True
                )
                tqdm_download_segments(
                    track_url_dict,
                    filename,
                    dl_desc,
                    position=position,
                    leave=leave,
                )
        else:
            # url_template already present — go straight to segmented download
            tqdm_download_segments(
                track_url_dict,
                filename,
                dl_desc,
                position=position,
                leave=leave,
            )

        def _run_integrity_check():
            # Integrity check before tagging
            if is_mp3 or not os.path.isfile(filename):
                return
            try:
                result = subprocess.run(
                    ["flac", "-t", "-s", filename],
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    processing_error["value"] = True
                    logger.warning(
                        f"{YELLOW}FLAC integrity check failed for '{track_title}'. "
                        "File may be corrupt."
                    )
                else:
                    logger.debug(f"FLAC integrity OK: {track_title}")
            except FileNotFoundError:
                logger.debug("flac binary not found, skipping integrity check")
            except subprocess.TimeoutExpired:
                logger.debug(f"FLAC integrity check timed out for {track_title}")

        def _run_tagging():
            tag_function = metadata.tag_mp3 if is_mp3 else metadata.tag_flac
            try:
                tag_function(
                    filename,
                    work_root_dir,
                    staged_final_file,
                    track_metadata,
                    album_or_track_metadata,
                    is_track,
                    self.embed_art,
                )
            except Exception as e:
                processing_error["value"] = True
                logger.error(f"{RED}Error tagging the file: {e}", exc_info=True)

        def _run_promotion():
            if defer_promotion:
                return
            if not using_staging or not os.path.isfile(staged_final_file):
                return
            try:
                self._promote_from_staging(staged_final_file, final_file)
            except Exception as e:
                processing_error["value"] = True
                logger.error(
                    f"{RED}Error moving the file to destination: {e}",
                    exc_info=True,
                )

        show_postprocess_status = position is not None and not leave
        if show_postprocess_status:
            post_steps = []
            if not is_mp3:
                post_steps.append("verifying")
            post_steps.append("tagging")
            if using_staging and not defer_promotion:
                post_steps.append("moving")
            current_status = post_steps[0]
            initial_post_color = GREEN if current_status == "tagging" else YELLOW
            with tqdm(
                total=len(post_steps),
                desc=dl_desc,
                position=position,
                leave=False,
                bar_format=_build_postprocess_bar_format(
                    compact_ui,
                    initial_post_color,
                    current_status,
                ),
                dynamic_ncols=True,
                mininterval=_TQDM_MININTERVAL,
                maxinterval=_TQDM_MAXINTERVAL,
            ) as post_bar:
                if not is_mp3:
                    _run_integrity_check()
                    post_bar.update(1)
                    current_status = "tagging"
                    post_bar.bar_format = _build_postprocess_bar_format(
                        compact_ui,
                        GREEN,
                        current_status,
                    )
                    post_bar.refresh()
                _run_tagging()
                post_bar.update(1)
                if using_staging and not defer_promotion:
                    current_status = "moving"
                    post_bar.bar_format = _build_postprocess_bar_format(
                        compact_ui,
                        CYAN,
                        current_status,
                    )
                    post_bar.refresh()
                    _run_promotion()
                    post_bar.update(1)
        else:
            _run_integrity_check()
            _run_tagging()
            _run_promotion()

        return "failed" if processing_error["value"] else "downloaded"

    @staticmethod
    def _get_filename_attr(artist, track_metadata, track_title):
        return {
            "artist": artist,
            "albumartist": sanitize_filename(_safe_get(
                track_metadata, "album", "artist", "name", default=artist
            )),
            "album": sanitize_filename(_safe_get(track_metadata, "album", "title", default="Unknown Album")),
            "year": _safe_get(track_metadata, "album", "release_date_original", default="").split("-")[0],
            "bit_depth": track_metadata["maximum_bit_depth"],
            "sampling_rate": track_metadata["maximum_sampling_rate"],
            "tracktitle": track_title,
            "version": track_metadata.get("version"),
            "tracknumber": f"{track_metadata['track_number']:02}",
        }

    @staticmethod
    def _get_track_attr(meta, track_title, bit_depth, sampling_rate):
        return {
            "album": sanitize_filename(meta["album"]["title"]),
            "artist": sanitize_filename(meta["album"]["artist"]["name"]),
            "albumartist": sanitize_filename(meta["album"]["artist"]["name"]),
            "tracktitle": track_title,
            "year": meta["album"]["release_date_original"].split("-")[0],
            "bit_depth": bit_depth,
            "sampling_rate": sampling_rate,
        }

    @staticmethod
    def _get_album_attr(meta, album_title, file_format, bit_depth, sampling_rate):
        return {
            "artist": sanitize_filename(meta["artist"]["name"]),
            "albumartist": sanitize_filename(meta["artist"]["name"]),
            "album": sanitize_filename(album_title),
            "year": meta["release_date_original"].split("-")[0],
            "format": file_format,
            "bit_depth": bit_depth,
            "sampling_rate": sampling_rate,
        }

    def _get_format(self, item_dict, is_track_id=False, track_url_dict=None):
        quality_met = True
        if int(self.quality) == 5:
            return ("MP3", quality_met, None, None)
        track_dict = item_dict
        if not is_track_id:
            track_dict = item_dict["tracks"]["items"][0]

        try:
            new_track_dict = (
                self.client.get_track_url(track_dict["id"], fmt_id=self.quality)
                if not track_url_dict
                else track_url_dict
            )
            restrictions = new_track_dict.get("restrictions")
            if isinstance(restrictions, list):
                if any(
                    restriction.get("code") == QL_DOWNGRADE
                    for restriction in restrictions
                ):
                    quality_met = False

            return (
                "FLAC",
                quality_met,
                new_track_dict["bit_depth"],
                new_track_dict["sampling_rate"],
            )
        except (KeyError, requests.exceptions.HTTPError):
            return ("Unknown", quality_met, None, None)


def tqdm_download(
    url,
    fname,
    desc,
    max_retries=3,
    position: Optional[int] = None,
    leave: bool = True,
    show_progress: bool = True,
):
    """Download *url* to *fname* with automatic retry, exponential backoff,
    and HTTP Range-based resume.

    On each retry attempt, checks if a partial file exists and sends a
    Range header to resume from that byte offset instead of restarting.
    If the server doesn't support Range requests, falls back gracefully
    to a full restart.

    After all retries are exhausted, raises ConnectionError which triggers
    the Akamai segmented download fallback in _download_and_tag.
    """
    for attempt in range(max_retries):
        # On retry, try to resume from partial file rather than restart
        resume_from = (
            os.path.getsize(fname)
            if attempt > 0 and os.path.isfile(fname)
            else 0
        )
        logger.debug(
            f"Download attempt {attempt + 1}/{max_retries} for {desc} "
            f"(resume={resume_from})"
        )
        try:
            _tqdm_download_once(
                url,
                fname,
                desc,
                resume_from=resume_from,
                position=position,
                leave=leave,
                show_progress=show_progress,
            )
            return  # success
        except ConnectionError as exc:
            if attempt < max_retries - 1:
                wait = 2 ** attempt  # 1 s, 2 s, 4 s
                logger.info(
                    f"{YELLOW}Download interrupted (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying in {wait}s..."
                    + (f" Resuming from {resume_from / 1024 / 1024:.1f} MB." if resume_from else "")
                )
                time.sleep(wait)
            else:
                # All retries exhausted — clean up and propagate to Akamai fallback
                if os.path.isfile(fname):
                    os.remove(fname)
                raise


def _tqdm_download_once(
    url,
    fname,
    desc,
    resume_from=0,
    position: Optional[int] = None,
    leave: bool = True,
    show_progress: bool = True,
):
    """Single download attempt with optional byte-range resume."""
    headers = {}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"

    try:
        r = requests.get(url, allow_redirects=True, stream=True, headers=headers)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Failed to start download for {fname}: {e}") from e

    # If server returns 200 instead of 206, it doesn't support Range — restart
    if resume_from and r.status_code == 200:
        logger.info(f"{YELLOW}Server doesn't support resume. Restarting download...")
        resume_from = 0
        if os.path.isfile(fname):
            os.remove(fname)

    content_length = int(r.headers.get("content-length", 0))
    total = content_length + resume_from
    download_size = resume_from
    file_mode = "ab" if resume_from else "wb"
    compact_ui = _is_compact_progress_layout()

    try:
        with open(fname, file_mode) as file, tqdm(
            total=total,
            initial=resume_from,
            unit="iB",
            unit_scale=True,
            unit_divisor=1024,
            desc=desc,
            position=position,
            leave=leave,
            disable=not show_progress,
            bar_format=_build_transfer_bar_format(compact_ui),
            dynamic_ncols=True,
            mininterval=_TQDM_MININTERVAL,
            maxinterval=_TQDM_MAXINTERVAL,
            smoothing=_TQDM_SMOOTHING,
        ) as bar:
            _rl_start = time.monotonic()
            _rl_bytes = 0
            for data in r.iter_content(chunk_size=1024):
                size = file.write(data)
                bar.update(size)
                download_size += size
                # Rate limiting
                if _rate_limit_bps > 0:
                    _rl_bytes += size
                    _rl_elapsed = time.monotonic() - _rl_start
                    _rl_expected = _rl_bytes / _rate_limit_bps
                    if _rl_expected > _rl_elapsed:
                        time.sleep(_rl_expected - _rl_elapsed)
    except requests.exceptions.RequestException as e:
        # Catches ChunkedEncodingError (IncompleteRead), ConnectionError, etc.
        raise ConnectionError(f"Download interrupted for {fname}: {e}") from e

    if total and total != download_size:
        raise ConnectionError("File download was interrupted for " + fname)


def tqdm_download_segments(
    track_url_dict,
    fname,
    desc,
    position: Optional[int] = None,
    leave: bool = True,
):
    """Download an Akamai-segmented track, decrypt each segment with AES-CTR,
    write a concatenated MP4, then remux to FLAC via ffmpeg."""
    tmp_fname = fname + ".mp4"
    segment_uuid = None
    total = 0
    for segment in range(track_url_dict["n_segments"] + 1):
        r = requests.head(
            track_url_dict["url_template"].replace("$SEGMENT$", str(segment)),
            allow_redirects=True,
        )
        r.raise_for_status()
        total += int(r.headers.get("content-length", 0))
    compact_ui = _is_compact_progress_layout()

    try:
        with open(tmp_fname, "wb") as file, tqdm(
            total=total,
            unit="iB",
            unit_scale=True,
            unit_divisor=1024,
            desc=desc,
            position=position,
            leave=leave,
            bar_format=_build_transfer_bar_format(compact_ui, segmented=True),
            dynamic_ncols=True,
            mininterval=_TQDM_MININTERVAL,
            maxinterval=_TQDM_MAXINTERVAL,
            smoothing=_TQDM_SMOOTHING,
        ) as bar:
            for segment in range(track_url_dict["n_segments"] + 1):
                r = requests.get(
                    track_url_dict["url_template"].replace("$SEGMENT$", str(segment)),
                    allow_redirects=True,
                    stream=True,
                )
                r.raise_for_status()
                segment_total = int(r.headers.get("content-length", 0))
                segment_size = 0
                segment_data = bytearray()
                for data in r.iter_content(chunk_size=1024):
                    segment_data.extend(data)
                    size = len(data)
                    bar.update(size)
                    segment_size += size
                r.close()

                if segment_total and segment_total != segment_size:
                    raise ConnectionError("Segment download interrupted for " + fname)
                if segment == 1:
                    segment_uuid = _get_qobuz_segment_uuid(segment_data)
                    if segment_uuid is None:
                        raise ConnectionError(
                            "Cannot find Qobuz segment UUID for " + fname
                        )
                file.write(
                    _decrypt_qobuz_segment(
                        segment_data, track_url_dict["raw_key"], segment_uuid
                    )
                )

        remux = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y",
             "-i", tmp_fname, "-c:a", "copy", "-f", "flac", fname],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if remux.returncode != 0:
            raise ConnectionError(
                "ffmpeg remux failed for {}: {}".format(
                    fname, remux.stderr.strip() or "unknown error"
                )
            )
    finally:
        if os.path.isfile(tmp_fname):
            os.remove(tmp_fname)


def _get_qobuz_segment_uuid(segment_data):
    pos = 0
    while pos + 24 <= len(segment_data):
        size = int.from_bytes(segment_data[pos : pos + 4], "big")
        if size <= 0 or pos + size > len(segment_data):
            break
        if bytes(segment_data[pos + 4 : pos + 8]) == b"uuid":
            return bytes(segment_data[pos + 8 : pos + 24])
        pos += size
    return None


def _decrypt_qobuz_segment(segment_data, raw_key, segment_uuid):
    if segment_uuid is None:
        return bytes(segment_data)

    buf = bytearray(segment_data)
    pos = 0
    while pos + 8 <= len(buf):
        size = int.from_bytes(buf[pos : pos + 4], "big")
        if size <= 0 or pos + size > len(buf):
            break
        if (
            bytes(buf[pos + 4 : pos + 8]) == b"uuid"
            and bytes(buf[pos + 8 : pos + 24]) == segment_uuid
        ):
            pointer = pos + 28
            data_end = pos + int.from_bytes(buf[pointer : pointer + 4], "big")
            pointer += 4
            counter_len = buf[pointer]
            pointer += 1
            frame_count = int.from_bytes(buf[pointer : pointer + 3], "big")
            pointer += 3
            for _ in range(frame_count):
                frame_len = int.from_bytes(buf[pointer : pointer + 4], "big")
                pointer += 6
                flags = int.from_bytes(buf[pointer : pointer + 2], "big")
                pointer += 2
                frame_start = data_end
                frame_end = frame_start + frame_len
                data_end = frame_end
                if flags:
                    counter = bytes(buf[pointer : pointer + counter_len]) + (
                        b"\x00" * (16 - counter_len)
                    )
                    decryptor = Cipher(
                        algorithms.AES(raw_key), modes.CTR(counter)
                    ).decryptor()
                    buf[frame_start:frame_end] = decryptor.update(
                        bytes(buf[frame_start:frame_end])
                    ) + decryptor.finalize()
                pointer += counter_len
        pos += size
    return bytes(buf)


def _get_description(item: dict, track_title, multiple=None):
    downloading_title = f"{track_title} "
    f'[{item["bit_depth"]}/{item["sampling_rate"]}]'
    if multiple:
        downloading_title = f"[Disc {multiple}] {downloading_title}"
    return downloading_title


def _get_title(item_dict):
    album_title = item_dict["title"]
    version = item_dict.get("version")
    if version:
        album_title = (
            f"{album_title} ({version})"
            if version.lower() not in album_title.lower()
            else album_title
        )
    return album_title


def _get_extra(item, dirn, extra="cover.jpg", og_quality=False):
    extra_file = os.path.join(dirn, extra)
    if os.path.isfile(extra_file):
        logger.info(f"{OFF}{extra} was already downloaded")
        return
    tqdm_download(
        item.replace("_600.", "_org.") if og_quality else item,
        extra_file,
        extra,
        show_progress=False,
    )


def _clean_format_str(folder: str, track: str, file_format: str) -> Tuple[str, str]:
    """Cleans up the format strings, avoids errors
    with MP3 files.
    """
    final = []
    for i, fs in enumerate((folder, track)):
        if fs.endswith(".mp3"):
            fs = fs[:-4]
        elif fs.endswith(".flac"):
            fs = fs[:-5]
        fs = fs.strip()

        # default to pre-chosen string if format is invalid
        if file_format in ("MP3", "Unknown") and (
            "bit_depth" in fs or "sampling_rate" in fs
        ):
            default = DEFAULT_FORMATS[file_format][i]
            logger.error(
                f"{RED}invalid format string for format {file_format}"
                f". defaulting to {default}"
            )
            fs = default
        final.append(fs)

    return tuple(final)


def _safe_get(d: dict, *keys, default=None):
    """A replacement for chained `get()` statements on dicts:
    >>> d = {'foo': {'bar': 'baz'}}
    >>> _safe_get(d, 'baz')
    None
    >>> _safe_get(d, 'foo', 'bar')
    'baz'
    """
    curr = d
    res = default
    for key in keys:
        res = curr.get(key, default)
        if res == default or not hasattr(res, "__getitem__"):
            return res
        else:
            curr = res
    return res
