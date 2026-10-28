"""HTTP endpoints for durable Claude export acquisition and recovery."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any

import portalocker
from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from everos.umif.package import read_package

from ..contracts import ApiEnvelope
from ..import_contracts import ImportWorkflowError
from ..import_jobs import MAX_LOCAL_ZIPS
from ..source_inventory import source_members_from_job
from .imports import _raise_api_error

router = APIRouter(prefix="/import-jobs", tags=["memo-vault-import-jobs"])


class PreviewOptions(BaseModel):
    confirm_project_merge: bool = False


def schedule_job(request: Request, job_id: str) -> None:
    launch_job(request.app, job_id)


def launch_job(app: Any, job_id: str) -> None:
    """Keep task references and deduplicate scheduling within the process."""
    tasks = app.state.import_job_tasks
    if job_id in tasks and not tasks[job_id].done():
        return

    async def worker() -> None:
        async with app.state.import_job_semaphore:
            await run_in_threadpool(app.state.import_jobs.run, job_id)

    task = asyncio.create_task(worker())
    tasks[job_id] = task

    def finished(done: asyncio.Task[Any]) -> None:
        tasks.pop(job_id, None)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)


@router.post("", status_code=202)
async def create_job(
    request: Request,
    file: Annotated[UploadFile, File()],
    project_id: Annotated[str, Form()] = "personal",
) -> ApiEnvelope[dict[str, Any]]:
    manager = request.app.state.import_jobs
    try:
        payload = await file.read(manager.downloader.max_bytes + 1)
        job = await run_in_threadpool(
            manager.create, file.filename or "export.json", payload, project_id
        )
        schedule_job(request, job["id"])
        return ApiEnvelope(request_id=uuid.uuid4().hex, data=job)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    except portalocker.exceptions.LockException:
        _raise_api_error(
            ImportWorkflowError(
                "IMPORT_JOB_RUNNING",
                "Job is busy; retry after it stops.",
                status_code=409,
            )
        )
    finally:
        await file.close()


@router.post("/local", status_code=202)
async def create_local_job(
    request: Request,
    files: Annotated[list[UploadFile], File()],
    project_id: Annotated[str, Form()] = "personal",
    manifest: Annotated[UploadFile | None, File()] = None,
) -> ApiEnvelope[dict[str, Any]]:
    """Stream a complete locally downloaded Claude export into one job."""
    manager = request.app.state.import_jobs
    try:
        if not 1 <= len(files) <= MAX_LOCAL_ZIPS:
            raise ImportWorkflowError(
                "CLAUDE_INVALID_EXPORT", "Select 1 to 999 Claude ZIP files."
            )
        with tempfile.TemporaryDirectory(
            prefix=".local-upload-", dir=manager.root
        ) as temp:
            staged: list[tuple[str, Path]] = []
            names: set[str] = set()
            total = 0
            for file in files:
                name = file.filename or ""
                if (
                    name != manager.imports.safe_filename(name)
                    or not name.lower().endswith(".zip")
                    or name.casefold() in names
                ):
                    raise ImportWorkflowError(
                        "CLAUDE_INVALID_EXPORT",
                        "Duplicate or unsafe ZIP filename in this selection.",
                    )
                names.add(name.casefold())
                path = Path(temp) / name
                with path.open("xb") as target:
                    path.chmod(0o600)
                    size = 0
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if (
                            size > manager.downloader.max_bytes
                            or total > manager.max_total_bytes
                        ):
                            raise ImportWorkflowError(
                                "IMPORT_SECURITY_REJECTED",
                                "Selected ZIP files exceed the import size limit.",
                                status_code=413,
                            )
                        target.write(chunk)
                staged.append((name, path))
            manifest_bytes = (
                await manifest.read(2 * 1024 * 1024 + 1)
                if manifest is not None
                else None
            )
            job = await run_in_threadpool(
                manager.create_local_bundle,
                staged,
                project_id,
                manifest=manifest_bytes,
            )
        schedule_job(request, job["id"])
        return ApiEnvelope(request_id=uuid.uuid4().hex, data=job)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    finally:
        for file in files:
            await file.close()
        if manifest is not None:
            await manifest.close()


@router.get("/{job_id}/source-inventory")
async def job_source_inventory(job_id: str, request: Request) -> ApiEnvelope[dict[str, Any]]:
    manager = request.app.state.import_jobs
    try:
        status = await run_in_threadpool(manager.status, job_id)
        if status["status"] not in ("ready_for_preview", "committed"):
            raise ImportWorkflowError("CLAUDE_PREVIEW_NOT_READY", "Source inventory is not ready.", status_code=409)
        directory = manager.path(job_id)
        package = await run_in_threadpool(read_package, directory / "umif")
        data = await run_in_threadpool(source_members_from_job, directory, package)
        return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    except (OSError, ValueError):
        _raise_api_error(ImportWorkflowError("UMIF_VALIDATION_FAILED", "Source inventory is unavailable."))


@router.get("")
async def list_jobs(request: Request) -> ApiEnvelope[list[dict[str, Any]]]:
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data=await run_in_threadpool(request.app.state.import_jobs.list_jobs),
    )


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> ApiEnvelope[dict[str, Any]]:
    try:
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data=await run_in_threadpool(request.app.state.import_jobs.status, job_id),
        )
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


@router.post("/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request) -> ApiEnvelope[dict[str, Any]]:
    try:
        await run_in_threadpool(request.app.state.import_jobs.retry, job_id)
        schedule_job(request, job_id)
        return await get_job(job_id, request)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> ApiEnvelope[dict[str, Any]]:
    try:
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data=await run_in_threadpool(request.app.state.import_jobs.cancel, job_id),
        )
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


@router.post("/{job_id}/parts", status_code=202)
async def supply_part(
    job_id: str, request: Request, file: Annotated[UploadFile, File()]
) -> ApiEnvelope[dict[str, Any]]:
    manager = request.app.state.import_jobs
    try:
        payload = await file.read(manager.downloader.max_bytes + 1)
        job = await run_in_threadpool(
            manager.supply_part, job_id, file.filename or "", payload
        )
        schedule_job(request, job_id)
        return ApiEnvelope(request_id=uuid.uuid4().hex, data=job)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    except portalocker.exceptions.LockException:
        _raise_api_error(
            ImportWorkflowError(
                "IMPORT_JOB_RUNNING",
                "Job is busy; retry after it stops.",
                status_code=409,
            )
        )
    finally:
        await file.close()


@router.post("/{job_id}/preview")
async def preview_job(
    job_id: str, body: PreviewOptions, request: Request
) -> ApiEnvelope[dict[str, Any]]:
    try:
        result = await run_in_threadpool(
            request.app.state.import_jobs.preview,
            job_id,
            confirm_project_merge=body.confirm_project_merge,
        )
        return ApiEnvelope(request_id=uuid.uuid4().hex, data=result)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
