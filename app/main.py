"""DICOM 批量匿名化 API。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .anonymizer import JobResult
from .config import settings
from .jobs import registry
from .schemas import FileOut, JobOut, PatientOut, SeriesOut, StudyOut


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.setup()
    task = asyncio.create_task(registry.purge_loop())
    yield
    task.cancel()


app = FastAPI(
    title="DICOM 批量匿名化 API",
    version="1.0.0",
    description=(
        "上传影像 ZIP，使用用户提供的稳定密钥生成患者级伪名，重写 "
        "Patient/Study/Series/SOP/FrameOfReference UID 与身份字段，"
        "递归更新序列内引用并保持检查关系。提供标签变更预览与匿名化 ZIP 下载。"
    ),
    lifespan=lifespan,
)

_executor = ThreadPoolExecutor(max_workers=2)


def _serialize_job(job) -> JobOut:
    if job.status == "failed":
        return JobOut(
            id=job.id,
            status="failed",
            secret_fingerprint=job.secret_fingerprint,
            shift_dates=job.shift_dates,
            source_zip=job.source_name,
            created_at=job.created_at,
            expires_at=job.expires_at,
            file_count=0,
            output_file_count=0,
            error_count=1,
            warning_count=0,
            changes_capped=False,
            patients=[],
            studies=[],
            series=[],
            files=[],
            error=job.error,
        )

    result: JobResult = job.result
    files = [FileOut.model_validate(r.to_dict(include_changes=True)) for r in result.files]
    error_count = sum(r.status == "error" for r in result.files)
    warning_count = sum(r.status == "warning" for r in result.files)
    output_count = sum(bool(r.output_name) for r in result.files)

    return JobOut(
        id=job.id,
        status="ready",
        secret_fingerprint=job.secret_fingerprint,
        shift_dates=job.shift_dates,
        source_zip=job.source_name,
        created_at=job.created_at,
        expires_at=job.expires_at,
        file_count=len(result.files),
        output_file_count=output_count,
        error_count=error_count,
        warning_count=warning_count,
        changes_capped=result.total_changes,
        patients=[
            PatientOut(
                key=p.key,
                new_patient_id=p.new_patient_id,
                new_patient_name=p.new_patient_name,
                date_shift_days=p.date_shift_days,
                file_count=p.file_count,
            )
            for p in sorted(result.patients.values(), key=lambda x: x.key)
        ],
        studies=[
            StudyOut(**v) for v in sorted(
                result.studies.values(), key=lambda x: x["new_study_instance_uid"]
            )
        ],
        series=[
            SeriesOut(**v) for v in sorted(
                result.series.values(), key=lambda x: x["new_series_instance_uid"]
            )
        ],
        files=files,
    )


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/jobs",
    response_model=JobOut,
    status_code=201,
    tags=["jobs"],
    summary="上传影像 ZIP 并创建匿名化任务（同步返回标签变更预览）",
)
async def create_job(
    file: UploadFile = File(..., description="包含 DICOM 文件的 ZIP 压缩包"),
    secret: str = Form(..., description="用户稳定密钥（>=8 字符），用于确定性派生伪名/UID"),
    shift_dates: bool = Form(True, description="是否按患者稳定偏移平移 DA/DT 日期"),
):
    if len(secret) < settings.min_secret_length:
        raise HTTPException(
            status_code=422,
            detail=f"稳定密钥长度至少 {settings.min_secret_length} 个字符",
        )
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=422, detail="仅接受 .zip 压缩包")

    # 流式落盘，超过大小上限立即拒绝
    spool = tempfile.NamedTemporaryFile(prefix="upload-", suffix=".zip", delete=False)
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"上传文件超过大小上限 "
                        f"{settings.max_upload_bytes // (1024 * 1024)} MiB"
                    ),
                )
            spool.write(chunk)
        spool.close()
        if total == 0:
            raise HTTPException(status_code=422, detail="上传内容为空")

        loop = asyncio.get_running_loop()
        job = await loop.run_in_executor(
            _executor,
            registry.create_and_process,
            spool.name,
            file.filename,
            secret,
            shift_dates,
        )
    finally:
        if os.path.exists(spool.name):
            try:
                os.unlink(spool.name)
            except OSError:
                pass

    return _serialize_job(job)


@app.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    tags=["jobs"],
    summary="获取任务标签变更预览与逐文件诊断",
)
def get_job(job_id: str) -> JobOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return _serialize_job(job)


@app.get(
    "/jobs/{job_id}/files",
    response_model=list[FileOut],
    tags=["jobs"],
    summary="仅获取逐文件诊断与变更列表",
)
def list_job_files(job_id: str) -> list[FileOut]:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status != "ready":
        return []
    return [FileOut.model_validate(r.to_dict(include_changes=True)) for r in job.result.files]


@app.get(
    "/jobs/{job_id}/download",
    tags=["jobs"],
    summary="下载匿名化后的 ZIP（扁平文件名，不含原始路径）",
)
def download_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status != "ready":
        raise HTTPException(status_code=409, detail=f"任务处理失败：{job.error}")
    zip_path = registry.ensure_zip(job)
    error_count = sum(r.status == "error" for r in job.result.files)
    name = f"anonymized-{job.id[:8]}.zip"
    headers = {
        "X-Job-File-Count": str(len(job.result.files)),
        "X-Job-Error-Count": str(error_count),
    }
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=name,
        headers=headers,
    )


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "dicom-anon-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": [
            "POST /jobs",
            "GET /jobs/{job_id}",
            "GET /jobs/{job_id}/files",
            "GET /jobs/{job_id}/download",
            "GET /health",
        ],
    }
