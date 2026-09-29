#!/usr/bin/env python3
"""Safely operate one Source2 encrypted medical archive.

This is deliberately a per-idempotency-key manual tool. It never lists
archives and never schedules retention cleanup. Administrator secrets are read
only from an environment variable.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_PREFIX = "/api/integrations/source2/v1/archive/"
MAX_RESPONSE_BYTES = 1024 * 1024
MIN_SECRET_BYTES = 32
SAFE_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
SAFE_HASH = re.compile(r"[0-9a-f]{64}\Z")
SAFE_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


class ArchiveAdminError(RuntimeError):
    """Stable error that never includes credentials or response bodies."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_base_url(value: str, allow_insecure_localhost: bool = False) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (not raw or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or not parsed.hostname):
        raise ArchiveAdminError("CowAgent 地址必须是不含路径、凭证和查询参数的根地址")
    hostname = parsed.hostname.lower()
    if parsed.scheme == "https":
        pass
    elif (parsed.scheme == "http" and allow_insecure_localhost
          and hostname in {"localhost", "127.0.0.1", "::1"}):
        pass
    else:
        raise ArchiveAdminError("CowAgent 地址必须使用 HTTPS")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ArchiveAdminError("CowAgent 地址端口无效") from exc
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def build_signature(method: str, path: str, tenant_id: str, timestamp: str,
                    nonce: str, idempotency_key: str, secret: str) -> str:
    body_digest = hashlib.sha256(b"").hexdigest()
    canonical = "\n".join((method, path, tenant_id, timestamp, nonce,
                            idempotency_key, body_digest))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def validate_json_response_content_type(value: Any) -> None:
    """Accept only the integration JSON media type and UTF-8 charset."""
    parts = str(value or "").split(";")
    if parts[0].strip().lower() != "application/json":
        raise ArchiveAdminError("CowAgent 归档操作响应媒体类型无效")
    charsets = []
    for raw_parameter in parts[1:]:
        name, separator, parameter_value = raw_parameter.partition("=")
        if name.strip().lower() != "charset":
            continue
        normalized = parameter_value.strip()
        if (len(normalized) >= 2 and normalized[0] == normalized[-1]
                and normalized[0] in {'"', "'"}):
            normalized = normalized[1:-1].strip()
        if not separator or normalized.lower() != "utf-8":
            raise ArchiveAdminError("CowAgent 归档操作响应媒体类型无效")
        charsets.append(normalized.lower())
    if len(charsets) > 1:
        raise ArchiveAdminError("CowAgent 归档操作响应媒体类型无效")


def validate_identity_response_content_encoding(value: Any) -> None:
    """Require archive responses to be unencoded before enforcing the wire limit."""
    normalized = str(value or "").strip().lower()
    if normalized not in {"", "identity"}:
        raise ArchiveAdminError("CowAgent 归档操作响应 Content-Encoding 无效")


