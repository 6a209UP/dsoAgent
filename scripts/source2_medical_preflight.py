#!/usr/bin/env python3
"""Run a safe, read-only Source2 medical integration deployment preflight.

The probe calls only the signed health endpoint. Secrets are read from an
environment variable so they do not appear in shell history or process lists.
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
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_PATH = "/api/integrations/source2/v1/health"
POLICY_VERSION = "medical-service-v1"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CLOCK_SKEW_SECONDS = 300
MIN_SECRET_BYTES = 32
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MAX_MEDIA_HOSTS = 64
MEDIA_HOST_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z")
HEALTH_RESPONSE_FIELDS = frozenset({
    "status", "tenantId", "agentId", "modelProfile", "corpusVersion",
    "encryptionKeyId", "mediaHostAllowlist", "policyVersion",
    "complianceApprovalRef", "externalSecrets", "timestamp",
})


class PreflightError(RuntimeError):
    """Stable, non-sensitive deployment validation failure."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_base_url(value: str, allow_insecure_localhost: bool = False) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (not raw or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise PreflightError("CowAgent 地址必须是不含路径、用户信息、查询串和片段的根地址")
    if not parsed.hostname:
        raise PreflightError("CowAgent 地址缺少主机名")
    if parsed.scheme == "https":
        pass
    elif (parsed.scheme == "http" and allow_insecure_localhost
          and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}):
        pass
    else:
        raise PreflightError("CowAgent 地址必须使用 HTTPS；本地联调需显式允许 localhost HTTP")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PreflightError("CowAgent 地址端口无效") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def normalize_media_host(value: str) -> str:
    if not isinstance(value, str):
        raise PreflightError("媒体主机名格式无效")
    host = value.strip().rstrip(".").lower()
    if (not host or len(host) > 253 or not MEDIA_HOST_PATTERN.fullmatch(host)):
        raise PreflightError("媒体主机名格式无效")
    try:
        parsed = urlsplit("https://" + host)
        port = parsed.port
    except ValueError as exc:
        raise PreflightError("媒体主机名格式无效") from exc
    if (parsed.hostname or "").rstrip(".").lower() != host or port is not None:
        raise PreflightError("媒体主机名格式无效")
    return host


def build_signature(method: str, path: str, tenant_id: str, timestamp: str,
                    nonce: str, idempotency_key: str, body: bytes,
                    secret: str) -> str:
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join((method.upper(), path, str(tenant_id), str(timestamp),
                            nonce, idempotency_key, digest))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def build_headers(tenant_id: str, app_key: str, secret: str,
                  timestamp: Optional[int] = None,
                  nonce: Optional[str] = None) -> Dict[str, str]:
    timestamp_text = str(int(time.time()) if timestamp is None else int(timestamp))
    nonce_text = nonce or secrets.token_hex(16)
    idempotency_key = f"health-{nonce_text}"
    signature = build_signature("GET", API_PATH, tenant_id, timestamp_text,
                                nonce_text, idempotency_key, b"", secret)
    return {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-App-Key": app_key,
        "X-Tenant-Id": str(tenant_id),
        "X-Timestamp": timestamp_text,
        "X-Nonce": nonce_text,
        "X-Signature": signature,
        "Idempotency-Key": idempotency_key,
    }


def validate_json_response_content_type(value: Any) -> None:
    """Accept only the integration JSON media type and UTF-8 charset."""
    parts = str(value or "").split(";")
    if parts[0].strip().lower() != "application/json":
        raise PreflightError("CowAgent 健康检查响应媒体类型无效")
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
            raise PreflightError("CowAgent 健康检查响应媒体类型无效")
        charsets.append(normalized.lower())
    if len(charsets) > 1:
        raise PreflightError("CowAgent 健康检查响应媒体类型无效")


def validate_identity_response_content_encoding(value: Any) -> None:
    """Require health responses to be unencoded so the wire limit is unambiguous."""
    normalized = str(value or "").strip().lower()
    if normalized not in {"", "identity"}:
        raise PreflightError("CowAgent 健康检查响应 Content-Encoding 无效")


