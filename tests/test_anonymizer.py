"""End-to-end test using real pydicom datasets (no pixel data required).

Run inside the container or a venv with requirements installed:

    python -m pytest tests/ -q
    # or, without pytest:
    python tests/test_anonymizer.py
"""
from __future__ import annotations

import io
import zipfile

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    CTImageStorage,
    generate_uid,
)

from app.anonymizer import AnonymizeOptions, process_zip


def _dataset(patient_id: str, study_uid: str, series_uid: str,
             sop_uid: str, refs: list[str] | None = None,
             missing: list[str] | None = None) -> Dataset:
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.FrameOfReferenceUID = generate_uid()
    ds.PatientID = patient_id
    ds.PatientName = f"PATIENT^{patient_id}"
    ds.PatientBirthDate = "19800515"
    ds.PatientSex = "M"
    ds.StudyID = "STUDY1"
    ds.AccessionNumber = "ACC-0001"
    ds.InstitutionName = "Example Hospital"
    ds.ReferringPhysicianName = "DOCTOR^A"
    ds.StudyDescription = "CT ABDOMEN"
    ds.StudyDate = "20240102"
    ds.SeriesDate = "20240102"

    if refs or missing:
        seq = []
        for uid in refs or []:
            item = Dataset()
            item.ReferencedSOPClassUID = CTImageStorage
            item.ReferencedSOPInstanceUID = uid
            seq.append(item)
        for uid in missing or []:
            item = Dataset()
            item.ReferencedSOPClassUID = CTImageStorage
            item.ReferencedSOPInstanceUID = uid
            seq.append(item)
        ds.ReferencedSOPSequence = seq
    return ds


def _zip_bytes() -> bytes:
    study = generate_uid()
    s1 = generate_uid()
    s2 = generate_uid()
    i1 = generate_uid()
    i2 = generate_uid()
    d1 = _dataset("P-001", study, s1, i1)
    d2 = _dataset("P-001", study, s2, i2, refs=[i1],
                  missing=[generate_uid()])
    d3 = _dataset("P-002", generate_uid(), generate_uid(), generate_uid())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, ds in [("study/IM001", d1), ("study/IM002", d2),
                         ("other/IM001", d3)]:
            b = io.BytesIO()
            ds.save_as(b, write_like_original=False)
            z.writestr(name, b.getvalue())
        z.writestr("readme.txt", b"not a dicom file")
        z.writestr("../evil", b"x")
    return buf.getvalue(), i1


def test_pipeline():
    data, original_i1 = _zip_bytes()
    opts = AnonymizeOptions(secret="unit-test-secret-1234")
    result = process_zip(data, opts)

    assert result.non_dicom == ["readme.txt"]
    assert any(d.code == "unsafe_path" for d in result.zip_diagnostics)
    assert len(result.patients) == 2

    out = zipfile.ZipFile(io.BytesIO(result.output_zip))
    names = set(out.namelist())
    assert {"study/IM001", "study/IM002", "other/IM001",
            "ANONYMIZATION_REPORT.json"} <= names
    assert "readme.txt" not in names

    from pydicom import dcmread
    a1 = dcmread(io.BytesIO(out.read("study/IM001")))
    a2 = dcmread(io.BytesIO(out.read("study/IM002")))

    # patient-level pseudonymization
    assert a1.PatientID.startswith("ANON-")
    assert a1.PatientID == a2.PatientID
    assert str(a1.PatientName).startswith("ANON^")
    assert a1.AccessionNumber == ""
    assert a1.InstitutionName == ""
    assert "ReferringPhysicianName" not in a1
    assert a1.PatientIdentityRemoved == "YES"

    # study relationship preserved
    assert a1.StudyInstanceUID == a2.StudyInstanceUID
    assert a1.SeriesInstanceUID != a2.SeriesInstanceUID

    # recursive reference rewritten to the new SOP UID
    ref = a2.ReferencedSOPSequence[0].ReferencedSOPInstanceUID
    assert ref == a1.SOPInstanceUID == a1.file_meta.MediaStorageSOPInstanceUID
    assert ref != original_i1

    # SOP class / transfer syntax stay untouched
    assert a2.ReferencedSOPSequence[0].ReferencedSOPClassUID == CTImageStorage
    assert a1.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian

    # missing reference surfaced per-file
    r2 = result.file_reports["study/IM002"]
    codes = {(d.code, d.severity) for d in r2.diagnostics}
    assert ("missing_reference", "warning") in codes

    # determinism: same secret -> identical UIDs
    again = process_zip(data, opts)
    out2 = zipfile.ZipFile(io.BytesIO(again.output_zip))
    b1 = dcmread(io.BytesIO(out2.read("study/IM001")))
    assert b1.SOPInstanceUID == a1.SOPInstanceUID

    # different secret -> different pseudonyms / UIDs
    other = process_zip(data, AnonymizeOptions(secret="different-secret-9876"))
    out3 = zipfile.ZipFile(io.BytesIO(other.output_zip))
    c1 = dcmread(io.BytesIO(out3.read("study/IM001")))
    assert c1.PatientID != a1.PatientID
    assert c1.SOPInstanceUID != a1.SOPInstanceUID

    # strict mode promotes missing references to errors
    strict = process_zip(
        data, AnonymizeOptions(secret="unit-test-secret-1234",
                               strict_references=True))
    s2_report = strict.file_reports["study/IM002"]
    assert any(d.code == "missing_reference" and d.severity == "error"
               for d in s2_report.diagnostics)
    print("all assertions passed")


if __name__ == "__main__":
    test_pipeline()