def _strict_json_object(raw: bytes) -> Dict[str, Any]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ArchiveAdminError("CowAgent 响应包含重复字段")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               ArchiveAdminError("CowAgent 响应包含非法数值")))
    except ArchiveAdminError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveAdminError("CowAgent 响应不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ArchiveAdminError("CowAgent 响应不是 JSON 对象")
    return value


def _validate_result(action: str, response: Dict[str, Any],
                     idempotency_key: str) -> Dict[str, Any]:
    allowed_fields = ({"status", "idempotencyKeyHash"} if action == "delete"
                      else {"status", "idempotencyKeyHash", "keyId", "archives"})
    if not set(response).issubset(allowed_fields):
        raise ArchiveAdminError("CowAgent 归档操作响应包含未允许字段")
    expected_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    if response.get("idempotencyKeyHash") != expected_hash:
        raise ArchiveAdminError("CowAgent 归档操作确认摘要不匹配")
    if action == "delete":
        if response.get("status") not in {"DELETED", "NOT_FOUND"}:
            raise ArchiveAdminError("CowAgent 删除确认状态无效")
        return {"status": response["status"], "idempotencyKeyHash": expected_hash}
    if response.get("status") not in {"REENCRYPTED", "NOT_FOUND"}:
        raise ArchiveAdminError("CowAgent 重加密确认状态无效")
    key_id = response.get("keyId")
    archives = response.get("archives")
    if (not isinstance(key_id, str) or not SAFE_KEY_ID.fullmatch(key_id)
            or not isinstance(archives, list) or len(archives) > 33
            or any(not isinstance(item, str)
                   or (item != "request" and not re.fullmatch(r"media:(?:[0-9]|[12][0-9]|3[01])", item))
                   for item in archives)
            or len(set(archives)) != len(archives)):
        raise ArchiveAdminError("CowAgent 重加密确认字段无效")
    return {"status": response["status"], "idempotencyKeyHash": expected_hash,
            "keyId": key_id, "archives": archives}


def operate_archive(base_url: str, tenant_id: str, app_key: str, secret: str,
                    idempotency_key: str, action: str,
                    confirm_delete_sha256: str = "", timeout_seconds: float = 10.0,
                    allow_insecure_localhost: bool = False) -> Dict[str, Any]:
    tenant_id = str(tenant_id or "")
    app_key = str(app_key or "")
    if not tenant_id or len(tenant_id) > 64:
        raise ArchiveAdminError("租户 ID 长度必须为 1 到 64 个字符")
    if not app_key or len(app_key) > 128:
        raise ArchiveAdminError("管理员 App Key 长度必须为 1 到 128 个字符")
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise ArchiveAdminError("管理员 App Secret 至少需要 32 字节")
    if not SAFE_IDEMPOTENCY_KEY.fullmatch(str(idempotency_key or "")):
        raise ArchiveAdminError("消息幂等键格式无效")
    if action not in {"reencrypt", "delete"}:
        raise ArchiveAdminError("不支持的归档操作")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise ArchiveAdminError("超时时间必须大于 0 且不超过 60 秒")
    expected_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    if action == "delete":
        confirmation = str(confirm_delete_sha256 or "").lower()
        if not SAFE_HASH.fullmatch(confirmation) or not hmac.compare_digest(
                confirmation, expected_hash):
            raise ArchiveAdminError("删除确认摘要与目标幂等键不匹配")

    method = "DELETE" if action == "delete" else "PUT"
    path = API_PREFIX + idempotency_key
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-App-Key": app_key,
        "X-Tenant-Id": tenant_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": build_signature(
            method, path, tenant_id, timestamp, nonce, idempotency_key, secret),
        "Idempotency-Key": idempotency_key,
    }
    request = Request(normalize_base_url(base_url, allow_insecure_localhost) + path,
                      method=method, headers=headers, data=None)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
            validate_identity_response_content_encoding(
                response.headers.get("Content-Encoding", ""))
            validate_json_response_content_type(response.headers.get("Content-Type", ""))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ArchiveAdminError("CowAgent 响应超过 1 MiB")
            if response.status < 200 or response.status >= 300:
                raise ArchiveAdminError(f"CowAgent 归档操作返回 HTTP {response.status}")
    except HTTPError as exc:
        raise ArchiveAdminError(f"CowAgent 归档操作返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise ArchiveAdminError("无法连接 CowAgent 归档管理接口") from None
    return _validate_result(action, _strict_json_object(raw), idempotency_key)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按消息幂等键管理 Source2 医疗加密归档")
    parser.add_argument("action", choices=("reencrypt", "delete"))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--app-key", required=True, help="独立管理员 App Key")
    parser.add_argument("--secret-env", required=True,
                        help="保存独立管理员 App Secret 的环境变量名")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--confirm-delete-sha256",
                        help="删除时必填：目标幂等键的 64 位 SHA-256")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    secret = os.environ.get(args.secret_env, "")
    if not secret:
        print(f"archive admin failed: 环境变量 {args.secret_env} 未设置", file=sys.stderr)
        return 2
    try:
        result = operate_archive(
            args.base_url, args.tenant_id, args.app_key, secret,
            args.idempotency_key, args.action,
            confirm_delete_sha256=args.confirm_delete_sha256 or "",
            timeout_seconds=args.timeout,
            allow_insecure_localhost=args.allow_insecure_localhost,
        )
    except ArchiveAdminError as exc:
        print(f"archive admin failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"archiveAdmin": "PASS", "result": result},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
