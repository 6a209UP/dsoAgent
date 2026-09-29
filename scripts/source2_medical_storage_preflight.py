#!/usr/bin/env python3
"""Read-only filesystem isolation preflight for Source2 medical workspaces."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


SENSITIVE_ROOTS = (Path("medical/source2"), Path("knowledge/source2"))
WINDOWS_SYSTEM_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
SID_PATTERN = re.compile(r"S-\d+(?:-\d+)+\Z", re.IGNORECASE)
WINDOWS_FILE_REQUIRED_RIGHTS = 0x01 | 0x02  # ReadData + WriteData
WINDOWS_DIRECTORY_REQUIRED_RIGHTS = (
    WINDOWS_FILE_REQUIRED_RIGHTS | 0x20)  # List/Create + Traverse


class StoragePreflightError(RuntimeError):
    """Stable, non-sensitive deployment validation failure."""


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def validate_workspace_roots(values: Sequence[str]) -> List[Path]:
    if not values:
        raise StoragePreflightError("至少需要提供一个 CowAgent Agent 工作区")
    roots: List[Path] = []
    for value in values:
        path = Path(str(value or "").strip())
        if not path.is_absolute():
            raise StoragePreflightError("Agent 工作区必须使用绝对路径")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise StoragePreflightError("Agent 工作区不存在或无法安全解析") from exc
        if not resolved.is_dir():
            raise StoragePreflightError("Agent 工作区必须是目录")
        if any(_overlap(resolved, existing) for existing in roots):
            raise StoragePreflightError("Agent 工作区不得相同或互相嵌套")
        roots.append(resolved)
    return roots


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)
    except OSError as exc:
        raise StoragePreflightError("医疗存储路径无法读取") from exc


def collect_sensitive_entries(workspace: Path) -> List[Path]:
    entries: List[Path] = []
    for relative_root in SENSITIVE_ROOTS:
        root = workspace / relative_root
        if not root.is_dir() or _is_link_or_reparse(root):
            raise StoragePreflightError("医疗存储目录未初始化或使用了链接/重解析点")
        try:
            root.resolve(strict=True).relative_to(workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StoragePreflightError("医疗存储目录逃逸出 Agent 工作区") from exc
        for current_text, directory_names, file_names in os.walk(
                str(root), topdown=True, followlinks=False):
            current = Path(current_text)
            entries.append(current)
            for name in list(directory_names):
                child = current / name
                if _is_link_or_reparse(child):
                    raise StoragePreflightError("医疗存储目录包含链接或重解析点")
            for name in file_names:
                child = current / name
                if _is_link_or_reparse(child):
                    raise StoragePreflightError("医疗存储文件使用了链接或重解析点")
                entries.append(child)
    return entries


def validate_posix_record(is_directory: bool, mode: int, owner_uid: int,
                          effective_uid: int) -> None:
    expected_mode = 0o700 if is_directory else 0o600
    if owner_uid != effective_uid:
        raise StoragePreflightError("医疗存储对象不属于 CowAgent 运行账号")
    if stat.S_IMODE(mode) != expected_mode:
        raise StoragePreflightError(
            "医疗存储目录必须为 0700，文件必须为 0600")


def validate_posix_entries(entries: Iterable[Path]) -> None:
    effective_uid = os.geteuid()
    for path in entries:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise StoragePreflightError("医疗存储对象无法读取") from exc
        validate_posix_record(stat.S_ISDIR(metadata.st_mode), metadata.st_mode,
                              metadata.st_uid, effective_uid)


def validate_windows_acl_rows(rows: Sequence[Mapping[str, Any]],
                              service_sid: str) -> None:
    normalized_service_sid = str(service_sid or "").strip().upper()
    if not SID_PATTERN.fullmatch(normalized_service_sid):
        raise StoragePreflightError("Windows 部署必须提供合法的 CowAgent 服务账号 SID")
    allowed_sids = set(WINDOWS_SYSTEM_SIDS)
    allowed_sids.add(normalized_service_sid)
    for row in rows:
        is_directory = row.get("isDirectory")
        if not isinstance(is_directory, bool):
            raise StoragePreflightError("医疗存储 ACL 响应格式无效")
        owner_sid = str(row.get("ownerSid") or "").upper()
        if owner_sid not in allowed_sids:
            raise StoragePreflightError("医疗存储对象所有者不是受信任账号")
        service_rights = 0
        rules = row.get("rules")
        if not isinstance(rules, list):
            raise StoragePreflightError("医疗存储 ACL 响应格式无效")
        for rule in rules:
            if not isinstance(rule, Mapping):
                raise StoragePreflightError("医疗存储 ACL 响应格式无效")
            if str(rule.get("type") or "").lower() != "allow":
                continue
            sid = str(rule.get("sid") or "").upper()
            if sid not in allowed_sids:
                raise StoragePreflightError("医疗存储 ACL 向非受信任账号授予了访问权限")
            rights = rule.get("rights")
            if isinstance(rights, bool) or not isinstance(rights, int):
                raise StoragePreflightError("医疗存储 ACL 响应格式无效")
            if sid == normalized_service_sid:
                service_rights |= rights
        required_rights = (WINDOWS_DIRECTORY_REQUIRED_RIGHTS if is_directory
                           else WINDOWS_FILE_REQUIRED_RIGHTS)
        if service_rights & required_rights != required_rights:
            raise StoragePreflightError("CowAgent 服务账号缺少医疗存储读写权限")


def _powershell_acl_script() -> str:
    return r"""
