"""CLI 与 HTTP 共用的处理运行时组装。"""

from __future__ import annotations

from pc.adapters.local_storage import LocalStorageBackend
from pc.adapters.magic_scan import MagicNumberScanner
from pc.services.pipeline import Worker


def build_worker(conn, paths, *, limits, security, jobs, worker_id=None):
    return Worker(
        conn,
        storage=LocalStorageBackend(paths.media_root),
        quarantine=LocalStorageBackend(paths.quarantine_root),
        scanner=MagicNumberScanner(policy=security, limits=limits),
        limits=limits,
        job_settings=jobs,
        worker_id=worker_id,
        upload_root=paths.data_root / "upload-staging",
    )
