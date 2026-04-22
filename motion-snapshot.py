from __future__ import annotations

import csv
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar, TypedDict

import requests
from dotenv import load_dotenv
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import EntryNotFoundError
from huggingface_hub.utils import HfHubHTTPError


SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")


METADATA_HEADERS = ["file_name", "camera_id", "timestamp"]

T = TypeVar("T")


class MetadataRow(TypedDict):
    file_name: str
    camera_id: str
    timestamp: str


@dataclass(frozen=True)
class Config:
    hf_token: str
    hf_repo_id: str
    motion_snapshot_endpoint: str
    motion_snapshot_user: str
    motion_snapshot_password: str
    motion_snapshots_root: Path
    http_timeout_seconds: int
    snapshot_wait_timeout_seconds: int
    snapshot_poll_interval_seconds: float
    upload_retry_count: int
    upload_retry_delay_seconds: float


@dataclass(frozen=True)
class SnapshotRecord:
    camera_id: str
    local_path: Path
    remote_path: str
    timestamp: str


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("motion_snapshot")


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    return Config(
        hf_token=env_required("HF_TOKEN"),
        hf_repo_id=env_required("HF_REPO_ID"),
        motion_snapshot_endpoint=os.getenv(
            "MOTION_SNAPSHOT_ENDPOINT",
            "http://localhost:8080/0/action/snapshot",
        ),
        motion_snapshot_user=env_required("MOTION_SNAPSHOT_USER"),
        motion_snapshot_password=env_required("MOTION_SNAPSHOT_PASSWORD"),
        motion_snapshots_root=Path(
            os.getenv("MOTION_SNAPSHOTS_ROOT", "/var/lib/motion")
        ),
        http_timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
        snapshot_wait_timeout_seconds=int(
            os.getenv("SNAPSHOT_WAIT_TIMEOUT_SECONDS", "20")
        ),
        snapshot_poll_interval_seconds=float(
            os.getenv("SNAPSHOT_POLL_INTERVAL_SECONDS", "0.5")
        ),
        upload_retry_count=int(os.getenv("UPLOAD_RETRY_COUNT", "3")),
        upload_retry_delay_seconds=float(
            os.getenv("UPLOAD_RETRY_DELAY_SECONDS", "2.0")
        ),
    )


def list_camera_dirs(root: Path) -> list[Path]:
    if not root.exists():
        raise RuntimeError(f"Snapshot root does not exist: {root}")

    camera_dirs = [path for path in root.iterdir() if path.is_dir()]
    return sorted(camera_dirs, key=lambda path: path.name)