$ErrorActionPreference = 'Stop'
$paths = @(([Console]::In.ReadToEnd() | ConvertFrom-Json))
$rows = foreach ($path in $paths) {
    $acl = Get-Acl -LiteralPath ([string]$path)
    $owner = ([System.Security.Principal.NTAccount]$acl.Owner).Translate(
        [System.Security.Principal.SecurityIdentifier]).Value
    $rules = @($acl.Access | ForEach-Object {
        [pscustomobject]@{
            sid = $_.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]).Value
            type = $_.AccessControlType.ToString()
            rights = [int64]$_.FileSystemRights
        }
    })
    $item = Get-Item -Force -LiteralPath ([string]$path)
    [pscustomobject]@{
        isDirectory = [bool]$item.PSIsContainer
        ownerSid = $owner
        rules = $rules
    }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 5
"""


def read_windows_acl_rows(entries: Sequence[Path]) -> List[Dict[str, Any]]:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        raise StoragePreflightError("Windows ACL 验收需要 PowerShell")
    encoded = base64.b64encode(
        _powershell_acl_script().encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=json.dumps([str(path) for path in entries]),
            text=True, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoragePreflightError("Windows ACL 检查无法执行") from exc
    if result.returncode != 0:
        raise StoragePreflightError("Windows ACL 检查失败")
    try:
        rows = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StoragePreflightError("Windows ACL 响应格式无效") from exc
    if not isinstance(rows, list) or len(rows) != len(entries):
        raise StoragePreflightError("Windows ACL 响应数量不匹配")
    return rows


def run_preflight(workspace_values: Sequence[str],
                  service_sid: Optional[str] = None) -> Dict[str, int]:
    workspaces = validate_workspace_roots(workspace_values)
    entry_count = 0
    for workspace in workspaces:
        entries = collect_sensitive_entries(workspace)
        if os.name == "nt":
            validate_windows_acl_rows(read_windows_acl_rows(entries), service_sid or "")
        else:
            validate_posix_entries(entries)
        entry_count += len(entries)
    return {"workspaceCount": len(workspaces), "checkedEntryCount": entry_count}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读验证 Source2 医疗 Agent 工作区隔离和宿主机权限")
    parser.add_argument("--workspace", action="append", required=True,
                        help="Agent 工作区绝对路径；每个租户重复传入一次")
    parser.add_argument("--service-sid",
                        help="Windows 上运行 CowAgent 的专用服务账号 SID")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = run_preflight(args.workspace, args.service_sid)
    except StoragePreflightError as exc:
        print(f"storage preflight failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"storagePreflight": "PASS", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
