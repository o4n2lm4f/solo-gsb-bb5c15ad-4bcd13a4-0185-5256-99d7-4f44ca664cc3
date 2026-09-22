"""API 响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DiagnosticOut(BaseModel):
    code: str
    severity: str = Field(description="info | warning | error")
    message: str


class ChangeOut(BaseModel):
    path: str = ""
    tag: Optional[str] = None
    keyword: str
    action: str = Field(description="REPLACE | EMPTY | REMOVE")
    old: Optional[str] = None
    new: Optional[str] = None


class FileOut(BaseModel):
    name: str
    status: str = Field(description="ok | warning | error | skipped")
    output_name: Optional[str] = None
    patient_key: Optional[str] = None
    study_instance_uid: Optional[str] = None
    series_instance_uid: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    max_severity: Optional[str] = None
    diagnostics: list[DiagnosticOut] = []
    changes: list[ChangeOut] = []
    changes_truncated: bool = False


class PatientOut(BaseModel):
    key: str
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int
    file_count: int


class StudyOut(BaseModel):
    old_study_instance_uid: str
    new_study_instance_uid: str
    patient_key: str
    file_count: int


class SeriesOut(BaseModel):
    old_series_instance_uid: str
    new_series_instance_uid: str
    new_study_instance_uid: str
    file_count: int


class JobOut(BaseModel):
    id: str
    status: str = Field(description="ready | failed")
    secret_fingerprint: str
    shift_dates: bool
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    changes_capped: bool
    patients: list[PatientOut]
    studies: list[StudyOut]
    series: list[SeriesOut]
    files: list[FileOut]
    error: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    status: str
    source_zip: str
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    created_at: float
    expires_at: float


class ErrorOut(BaseModel):
    error: str
    detail: Optional[str] = None
