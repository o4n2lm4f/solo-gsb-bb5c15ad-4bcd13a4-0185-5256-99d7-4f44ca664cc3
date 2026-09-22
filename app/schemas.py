"""Pydantic response models for the anonymization API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChangeOut(BaseModel):
    tag: str
    action: Literal["set", "blank", "uid", "delete"]
    old: str | None = None
    new: str | None = None


class DiagnosticOut(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    tag: str | None = None
    element: str | None = None


class FileReportOut(BaseModel):
    path: str
    sopClass: str | None = None
    patient: str | None = None
    studyInstanceUID: str | None = None
    seriesInstanceUID: str | None = None
    sopInstanceUID: str | None = None
    changes: list[ChangeOut] = Field(default_factory=list)
    diagnostics: list[DiagnosticOut] = Field(default_factory=list)


class PatientOut(BaseModel):
    pseudonymID: str
    pseudonymName: str
    dateOffsetDays: int
    fileCount: int


class PreviewOut(BaseModel):
    jobId: str
    status: Literal["ready", "rejected"]
    options: dict[str, Any]
    totals: dict[str, Any]
    patients: list[PatientOut]
    files: list[FileReportOut]
    nonDicomFiles: list[str]
    uidMapsSample: dict[str, dict[str, str]]
    diagnostics: list[DiagnosticOut]
    truncated: dict[str, bool]


class JobCreatedOut(BaseModel):
    jobId: str
    status: str
    preview: PreviewOut


class JobStatusOut(BaseModel):
    jobId: str
    status: str
    createdAt: str
    expiresAt: str
    fileCount: int
    dicomCount: int
    errorCount: int
    warningCount: int


class ErrorOut(BaseModel):
    detail: str
    diagnostics: list[DiagnosticOut] | None = None
