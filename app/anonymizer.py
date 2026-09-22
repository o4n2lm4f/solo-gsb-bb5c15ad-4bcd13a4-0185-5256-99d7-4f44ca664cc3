"""Core DICOM anonymization engine.

Pipeline:
1. Scan the uploaded ZIP for DICOM files (metadata pass, pixel data skipped).
2. Collect the global set of declared UIDs (SOP / Study / Series / Frame of
   Reference / Concatenation), group patients deterministically, and build
   stable pseudonym / UID maps derived from the user secret.
3. Full pass: rewrite identity fields, remap every UID *and* recurse through
   sequences so cross-level references (ReferencedStudySequence,
   ReferencedSOPSequence, SourceImageSequence, ...) keep pointing at the
   rewritten objects. Missing references and non-DICOM files are reported
   per file instead of aborting the batch.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import io
import json
import os
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterable

import pydicom
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.tag import Tag
from pydicom.uid import UID

# ---------------------------------------------------------------------------
# Tag catalogs (Basic Application Confidentiality Profile + selected options)
# ---------------------------------------------------------------------------

# Identity tags replaced with empty values. SQ-typed members are deleted.
# Names that do not exist in a dataset are simply never matched.
BLANK_TAGS: frozenset[str] = frozenset({
    # patient demographics / contacts
    "IssuerOfPatientID",
    "PatientBirthName",
    "PatientBirthTime",
    "PatientMotherBirthName",
    "PatientMotherName",
    "PatientReligiousPreference",
    "PatientTelephoneNumbers",
    "PatientAddress",
    "MilitaryRank",
    "BranchOfService",
    "MedicalRecordLocator",
    "Occupation",
    "CountryOfResidence",
    "RegionOfResidence",
    "PatientInstitutionResidence",
    "SmokingStatus",
    "PregnancyStatus",
    "AdditionalPatientHistory",
    "LastMenstrualDate",
    "MedicalAlerts",
    "Allergies",
    "SpecialNeeds",
    "ResponsiblePerson",
    "ResponsiblePersonRole",
    "ResponsibleOrganization",
    "PatientComments",
    "EthnicGroup",
    "PatientPrimaryLanguageCodeSequence",
    "PatientSpeciesDescription",
    "OtherPatientIDs",
    # institution / staff
    "ReferringPhysicianName",
    "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers",
    "ReferringPhysicianIdentificationSequence",
    "PhysiciansOfRecord",
    "PhysiciansOfRecordIdentificationSequence",
    "PerformingPhysicianName",
    "PerformingPhysicianIdentificationSequence",
    "NameOfPhysiciansReadingStudy",
    "PhysiciansReadingStudyIdentificationSequence",
    "OperatorsName",
    "OperatorsIdentificationSequence",
    "InstitutionName",
    "InstitutionAddress",
    "InstitutionalDepartmentName",
    "InstitutionCodeSequence",
    "StationName",
    "DeviceSerialNumber",
    "GantryID",
    "PlateID",
    "GeneratorID",
    "CassetteID",
    "DetectorID",
    "ConsultingPhysicianName",
    "ConsultingPhysicianIdentificationSequence",
    "ScheduledPerformingPhysicianName",
    "RequestingPhysician",
    "RequestingPhysicianIdentificationSequence",
    "RequestingService",
    "ReviewerName",
    "VerifyingObserverName",
    "VerifyingObserverIdentificationSequence",
    "PersonName",
    "PersonIdentificationCodeSequence",
    "DistributionName",
    "DistributionAddress",
    # visit / order / study descriptions
    "AdmittingDate",
    "AdmittingTime",
    "AdmittingDiagnosesDescription",
    "AdmittingDiagnosesCodeSequence",
    "AccessionNumber",
    "CurrentPatientLocation",
    "ServiceEpisodeDescription",
    "ServiceEpisodeID",
    "ReasonForStudy",
    "StudyDescription",
    "SeriesDescription",
    "StudyComments",
    "ImageComments",
    "ContrastAllergy",
    "RequestAttributesSequence",
    "ReasonForPerformedProcedureCodeSequence",
    "ScheduledProcedureStepDescription",
    "ScheduledProcedureStepID",
    "ScheduledProcedureStepStartDate",
    "ScheduledProcedureStepStartDateTime",
    "ScheduledProcedureStepStartTime",
    "PerformedProcedureStepDescription",
    "PerformedProcedureStepID",
    "PerformedProcedureStepStartDate",
    "PerformedProcedureStepStartTime",
    "PerformedProcedureStepEndDate",
    "PerformedProcedureStepEndTime",
    "PerformedProtocolCodeSequence",
    "IssuerOfPatientIDQualifiersSequence",
})

# Sequences deleted outright.
DELETE_SEQUENCES: frozenset[str] = frozenset({
    "OriginalAttributesSequence",
    "ReferencedPatientAliasSequence",
    "PatientIdentificationSequence",
    "OtherPatientIDsSequence",
    "PatientInsurancePlanCodeSequence",
})

# Top-level declared UID keywords - these form the global "known UID" set.
DECLARED_UID_KEYWORDS: tuple[str, ...] = (
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "FrameOfReferenceUID",
    "ConcatenationUID",
)

# SOP class / transfer syntax / well-known code UIDs are not instance UIDs and
# must never be rewritten.
UID_KEYWORDS_SKIP: frozenset[str] = frozenset({
    "ReferencedSOPClassUID",
    "SOPClassUID",
    "MediaStorageSOPClassUID",
    "TransferSyntaxUID",
    "ImplementationClassUID",
    "CodingSchemeUID",
    "SynchronizationClassUID",
    "MIMETypeOfEncapsulatedDocument",  # not UI, harmless to list
})

DEIDENTIFICATION_METHOD = "DICOM BATCH ANONYMIZER"
DEIDENTIFICATION_METHOD_CODE_MEANING = (
    "Basic Application Confidentiality Profile; stable pseudonymization"
)

UID_MAX_LENGTH = 64


# ---------------------------------------------------------------------------
# Diagnostics / records
# ---------------------------------------------------------------------------


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning"
    code: str
    message: str
    tag: str | None = None
    element: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.tag is not None:
            out["tag"] = self.tag
        if self.element is not None:
            out["element"] = self.element
        return out


@dataclass
class ChangeRecord:
    tag: str
    action: str  # "set" | "blank" | "uid" | "delete"
    old: str | None = None
    new: str | None = None


@dataclass
class FileReport:
    path: str
    sop_class: str | None = None
    patient: str | None = None
    study_uid: str | None = None
    series_uid: str | None = None
    sop_uid: str | None = None
    changes: list[ChangeRecord] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_dict(self, include_changes: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "sopClass": self.sop_class,
            "patient": self.patient,
            "studyInstanceUID": self.study_uid,
            "seriesInstanceUID": self.series_uid,
            "sopInstanceUID": self.sop_uid,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }
        if include_changes:
            out["changes"] = [c.__dict__ for c in self.changes]
        return out


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class AnonymizeOptions:
    secret: str
    uid_root: str = ""          # empty => 2.25. + UUID5-based UID
    shift_dates: bool = True
    keep_patient_sex: bool = True
    remove_private_tags: bool = True
    remove_curves: bool = True
    remove_overlays: bool = False
    strict_references: bool = False
    include_report: bool = True
    max_total_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_members: int = 20_000

    @staticmethod
    def validate_uid_root(root: str) -> str | None:
        root = (root or "").strip()
        if not root:
            return None
        if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", root):
            return "uid_root 必须是点分十进制 OID，例如 1.2.840.12345"
        if root.split(".")[0] in ("0", "2") or root.startswith("1.2.840.10008."):
            return "uid_root 不允许以 0/2 开头，也不能使用 DICOM 保留根 1.2.840.10008.*"
        return None


class ZipLimitError(Exception):
    """Raised when the uploaded ZIP exceeds configured safety limits."""


# ---------------------------------------------------------------------------
# Deterministic mapping helpers
# ---------------------------------------------------------------------------


class UidMapper:
    """Stable original-UID -> anonymized-UID mapping.

    Custom root: ``root + "." + digits`` derived from HMAC-SHA256 of the
    secret and the original UID, truncated to 64 characters.
    No root (default): ``2.25.<uuid5>`` where the uuid5 namespace is derived
    from the secret - reproducible and valid per DICOM PS3.5 2.25 UUID UIDs.
    """

    def __init__(self, secret: str, uid_root: str = "") -> None:
        self._secret = secret.encode("utf-8")
        self._root = uid_root.strip()
        self._cache: dict[str, str] = {}
        self._reverse: dict[str, str] = {}
        if not self._root:
            self._namespace = uuid.uuid5(uuid.NAMESPACE_URL, "urn:dicom-anon:" + secret)
        else:
            self._namespace = None  # type: ignore[assignment]

    def map_uid(self, original: str) -> str:
        original = str(original)
        if original in self._cache:
            return self._cache[original]

        if self._root:
            budget = UID_MAX_LENGTH - len(self._root) - 1
            if budget < 8:
                raise ValueError("uid_root 过长，无法生成不超过 64 字符的 UID")
            digest = hmac.new(self._secret, b"uid:" + original.encode(),
                              hashlib.sha256).digest()
            number = int.from_bytes(digest[:16], "big")
            candidate = f"{self._root}.{str(number)[:budget]}"
            # Practically unreachable collision guard.
            salt = 0
            while candidate in self._reverse and self._reverse[candidate] != original:
                salt += 1
                tail = str(number + salt)[:budget]
                candidate = f"{self._root}.{tail}"
        else:
            base = "2.25." + str(uuid.uuid5(self._namespace, original).int)
            candidate = base
            salt = 0
            while candidate in self._reverse and self._reverse[candidate] != original:
                salt += 1
                suffix = str(uuid.uuid5(self._namespace, original + f"#{salt}").int)
                candidate = ("2.25." + suffix)[:UID_MAX_LENGTH]

        self._cache[original] = candidate
        self._reverse[candidate] = original
        return candidate


def pseudonym_token(secret: str, namespace: str, value: str, length: int = 12) -> str:
    digest = hmac.new(secret.encode("utf-8"),
                      f"{namespace}:{value}".encode("utf-8"),
                      hashlib.sha256).digest()
    alphabet = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O/1/I/U
    n = int.from_bytes(digest, "big")
    out = []
    while len(out) < length:
        out.append(alphabet[n % len(alphabet)])
        n //= len(alphabet)
    return "".join(out)


# ---------------------------------------------------------------------------
# Dataset traversal helpers
# ---------------------------------------------------------------------------


def _iter_elements(ds: Dataset) -> Iterable[tuple[Dataset, Any]]:
    for elem in list(ds):
        yield ds, elem
        if elem.VR == "SQ":
            for item in elem.value or []:
                yield from _iter_elements(item)


def _keyword(elem: Any) -> str:
    return getattr(elem, "keyword", None) or ""


def _value_str(value: Any, limit: int = 80) -> str:
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        s = repr(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _is_overlay_tag(tag: Tag) -> bool:
    group = tag.group
    return 0x6000 <= group <= 0x60FF and (group & 1) == 0


def _is_curve_tag(tag: Tag) -> bool:
    return 0x5000 <= tag.group <= 0x50FF


# ---------------------------------------------------------------------------
# Plan (metadata pass)
# ---------------------------------------------------------------------------


@dataclass
class PatientInfo:
    key: str
    pseudonym_id: str
    pseudonym_name: str
    date_offset_days: int
    original_ids: set[str] = field(default_factory=set)
    original_names: set[str] = field(default_factory=set)
    file_count: int = 0


@dataclass
class BatchPlan:
    member_order: list[str]                 # normalized ZIP member names
    dicom_paths: list[str]
    non_dicom_paths: list[str]
    patients: dict[str, PatientInfo]
    studies: dict[str, set[str]]            # original study uid -> patient keys
    declared_uids: set[str]
    member_lookup: dict[str, str]           # normalized -> raw member name
    diagnostics: dict[str, list[Diagnostic]]
    zip_diagnostics: list[Diagnostic]
    total_uncompressed: int


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _dcmread(raw: bytes, stop_before_pixels: bool) -> Dataset:
    """Read DICOM, retrying without preamble (force=True) for files that
    lack the 128-byte DICM preamble but still carry a valid dataset."""
    try:
        return pydicom.dcmread(io.BytesIO(raw),
                               stop_before_pixels=stop_before_pixels)
    except InvalidDicomError:
        return pydicom.dcmread(io.BytesIO(raw),
                               stop_before_pixels=stop_before_pixels,
                               force=True)


class AnonymizationEngine:
    def __init__(self, options: AnonymizeOptions) -> None:
        self.opts = options
        self.uid_mapper = UidMapper(options.secret, options.uid_root)

    # -- pass A -----------------------------------------------------------

    def plan_from_zip(self, zf: zipfile.ZipFile) -> BatchPlan:
        member_order: list[str] = []
        member_lookup: dict[str, str] = {}
        dicom_paths: list[str] = []
        non_dicom: list[str] = []
        patients: dict[str, PatientInfo] = {}
        studies: dict[str, set[str]] = {}
        declared: set[str] = set()
        diagnostics: dict[str, list[Diagnostic]] = {}
        zip_diag: list[Diagnostic] = []
        total = 0

        for info in zf.infolist():
            if info.is_dir():
                continue
            if len(member_order) >= self.opts.max_members:
                raise ZipLimitError(f"ZIP 成员数量超过上限 {self.opts.max_members}")
            total += info.file_size
            if total > self.opts.max_total_uncompressed_bytes:
                raise ZipLimitError(
                    f"解压后总大小超过上限 {self.opts.max_total_uncompressed_bytes} 字节")

            path = self._safe_member_name(info.filename)
            if path is None:
                zip_diag.append(Diagnostic(
                    "error", "unsafe_path",
                    f"跳过含绝对路径或路径穿越片段的成员: {info.filename}"))
                continue
            if path in member_lookup:
                zip_diag.append(Diagnostic(
                    "warning", "duplicate_member_name",
                    f"ZIP 内出现重名成员，后者将覆盖前者的预览: {path}"))
            member_order.append(path)
            member_lookup[path] = info.filename

            diags: list[Diagnostic] = []
            raw = zf.read(info)
            try:
                ds = _dcmread(raw, stop_before_pixels=True)
            except Exception as exc:
                non_dicom.append(path)
                diags.append(Diagnostic(
                    "error", "not_dicom",
                    f"无法解析为 DICOM 文件: {exc.__class__.__name__}: {exc}"))
                diagnostics[path] = diags
                continue

            has_meta = getattr(ds, "file_meta", None) is not None
            if not has_meta and not getattr(ds, "SOPInstanceUID", None):
                non_dicom.append(path)
                diags.append(Diagnostic(
                    "error", "not_dicom",
                    "缺少 DICOM 文件元信息 (preamble/DICM) 且无 SOP Instance UID"))
                diagnostics[path] = diags
                continue

            dicom_paths.append(path)

            pid = str(getattr(ds, "PatientID", "") or "").strip()
            pname = str(getattr(ds, "PatientName", "") or "").strip()
            pkey = pid or pname or "__NO_ID__"
            if pkey not in patients:
                token = pseudonym_token(self.opts.secret, "patient", pkey, 12)
                patients[pkey] = PatientInfo(
                    key=pkey,
                    pseudonym_id=f"ANON-{token}",
                    pseudonym_name=f"ANON^{token}",
                    date_offset_days=self._patient_offset(pkey),
                )
            pinfo = patients[pkey]
            if pid:
                pinfo.original_ids.add(pid)
            if pname:
                pinfo.original_names.add(pname)
            pinfo.file_count += 1

            for kw in DECLARED_UID_KEYWORDS:
                val = getattr(ds, kw, None)
                if val:
                    declared.add(str(val))

            study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
            if study_uid:
                bucket = studies.setdefault(study_uid, set())
                bucket.add(pkey)
                if len(bucket) > 1:
                    diags.append(Diagnostic(
                        "warning", "study_patient_mismatch",
                        f"Study {study_uid} 关联了多个不同的患者标识: {sorted(bucket)}",
                        tag="StudyInstanceUID"))

            diagnostics.setdefault(path, diags)

        return BatchPlan(
            member_order=member_order,
            dicom_paths=dicom_paths,
            non_dicom_paths=non_dicom,
            patients=patients,
            studies=studies,
            declared_uids=declared,
            member_lookup=member_lookup,
            diagnostics=diagnostics,
            zip_diagnostics=zip_diag,
            total_uncompressed=total,
        )

    def _patient_offset(self, pkey: str) -> int:
        if not self.opts.shift_dates:
            return 0
        digest = hmac.new(self.opts.secret.encode(),
                          b"dateshift:" + pkey.encode(), hashlib.sha256).digest()
        return -(int.from_bytes(digest[:4], "big") % 181)  # 0..180 天，稳定

    @staticmethod
    def _safe_member_name(filename: str) -> str | None:
        name = filename.replace("\\", "/")
        name = re.sub(r"^[A-Za-z]:/", "", name).lstrip("/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if ".." in parts or name.startswith("/"):
            return None
        return "/".join(parts) if parts else None

    # -- pass B: per-file anonymization -----------------------------------

    def anonymize_file(self, ds: Dataset, path: str, plan: BatchPlan) -> FileReport:
        report = FileReport(path=path)
        diags: list[Diagnostic] = []

        pkey = (str(getattr(ds, "PatientID", "") or "").strip()
                or str(getattr(ds, "PatientName", "") or "").strip()
                or "__NO_ID__")
        pinfo = plan.patients[pkey]
        offset = pinfo.date_offset_days

        # 1) declared top-level UIDs --------------------------------------
        for kw in DECLARED_UID_KEYWORDS:
            old = getattr(ds, kw, None)
            if old:
                new = self.uid_mapper.map_uid(str(old))
                setattr(ds, kw, UID(new))
                report.changes.append(ChangeRecord(
                    kw, "uid", _value_str(old), new))

        # 2) file meta UIDs ------------------------------------------------
        meta = getattr(ds, "file_meta", None)
        if meta is not None:
            old_msuid = getattr(meta, "MediaStorageSOPInstanceUID", None)
            if old_msuid:
                new_msuid = self.uid_mapper.map_uid(str(old_msuid))
                meta.MediaStorageSOPInstanceUID = UID(new_msuid)
                report.changes.append(ChangeRecord(
                    "MediaStorageSOPInstanceUID", "uid",
                    _value_str(old_msuid), new_msuid))
            if getattr(meta, "ImplementationVersionName", None):
                old_v = str(meta.ImplementationVersionName)
                meta.ImplementationVersionName = "ANON_API_1"
                report.changes.append(ChangeRecord(
                    "ImplementationVersionName", "set",
                    _value_str(old_v), "ANON_API_1"))

        # 3) references inside sequences (recursive) -----------------------
        self._remap_references(ds, plan, report, diags)

        # 4) patient identity ----------------------------------------------
        old_pid = str(getattr(ds, "PatientID", "") or "")
        ds.PatientID = pinfo.pseudonym_id
        report.changes.append(ChangeRecord(
            "PatientID", "set", old_pid or None, pinfo.pseudonym_id))

        old_pname = str(getattr(ds, "PatientName", "") or "")
        ds.PatientName = pinfo.pseudonym_name
        report.changes.append(ChangeRecord(
            "PatientName", "set", old_pname or None, pinfo.pseudonym_name))

        if not self.opts.keep_patient_sex and "PatientSex" in ds:
            old_sex = str(ds.PatientSex)
            ds.PatientSex = ""
            report.changes.append(ChangeRecord(
                "PatientSex", "blank", old_sex, ""))

        if self.opts.shift_dates and "PatientBirthDate" in ds:
            old_bd = str(ds.PatientBirthDate or "")
            new_bd = self._shift_da(old_bd, offset)
            ds.PatientBirthDate = new_bd
            report.changes.append(ChangeRecord(
                "PatientBirthDate", "set", old_bd or None, new_bd or None))

        if "StudyInstanceUID" in ds:
            sid = pseudonym_token(
                self.opts.secret, "study", str(ds.StudyInstanceUID), 8)
            old_sid = str(getattr(ds, "StudyID", "") or "")
            ds.StudyID = sid
            report.changes.append(ChangeRecord(
                "StudyID", "set", old_sid or None, sid))

        # 5) identity tag removal / blanking -------------------------------
        # 5a) sequences first (top-level + nested), so the snapshot-based
        #     traversal below never descends into a deleted sequence.
        def delete_identity_sequences(owner: Dataset) -> None:
            for elem in list(owner):
                kw = _keyword(elem)
                if kw in DELETE_SEQUENCES or (kw in BLANK_TAGS and elem.VR == "SQ"):
                    old = _value_str(elem.value)
                    action = "delete" if kw in DELETE_SEQUENCES else "blank"
                    del owner[elem.tag]
                    report.changes.append(ChangeRecord(kw, action, old, None))
                    continue
                if elem.VR == "SQ":
                    for item in elem.value or []:
                        delete_identity_sequences(item)

        delete_identity_sequences(ds)

        # 5b) remaining scalar / PN elements (snapshot iteration is safe for
        #     deletions because only scalars are left to remove).
        for owner, elem in _iter_elements(ds):
            kw = _keyword(elem)
            if not kw:
                continue
            if elem.VR == "PN" and kw != "PatientName":
                old = _value_str(elem.value)
                del owner[elem.tag]
                report.changes.append(ChangeRecord(kw, "blank", old if old else None, ""))
            elif kw in BLANK_TAGS and elem.VR != "SQ":
                old = _value_str(elem.value)
                setattr(owner, kw, "")
                report.changes.append(ChangeRecord(kw, "blank", old if old else None, ""))

        # 6) (sequences were handled in 5a) ---------------------------------

        # 7) shift every remaining DA element (keeps longitudinal spacing) -
        if self.opts.shift_dates:
            for owner, elem in _iter_elements(ds):
                if elem.VR != "DA" or _keyword(elem) in ("PatientBirthDate",):
                    continue
                old = str(elem.value or "")
                if not old:
                    continue
                new = self._shift_da(old, offset)
                elem.value = new
                report.changes.append(ChangeRecord(
                    _keyword(elem) or str(elem.tag), "set", old, new))

        # 8) private / curves / overlays -----------------------------------
        if self.opts.remove_private_tags:
            n = self._remove_private(ds)
            if n:
                report.changes.append(ChangeRecord(
                    "(private tags)", "delete", f"{n} 个私有数据元素", None))
        if self.opts.remove_curves:
            n = self._remove_tag_range(ds, _is_curve_tag)
            if n:
                report.changes.append(ChangeRecord(
                    "(50xx curves)", "delete", f"{n} 个 Curve 元素", None))
        if self.opts.remove_overlays:
            n = self._remove_tag_range(ds, _is_overlay_tag)
            if n:
                report.changes.append(ChangeRecord(
                    "(60xx overlays)", "delete", f"{n} 个 Overlay 元素", None))

        # 9) de-identification bookkeeping ---------------------------------
        ds.PatientIdentityRemoved = "YES"
        report.changes.append(ChangeRecord(
            "PatientIdentityRemoved", "set", None, "YES"))
        ds.DeidentificationMethod = DEIDENTIFICATION_METHOD
        report.changes.append(ChangeRecord(
            "DeidentificationMethod", "set", None, DEIDENTIFICATION_METHOD))
        if self.opts.shift_dates:
            ds.LongitudinalTemporalInformationModified = "MODIFIED"
            report.changes.append(ChangeRecord(
                "LongitudinalTemporalInformationModified", "set", None, "MODIFIED"))
        if "DeidentificationMethodCodeSequence" not in ds:
            item = Dataset()
            item.CodeValue = "113100"
            item.CodingSchemeDesignator = "DCM"
            item.CodeMeaning = DEIDENTIFICATION_METHOD_CODE_MEANING
            ds.DeidentificationMethodCodeSequence = [item]
            report.changes.append(ChangeRecord(
                "DeidentificationMethodCodeSequence", "set", None,
                "[113100/DCM Basic Application Confidentiality Profile]"))

        report.sop_class = str(getattr(ds, "SOPClassUID", "") or None)
        report.patient = pinfo.pseudonym_id
        report.study_uid = str(getattr(ds, "StudyInstanceUID", "") or None)
        report.series_uid = str(getattr(ds, "SeriesInstanceUID", "") or None)
        report.sop_uid = str(getattr(ds, "SOPInstanceUID", "") or None)
        report.diagnostics = diags
        return report

    def _remap_references(self, ds: Dataset, plan: BatchPlan,
                          report: FileReport, diags: list[Diagnostic]) -> None:
        declared = plan.declared_uids
        severity = "error" if self.opts.strict_references else "warning"

        def walk(owner: Dataset, prefix: str) -> None:
            for elem in list(owner):
                kw = _keyword(elem)
                if elem.VR == "SQ":
                    for i, item in enumerate(elem.value or []):
                        walk(item, f"{prefix}{kw}[{i}].")
                    continue
                if elem.VR != "UI" or not elem.value or kw in UID_KEYWORDS_SKIP:
                    continue
                if kw in DECLARED_UID_KEYWORDS and prefix == "":
                    continue  # handled at top level already
                scalar = isinstance(elem.value, str)
                raw_values = [elem.value] if scalar else list(elem.value)
                values = [v for v in raw_values if str(v)]
                changed = False
                new_values: list[str] = []
                for v in values:
                    sv = str(v)
                    if sv not in declared:
                        diags.append(Diagnostic(
                            severity, "missing_reference",
                            f"引用的 UID 在本批次内未找到对应实例"
                            f"（仍确定性重写以防身份泄露）: {sv}",
                            tag=kw, element=f"{prefix}{kw}"))
                    nv = self.uid_mapper.map_uid(sv)
                    new_values.append(nv)
                    changed |= nv != sv
                if changed:
                    elem.value = new_values[0] if scalar else new_values
                    report.changes.append(ChangeRecord(
                        f"{prefix}{kw}", "uid",
                        _value_str(values), _value_str(new_values)))

        walk(ds, "")

    @staticmethod
    def _remove_private(ds: Dataset) -> int:
        count = 0
        stack = [ds]
        while stack:
            d = stack.pop()
            count += sum(1 for e in d if e.tag.is_private)
            d.remove_private_tags()
            for elem in d:
                if elem.VR == "SQ":
                    stack.extend(elem.value or [])
        return count

    @staticmethod
    def _remove_tag_range(ds: Dataset, predicate) -> int:
        count = 0
        for owner, elem in list(_iter_elements(ds)):
            try:
                if predicate(elem.tag):
                    del owner[elem.tag]
                    count += 1
            except Exception:
                pass
        return count

    @staticmethod
    def _shift_da(value: str, offset_days: int) -> str:
        value = (value or "").strip()
        if not value or offset_days == 0:
            return value

        def shift_one(part: str) -> str:
            m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", part)
            if not m:
                return part
            try:
                d = _dt.date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                return part
            return (d + _dt.timedelta(days=offset_days)).strftime("%Y%m%d")

        if "-" in value:
            lo, hi = value.split("-", 1)
            return f"{shift_one(lo)}-{shift_one(hi)}"
        return shift_one(value)


# ---------------------------------------------------------------------------
# ZIP driver
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    output_zip: bytes
    file_reports: dict[str, FileReport]
    non_dicom: list[str]
    patients: list[PatientInfo]
    uid_maps: dict[str, dict[str, str]]
    zip_diagnostics: list[Diagnostic]
    member_order: list[str]


def _unique_output_name(path: str, used: set[str]) -> str:
    """Preserve the in-ZIP relative path; de-duplicate any collision."""
    name = path or "dicom"
    if name in used:
        stem, ext = os.path.splitext(name)
        i = 1
        while f"{stem}_{i}{ext}" in used:
            i += 1
        name = f"{stem}_{i}{ext}"
    used.add(name)
    return name


def _read_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ZipLimitError(f"上传内容不是合法 ZIP: {exc}") from exc
    for info in zf.infolist():
        if info.flag_bits & 0x1:
            raise ZipLimitError(f"成员已加密，暂不支持: {info.filename}")
    return zf


def process_zip(data: bytes, options: AnonymizeOptions) -> ProcessResult:
    """Full single pipeline: metadata plan -> full rewrite -> output ZIP."""
    engine = AnonymizationEngine(options)

    with _read_zip(data) as zf:
        plan = engine.plan_from_zip(zf)

        uid_maps: dict[str, dict[str, str]] = {
            "study": {}, "series": {}, "sop": {}, "frameOfReference": {},
        }
        map_buckets = {
            "StudyInstanceUID": uid_maps["study"],
            "SeriesInstanceUID": uid_maps["series"],
            "SOPInstanceUID": uid_maps["sop"],
            "FrameOfReferenceUID": uid_maps["frameOfReference"],
        }

        reports: dict[str, FileReport] = {}
        outputs: dict[str, bytes] = {}

        for path in plan.non_dicom_paths:
            reports[path] = FileReport(
                path=path, diagnostics=list(plan.diagnostics.get(path, [])))

        for path in plan.dicom_paths:
            diags = list(plan.diagnostics.get(path, []))
            raw = zf.read(plan.member_lookup[path])
            try:
                ds = _dcmread(raw, stop_before_pixels=False)
            except Exception as exc:
                diags.append(Diagnostic(
                    "error", "not_dicom_full_read",
                    f"完整读取失败（元信息可读但像素数据损坏?）: "
                    f"{exc.__class__.__name__}: {exc}"))
                reports[path] = FileReport(path=path, diagnostics=diags)
                continue

            # remember declared UID map entries before rewriting
            for kw, bucket in map_buckets.items():
                val = getattr(ds, kw, None)
                if val and str(val) not in bucket:
                    bucket[str(val)] = engine.uid_mapper.map_uid(str(val))

            report = engine.anonymize_file(ds, path, plan)
            report.diagnostics = diags + report.diagnostics

            buf = io.BytesIO()
            # Preserve the original transfer encoding / preamble style; we only
            # rewrite data elements, so pixel compatibility is unaffected.
            pydicom.dcmwrite(buf, ds, write_like_original=True)
            outputs[path] = buf.getvalue()
            reports[path] = report

        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
            used: set[str] = set()
            for path in plan.dicom_paths:
                if path not in outputs:
                    continue  # failed file: excluded from result, diagnosed
                out_name = _unique_output_name(path, used)
                zout.writestr(out_name, outputs[path])
            if options.include_report:
                zout.writestr("ANONYMIZATION_REPORT.json",
                              _build_report_json(plan, reports, uid_maps, options))

    return ProcessResult(
        output_zip=out_buf.getvalue(),
        file_reports=reports,
        non_dicom=plan.non_dicom_paths,
        patients=list(plan.patients.values()),
        uid_maps=uid_maps,
        zip_diagnostics=plan.zip_diagnostics,
        member_order=plan.member_order,
    )


def _build_report_json(plan: BatchPlan, reports: dict[str, FileReport],
                       uid_maps: dict[str, dict[str, str]],
                       options: AnonymizeOptions) -> bytes:
    payload = {
        "generatedAt": _dt.datetime.utcnow().isoformat() + "Z",
        "options": {
            "shiftDates": options.shift_dates,
            "keepPatientSex": options.keep_patient_sex,
            "removePrivateTags": options.remove_private_tags,
            "removeCurves": options.remove_curves,
            "removeOverlays": options.remove_overlays,
            "strictReferences": options.strict_references,
        },
        "patients": [
            {
                "pseudonymID": p.pseudonym_id,
                "pseudonymName": p.pseudonym_name,
                "dateOffsetDays": p.date_offset_days,
                "fileCount": p.file_count,
            }
            for p in plan.patients.values()
        ],
        "uidMaps": uid_maps,
        "files": {path: r.to_dict(include_changes=True)
                  for path, r in reports.items()},
        "nonDicomFiles": plan.non_dicom_paths,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
