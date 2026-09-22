"""FastAPI application: DICOM batch anonymization.

Endpoints
---------
POST /api/jobs              upload a ZIP + stable secret, run anonymization,
                            return a tag-change preview and a job id
GET  /api/jobs/{job_id}     job status + (optionally) preview
GET  /api/jobs/{job_id}/download
                            download the anonymized ZIP
GET  /healthz               liveness probe
GET  /docs                  Swagger UI
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import schemas
from .anonymizer import (
    AnonymizeOptions,
    ProcessResult,
    ZipLimitError,
    process_zip,
)

MAX_UPLOAD_BYTES = 3 * 1024 * 1024 * 1024
JOB_TTL_SECONDS = 60 * 60
CLEANUP_INTERVAL_SECONDS = 5 * 60
PREVIEW_CHANGE_FILES = 50       # per-file change lists beyond this are truncated
PREVIEW_UID_MAP_ENTRIES = 200   # per UID class

app = FastAPI(
    title="DICOM 批量匿名化 API",
    description=(
        "上传影像 ZIP，基于用户提供的稳定密钥生成患者级伪名，"
        "重写 Patient/Study/Series UID 与身份字段，"
        "递归更新序列内引用，并提供标签变更预览与匿名化 ZIP 下载。"
    ),
    version="1.0.0",
)


@dataclass
class Job:
    job_id: str
    result: ProcessResult
    options: AnonymizeOptions
    created_at: float

    @property
    def expires_at(self) -> float:
        return self.created_at + JOB_TTL_SECONDS


JOBS: dict[str, Job] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diag_severity_counts(result: ProcessResult) -> tuple[int, int]:
    errors = warnings = 0
    for report in result.file_reports.values():
        for d in report.diagnostics:
            if d.severity == "error":
                errors += 1
            else:
                warnings += 1
    for d in result.zip_diagnostics:
        if d.severity == "error":
            errors += 1
        else:
            warnings += 1
    return errors, warnings


def _all_diagnostics(result: ProcessResult) -> list[dict]:
    out: list[dict] = []
    for d in result.zip_diagnostics:
        out.append(d.to_dict())
    for path in result.member_order:
        report = result.file_reports.get(path)
        if not report:
            continue
        for d in report.diagnostics:
            payload = d.to_dict()
            payload["path"] = path
            out.append(payload)
    return out


def _build_preview(job: Job) -> dict:
    result = job.result
    errors, warnings = _diag_severity_counts(result)

    files_out: list[dict] = []
    change_budget = PREVIEW_CHANGE_FILES
    truncated_changes = False
    for path in result.member_order:
        report = result.file_reports.get(path)
        if report is None:
            continue
        include_changes = change_budget > 0 and bool(report.changes)
        if include_changes:
            change_budget -= 1
        elif report.changes:
            truncated_changes = True
        files_out.append(report.to_dict(include_changes=include_changes))

    uid_sample: dict[str, dict[str, str]] = {}
    truncated_uids = False
    for cls, mapping in result.uid_maps.items():
        items = list(mapping.items())
        if len(items) > PREVIEW_UID_MAP_ENTRIES:
            truncated_uids = True
        uid_sample[cls] = dict(items[:PREVIEW_UID_MAP_ENTRIES])

    return {
        "jobId": job.job_id,
        "status": "ready",
        "options": {
            "shiftDates": job.options.shift_dates,
            "keepPatientSex": job.options.keep_patient_sex,
            "removePrivateTags": job.options.remove_private_tags,
            "removeCurves": job.options.remove_curves,
            "removeOverlays": job.options.remove_overlays,
            "strictReferences": job.options.strict_references,
            "includeReport": job.options.include_report,
            "uidRoot": job.options.uid_root or "2.25.<secret-derived-uuid5>",
        },
        "totals": {
            "members": len(result.member_order),
            "dicomFiles": sum(1 for r in result.file_reports.values()
                              if r.sop_uid is not None),
            "nonDicomFiles": len(result.non_dicom),
            "patients": len(result.patients),
            "errorCount": errors,
            "warningCount": warnings,
            "outputBytes": len(result.output_zip),
        },
        "patients": [
            {
                "pseudonymID": p.pseudonym_id,
                "pseudonymName": p.pseudonym_name,
                "dateOffsetDays": p.date_offset_days,
                "fileCount": p.file_count,
            }
            for p in result.patients
        ],
        "files": files_out,
        "nonDicomFiles": result.non_dicom,
        "uidMapsSample": uid_sample,
        "diagnostics": _all_diagnostics(result),
        "truncated": {
            "fileChanges": truncated_changes,
            "uidMaps": truncated_uids,
        },
    }


def _build_options(secret: str, uid_root: str, shift_dates: bool,
                   keep_patient_sex: bool, remove_private_tags: bool,
                   remove_curves: bool, remove_overlays: bool,
                   strict_references: bool, include_report: bool) -> AnonymizeOptions:
    secret = (secret or "").strip()
    if len(secret) < 8:
        raise HTTPException(
            status_code=422,
            detail="secret 至少需要 8 个字符；它用于 HMAC 派生稳定伪名与 UID。")
    root_error = AnonymizeOptions.validate_uid_root(uid_root)
    if root_error:
        raise HTTPException(status_code=422, detail=root_error)
    return AnonymizeOptions(
        secret=secret,
        uid_root=(uid_root or "").strip(),
        shift_dates=shift_dates,
        keep_patient_sex=keep_patient_sex,
        remove_private_tags=remove_private_tags,
        remove_curves=remove_curves,
        remove_overlays=remove_overlays,
        strict_references=strict_references,
        include_report=include_report,
    )


# ---------------------------------------------------------------------------
# Lifecycle / middleware
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_cleanup_loop())


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        expired = [jid for jid, job in JOBS.items() if job.expires_at < now]
        for jid in expired:
            JOBS.pop(jid, None)


@app.middleware("http")
async def _limit_upload_size(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/api/jobs":
        cl = request.headers.get("content-length")
        if cl and int(cl) > MAX_UPLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"上传体积超过上限 {MAX_UPLOAD_BYTES} 字节"},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "activeJobs": len(JOBS)}


@app.post("/api/jobs", response_model=schemas.JobCreatedOut,
          status_code=201,
          responses={
              400: {"model": schemas.ErrorOut},
              413: {"model": schemas.ErrorOut},
              422: {"model": schemas.ErrorOut},
          })
async def create_job(
    file: UploadFile = File(..., description="包含 DICOM 影像的 ZIP 压缩包"),
    secret: str = Form(..., min_length=8,
                       description="用户稳定密钥；相同密钥+相同原始 ID 永远得到相同伪名/UID"),
    uid_root: str = Form("", description="可选组织 UID 根（点分十进制 OID），留空使用 2.25."),
    shift_dates: bool = Form(True),
    keep_patient_sex: bool = Form(True),
    remove_private_tags: bool = Form(True),
    remove_curves: bool = Form(True),
    remove_overlays: bool = Form(False),
    strict_references: bool = Form(
        False, description="为 true 时，引用缺失按错误处理并拒绝任务（422）"),
    include_report: bool = Form(True,
                                description="在输出 ZIP 中附带 ANONYMIZATION_REPORT.json"),
):
    options = _build_options(
        secret, uid_root, shift_dates, keep_patient_sex,
        remove_private_tags, remove_curves, remove_overlays,
        strict_references, include_report)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空。")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, process_zip, data, options)
    except ZipLimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    errors, warnings = _diag_severity_counts(result)
    valid_dicom = sum(1 for r in result.file_reports.values() if r.sop_uid is not None)

    job = Job(
        job_id=uuid.uuid4().hex,
        result=result,
        options=options,
        created_at=time.time(),
    )
    preview = _build_preview(job)

    if valid_dicom == 0:
        # Nothing usable: keep no job, return full per-file diagnostics.
        raise HTTPException(
            status_code=422,
            detail={"detail": "ZIP 中没有可处理的合法 DICOM 文件。",
                    "diagnostics": preview["diagnostics"],
                    "nonDicomFiles": result.non_dicom})

    if strict_references:
        ref_errors = [
            d for d in preview["diagnostics"]
            if d.get("code") == "missing_reference" and d["severity"] == "error"
        ]
        if ref_errors:
            raise HTTPException(
                status_code=422,
                detail={"detail": "存在缺失的 DICOM 引用（strict_references=true）。",
                        "diagnostics": preview["diagnostics"]})

    JOBS[job.job_id] = job
    preview["jobId"] = job.job_id
    return {"jobId": job.job_id, "status": "ready", "preview": preview}


@app.get("/api/jobs/{job_id}", response_model=schemas.JobStatusOut)
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    errors, warnings = _diag_severity_counts(job.result)
    return {
        "jobId": job.job_id,
        "status": "ready",
        "createdAt": datetime.fromtimestamp(job.created_at, timezone.utc).isoformat(),
        "expiresAt": datetime.fromtimestamp(job.expires_at, timezone.utc).isoformat(),
        "fileCount": len(job.result.member_order),
        "dicomCount": sum(1 for r in job.result.file_reports.values()
                          if r.sop_uid is not None),
        "errorCount": errors,
        "warningCount": warnings,
    }


@app.get("/api/jobs/{job_id}/preview")
async def get_preview(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    return _build_preview(job)


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    return Response(
        content=job.result.output_zip,
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f'attachment; filename="anonymized-{job.job_id[:8]}.zip"'
        },
    )


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    if JOBS.pop(job_id, None) is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    return Response(status_code=204)
