import os
from pathlib import Path

import pytest

from scripts import source2_medical_storage_preflight as preflight


def _initialize_workspace(workspace: Path) -> None:
    (workspace / "medical" / "source2").mkdir(parents=True)
    (workspace / "knowledge" / "source2").mkdir(parents=True)


def test_workspace_roots_must_be_absolute_existing_and_non_overlapping(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert preflight.validate_workspace_roots([str(first), str(second)]) == [
        first.resolve(), second.resolve()]
    with pytest.raises(preflight.StoragePreflightError, match="绝对路径"):
        preflight.validate_workspace_roots(["relative-workspace"])
    with pytest.raises(preflight.StoragePreflightError, match="不存在"):
        preflight.validate_workspace_roots([str(tmp_path / "missing")])


def test_workspace_roots_reject_parent_child_overlap(tmp_path):
    parent = tmp_path / "tenant"
    child = parent / "nested"
    child.mkdir(parents=True)

    with pytest.raises(preflight.StoragePreflightError, match="互相嵌套"):
        preflight.validate_workspace_roots([str(parent), str(child)])


def test_sensitive_storage_must_be_initialized(tmp_path):
    with pytest.raises(preflight.StoragePreflightError, match="未初始化"):
        preflight.collect_sensitive_entries(tmp_path)


def test_sensitive_storage_collection_rejects_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _initialize_workspace(workspace)
    (workspace / "medical" / "source2" / "escaped").symlink_to(
        outside, target_is_directory=True)

    with pytest.raises(preflight.StoragePreflightError, match="链接"):
        preflight.collect_sensitive_entries(workspace)


def test_posix_permission_policy_requires_exact_mode_and_owner():
    preflight.validate_posix_record(True, 0o40700, 1000, 1000)
    preflight.validate_posix_record(False, 0o100600, 1000, 1000)

    with pytest.raises(preflight.StoragePreflightError, match="0700"):
        preflight.validate_posix_record(True, 0o40750, 1000, 1000)
    with pytest.raises(preflight.StoragePreflightError, match="运行账号"):
        preflight.validate_posix_record(False, 0o100600, 1001, 1000)


def test_windows_acl_policy_accepts_only_service_system_and_administrators():
    service_sid = "S-1-5-21-1-2-3-1001"
    rows = [{
        "isDirectory": True,
        "ownerSid": service_sid,
        "rules": [
            {"sid": service_sid, "type": "Allow", "rights": 0x23},
            {"sid": "S-1-5-18", "type": "Allow", "rights": 0x23},
            {"sid": "S-1-1-0", "type": "Deny", "rights": 0x23},
        ],
    }, {
        "isDirectory": False,
        "ownerSid": "S-1-5-32-544",
        "rules": [
            {"sid": service_sid, "type": "Allow", "rights": 0x03},
            {"sid": "S-1-5-18", "type": "Allow", "rights": 0x03},
        ],
    }]

    preflight.validate_windows_acl_rows(rows, service_sid)


@pytest.mark.parametrize("rows,match", [
    ([{"isDirectory": True, "ownerSid": "S-1-5-21-1-2-3-1001", "rules": [
        {"sid": "S-1-5-11", "type": "Allow", "rights": 0x23},
    ]}], "非受信任账号"),
    ([{"isDirectory": True, "ownerSid": "S-1-5-21-1-2-3-1001", "rules": [
        {"sid": "S-1-5-21-1-2-3-1001", "type": "Allow", "rights": 0x01},
    ]}], "缺少医疗存储读写权限"),
    ([{"isDirectory": False, "ownerSid": "S-1-5-11", "rules": []}], "所有者"),
])
def test_windows_acl_policy_fails_closed(rows, match):
    with pytest.raises(preflight.StoragePreflightError, match=match):
        preflight.validate_windows_acl_rows(rows, "S-1-5-21-1-2-3-1001")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL APIs")
def test_windows_acl_reader_preserves_single_entry_as_array(tmp_path):
    target = tmp_path / "acl-test"
    target.mkdir()

    rows = preflight.read_windows_acl_rows([target])

    assert len(rows) == 1
    assert rows[0]["isDirectory"] is True
    assert isinstance(rows[0]["ownerSid"], str)
    assert isinstance(rows[0]["rules"], list)
