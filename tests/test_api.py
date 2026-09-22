"""FastAPI 端到端测试：上传、预览、诊断、下载、错误处理。"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.sample_data import build_sample_zip


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 必须在导入应用配置前设置工作目录
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("DICOM_ANON_JOB_TTL_SECONDS", "3600")
    # 清理已导入模块以让配置重新生效
    import importlib
    import app.config
    import app.jobs
    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c


@pytest.fixture()
def sample_zip_bytes(tmp_path):
    p = tmp_path / "sample.zip"
    build_sample_zip(str(p))
    return p.read_bytes()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_flow_upload_preview_download(client, sample_zip_bytes):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890", "shift_dates": "true"},
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "ready"
    assert job["file_count"] == 6
    assert job["output_file_count"] == 4
    assert job["error_count"] == 2
    assert len(job["patients"]) == 2
    # 预览中包含逐文件变更
    ct = next(f for f in job["files"] if f["name"] == "patient_1/CT0001.dcm")
    assert ct["status"] == "ok"
    keywords = {c["keyword"] for c in ct["changes"]}
    assert "PatientID" in keywords and "StudyInstanceUID" in keywords

    job_id = job["id"]
    # GET /jobs/{id}
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == job_id

    # /files 子资源
    r3 = client.get(f"/jobs/{job_id}/files")
    assert r3.status_code == 200
    assert len(r3.json()) == 6

    # 下载
    r4 = client.get(f"/jobs/{job_id}/download")
    assert r4.status_code == 200
    assert r4.headers["content-type"] == "application/zip"
    body = io.BytesIO(r4.content)
    with zipfile.ZipFile(body) as zf:
        assert len(zf.namelist()) == 4
        assert all(n.endswith(".dcm") for n in zf.namelist())


def test_dangling_reference_diagnostic_visible(client, sample_zip_bytes):
    job = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890"},
    ).json()
    rt = next(f for f in job["files"] if f["name"].endswith("RS0001.dcm"))
    assert rt["status"] == "warning"
    codes = {d["code"] for d in rt["diagnostics"]}
    assert "DANGLING_REFERENCE" in codes


def test_invalid_dicom_diagnostic_visible(client, sample_zip_bytes):
    job = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890"},
    ).json()
    bad = next(f for f in job["files"] if f["name"] == "broken/bad.dcm")
    assert bad["status"] == "error"
    assert any(d["code"] == "INVALID_DICOM" and d["severity"] == "error"
               for d in bad["diagnostics"])


def test_short_secret_rejected(client, sample_zip_bytes):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "short"},
    )
    assert r.status_code == 422


def test_non_zip_rejected(client):
    r = client.post(
        "/jobs",
        files={"file": ("study.txt", b"hello", "text/plain")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 422


def test_empty_upload_rejected(client):
    r = client.post(
        "/jobs",
        files={"file": ("empty.zip", b"", "application/zip")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 422


def test_bad_zip_reports_failed_job(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"]


def test_job_not_found(client):
    assert client.get("/jobs/nope/download").status_code == 404
    assert client.get("/jobs/nope").status_code == 404


def test_same_secret_cross_batch_same_pseudonym(client, sample_zip_bytes):
    def upload():
        return client.post(
            "/jobs",
            files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
            data={"secret": "cross-batch-secret-123"},
        ).json()

    j1 = upload()
    j2 = upload()
    p1 = {p["key"]: p["new_patient_id"] for p in j1["patients"]}
    p2 = {p["key"]: p["new_patient_id"] for p in j2["patients"]}
    assert p1 == p2