def wait_for_camera_dirs(
    root: Path,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> list[Path]:
    def probe() -> list[Path] | None:
        camera_dirs = list_camera_dirs(root)
        if camera_dirs:
            return camera_dirs
        return None

    camera_dirs = poll_until_result(
        probe=probe,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if camera_dirs is not None:
        return camera_dirs

    raise RuntimeError(f"Timed out waiting for camera directories under {root}")


def is_valid_snapshot_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def is_cleanup_candidate(path: Path) -> bool:
    return path.is_symlink() or path.is_file()


def extract_snapshot_timestamp(path: Path) -> datetime:
    stat = path.stat()

    # Prefer true creation time when supported; otherwise fall back to ctime.
    timestamp = getattr(stat, "st_birthtime", stat.st_ctime)
    return datetime.fromtimestamp(timestamp)


def list_snapshot_files(directory: Path) -> list[Path]:
    return sorted(
        [path for path in directory.iterdir() if is_valid_snapshot_file(path)],
        key=lambda path: (extract_snapshot_timestamp(path), path.name),
    )


def get_latest_snapshot(directory: Path) -> Path | None:
    snapshots = list_snapshot_files(directory)
    if not snapshots:
        return None
    return snapshots[-1]


def is_fresh_snapshot(
    baseline_path: Path | None, current_path: Path | None
) -> bool:
    if current_path is None:
        return False
    if baseline_path is None:
        return True
    return current_path != baseline_path


def trigger_snapshot(
    session: requests.Session,
    endpoint: str,
    timeout_seconds: int,
) -> None:
    response = session.get(
        endpoint,
        timeout=timeout_seconds,
    )
    response.raise_for_status()


def wait_for_new_files(
    snapshots_root: Path,
    baseline_paths: dict[str, Path | None],
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> dict[str, Path]:
    def probe() -> dict[str, Path] | None:
        camera_dirs = list_camera_dirs(snapshots_root)
        if not camera_dirs:
            return None

        latest_files: dict[str, Path] = {}
        all_ready = True

        for camera_dir in camera_dirs:
            latest = get_latest_snapshot(camera_dir)
            baseline_path = baseline_paths.get(camera_dir.name)

            if is_fresh_snapshot(baseline_path, latest):
                latest_files[camera_dir.name] = latest
            else:
                all_ready = False

        if all_ready and len(latest_files) == len(camera_dirs):
            return latest_files
        return None

    latest_files = poll_until_result(
        probe=probe,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if latest_files is not None:
        return latest_files

    problems: list[str] = []
    camera_dirs = list_camera_dirs(snapshots_root)

    if not camera_dirs:
        problems.append("no camera directories found")

    for camera_dir in camera_dirs:
        latest = get_latest_snapshot(camera_dir)
        baseline_path = baseline_paths.get(camera_dir.name)

        if latest is None:
            problems.append(f"{camera_dir.name}: no valid snapshot found")
        elif not is_fresh_snapshot(baseline_path, latest):
            problems.append(f"{camera_dir.name}: no new snapshot detected")

    raise RuntimeError("Timed out waiting for fresh snapshots: " + "; ".join(problems))


def poll_until_result(
    probe: Callable[[], T | None],
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> T | None:
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        result = probe()
        if result is not None:
            return result
        time.sleep(poll_interval_seconds)

    return None


def upload_file_with_retries(
    api: HfApi,
    repo_id: str,
    token: str,
    local_path: Path,
    remote_path: str,
    retry_count: int,
    retry_delay_seconds: float,
    logger: logging.Logger,
) -> None:
    retryable_errors = (HfHubHTTPError, requests.RequestException)
    last_error: Exception | None = None

    for attempt in range(1, retry_count + 1):
        try:
            api.upload_file(
                path_or_fileobj=str(local_path),
                path_in_repo=remote_path,
                repo_id=repo_id,
                repo_type="dataset",
                token=token,
            )
            return
        except retryable_errors as exc:
            last_error = exc
            if attempt < retry_count:
                logger.warning(
                    "Upload attempt %s/%s failed for %s: %s. Retrying in %.1f seconds.",
                    attempt,
                    retry_count,
                    local_path,
                    exc,
                    retry_delay_seconds,
                )
                time.sleep(retry_delay_seconds)
            else:
                logger.error(
                    "Upload attempt %s/%s failed for %s: %s",
                    attempt,
                    retry_count,
                    local_path,
                    exc,
                )

    if last_error is None:
        raise RuntimeError(
            f"Upload failed for {local_path} but no retryable exception was captured"
        )

    raise last_error


def download_metadata_csv(
    repo_id: str,
    token: str,
    logger: logging.Logger,
) -> list[MetadataRow]:
    try:
        metadata_path = hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename="metadata.csv",
            token=token,
        )
    except EntryNotFoundError:
        logger.info(
            "Remote metadata.csv not found yet. Starting with an empty metadata table."
        )
        return []

    rows: list[MetadataRow] = []
    with open(metadata_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                {
                    "file_name": row["file_name"],
                    "camera_id": row["camera_id"],
                    "timestamp": row["timestamp"],
                }
            )
    return rows


def write_metadata_csv(path: Path, rows: list[MetadataRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METADATA_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def build_snapshot_records(latest_images: dict[str, Path]) -> list[SnapshotRecord]:
    records: list[SnapshotRecord] = []

    for camera_id, image_path in sorted(latest_images.items()):
        timestamp = extract_snapshot_timestamp(image_path).isoformat()
        remote_path = f"{camera_id}/{image_path.name}"
        records.append(
            SnapshotRecord(
                camera_id=camera_id,
                local_path=image_path,
                remote_path=remote_path,
                timestamp=timestamp,
            )
        )

    return records


def merge_metadata_rows(
    existing_rows: list[MetadataRow],
    new_records: list[SnapshotRecord],
) -> list[MetadataRow]:
    by_file_name = {row["file_name"]: row for row in existing_rows}

    for record in new_records:
        row = {
            "file_name": f"{record.camera_id}/{record.local_path.name}",
            "camera_id": record.camera_id,
            "timestamp": record.timestamp,
        }
        by_file_name[row["file_name"]] = row

    return [by_file_name[key] for key in sorted(by_file_name.keys())]


def clear_snapshot_directory(directory: Path, logger: logging.Logger) -> None:
    deleted_count = 0

    for path in directory.iterdir():
        if is_cleanup_candidate(path):
            path.unlink()
            deleted_count += 1

    logger.info("Cleared %s snapshot artifact(s) from %s.", deleted_count, directory)


def main() -> int:
    logger = setup_logging()
    config = load_config()

    logger.info("Starting snapshot capture and publish run.")
    logger.info("Using snapshots root: %s", config.motion_snapshots_root)

    initial_camera_dirs = list_camera_dirs(config.motion_snapshots_root)
    if initial_camera_dirs:
        logger.info(
            "Discovered camera directories: %s",
            ", ".join(path.name for path in initial_camera_dirs),
        )
    else:
        logger.info(
            "No camera directories found yet. Will trigger snapshot and wait for directories to appear."
        )

    baseline_paths = {
        camera_dir.name: get_latest_snapshot(camera_dir)
        for camera_dir in initial_camera_dirs
    }

    with requests.Session() as session:
        session.headers.update({"User-Agent": "motion-snapshot/1.0"})
        session.auth = (
            config.motion_snapshot_user,
            config.motion_snapshot_password,
        )
        trigger_snapshot(
            session=session,
            endpoint=config.motion_snapshot_endpoint,
            timeout_seconds=config.http_timeout_seconds,
        )

    logger.info("Snapshot trigger sent successfully.")

    discovered_camera_dirs = wait_for_camera_dirs(
        root=config.motion_snapshots_root,
        timeout_seconds=config.snapshot_wait_timeout_seconds,
        poll_interval_seconds=config.snapshot_poll_interval_seconds,
    )
    logger.info(
        "Watching camera directories: %s",
        ", ".join(path.name for path in discovered_camera_dirs),
    )

    latest_images = wait_for_new_files(
        snapshots_root=config.motion_snapshots_root,
        baseline_paths=baseline_paths,
        timeout_seconds=config.snapshot_wait_timeout_seconds,
        poll_interval_seconds=config.snapshot_poll_interval_seconds,
    )

    records = build_snapshot_records(latest_images)
    logger.info("Fresh snapshots detected for all configured cameras.")

    api = HfApi(token=config.hf_token)
    existing_rows = download_metadata_csv(
        repo_id=config.hf_repo_id,
        token=config.hf_token,
        logger=logger,
    )

    failures: list[str] = []

    for record in records:
        try:
            upload_file_with_retries(
                api=api,
                repo_id=config.hf_repo_id,
                token=config.hf_token,
                local_path=record.local_path,
                remote_path=record.remote_path,
                retry_count=config.upload_retry_count,
                retry_delay_seconds=config.upload_retry_delay_seconds,
                logger=logger,
            )
            logger.info("Uploaded %s -> %s.", record.local_path, record.remote_path)
        except Exception as exc:
            failures.append(f"{record.camera_id}: image upload failed: {exc}")

    if failures:
        logger.error("Run completed with failures during image upload:")
        for failure in failures:
            logger.error("  %s", failure)
        return 1

    updated_rows = merge_metadata_rows(existing_rows, records)

    with tempfile.TemporaryDirectory(prefix="motion-snapshot-metadata-") as tmpdir:
        metadata_local_path = Path(tmpdir) / "metadata.csv"
        write_metadata_csv(metadata_local_path, updated_rows)

        try:
            upload_file_with_retries(
                api=api,
                repo_id=config.hf_repo_id,
                token=config.hf_token,
                local_path=metadata_local_path,
                remote_path="metadata.csv",
                retry_count=config.upload_retry_count,
                retry_delay_seconds=config.upload_retry_delay_seconds,
                logger=logger,
            )
            logger.info("Uploaded updated metadata.csv.")
        except Exception as exc:
            logger.error("Metadata upload failed: %s", exc)
            return 1

    cleanup_camera_dirs = sorted(
        {path.parent for path in latest_images.values()},
        key=lambda path: path.name,
    )
    for camera_dir in cleanup_camera_dirs:
        clear_snapshot_directory(camera_dir, logger)

    logger.info("Run completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