def _strict_json_object(raw: bytes) -> Dict[str, Any]:
    """Parse one UTF-8 JSON object without duplicate keys or non-finite numbers."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PreflightError("CowAgent 健康检查响应不是合法 JSON")
            result[key] = value
        return result

    def reject_constant(_value):
        raise PreflightError("CowAgent 健康检查响应不是合法 JSON")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                           parse_constant=reject_constant)
    except PreflightError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("CowAgent 健康检查响应不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise PreflightError("CowAgent 健康检查响应不是 JSON 对象")
    return value


def validate_health(response: Dict[str, Any], tenant_id: str, agent_id: str,
                    expected_model_profile: Optional[str] = None,
                    expected_corpus_version: Optional[str] = None,
                    expected_compliance_ref: Optional[str] = None,
                    expected_media_host: Optional[str] = None,
                    now: Optional[int] = None,
                    expected_encryption_key_id: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(response, dict):
        raise PreflightError("CowAgent 健康检查响应不是 JSON 对象")
    if not set(response).issubset(HEALTH_RESPONSE_FIELDS):
        raise PreflightError("CowAgent 健康检查响应包含未允许字段")
    expected = {
        "status": "UP",
        "tenantId": str(tenant_id),
        "agentId": agent_id,
        "policyVersion": POLICY_VERSION,
    }
    for field, value in expected.items():
        if not isinstance(response.get(field), str) or response.get(field) != value:
            raise PreflightError(f"CowAgent 健康检查字段 {field} 不匹配")
    optional_expected = {
        "modelProfile": expected_model_profile,
        "corpusVersion": expected_corpus_version,
        "complianceApprovalRef": expected_compliance_ref,
    }
    for field, value in optional_expected.items():
        if (value is not None
                and (not isinstance(response.get(field), str)
                     or response.get(field) != value)):
            raise PreflightError(f"CowAgent 健康检查字段 {field} 不匹配")
    if (not isinstance(response.get("encryptionKeyId"), str)
            or not response["encryptionKeyId"].strip()):
        raise PreflightError("CowAgent 未报告活动医疗归档密钥")
    if (expected_encryption_key_id is not None
            and response["encryptionKeyId"] != expected_encryption_key_id):
        raise PreflightError("CowAgent 活动医疗归档密钥版本不匹配")
    if response.get("externalSecrets") is not True:
        raise PreflightError("CowAgent 未强制使用外部注入的集成凭证和医疗数据密钥")
    media_hosts = response.get("mediaHostAllowlist")
    if (not isinstance(media_hosts, list) or not media_hosts
            or len(media_hosts) > MAX_MEDIA_HOSTS):
        raise PreflightError("CowAgent 未报告有效的医疗媒体主机白名单")
    normalized_hosts = []
    for media_host in media_hosts:
        normalized_host = normalize_media_host(media_host)
        if media_host != normalized_host or normalized_host in normalized_hosts:
            raise PreflightError("CowAgent 医疗媒体主机白名单格式无效")
        normalized_hosts.append(normalized_host)
    if (expected_media_host is not None
            and normalize_media_host(expected_media_host) not in normalized_hosts):
        raise PreflightError("source2 医疗媒体代理主机不在 CowAgent 白名单中")
    timestamp = response.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise PreflightError("CowAgent 健康检查时间戳无效")
    response_time = timestamp
    current_time = int(time.time()) if now is None else int(now)
    if abs(current_time - response_time) > MAX_CLOCK_SKEW_SECONDS:
        raise PreflightError("CowAgent 健康检查时间戳超出允许偏差")
    return {field: response.get(field) for field in HEALTH_RESPONSE_FIELDS
            if field in response}


def probe_health(base_url: str, tenant_id: str, agent_id: str, app_key: str,
                 secret: str, timeout_seconds: float = 10.0,
                 allow_insecure_localhost: bool = False,
                 expected_model_profile: Optional[str] = None,
                 expected_corpus_version: Optional[str] = None,
                 expected_compliance_ref: Optional[str] = None,
                 expected_media_host: Optional[str] = None,
                 expected_encryption_key_id: Optional[str] = None) -> Dict[str, Any]:
    tenant_id = str(tenant_id or "")
    app_key = str(app_key or "")
    if not tenant_id or len(tenant_id) > 64:
        raise PreflightError("租户 ID 长度必须为 1 到 64 个字符")
    if not app_key or len(app_key) > 128:
        raise PreflightError("App Key 长度必须为 1 到 128 个字符")
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise PreflightError("Source2 App Secret 至少需要 32 字节")
    if not SAFE_ID.fullmatch(str(agent_id or "")):
        raise PreflightError("Agent ID 格式无效")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise PreflightError("超时时间必须大于 0 且不超过 60 秒")
    normalized_url = normalize_base_url(base_url, allow_insecure_localhost)
    request = Request(normalized_url + API_PATH, method="GET",
                      headers=build_headers(tenant_id, app_key, secret))
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            validate_identity_response_content_encoding(
                response.headers.get("Content-Encoding", ""))
            validate_json_response_content_type(response.headers.get("Content-Type", ""))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise PreflightError("CowAgent 健康检查响应超过 1 MiB")
            if response.status < 200 or response.status >= 300:
                raise PreflightError(f"CowAgent 健康检查返回 HTTP {response.status}")
    except HTTPError as exc:
        raise PreflightError(f"CowAgent 健康检查返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise PreflightError("无法连接 CowAgent 健康检查接口") from exc
    payload = _strict_json_object(raw)
    return validate_health(payload, tenant_id, agent_id, expected_model_profile,
                           expected_corpus_version, expected_compliance_ref,
                           expected_media_host,
                           expected_encryption_key_id=expected_encryption_key_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="验证 Source2 到 CowAgent 医疗客服连接（只调用只读健康接口）")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--app-key", required=True)
    parser.add_argument("--secret-env", required=True,
                        help="保存 App Secret 的环境变量名；不接受命令行明文密钥")
    parser.add_argument("--expected-model-profile", required=True,
                        help="部署单中批准使用的商业模型配置标识")
    parser.add_argument("--expected-corpus-version", required=True,
                        help="灰度租户当前发布的不可变语料版本")
    parser.add_argument("--expected-compliance-ref", required=True,
                        help="已批准的外部合规审批记录引用")
    parser.add_argument("--expected-encryption-key-id", required=True,
                        help="部署单中指定的活动医疗归档密钥版本标识")
    parser.add_argument("--expected-media-host", required=True,
                        help="source2 一次性医疗媒体代理的主机名（不含协议、端口和路径）")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    secret = os.environ.get(args.secret_env, "")
    if not secret:
        print(f"preflight failed: 环境变量 {args.secret_env} 未设置", file=sys.stderr)
        return 2
    try:
        result = probe_health(
            args.base_url, args.tenant_id, args.agent_id, args.app_key, secret,
            timeout_seconds=args.timeout,
            allow_insecure_localhost=args.allow_insecure_localhost,
            expected_model_profile=args.expected_model_profile,
            expected_corpus_version=args.expected_corpus_version,
            expected_compliance_ref=args.expected_compliance_ref,
            expected_media_host=args.expected_media_host,
            expected_encryption_key_id=args.expected_encryption_key_id,
        )
    except PreflightError as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"preflight": "PASS", "health": result},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
