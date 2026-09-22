"""加密派生的单元测试。"""
from app import crypto


def test_pseudonym_is_stable_and_16_chars():
    a = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    b = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    assert a == b
    pid, pname = a
    assert len(pid) == 16 and pid.isalnum()
    assert pname.startswith("ANON^")


def test_different_patients_differ():
    a = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    b = crypto.patient_pseudonyms("secret-12345678", "id:P-002")
    assert a != b


def test_different_secrets_differ():
    a = crypto.patient_pseudonyms("secret-12345678", "id:P-001")
    b = crypto.patient_pseudonyms("secret-99999999", "id:P-001")
    assert a[0] != b[0]


def test_derive_uid_deterministic_and_valid_format():
    original = "1.2.840.113619.2.55.3.604688119.1"
    u1 = crypto.derive_uid("secret-12345678", original)
    u2 = crypto.derive_uid("secret-12345678", original)
    assert u1 == u2
    assert u1.startswith("2.25.")
    assert all(part.isdigit() for part in u1.split("."))
    # DICOM UI 规则：总长 <=64，每个组件 <=64 位
    assert len(u1) <= 64
    assert all(len(part) <= 64 for part in u1.split("."))
    assert u1 != crypto.derive_uid("other-secret-9999", original)


def test_date_shift_per_patient_stable_in_range():
    d1 = crypto.date_shift_days("secret-12345678", "id:P-001")
    d2 = crypto.date_shift_days("secret-12345678", "id:P-001")
    assert d1 == d2
    assert -365 <= d1 <= 365
