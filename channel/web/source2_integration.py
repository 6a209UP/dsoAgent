"""Private Source2 medical-chat integration endpoints.

The web handlers live in :mod:`channel.web.web_channel`; this module keeps the
protocol, tenant isolation, knowledge snapshots and encrypted audit archive
independent from web.py so they can be tested without starting the console.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from config import conf


logger = logging.getLogger(__name__)

API_PREFIX = "/api/integrations/source2/v1"
TIMESTAMP_SKEW_SECONDS = 300
NONCE_TTL_SECONDS = 600
POLICY_VERSION = "medical-service-v1"
MAX_MEDIA_ARCHIVES_PER_REQUEST = 32
MAX_MEDIA_HOST_ALLOWLIST_ITEMS = 64
MAX_MEDIA_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_MEDIA_ARCHIVE_BYTES_PER_REQUEST = 64 * 1024 * 1024
MAX_CHAT_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_KNOWLEDGE_PAYLOAD_BYTES = 50 * 1024 * 1024
MAX_KNOWLEDGE_ITEMS = 5000
KNOWLEDGE_CHUNK_CHARS = 1600
KNOWLEDGE_CHUNK_OVERLAP_CHARS = 200
MIN_CORPUS_RELEVANCE = 0.25
MAX_CONTEXT_TEXT_CHARS = 20000
MAX_CONTEXT_APPOINTMENTS = 10
MAX_CONTEXT_RECENT_MESSAGES = 20
MAX_KNOWLEDGE_TEXT_CHARS = 200000
MIN_HMAC_SECRET_BYTES = 32
_MEDIA_HOST_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z")
_KNOWLEDGE_REQUEST_FIELDS = frozenset({
    "tenantId", "version", "checksum", "publishedAt", "items",
})
_KNOWLEDGE_ITEM_FIELDS = frozenset({
    "id", "type", "title", "question", "answer", "content", "sourceHash",
})
_CHAT_REQUEST_FIELDS = frozenset({
    "requestId", "tenantId", "agentId", "modelProfile", "conversationId",
    "corpusVersion", "message", "context",
})
_MODEL_RESULT_FIELDS = frozenset({
    "decision", "replyText", "confidence", "reasonCode", "citationIds",
})
_MESSAGE_FIELDS = frozenset({
    "id", "type", "content", "transcript", "mediaUrl", "duration",
    "auditStatus", "disclaimer",
})
_CONTEXT_FIELDS = frozenset({
    "schemaVersion", "patient", "familyMember", "doctor", "appointments",
    "authorizedRecord", "emergency", "recentMessages",
})
_PATIENT_FIELDS = frozenset({
    "id", "name", "gender", "birthday", "allergies", "medicalHistory", "bloodType",
})
_FAMILY_MEMBER_FIELDS = frozenset({
    "id", "name", "relation", "gender", "birthday", "allergies", "medicalHistory",
})
_DOCTOR_FIELDS = frozenset({
    "id", "clinicId", "departmentId", "name", "title", "specialty", "experienceYears",
})
_APPOINTMENT_FIELDS = frozenset({
    "id", "clinicId", "departmentId", "serviceItemId", "appointmentDate", "timeSlot",
    "status", "appointmentType", "symptoms", "diagnosis", "treatment", "cancelReason",
    "cancelTime",
})
_RECORD_FIELDS = frozenset({
    "id", "appointmentId", "diagnosis", "symptoms", "treatment", "prescription",
    "advice", "followUp", "visitDate", "status", "attachments",
})
_ATTACHMENT_FIELDS = frozenset({"id", "type", "name", "size", "mediaUrl"})
_EMERGENCY_FIELDS = frozenset({
    "id", "doctorId", "type", "title", "description", "status", "reply", "replyTime",
})
_RECENT_MESSAGE_FIELDS = frozenset({
    "id", "senderType", "type", "content", "transcript", "mediaUrl", "duration",
    "serviceType", "auditStatus", "disclaimer", "createTime",
})
_NONCES: Dict[Tuple[str, str], float] = {}
_NONCE_LOCK = threading.Lock()
_ALLOW_INLINE_SECRETS_FOR_TESTS = False

_MEDICAL_RISK_RE = re.compile(
    r"(诊断|确诊|什么病|治疗方案|怎么治|处方|用药|吃什么药|消炎药|止痛药|"
    r"抗生素|剂量|停药|换药|过敏|不良反应|牙疼|牙痛|疼痛|肿痛|肿胀|牙龈肿|"
    r"牙龈出血|流血|发热|发烧|麻木|牙齿松动|口腔溃疡|流脓|咬合痛|症状|"
    r"胸痛|呼吸困难|昏迷|大量出血|休克|急救|急诊|自杀|拔牙后|术后异常|"
    r"伤口感染|发烧不退|diagnos(?:e|is|tic)?|symptom|pain|ache|swelling|"
    r"bleeding|fever|medicine|medication|dosage|antibiotic|emergency|"
    r"treatment|surgery|allergic)",
    re.IGNORECASE,
)
_PROMPT_INJECTION_RE = re.compile(
    r"(忽略(?:以上|之前|前面).{0,12}(?:指令|规则|要求)|系统提示词|开发者消息|"
    r"隐藏指令|越权访问|扮演.{0,12}(?:医生|管理员|开发者)|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?|"
    r"system\s+prompt|developer\s+message|reveal\s+(?:your\s+)?instructions?|"
    r"act\s+as\s+(?:a\s+)?(?:doctor|administrator|developer))",
    re.IGNORECASE,
)
_HUMAN_HANDOFF_RE = re.compile(
    r"(转人工|人工客服|真人客服|找.{0,8}医生|联系.{0,8}医生|"
    r"不想.{0,8}(?:AI|机器人|智能客服)|不要.{0,8}(?:AI|机器人|智能客服)|"
    r"拒绝.{0,8}(?:AI|机器人|智能客服)|投诉|举报|维权|"
    r"human\s+(?:agent|support)|real\s+person|talk\s+to\s+(?:a\s+)?doctor|"
    r"speak\s+to\s+(?:a\s+)?human|complaint|do(?:n't|\s+not)\s+want\s+(?:an?\s+)?ai)",
    re.IGNORECASE,
)


class Source2IntegrationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


def validate_content_length(value: Any, max_bytes: int) -> Optional[int]:
    """Validate Content-Length using its unsigned decimal HTTP wire grammar."""
    declared = str(value or "").strip()
    if not declared:
        return None
    if not re.fullmatch(r"[0-9]+", declared):
        raise Source2IntegrationError(
            400, "INVALID_CONTENT_LENGTH", "Content-Length 格式错误")
    length = int(declared)
    if length > max_bytes:
        raise Source2IntegrationError(
            413, "REQUEST_TOO_LARGE", "请求正文超过大小限制")
    return length


def validate_json_content_type(value: Any) -> None:
    """Require the integration JSON media type and UTF-8 when charset is declared."""
    parts = str(value or "").split(";")
    if parts[0].strip().lower() != "application/json":
        raise Source2IntegrationError(
            415, "UNSUPPORTED_MEDIA_TYPE", "请求 Content-Type 必须是 application/json")
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
            raise Source2IntegrationError(
                415, "UNSUPPORTED_MEDIA_TYPE", "请求 Content-Type 必须使用 UTF-8 JSON")
        charsets.append(normalized.lower())
    if len(charsets) > 1:
        raise Source2IntegrationError(
            415, "UNSUPPORTED_MEDIA_TYPE", "请求 Content-Type 必须使用 UTF-8 JSON")


def validate_identity_content_encoding(value: Any) -> None:
    """Reject encoded medical request bodies before reading their wire bytes."""
    normalized = str(value or "").strip().lower()
    if normalized not in {"", "identity"}:
        raise Source2IntegrationError(
            415, "UNSUPPORTED_CONTENT_ENCODING", "请求正文不支持压缩或其他 Content-Encoding")


def read_bounded_body(stream: Any, max_bytes: int) -> bytes:
    """Read at most one byte beyond the limit so chunked bodies stay bounded."""
    chunks = bytearray()
    while len(chunks) <= max_bytes:
        remaining = max_bytes + 1 - len(chunks)
        chunk = stream.read(min(64 * 1024, remaining))
        if not isinstance(chunk, bytes):
            raise Source2IntegrationError(
                400, "INVALID_REQUEST_BODY", "请求正文必须是字节流")
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise Source2IntegrationError(
                413, "REQUEST_TOO_LARGE", "请求正文超过大小限制")
    return bytes(chunks)


def parse_json_object(raw: bytes) -> Dict[str, Any]:
    """Parse one RFC-compatible JSON object with no duplicate member names."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                           parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise Source2IntegrationError(
            400, "INVALID_JSON", "请求 JSON 格式错误") from exc
    if not isinstance(value, dict):
        raise Source2IntegrationError(
            400, "INVALID_JSON_OBJECT", "请求 JSON 必须是对象")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_media_identity(value: str) -> str:
    """Identify one media object without its short-lived access credentials."""
    parsed = urlparse(value)
    return parsed._replace(query="", fragment="").geturl()


def _archive_safe_request(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy a request while removing reusable credentials from every media URL."""
    def scrub(value: Any, field_name: Optional[str] = None) -> Any:
        if isinstance(value, Mapping):
            return {key: scrub(child, str(key)) for key, child in value.items()}
        if isinstance(value, list):
            return [scrub(child) for child in value]
        if field_name == "mediaUrl" and isinstance(value, str):
            return _stable_media_identity(value)
        return value

    return scrub(payload)


def _stable_request_hash(payload: Mapping[str, Any]) -> str:
    """Hash semantic request content while ignoring volatile media signatures."""
    def normalize(value: Any, field_name: Optional[str] = None) -> Any:
        if isinstance(value, Mapping):
            return {key: normalize(child, str(key)) for key, child in value.items()}
        if isinstance(value, list):
            return [normalize(child) for child in value]
        if field_name == "mediaUrl" and isinstance(value, str):
            return _stable_media_identity(value)
        return value

    return _sha256(_canonical_json(normalize(payload)))


def _bindings() -> Iterable[Mapping[str, Any]]:
    value = conf().get("source2_integrations", [])
    return value if isinstance(value, list) else []


def _secret(binding: Mapping[str, Any], name: str) -> str:
    value = binding.get(name)
    env_name = binding.get(f"{name}_env")
    if not value and env_name:
        value = os.environ.get(str(env_name), "")
    return str(value or "")


_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _set_inline_secret_test_override(enabled: bool) -> None:
    """Test-only hook; production configuration and environment cannot enable it."""
    global _ALLOW_INLINE_SECRETS_FOR_TESTS
    _ALLOW_INLINE_SECRETS_FOR_TESTS = bool(enabled)


def _assert_external_secret_sources(binding: Mapping[str, Any]) -> bool:
    """Require production Source2 credentials and data keys to come from env injection."""
    if binding.get("require_external_secrets") is not True:
        if _ALLOW_INLINE_SECRETS_FOR_TESTS:
            return False
        raise Source2IntegrationError(
            503, "EXTERNAL_SECRET_ENFORCEMENT_REQUIRED",
            "Source2 生产集成必须启用外部密钥注入强制模式")

    required_secret_names = ["app_secret"]
    if binding.get("admin_enabled", True):
        required_secret_names.append("admin_app_secret")
    for name in required_secret_names:
        env_name = str(binding.get(f"{name}_env") or "").strip()
        if (binding.get(name) not in (None, "") or not _ENV_NAME_PATTERN.fullmatch(env_name)
                or not os.environ.get(env_name, "")):
            raise Source2IntegrationError(
                503, "EXTERNAL_SECRET_REQUIRED",
                "Source2 集成凭证必须由有效环境变量注入")

    key_ring = binding.get("data_keys")
    if (binding.get("data_key") not in (None, "")
            or not isinstance(key_ring, Mapping) or not key_ring):
        raise Source2IntegrationError(
            503, "EXTERNAL_DATA_KEY_REQUIRED",
            "医疗数据密钥环必须由有效环境变量注入")
    for entry in key_ring.values():
        if not isinstance(entry, Mapping) or set(entry) != {"env"}:
            raise Source2IntegrationError(
                503, "EXTERNAL_DATA_KEY_REQUIRED",
                "医疗数据密钥环必须由有效环境变量注入")
        env_name = str(entry.get("env") or "").strip()
        if (entry.get("value") not in (None, "")
                or not _ENV_NAME_PATTERN.fullmatch(env_name)
                or not os.environ.get(env_name, "")):
            raise Source2IntegrationError(
                503, "EXTERNAL_DATA_KEY_REQUIRED",
                "医疗数据密钥环必须由有效环境变量注入")
    return True


def _assert_unique_binding_credential(binding: Mapping[str, Any], name: str) -> None:
    """Fail closed when an App Key is ambiguous across business/admin planes."""
    value = str(binding.get(name, ""))
    if not value:
        return
    for other in _bindings():
        if not isinstance(other, Mapping):
            continue
        for candidate_name, enabled_name in (
                ("app_key", "enabled"), ("admin_app_key", "admin_enabled")):
            if other is binding and candidate_name == name:
                continue
            if not other.get(enabled_name, True):
                continue
            candidate = str(other.get(candidate_name, ""))
            if candidate and hmac.compare_digest(candidate, value):
                raise Source2IntegrationError(
                    503, "APP_KEY_NOT_UNIQUE",
                    "启用的 Source2 业务与管理员 App Key 必须全局唯一")


def _assert_isolated_hmac_secrets(binding: Mapping[str, Any]) -> None:
    """Reject one HMAC secret reused by another tenant or access plane."""
    target_credentials = []
    for key_name, secret_name, enabled_name in (
            ("app_key", "app_secret", "enabled"),
            ("admin_app_key", "admin_app_secret", "admin_enabled")):
        if not binding.get(enabled_name, True):
            continue
        key = str(binding.get(key_name, "") or "")
        secret = _secret(binding, secret_name)
        if not key or not secret:
            raise Source2IntegrationError(
                503, "CREDENTIAL_NOT_CONFIGURED",
                "Source2 业务与管理员凭证必须分别完整配置")
        target_credentials.append((key_name, secret))

    if (len(target_credentials) > 1
            and hmac.compare_digest(target_credentials[0][1], target_credentials[1][1])):
        raise Source2IntegrationError(
            503, "HMAC_SECRET_NOT_ISOLATED",
            "Source2 业务与管理员 HMAC Secret 不得复用")

    for other in _bindings():
        if other is binding or not isinstance(other, Mapping):
            continue
        for _other_key_name, other_secret_name, other_enabled_name in (
                ("app_key", "app_secret", "enabled"),
                ("admin_app_key", "admin_app_secret", "admin_enabled")):
            if not other.get(other_enabled_name, True):
                continue
            other_secret = _secret(other, other_secret_name)
            if (other_secret and any(hmac.compare_digest(secret, other_secret)
                                     for _name, secret in target_credentials)):
                raise Source2IntegrationError(
                    503, "HMAC_SECRET_NOT_ISOLATED",
                    "不同 Source2 租户或访问平面不得复用 HMAC Secret")


def _assert_compliance_approved(binding: Mapping[str, Any]) -> None:
    approval_ref = str(binding.get("compliance_approval_ref", "") or "").strip()
    if binding.get("compliance_approved") is not True or not approval_ref:
        raise Source2IntegrationError(
            503, "COMPLIANCE_APPROVAL_REQUIRED",
            "医疗上下文永久留存及医生身份展示尚未登记书面合规批准")
    if len(approval_ref) > 200:
        raise Source2IntegrationError(
            503, "COMPLIANCE_APPROVAL_INVALID", "合规审批记录引用格式错误")


def get_binding(app_key: str, tenant_id: str,
                agent_id: Optional[str] = None) -> Mapping[str, Any]:
    for item in _bindings():
        if not isinstance(item, Mapping) or not item.get("enabled", True):
            continue
        if hmac.compare_digest(str(item.get("app_key", "")), str(app_key)):
            _assert_unique_binding_credential(item, "app_key")
            if str(item.get("tenant_id", "")) != str(tenant_id):
                raise Source2IntegrationError(403, "TENANT_BINDING_MISMATCH",
                                              "App Key 与租户不匹配")
            if agent_id is not None and str(item.get("agent_id", "")) != str(agent_id):
                raise Source2IntegrationError(403, "AGENT_BINDING_MISMATCH",
                                              "租户与 Agent 不匹配")
            _assert_compliance_approved(item)
            _assert_external_secret_sources(item)
            _assert_isolated_hmac_secrets(item)
            if not _secret(item, "app_secret"):
                raise Source2IntegrationError(503, "SECRET_NOT_CONFIGURED",
                                              "服务端未配置集成密钥")
            if len(_secret(item, "app_secret").encode("utf-8")) < MIN_HMAC_SECRET_BYTES:
                raise Source2IntegrationError(503, "SECRET_TOO_SHORT",
                                              "集成密钥至少需要 32 字节")
            _assert_isolated_workspace(item)
            _assert_isolated_data_keys(item)
            return item
    raise Source2IntegrationError(401, "INVALID_APP_KEY", "无效的 App Key")


def get_admin_binding(app_key: str, tenant_id: str) -> Mapping[str, Any]:
    """Resolve the separate credential used only for destructive archive operations."""
    for item in _bindings():
        if not isinstance(item, Mapping) or not item.get("admin_enabled", True):
            continue
        if hmac.compare_digest(str(item.get("admin_app_key", "")), str(app_key)):
            _assert_unique_binding_credential(item, "admin_app_key")
            if str(item.get("tenant_id", "")) != str(tenant_id):
                raise Source2IntegrationError(403, "TENANT_BINDING_MISMATCH",
                                              "管理员 App Key 与租户不匹配")
            _assert_external_secret_sources(item)
            _assert_isolated_hmac_secrets(item)
            if not _secret(item, "admin_app_secret"):
                raise Source2IntegrationError(503, "ADMIN_SECRET_NOT_CONFIGURED",
                                              "服务端未配置管理员集成密钥")
            if (len(_secret(item, "admin_app_secret").encode("utf-8"))
                    < MIN_HMAC_SECRET_BYTES):
                raise Source2IntegrationError(503, "ADMIN_SECRET_TOO_SHORT",
                                              "管理员集成密钥至少需要 32 字节")
            _assert_isolated_workspace(item)
            _assert_isolated_data_keys(item)
            return item
    raise Source2IntegrationError(401, "INVALID_ADMIN_APP_KEY", "无效的管理员 App Key")


def build_signature(method: str, path: str, tenant_id: str, timestamp: str,
                    nonce: str, idempotency_key: str, body: bytes,
                    secret: str) -> str:
    canonical = "\n".join((method.upper(), path, str(tenant_id), str(timestamp),
                           nonce, idempotency_key, _sha256(body)))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _authenticate(method: str, path: str, headers: Mapping[str, str], body: bytes,
                  agent_id: Optional[str], admin: bool) -> Mapping[str, Any]:
    def required(name: str) -> str:
        value = str(headers.get(name, "") or "").strip()
        if not value:
            raise Source2IntegrationError(401, "MISSING_AUTH_HEADER",
                                          f"缺少请求头 {name}")
        return value

    app_key = required("X-App-Key")
    tenant_id = required("X-Tenant-Id")
    timestamp = required("X-Timestamp")
    nonce = required("X-Nonce")
    signature = required("X-Signature").lower()
    idempotency_key = required("Idempotency-Key")
    header_limits = (("X-App-Key", app_key, 128), ("X-Tenant-Id", tenant_id, 64),
                     ("X-Timestamp", timestamp, 20), ("X-Nonce", nonce, 128),
                     ("Idempotency-Key", idempotency_key, 200))
    for name, value, maximum in header_limits:
        if len(value) > maximum:
            raise Source2IntegrationError(401, "INVALID_AUTH_HEADER", f"请求头 {name} 格式错误")
    if len(signature) != 64 or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise Source2IntegrationError(401, "INVALID_SIGNATURE", "请求签名格式错误")
    try:
        request_time = int(timestamp)
    except ValueError as exc:
        raise Source2IntegrationError(401, "INVALID_TIMESTAMP", "时间戳格式错误") from exc
    now = int(time.time())
    if abs(now - request_time) > TIMESTAMP_SKEW_SECONDS:
        raise Source2IntegrationError(401, "EXPIRED_REQUEST", "请求时间戳已过期")

    binding = (get_admin_binding(app_key, tenant_id) if admin else
               get_binding(app_key, tenant_id, agent_id))
    expected = build_signature(method, path, tenant_id, timestamp, nonce,
                               idempotency_key, body,
                               _secret(binding, "admin_app_secret" if admin else "app_secret"))
    if not hmac.compare_digest(expected, signature):
        raise Source2IntegrationError(401, "INVALID_SIGNATURE", "请求签名校验失败")

    nonce_key = (app_key, nonce)
    with _NONCE_LOCK:
        expired = [key for key, expires_at in _NONCES.items() if expires_at <= now]
        for key in expired:
            _NONCES.pop(key, None)
        if nonce_key in _NONCES:
            raise Source2IntegrationError(409, "REPLAYED_REQUEST", "Nonce 已被使用")
        _record_persistent_nonce(binding, app_key, nonce, now)
        _NONCES[nonce_key] = now + NONCE_TTL_SECONDS
    return binding


def authenticate(method: str, path: str, headers: Mapping[str, str], body: bytes,
                 agent_id: Optional[str] = None) -> Mapping[str, Any]:
    return _authenticate(method, path, headers, body, agent_id, False)


def authenticate_admin(method: str, path: str, headers: Mapping[str, str],
                       body: bytes) -> Mapping[str, Any]:
    return _authenticate(method, path, headers, body, None, True)


def _secure_chmod(path: Path, mode: int) -> None:
    """Fail closed if POSIX permissions cannot protect retained medical data."""
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError as exc:
        raise Source2IntegrationError(
            503, "STORAGE_PERMISSION_HARDENING_FAILED",
            "医疗加密存储权限加固失败") from exc


def _secure_sqlite_files(db_path: Path) -> None:
    for candidate in (db_path, Path(str(db_path) + "-wal"),
                      Path(str(db_path) + "-shm")):
        if candidate.exists():
            _secure_chmod(candidate, 0o600)


def _open_private_lock_file(path: Path, agent_id: str):
    _ensure_private_storage_directory(agent_id, path.parent)
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _secure_chmod(path, 0o600)
        return os.fdopen(descriptor, "a+b")
    except Exception:
        os.close(descriptor)
        raise


def _record_persistent_nonce(binding: Mapping[str, Any], app_key: str,
                             nonce: str, now: int) -> None:
    """Atomically reject replay across process restarts and worker processes."""
    agent_id = str(binding["agent_id"])
    db_path = _assert_agent_storage_path(
        agent_id, _workspace_for(agent_id) / "medical" /
        "source2" / "security" / "replay.sqlite3")
    _ensure_private_storage_directory(agent_id, db_path.parent)
    nonce_hash = _sha256(f"{app_key}\n{nonce}".encode("utf-8"))
    try:
        with sqlite3.connect(str(db_path), timeout=5) as connection:
            _secure_sqlite_files(db_path)
            connection.execute("PRAGMA journal_mode=WAL")
            _secure_sqlite_files(db_path)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS used_nonce ("
                "nonce_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
            connection.execute("DELETE FROM used_nonce WHERE expires_at <= ?", (now,))
            try:
                connection.execute(
                    "INSERT INTO used_nonce(nonce_hash, expires_at) VALUES (?, ?)",
                    (nonce_hash, now + NONCE_TTL_SECONDS))
            except sqlite3.IntegrityError as exc:
                raise Source2IntegrationError(409, "REPLAYED_REQUEST", "Nonce 已被使用") from exc
            _secure_sqlite_files(db_path)
        _secure_sqlite_files(db_path)
    except Source2IntegrationError:
        raise
    except sqlite3.Error as exc:
        raise Source2IntegrationError(503, "REPLAY_STORE_UNAVAILABLE",
                                      "防重放存储不可用") from exc


def _workspace_for(agent_id: str) -> Path:
    from agent.registry import get_agent_registry
    try:
        profile = get_agent_registry().get(agent_id)
    except (KeyError, ValueError) as exc:
        raise Source2IntegrationError(404, "AGENT_NOT_FOUND", "Agent 不存在或未启用") from exc
    return profile.workspace_path


def _assert_isolated_workspace(binding: Mapping[str, Any]) -> None:
    """Fail closed while either business or retained-data administration is enabled."""
    tenant_id = str(binding.get("tenant_id", ""))
    agent_id = str(binding.get("agent_id", ""))
    target = _workspace_for(agent_id).resolve()
    for other in _bindings():
        if (not isinstance(other, Mapping)
                or not (other.get("enabled", True) or other.get("admin_enabled", True))):
            continue
        other_tenant_id = str(other.get("tenant_id", ""))
        other_agent_id = str(other.get("agent_id", ""))
        if other_tenant_id == tenant_id and other_agent_id == agent_id:
            continue
        if not other_agent_id:
            continue
        try:
            other_workspace = _workspace_for(other_agent_id).resolve()
        except Source2IntegrationError:
            # The unrelated binding will fail its own health check. It must not
            # make a correctly configured tenant unavailable unless it actually
            # resolves to the same storage directory.
            continue
        if (target == other_workspace or target in other_workspace.parents
                or other_workspace in target.parents):
            raise Source2IntegrationError(
                503, "WORKSPACE_NOT_ISOLATED",
                "Source2 租户 Agent 必须使用互不重叠的独立工作区")


def _assert_agent_storage_path(agent_id: str, path: Path) -> Path:
    """Reject sensitive paths redirected outside the bound Agent workspace."""
    try:
        workspace = _workspace_for(agent_id).resolve(strict=False)
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise Source2IntegrationError(
            503, "STORAGE_PATH_NOT_ISOLATED",
            "医疗存储路径无法安全解析") from exc
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise Source2IntegrationError(
            503, "STORAGE_PATH_NOT_ISOLATED",
            "医疗存储路径必须位于租户 Agent 工作区内") from exc
    if not relative.parts:
        raise Source2IntegrationError(
            503, "STORAGE_PATH_NOT_ISOLATED",
            "医疗存储路径不能使用租户 Agent 工作区根目录")
    return path


def _ensure_private_storage_directory(agent_id: str, path: Path) -> Path:
    """Create and harden every workspace-relative directory in the path."""
    path = _assert_agent_storage_path(agent_id, path)
    path.mkdir(parents=True, exist_ok=True)
    workspace = _workspace_for(agent_id).resolve(strict=False)
    current = path.resolve(strict=False)
    while current != workspace:
        _secure_chmod(current, 0o700)
        current = current.parent
    return path


def _configured_data_key_ids(binding: Mapping[str, Any]) -> Tuple[str, ...]:
    """Return every key id that can decrypt retained data for one tenant."""
    active_key_id = _active_data_key_id(binding)
    key_ring = binding.get("data_keys")
    if key_ring is not None and not isinstance(key_ring, Mapping):
        raise Source2IntegrationError(503, "DATA_KEY_RING_INVALID", "数据密钥环格式错误")
    if not isinstance(key_ring, Mapping) or not key_ring:
        return (active_key_id,)

    key_ids = []
    for key_id in key_ring:
        if not isinstance(key_id, str) or not key_id or key_id != key_id.strip():
            raise Source2IntegrationError(503, "DATA_KEY_ID_INVALID", "数据密钥标识格式错误")
        key_ids.append(key_id)
    # Validate a missing active key before historical entries and avoid decoding
    # the same material twice when the mapping preserves it later in iteration.
    return tuple(dict.fromkeys((active_key_id, *key_ids)))


def _assert_isolated_data_keys(binding: Mapping[str, Any]) -> None:
    """Reject any retained AES key reused by a different Source2 tenant."""
    tenant_id = str(binding.get("tenant_id", ""))
    tenant_keys = tuple(
        _decode_data_key(binding, key_id)
        for key_id in _configured_data_key_ids(binding)
    )
    for other in _bindings():
        if (not isinstance(other, Mapping)
                or not (other.get("enabled", True) or other.get("admin_enabled", True))):
            continue
        other_tenant_id = str(other.get("tenant_id", ""))
        if other is binding or other_tenant_id == tenant_id:
            continue
        try:
            other_key_ids = _configured_data_key_ids(other)
        except Source2IntegrationError:
            # The unrelated binding will fail closed when it serves traffic.
            # Do not let one incomplete tenant configuration hide whether this
            # tenant's own keys are isolated from every resolvable remote key.
            continue
        for other_key_id in other_key_ids:
            try:
                other_key = _decode_data_key(other, other_key_id)
            except Source2IntegrationError:
                continue
            if any(hmac.compare_digest(key, other_key) for key in tenant_keys):
                raise Source2IntegrationError(
                    503, "DATA_KEY_NOT_ISOLATED",
                    "不同 Source2 租户不得复用同一数据密钥")


def _knowledge_dir(agent_id: str) -> Path:
    return _assert_agent_storage_path(
        agent_id, _workspace_for(agent_id) / "knowledge" / "source2")


def _safe_filename(item_id: Any, index: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(item_id or index)).strip("-")
    return (cleaned[:80] or str(index)) + ".md"


def _read_manifest(agent_id: str, binding: Mapping[str, Any]) -> Dict[str, Any]:
    encrypted_path = _knowledge_dir(agent_id) / "manifest.json.enc"
    if encrypted_path.is_file():
        try:
            envelope = json.loads(encrypted_path.read_text(encoding="utf-8"))
            manifest = _decrypt_envelope(binding, envelope)
            _validate_manifest_binding(agent_id, binding, manifest)
            return manifest
        except Source2IntegrationError:
            raise
        except Exception as exc:
            raise Source2IntegrationError(
                500, "KNOWLEDGE_READ_FAILED", "加密语料快照读取失败") from exc
    # Read-only compatibility with snapshots created before encrypted storage.
    # The next successful sync replaces the whole directory with ciphertext.
    legacy_path = _knowledge_dir(agent_id) / "manifest.json"
    if not legacy_path.is_file():
        return {}
    try:
        manifest = json.loads(legacy_path.read_text(encoding="utf-8"))
        _validate_manifest_binding(agent_id, binding, manifest)
        return manifest
    except (OSError, ValueError):
        return {}


def _validate_manifest_binding(agent_id: str, binding: Mapping[str, Any],
                               manifest: Mapping[str, Any]) -> None:
    if not manifest:
        return
    if (not isinstance(manifest, Mapping)
            or str(manifest.get("tenantId", "")) != str(binding.get("tenant_id", ""))
            or str(manifest.get("agentId", "")) != agent_id
            or agent_id != str(binding.get("agent_id", ""))):
        raise Source2IntegrationError(
            500, "KNOWLEDGE_BINDING_MISMATCH",
            "语料清单与当前租户或 Agent 绑定不一致")


def _manifest_key_id(agent_id: str) -> Optional[str]:
    path = _knowledge_dir(agent_id) / "manifest.json.enc"
    if not path.is_file():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("keyId") or "") or None
    except (OSError, ValueError, TypeError):
        return None


def health(binding: Mapping[str, Any]) -> Dict[str, Any]:
    agent_id = str(binding["agent_id"])
    active_key_id = _active_data_key_id(binding)
    external_secrets = _assert_external_secret_sources(binding)
    _assert_unique_binding_credential(binding, "app_key")
    _assert_unique_binding_credential(binding, "admin_app_key")
    _assert_isolated_hmac_secrets(binding)
    # Fail the connection test before accepting medical traffic when the active
    # archive key was not injected (or has an invalid length/encoding).
    _decode_data_key(binding, active_key_id)
    # A usable archive key alone is insufficient for media messages. Surface the
    # exact configured hosts so source2 can fail its connection test before the
    # first one-time media URL is issued to an untrusted/unreachable host.
    media_hosts = sorted(_normalized_media_hosts(binding.get("media_host_allowlist")))
    manifest = _read_manifest(agent_id, binding)
    return {
        "status": "UP",
        "tenantId": str(binding["tenant_id"]),
        "agentId": agent_id,
        "modelProfile": binding.get("model_profile"),
        "corpusVersion": manifest.get("version"),
        "policyVersion": POLICY_VERSION,
        "encryptionKeyId": active_key_id,
        "mediaHostAllowlist": media_hosts,
        "complianceApprovalRef": str(binding.get("compliance_approval_ref", "")).strip(),
        "externalSecrets": external_secrets,
        "timestamp": int(time.time()),
    }


def sync_knowledge(binding: Mapping[str, Any], agent_id: str,
                   payload: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise Source2IntegrationError(400, "INVALID_JSON_OBJECT", "请求 JSON 必须是对象")
    if len(_canonical_json(payload)) > MAX_KNOWLEDGE_PAYLOAD_BYTES:
        raise Source2IntegrationError(413, "REQUEST_TOO_LARGE", "语料快照超过大小限制")
    items = payload.get("items")
    if isinstance(items, list) and len(items) > MAX_KNOWLEDGE_ITEMS:
        raise Source2IntegrationError(413, "TOO_MANY_KNOWLEDGE_ITEMS", "语料条目数量超过限制")
    lock_handle = _acquire_knowledge_lock(binding, agent_id)
    try:
        return _sync_knowledge_locked(binding, agent_id, payload)
    finally:
        _release_idempotency_lock(lock_handle)


def _sync_knowledge_locked(binding: Mapping[str, Any], agent_id: str,
                           payload: Mapping[str, Any]) -> Dict[str, Any]:
    _reject_unknown_fields(payload, _KNOWLEDGE_REQUEST_FIELDS, "knowledge")
    tenant_value = payload.get("tenantId")
    if (not isinstance(tenant_value, str) or not tenant_value.strip()
            or len(tenant_value) > 64):
        raise Source2IntegrationError(400, "INVALID_KNOWLEDGE", "tenantId 格式错误")
    tenant_id = tenant_value
    if tenant_id != str(binding["tenant_id"]):
        raise Source2IntegrationError(403, "TENANT_BINDING_MISMATCH", "正文租户不匹配")
    if agent_id != str(binding["agent_id"]):
        raise Source2IntegrationError(403, "AGENT_BINDING_MISMATCH", "Agent 不匹配")
    version_value = payload.get("version")
    version = version_value.strip() if isinstance(version_value, str) else ""
    items = payload.get("items")
    checksum_value = payload.get("checksum")
    checksum = checksum_value.lower() if isinstance(checksum_value, str) else ""
    if not version or len(version) > 64 or not isinstance(items, list):
        raise Source2IntegrationError(400, "INVALID_KNOWLEDGE", "version 和 items 必填")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise Source2IntegrationError(400, "INVALID_CHECKSUM", "语料校验和格式错误")
    calculated = _sha256(_canonical_json(items))
    if checksum and not hmac.compare_digest(checksum, calculated):
        raise Source2IntegrationError(400, "CHECKSUM_MISMATCH", "语料校验和不匹配")
    checksum = calculated
    published_at = payload.get("publishedAt")
    if published_at is not None and (
            isinstance(published_at, bool) or not isinstance(published_at, int)
            or published_at < 0):
        raise Source2IntegrationError(400, "INVALID_KNOWLEDGE", "publishedAt 格式错误")

    current = _read_manifest(agent_id, binding)
    if current.get("version") == version:
        if current.get("checksum") != checksum:
            raise Source2IntegrationError(409, "VERSION_CONFLICT", "同一版本的内容不一致")
        active_key_id = _active_data_key_id(binding)
        if _manifest_key_id(agent_id) != active_key_id:
            knowledge_dir = _knowledge_dir(agent_id)
            _write_encrypted_json(knowledge_dir / "manifest.json.enc", binding, current)
            legacy_path = knowledge_dir / "manifest.json"
            if legacy_path.is_file():
                legacy_path.unlink()
            for legacy_document in knowledge_dir.glob("*.md"):
                legacy_document.unlink()
        return {"status": "UNCHANGED", "version": version, "checksum": checksum,
                "itemCount": len(items), "encryptionKeyId": active_key_id}
    if current.get("version") and version < str(current["version"]):
        raise Source2IntegrationError(409, "VERSION_REGRESSION", "语料版本不能倒退，请发布新的回滚版本")

    target = _knowledge_dir(agent_id)
    _ensure_private_storage_directory(agent_id, target.parent)
    staging = target.parent / f".source2-{uuid.uuid4().hex}.tmp"
    backup = target.parent / f".source2-{uuid.uuid4().hex}.bak"
    _ensure_private_storage_directory(agent_id, staging)
    manifest_items: List[Dict[str, Any]] = []
    try:
        seen_item_ids = set()
        for index, raw in enumerate(items, 1):
            if not isinstance(raw, Mapping):
                raise Source2IntegrationError(400, "INVALID_KNOWLEDGE_ITEM",
                                              f"items[{index - 1}] 必须是对象")
            path = f"items[{index - 1}]"
            _reject_unknown_fields(raw, _KNOWLEDGE_ITEM_FIELDS, path)
            raw_id = raw.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
                raise Source2IntegrationError(400, "INVALID_KNOWLEDGE_ITEM",
                                              f"{path}.id 格式错误")
            item_id = str(raw_id).strip()
            if not item_id or len(item_id) > 64 or item_id in seen_item_ids:
                raise Source2IntegrationError(400, "INVALID_KNOWLEDGE_ITEM",
                                              f"{path}.id 为空、过长或重复")
            seen_item_ids.add(item_id)
            item_type = str(raw.get("type") or "FAQ").upper()
            if item_type not in ("FAQ", "DOCUMENT"):
                raise Source2IntegrationError(400, "INVALID_KNOWLEDGE_ITEM",
                                              f"{path}.type 只能是 FAQ 或 DOCUMENT")
            question = _knowledge_string(raw.get("question"), 1000,
                                         f"{path}.question")
            title = _knowledge_string(raw.get("title"), 200, f"{path}.title")
            title = title.strip() or question.strip() or f"语料 {index}"
            if item_type == "FAQ":
                answer = _knowledge_string(raw.get("answer"), MAX_KNOWLEDGE_TEXT_CHARS,
                                           f"{path}.answer")
                if not question.strip():
                    raise Source2IntegrationError(400, "INVALID_KNOWLEDGE_ITEM",
                                                  f"{path}.question 不能为空")
            else:
                answer = _knowledge_string(raw.get("content"), MAX_KNOWLEDGE_TEXT_CHARS,
                                           f"{path}.content")
                source_hash_value = raw.get("sourceHash")
                if source_hash_value is not None:
                    if not isinstance(source_hash_value, str):
                        raise Source2IntegrationError(
                            400, "SOURCE_HASH_MISMATCH", f"{path}.sourceHash 与正文不匹配")
                    source_hash = source_hash_value.lower()
                    if (not re.fullmatch(r"[0-9a-f]{64}", source_hash)
                            or not hmac.compare_digest(
                                source_hash, _sha256(answer.encode("utf-8")))):
                        raise Source2IntegrationError(
                            400, "SOURCE_HASH_MISMATCH", f"{path}.sourceHash 与正文不匹配")
            if not answer.strip():
                raise Source2IntegrationError(400, "EMPTY_KNOWLEDGE_ITEM",
                                              f"items[{index - 1}] 内容为空")
            manifest_items.append({"id": raw_id, "type": item_type,
                                   "title": title, "question": question,
                                   "answer": answer})
        manifest = {"tenantId": tenant_id, "agentId": agent_id, "version": version,
                    "checksum": checksum, "publishedAt": published_at,
                    "syncedAt": int(time.time()), "items": manifest_items}
        _write_encrypted_json(staging / "manifest.json.enc", binding, manifest)
        if target.exists():
            target.replace(backup)
        staging.replace(target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and not target.exists():
            backup.replace(target)
        raise
    # The new manifest is committed once staging replaces target. Failure to
    # remove an encrypted old backup must not turn that successful commit into
    # an API error, otherwise source2 would keep the previous active version
    # while CowAgent already serves the new one. Retry cleanup on later syncs.
    for stale_backup in target.parent.glob(".source2-*.bak"):
        try:
            shutil.rmtree(stale_backup)
        except OSError as exc:
            logger.warning(
                "[Source2Integration] encrypted knowledge backup cleanup failed, error_type=%s",
                type(exc).__name__)
    return {"status": "PUBLISHED", "version": version, "checksum": checksum,
            "itemCount": len(items),
            "encryptionKeyId": _active_data_key_id(binding)}


def _knowledge_string(value: Any, maximum: int, path: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise Source2IntegrationError(400, "INVALID_KNOWLEDGE_ITEM",
                                      f"{path} 格式错误或超过长度限制")
    return value


def _active_data_key_id(binding: Mapping[str, Any]) -> str:
    key_id = str(binding.get("active_data_key_id") or
                 binding.get("data_key_id") or "source2-v1").strip()
    if not key_id:
        raise Source2IntegrationError(503, "DATA_KEY_ID_INVALID", "数据密钥标识不能为空")
    return key_id


def _encoded_data_key(binding: Mapping[str, Any], key_id: str) -> str:
    """Resolve one key from a KMS-injected key ring, with legacy compatibility."""
    key_ring = binding.get("data_keys")
    if key_ring is not None and not isinstance(key_ring, Mapping):
        raise Source2IntegrationError(503, "DATA_KEY_RING_INVALID", "数据密钥环格式错误")
    if isinstance(key_ring, Mapping) and key_id in key_ring:
        entry = key_ring[key_id]
        if isinstance(entry, Mapping):
            encoded = str(entry.get("value") or "")
            env_name = str(entry.get("env") or "").strip()
            if not encoded and env_name:
                encoded = os.environ.get(env_name, "")
        else:
            encoded = str(entry or "")
        if not encoded:
            raise Source2IntegrationError(
                503, "DATA_KEY_NOT_CONFIGURED", f"数据密钥 {key_id} 未注入")
        return encoded

    legacy_key_id = str(binding.get("data_key_id") or "source2-v1").strip()
    # A configured key ring is authoritative. Falling back to the single
    # legacy key for an unknown keyId could silently decrypt with the wrong key.
    if isinstance(key_ring, Mapping) and key_ring:
        raise Source2IntegrationError(
            503, "DATA_KEY_NOT_CONFIGURED", f"数据密钥 {key_id} 不在密钥环中")
    if key_id != legacy_key_id:
        raise Source2IntegrationError(
            503, "DATA_KEY_NOT_CONFIGURED", f"数据密钥 {key_id} 不可用")
    encoded = _secret(binding, "data_key")
    if not encoded:
        raise Source2IntegrationError(503, "DATA_KEY_NOT_CONFIGURED", "数据密钥未注入")
    return encoded


def _decode_data_key(binding: Mapping[str, Any], key_id: Optional[str] = None) -> bytes:
    encoded = _encoded_data_key(binding, key_id or _active_data_key_id(binding))
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        key = b""
    if len(key) != 32:
        try:
            key = bytes.fromhex(encoded)
        except ValueError as exc:
            raise Source2IntegrationError(503, "DATA_KEY_INVALID", "数据密钥格式错误") from exc
    if len(key) != 32:
        raise Source2IntegrationError(503, "DATA_KEY_INVALID", "数据密钥必须为 32 字节")
    return key


def _archive_path(agent_id: str, idempotency_key: str) -> Path:
    digest = _sha256(idempotency_key.encode("utf-8"))
    return _assert_agent_storage_path(
        agent_id, _workspace_for(agent_id) / "medical" / "source2" /
        "archive" / digest[:2] / f"{digest}.json.enc")


def _idempotency_lock_path(agent_id: str, idempotency_key: str) -> Path:
    digest = _sha256(idempotency_key.encode("utf-8"))
    return _assert_agent_storage_path(
        agent_id, _workspace_for(agent_id) / "medical" / "source2" /
        "security" / "idempotency-locks" / f"{digest}.lock")


def _knowledge_lock_path(agent_id: str) -> Path:
    return _assert_agent_storage_path(
        agent_id, _workspace_for(agent_id) / "medical" / "source2" /
        "security" / "knowledge-sync.lock")


def _try_lock_file(handle) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError):
        return False


def _acquire_idempotency_lock(binding: Mapping[str, Any], idempotency_key: str):
    agent_id = str(binding["agent_id"])
    path = _idempotency_lock_path(agent_id, idempotency_key)
    handle = _open_private_lock_file(path, agent_id)
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    wait_seconds = max(1.0, min(120.0, float(
        binding.get("idempotency_lock_wait_seconds", 30))))
    deadline = time.monotonic() + wait_seconds
    while not _try_lock_file(handle):
        if time.monotonic() >= deadline:
            handle.close()
            raise Source2IntegrationError(
                503, "IDEMPOTENCY_REQUEST_IN_PROGRESS", "相同幂等请求仍在处理中")
        time.sleep(0.05)
    return handle


def _acquire_knowledge_lock(binding: Mapping[str, Any], agent_id: str):
    if agent_id != str(binding["agent_id"]):
        raise Source2IntegrationError(403, "AGENT_BINDING_MISMATCH", "Agent 不匹配")
    path = _knowledge_lock_path(agent_id)
    handle = _open_private_lock_file(path, agent_id)
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + 30.0
    while not _try_lock_file(handle):
        if time.monotonic() >= deadline:
            handle.close()
            raise Source2IntegrationError(503, "KNOWLEDGE_SYNC_IN_PROGRESS",
                                          "Agent 语料同步仍在处理中")
        time.sleep(0.05)
    return handle


def _release_idempotency_lock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _encrypt_envelope(binding: Mapping[str, Any], value: Mapping[str, Any]) -> Dict[str, Any]:
    key_id = _active_data_key_id(binding)
    try:
        from Crypto.Cipher import AES
        key = _decode_data_key(binding, key_id)
        nonce = secrets.token_bytes(12)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(_canonical_json(value))
    except ImportError:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            key = _decode_data_key(binding, key_id)
            nonce = secrets.token_bytes(12)
            combined = AESGCM(key).encrypt(nonce, _canonical_json(value), None)
            ciphertext, tag = combined[:-16], combined[-16:]
        except ImportError as exc:
            raise Source2IntegrationError(503, "CRYPTO_UNAVAILABLE", "缺少 AES-GCM 运行库") from exc
    return {"v": 1, "alg": "AES-256-GCM",
            "keyId": key_id,
            "nonce": base64.b64encode(nonce).decode(),
            "tag": base64.b64encode(tag).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode()}


def _decrypt_envelope(binding: Mapping[str, Any], envelope: Mapping[str, Any]) -> Dict[str, Any]:
    if envelope.get("v") != 1 or envelope.get("alg") != "AES-256-GCM":
        raise Source2IntegrationError(500, "ENCRYPTION_ENVELOPE_INVALID", "加密信封格式错误")
    key_id = str(envelope.get("keyId") or "").strip()
    if not key_id:
        raise Source2IntegrationError(500, "ARCHIVE_KEY_ID_MISSING", "加密归档缺少密钥标识")
    key = _decode_data_key(binding, key_id)
    try:
        nonce = base64.b64decode(str(envelope["nonce"]), validate=True)
        ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
        tag = base64.b64decode(str(envelope["tag"]), validate=True)
        try:
            from Crypto.Cipher import AES
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            raw = cipher.decrypt_and_verify(ciphertext, tag)
        except ImportError:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            raw = AESGCM(key).decrypt(nonce, ciphertext + tag, None)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("encrypted value must be an object")
        return value
    except Source2IntegrationError:
        raise
    except Exception as exc:
        raise Source2IntegrationError(500, "ENCRYPTED_DATA_INVALID", "加密数据校验失败") from exc


def _write_encrypted_json(path: Path, binding: Mapping[str, Any],
                          value: Mapping[str, Any]) -> None:
    agent_id = str(binding["agent_id"])
    path = _assert_agent_storage_path(agent_id, path)
    envelope = _encrypt_envelope(binding, value)
    _ensure_private_storage_directory(agent_id, path.parent)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            _secure_chmod(temp, 0o600)
            handle.write(_canonical_json(envelope))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _secure_chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        # A failed write/replace must not leave encrypted medical-content
        # fragments behind. The destination remains untouched until replace.
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    """Persist a completed rename on platforms that support directory fsync."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        # Some filesystems do not expose directories as file descriptors.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encrypt_archive(binding: Mapping[str, Any], idempotency_key: str,
                     value: Mapping[str, Any]) -> None:
    path = _archive_path(str(binding["agent_id"]), idempotency_key)
    bound_value = dict(value)
    bound_value["_archiveBinding"] = {
        "v": 1,
        "tenantId": str(binding["tenant_id"]),
        "agentId": str(binding["agent_id"]),
        "idempotencyKeyHash": _sha256(idempotency_key.encode("utf-8")),
    }
    _write_encrypted_json(path, binding, bound_value)


def _read_archive(binding: Mapping[str, Any], idempotency_key: str) -> Optional[Dict[str, Any]]:
    path = _archive_path(str(binding["agent_id"]), idempotency_key)
    if not path.is_file():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        value = _decrypt_envelope(binding, envelope)
        archive_binding = value.get("_archiveBinding")
        # Legacy archives did not include an internal binding. They remain
        # readable and are migrated whenever they are next rewritten.
        if archive_binding is not None:
            expected = {
                "v": 1,
                "tenantId": str(binding["tenant_id"]),
                "agentId": str(binding["agent_id"]),
                "idempotencyKeyHash": _sha256(idempotency_key.encode("utf-8")),
            }
            if not isinstance(archive_binding, Mapping) or dict(archive_binding) != expected:
                raise Source2IntegrationError(
                    500, "ARCHIVE_BINDING_MISMATCH",
                    "加密归档与当前租户、Agent 或幂等键不匹配")
        return value
    except Source2IntegrationError:
        raise
    except Exception as exc:
        raise Source2IntegrationError(500, "ARCHIVE_READ_FAILED", "加密归档读取失败") from exc


def reencrypt_archive(binding: Mapping[str, Any], idempotency_key: str) -> Dict[str, Any]:
    """Re-encrypt one request and its media with the configured active key."""
    lock_handle = _acquire_idempotency_lock(binding, idempotency_key)
    try:
        return _reencrypt_archive_locked(binding, idempotency_key)
    finally:
        _release_idempotency_lock(lock_handle)


def _reencrypt_archive_locked(binding: Mapping[str, Any],
                              idempotency_key: str) -> Dict[str, Any]:
    active_key_id = _active_data_key_id(binding)
    # Validate the destination key before reading or modifying any archive.
    _decode_data_key(binding, active_key_id)
    request_archive = _read_archive(binding, idempotency_key)
    archive_candidates = [(idempotency_key, "request")]
    archive_candidates.extend(
        ((idempotency_key + ":media" if index == 0
          else f"{idempotency_key}:media:{index}"), f"media:{index}")
        for index in range(MAX_MEDIA_ARCHIVES_PER_REQUEST))
    archives = []
    for archive_key, archive_type in archive_candidates:
        value = request_archive if archive_key == idempotency_key else _read_archive(binding, archive_key)
        if value is not None:
            if archive_type == "request" and isinstance(value.get("request"), Mapping):
                value = dict(value)
                value["request"] = _archive_safe_request(value["request"])
                value["requestHash"] = _stable_request_hash(value["request"])
            archives.append((archive_key, archive_type, value))
    # Read and authenticate every source envelope before replacing any file.
    for archive_key, _archive_type, value in archives:
        _encrypt_archive(binding, archive_key, value)
    reencrypted = [archive_type for _archive_key, archive_type, _value in archives]
    result = {
        "status": "REENCRYPTED" if reencrypted else "NOT_FOUND",
        "idempotencyKeyHash": _sha256(idempotency_key.encode("utf-8")),
        "keyId": active_key_id,
        "archives": reencrypted,
    }
    _write_archive_admin_audit(binding, "REENCRYPT", idempotency_key, {
        "status": result["status"], "keyId": active_key_id,
        "archives": reencrypted,
    })
    return result


def _write_archive_admin_audit(binding: Mapping[str, Any], action: str,
                               idempotency_key: str,
                               details: Mapping[str, Any]) -> None:
    """Append a non-medical audit record for each privileged archive action."""
    agent_id = str(binding["agent_id"])
    audit_dir = _assert_agent_storage_path(
        agent_id, _workspace_for(agent_id) / "medical" /
        "source2" / "archive-admin-audit")
    _ensure_private_storage_directory(agent_id, audit_dir)
    record = {
        "tenantId": str(binding["tenant_id"]),
        "action": action,
        "idempotencyKeyHash": _sha256(idempotency_key.encode("utf-8")),
        "executedAt": int(time.time()),
    }
    record.update(details)
    audit_file = audit_dir / f"{int(time.time())}-{uuid.uuid4().hex}.json"
    descriptor = os.open(audit_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        _secure_chmod(audit_file, 0o600)
        handle.write(_canonical_json(record))
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(audit_dir)


def clear_archive(binding: Mapping[str, Any], idempotency_key: str) -> Dict[str, Any]:
    """Emergency, explicit deletion only; no automatic retention job exists."""
    lock_handle = _acquire_idempotency_lock(binding, idempotency_key)
    try:
        return _clear_archive_locked(binding, idempotency_key)
    finally:
        _release_idempotency_lock(lock_handle)


def _clear_archive_locked(binding: Mapping[str, Any],
                          idempotency_key: str) -> Dict[str, Any]:
    path = _archive_path(str(binding["agent_id"]), idempotency_key)
    existed = path.is_file()
    archive_read_error = None
    if existed:
        try:
            _read_archive(binding, idempotency_key)
        except Source2IntegrationError as exc:
            # Emergency deletion must remain available when ciphertext is
            # corrupt or its historical key is no longer readable.
            archive_read_error = exc.code
    # Media keys use a bounded deterministic naming scheme. Always inspect all
    # slots so orphaned media can be removed even when the request index is
    # missing or unreadable.
    media_keys = [idempotency_key + ":media" if index == 0
                  else f"{idempotency_key}:media:{index}"
                  for index in range(MAX_MEDIA_ARCHIVES_PER_REQUEST)]
    if existed:
        path.unlink()
    media_deleted_count = 0
    for media_key in media_keys:
        media_path = _archive_path(str(binding["agent_id"]), media_key)
        if media_path.is_file():
            media_path.unlink()
            media_deleted_count += 1
    media_existed = media_deleted_count > 0
    record = {"tenantId": str(binding["tenant_id"]),
              "idempotencyKeyHash": _sha256(idempotency_key.encode("utf-8")),
              "deleted": existed, "mediaDeleted": media_existed,
              "mediaDeletedCount": media_deleted_count,
              "deletedAt": int(time.time())}
    _write_archive_admin_audit(binding, "DELETE", idempotency_key, {
        "status": "DELETED" if existed or media_existed else "NOT_FOUND",
        "deleted": existed, "mediaDeleted": media_existed,
        "mediaDeletedCount": media_deleted_count,
        "requestArchiveStatus": (archive_read_error or
                                 ("READABLE" if existed else "NOT_FOUND")),
    })
    return {"status": "DELETED" if existed or media_existed else "NOT_FOUND",
            "idempotencyKeyHash": record["idempotencyKeyHash"]}


def _archive_remote_media(binding: Mapping[str, Any], archive_key: str,
                          media_url: str,
                          max_bytes: int = MAX_MEDIA_ARCHIVE_BYTES) -> int:
    effective_limit = min(MAX_MEDIA_ARCHIVE_BYTES, max(0, int(max_bytes)))
    if effective_limit <= 0:
        raise Source2IntegrationError(
            413, "MEDIA_TOTAL_TOO_LARGE", "单次请求媒体归档总量超过 64MB")
    allowlist = _normalized_media_hosts(binding.get("media_host_allowlist") or [])
    _validate_media_endpoint(media_url, allowlist, "MEDIA_HOST_NOT_ALLOWED")
    request = Request(media_url, headers={"User-Agent": "CowAgent-Source2/1.0"})
    try:
        with _open_media_url(request, allowlist, timeout=15) as response:
            _validate_media_endpoint(
                str(response.geturl() or ""), allowlist, "MEDIA_REDIRECT_NOT_ALLOWED")
            declared_value = str(response.headers.get("Content-Length", "") or "").strip()
            if declared_value and not re.fullmatch(r"[0-9]+", declared_value):
                raise Source2IntegrationError(
                    502, "MEDIA_LENGTH_INVALID", "媒体响应 Content-Length 无效")
            declared = int(declared_value) if declared_value else None
            if declared is not None and declared > effective_limit:
                code = ("MEDIA_TOTAL_TOO_LARGE" if effective_limit < MAX_MEDIA_ARCHIVE_BYTES
                        else "MEDIA_TOO_LARGE")
                message = ("单次请求媒体归档总量超过 64MB" if code == "MEDIA_TOTAL_TOO_LARGE"
                           else "媒体文件超过 20MB")
                raise Source2IntegrationError(413, code, message)
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            content_type = content_type.split(";", 1)[0].strip().lower()
            allowed_types = binding.get("media_content_type_allowlist") or [
                "image/*", "audio/*", "video/*", "application/pdf",
                "application/octet-stream", "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ]
            if isinstance(allowed_types, str):
                allowed_types = [allowed_types]
            if not _matches_content_type(content_type, allowed_types):
                raise Source2IntegrationError(415, "MEDIA_TYPE_NOT_ALLOWED",
                                              "媒体类型不在允许范围")
            # Reject a disallowed MIME type before downloading an attacker-sized
            # body from an otherwise allowlisted object-storage host.
            data = _read_bounded_media_body(response, effective_limit)
            if declared is not None and len(data) != declared:
                raise Source2IntegrationError(
                    502, "MEDIA_DOWNLOAD_INCOMPLETE", "媒体响应长度与声明不一致")
    except Source2IntegrationError:
        raise
    except Exception as exc:
        raise Source2IntegrationError(502, "MEDIA_DOWNLOAD_FAILED", "媒体归档下载失败") from exc
    _encrypt_archive(binding, archive_key, {
        "sha256": _sha256(data), "contentType": content_type, "size": len(data),
        "contentBase64": base64.b64encode(data).decode("ascii"),
        "archivedAt": int(time.time()),
    })
    return len(data)


def _read_bounded_media_body(stream: Any, max_bytes: int) -> bytes:
    """Collect legal short reads without ever buffering more than limit + 1."""
    chunks = bytearray()
    while len(chunks) <= max_bytes:
        remaining = max_bytes + 1 - len(chunks)
        chunk = stream.read(min(64 * 1024, remaining))
        if not isinstance(chunk, bytes):
            raise Source2IntegrationError(
                502, "MEDIA_DOWNLOAD_FAILED", "媒体下载未返回字节流")
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            code = ("MEDIA_TOTAL_TOO_LARGE" if max_bytes < MAX_MEDIA_ARCHIVE_BYTES
                    else "MEDIA_TOO_LARGE")
            message = ("单次请求媒体归档总量超过 64MB" if code == "MEDIA_TOTAL_TOO_LARGE"
                       else "媒体文件超过 20MB")
            raise Source2IntegrationError(413, code, message)
    return bytes(chunks)


def _media_archive_size(archive: Mapping[str, Any]) -> int:
    """Read the authenticated media size, including legacy envelopes."""
    size = archive.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= MAX_MEDIA_ARCHIVE_BYTES:
        return size
    encoded = archive.get("contentBase64")
    if isinstance(encoded, str):
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise Source2IntegrationError(
                500, "ARCHIVE_INVALID", "媒体归档内容格式错误") from exc
        if len(decoded) <= MAX_MEDIA_ARCHIVE_BYTES:
            return len(decoded)
    raise Source2IntegrationError(500, "ARCHIVE_INVALID", "媒体归档大小字段无效")


def _normalized_media_hosts(values: Any) -> set:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise Source2IntegrationError(
            503, "MEDIA_HOST_ALLOWLIST_INVALID", "媒体主机白名单配置格式错误")
    if len(values) > MAX_MEDIA_HOST_ALLOWLIST_ITEMS:
        raise Source2IntegrationError(
            503, "MEDIA_HOST_ALLOWLIST_INVALID", "媒体主机白名单配置项过多")
    hosts = set()
    for value in values:
        if not isinstance(value, str):
            raise Source2IntegrationError(
                503, "MEDIA_HOST_ALLOWLIST_INVALID", "媒体主机白名单配置格式错误")
        host = value.strip().rstrip(".").lower()
        if (not host or len(host) > 253 or not _MEDIA_HOST_PATTERN.fullmatch(host)):
            raise Source2IntegrationError(
                503, "MEDIA_HOST_ALLOWLIST_INVALID", "媒体主机白名单配置格式错误")
        try:
            parsed = urlparse("https://" + host)
            port = parsed.port
        except ValueError as exc:
            raise Source2IntegrationError(
                503, "MEDIA_HOST_ALLOWLIST_INVALID", "媒体主机白名单配置格式错误") from exc
        if (parsed.hostname or "").rstrip(".").lower() != host or port is not None:
            raise Source2IntegrationError(
                503, "MEDIA_HOST_ALLOWLIST_INVALID", "媒体主机白名单只能配置主机名")
        hosts.add(host)
    if not hosts:
        raise Source2IntegrationError(
            503, "MEDIA_HOST_ALLOWLIST_EMPTY", "医疗媒体主机白名单不能为空")
    return hosts


def _validate_media_endpoint(url: str, allowlist: set, error_code: str) -> None:
    try:
        parsed = urlparse(str(url or ""))
        port = parsed.port
    except ValueError as exc:
        raise Source2IntegrationError(
            400, error_code, "媒体地址格式不合法") from exc
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (parsed.scheme.lower() != "https" or not hostname or hostname not in allowlist
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443)):
        raise Source2IntegrationError(
            400, error_code, "媒体地址不是允许的 HTTPS 主机")


class _AllowlistedMediaRedirectHandler(HTTPRedirectHandler):
    """Validate every redirect target before urllib opens the next connection."""

    def __init__(self, allowlist: set):
        super().__init__()
        self.allowlist = allowlist

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_media_endpoint(newurl, self.allowlist, "MEDIA_REDIRECT_NOT_ALLOWED")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_media_url(request: Request, allowlist: set, timeout: int):
    opener = build_opener(_AllowlistedMediaRedirectHandler(allowlist))
    return opener.open(request, timeout=timeout)


def _matches_content_type(content_type: str, allowed_types: Iterable[Any]) -> bool:
    for candidate in allowed_types:
        value = str(candidate or "").strip().lower()
        if value.endswith("/*") and content_type.startswith(value[:-1]):
            return True
        if hmac.compare_digest(value, content_type):
            return True
    return False


def _media_urls(payload: Optional[Mapping[str, Any]]) -> List[str]:
    """Return the explicitly supported Source2 media fields in stable order."""
    if not isinstance(payload, Mapping):
        return []
    candidates: List[Any] = []
    message = payload.get("message")
    if isinstance(message, Mapping):
        candidates.append(message.get("mediaUrl"))
    context = payload.get("context")
    if isinstance(context, Mapping):
        recent_messages = context.get("recentMessages")
        if isinstance(recent_messages, list):
            for recent_message in recent_messages:
                if isinstance(recent_message, Mapping):
                    candidates.append(recent_message.get("mediaUrl"))
        record = context.get("authorizedRecord")
        if isinstance(record, Mapping):
            attachments = record.get("attachments")
            if isinstance(attachments, list):
                for attachment in attachments:
                    if isinstance(attachment, Mapping):
                        candidates.append(attachment.get("mediaUrl"))
    result: List[str] = []
    seen = set()
    for candidate in candidates:
        value = str(candidate or "").strip()
        identity = _stable_media_identity(value) if value else ""
        if value and identity not in seen:
            seen.add(identity)
            result.append(value)
    if len(result) > MAX_MEDIA_ARCHIVES_PER_REQUEST:
        raise Source2IntegrationError(413, "TOO_MANY_MEDIA_ITEMS",
                                      "单次请求媒体数量超过限制")
    return result


def _media_archive_keys(idempotency_key: str,
                        archive: Optional[Mapping[str, Any]]) -> List[str]:
    """Derive media archive keys from the encrypted request, including legacy key 0."""
    payload = archive.get("request") if isinstance(archive, Mapping) else None
    count = len(_media_urls(payload)) if isinstance(payload, Mapping) else 0
    # Always include the legacy first-media key so pre-index archives can still be
    # rotated or deleted even when their request envelope predates media discovery.
    count = max(1, count)
    return [idempotency_key + ":media" if index == 0
            else f"{idempotency_key}:media:{index}" for index in range(count)]


def _knowledge_chunks(value: str) -> Iterable[Tuple[int, str]]:
    if len(value) <= KNOWLEDGE_CHUNK_CHARS:
        yield 0, value
        return
    start = 0
    index = 0
    while start < len(value):
        maximum_end = min(len(value), start + KNOWLEDGE_CHUNK_CHARS)
        end = maximum_end
        if maximum_end < len(value):
            minimum_boundary = start + KNOWLEDGE_CHUNK_CHARS // 2
            boundary = max(value.rfind("\n", minimum_boundary, maximum_end),
                           value.rfind("。", minimum_boundary, maximum_end))
            if boundary >= minimum_boundary:
                end = boundary + 1
        chunk = value[start:end].strip()
        if chunk:
            yield index, chunk
            index += 1
        if end >= len(value):
            break
        start = max(start + 1, end - KNOWLEDGE_CHUNK_OVERLAP_CHARS)


def _relevant_corpus(manifest: Mapping[str, Any], text: str, limit: int = 8) -> List[Dict[str, Any]]:
    query_chars = set(re.sub(r"[\W_]+", "", text.lower()))
    if not query_chars:
        return []
    ranked = []
    for item in manifest.get("items", []):
        if not isinstance(item, Mapping):
            continue
        answer = str(item.get("answer") or item.get("content") or "")
        chunks = (_knowledge_chunks(answer) if str(item.get("type", "FAQ")).upper() == "DOCUMENT"
                  else [(0, answer)])
        for chunk_index, chunk in chunks:
            haystack = f"{item.get('title', '')}{item.get('question', '')}{chunk}".lower()
            score = len(query_chars.intersection(set(haystack))) / max(len(query_chars), 1)
            if score >= MIN_CORPUS_RELEVANCE:
                ranked.append((score, str(item.get("id")), chunk_index, item, chunk))
    ranked.sort(key=lambda value: (-value[0], value[1], value[2]))
    result = []
    for score, _item_id, chunk_index, item, chunk in ranked[:limit]:
        relevant = dict(item)
        relevant["answer"] = chunk
        relevant.pop("content", None)
        relevant["chunkIndex"] = chunk_index
        relevant["relevance"] = round(score, 4)
        result.append(relevant)
    return result


def _extract_model_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        for key in ("content", "text", "answer", "reply"):
            value = response.get(key)
            if isinstance(value, str):
                return value
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            return _extract_model_text(choices[0])
        message = response.get("message")
        if message is not None:
            return _extract_model_text(message)
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    raise Source2IntegrationError(502, "INVALID_MODEL_RESPONSE", "模型未返回可解析内容")


def _parse_model_result(text: str) -> Dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    raw = fenced.group(1) if fenced else text.strip()
    try:
        value = parse_json_object(raw.encode("utf-8"))
    except (UnicodeEncodeError, Source2IntegrationError) as exc:
        raise Source2IntegrationError(422, "INVALID_MODEL_RESPONSE", "模型未返回合法 JSON") from exc
    if any(key not in _MODEL_RESULT_FIELDS for key in value):
        # Never include an untrusted model key in the error. Provider output can
        # contain medical content and this exception crosses the HTTP boundary.
        raise Source2IntegrationError(422, "INVALID_MODEL_FIELDS", "模型结果包含未允许字段")
    if value.get("decision") not in ("REPLY", "HANDOFF"):
        raise Source2IntegrationError(422, "INVALID_MODEL_DECISION", "模型决策值无效")
    raw_confidence = value.get("confidence")
    if (isinstance(raw_confidence, bool)
            or not isinstance(raw_confidence, (int, float))):
        raise Source2IntegrationError(422, "INVALID_MODEL_CONFIDENCE", "模型置信度无效")
    confidence = float(raw_confidence)
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        raise Source2IntegrationError(422, "INVALID_MODEL_CONFIDENCE", "模型置信度无效")
    reply_text = value.get("replyText", "")
    if not isinstance(reply_text, str) or len(reply_text) > 4000:
        raise Source2IntegrationError(422, "INVALID_MODEL_REPLY", "模型回复正文无效")
    if value["decision"] == "HANDOFF" and reply_text.strip():
        raise Source2IntegrationError(422, "INVALID_MODEL_REPLY", "转人工结果不得携带回复正文")
    reason_code = value.get("reasonCode", "")
    if not isinstance(reason_code, str) or len(reason_code) > 64:
        raise Source2IntegrationError(422, "INVALID_MODEL_REASON", "模型原因码无效")
    citation_ids = value.get("citationIds", [])
    if not isinstance(citation_ids, list) or len(citation_ids) > 20:
        raise Source2IntegrationError(422, "INVALID_MODEL_CITATIONS", "模型引用列表无效")
    for citation_id in citation_ids:
        if (isinstance(citation_id, bool)
                or not isinstance(citation_id, (str, int))
                or not str(citation_id).strip()
                or len(str(citation_id)) > 64):
            raise Source2IntegrationError(422, "INVALID_MODEL_CITATIONS", "模型引用列表无效")
    normalized = dict(value)
    normalized["confidence"] = confidence
    normalized["replyText"] = reply_text
    normalized["reasonCode"] = reason_code
    normalized["citationIds"] = citation_ids
    return normalized


def _base_result(binding: Mapping[str, Any], request_id: str, idempotency_key: str,
                 manifest: Mapping[str, Any]) -> Dict[str, Any]:
    from agent.registry import get_agent_registry
    profile = get_agent_registry().get(str(binding["agent_id"]))
    return {"requestId": request_id, "idempotencyKey": idempotency_key,
            "model": str(profile.model or conf().get("model") or ""),
            "corpusVersion": manifest.get("version"), "policyVersion": POLICY_VERSION,
            "citations": []}


def chat_completion(binding: Mapping[str, Any], payload: Mapping[str, Any],
                    idempotency_key: str, model_call=None) -> Dict[str, Any]:
    _validate_chat_request(payload, idempotency_key)
    lock_handle = _acquire_idempotency_lock(binding, idempotency_key)
    try:
        return _chat_completion_locked(binding, payload, idempotency_key, model_call)
    finally:
        _release_idempotency_lock(lock_handle)


def _chat_completion_locked(binding: Mapping[str, Any], payload: Mapping[str, Any],
                            idempotency_key: str, model_call=None) -> Dict[str, Any]:
    tenant_id = str(payload.get("tenantId", ""))
    agent_id = str(payload.get("agentId", ""))
    if tenant_id != str(binding["tenant_id"]) or agent_id != str(binding["agent_id"]):
        raise Source2IntegrationError(403, "BINDING_MISMATCH", "租户或 Agent 绑定不匹配")
    expected_model_profile = str(binding.get("model_profile") or "").strip()
    requested_model_profile = str(payload.get("modelProfile") or "").strip()
    if expected_model_profile and requested_model_profile != expected_model_profile:
        raise Source2IntegrationError(403, "MODEL_PROFILE_MISMATCH", "商业模型配置标识不匹配")
    request_hash = _stable_request_hash(payload)
    archived = _read_archive(binding, idempotency_key)
    if archived:
        archived_request = archived.get("request")
        archived_semantic_hash = (_stable_request_hash(archived_request)
                                  if isinstance(archived_request, Mapping) else None)
        if (archived.get("requestHash") != request_hash
                and archived_semantic_hash != request_hash):
            raise Source2IntegrationError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他请求")
        if isinstance(archived_request, Mapping):
            safe_archived_request = _archive_safe_request(archived_request)
            if safe_archived_request != archived_request:
                archived = dict(archived)
                archived["request"] = safe_archived_request
                archived["requestHash"] = archived_semantic_hash
                _encrypt_archive(binding, idempotency_key, archived)
        archived_response = archived.get("response")
        if archived_response is not None:
            if not isinstance(archived_response, Mapping):
                raise Source2IntegrationError(500, "ARCHIVE_INVALID", "加密归档响应格式错误")
            return dict(archived_response)

    manifest = _read_manifest(agent_id, binding)
    requested_corpus_version = str(payload.get("corpusVersion") or "").strip()
    active_corpus_version = str(manifest.get("version") or "").strip()
    if requested_corpus_version != active_corpus_version:
        raise Source2IntegrationError(
            409, "CORPUS_VERSION_MISMATCH",
            "请求语料版本与 Agent 当前版本不一致，请重新同步语料")

    message = payload["message"]
    message_type = str(message.get("type", "text")).lower()
    text = str(message.get("content") or message.get("transcript") or "").strip()
    media_url = str(message.get("mediaUrl") or "").strip()
    if message_type == "text" and not text:
        raise Source2IntegrationError(400, "EMPTY_MESSAGE", "消息内容不能为空")
    discovered_media = _media_urls(payload)
    # Persist an encrypted request index before any fallible media download or
    # model call. An interrupted attempt is resumable and emergency deletion can
    # still discover every media key from the request payload.
    archived_request = _archive_safe_request(payload)
    _encrypt_archive(binding, idempotency_key,
                     {"requestHash": request_hash, "request": archived_request,
                      "state": "PROCESSING", "archivedAt": int(time.time())})
    archived_media_bytes = 0
    for index, discovered_url in enumerate(discovered_media):
        archive_key = (idempotency_key + ":media" if index == 0
                       else f"{idempotency_key}:media:{index}")
        # A retry after a partial failure must not fetch already archived
        # short-lived URLs again; authenticate the existing envelope instead.
        existing_media = _read_archive(binding, archive_key)
        if existing_media is not None:
            archived_media_bytes += _media_archive_size(existing_media)
            if archived_media_bytes > MAX_MEDIA_ARCHIVE_BYTES_PER_REQUEST:
                raise Source2IntegrationError(
                    413, "MEDIA_TOTAL_TOO_LARGE", "单次请求媒体归档总量超过 64MB")
            continue
        remaining_bytes = MAX_MEDIA_ARCHIVE_BYTES_PER_REQUEST - archived_media_bytes
        if remaining_bytes <= 0:
            raise Source2IntegrationError(
                413, "MEDIA_TOTAL_TOO_LARGE", "单次请求媒体归档总量超过 64MB")
        downloaded_bytes = _archive_remote_media(
            binding, archive_key, discovered_url, remaining_bytes)
        # The production downloader always returns the persisted byte count.
        # Accept None only for legacy/test adapters that predate this contract.
        if downloaded_bytes is not None:
            if (not isinstance(downloaded_bytes, int) or isinstance(downloaded_bytes, bool)
                    or downloaded_bytes < 0 or downloaded_bytes > remaining_bytes):
                raise Source2IntegrationError(
                    413, "MEDIA_TOTAL_TOO_LARGE", "单次请求媒体归档总量超过 64MB")
            archived_media_bytes += downloaded_bytes
    request_id = str(payload["requestId"])
    result = _base_result(binding, request_id, idempotency_key, manifest)
    context = payload.get("context")
    emergency_context = (context.get("emergency")
                         if isinstance(context, Mapping) else None)

    if isinstance(emergency_context, Mapping):
        # The structured emergency relationship is authoritative even when the
        # current text resembles a harmless service FAQ. Emergency sessions
        # always require a clinician and must never enter the reply model.
        result.update(decision="HANDOFF", replyText="", confidence=1.0,
                      reasonCode="MEDICAL_RISK")
    elif message_type != "text":
        result.update(decision="HANDOFF", replyText="", confidence=1.0,
                      reasonCode="MEDIA_REQUIRES_DOCTOR_REVIEW")
    elif _HUMAN_HANDOFF_RE.search(text):
        result.update(decision="HANDOFF", replyText="", confidence=1.0,
                      reasonCode="USER_REQUESTED_HUMAN")
    elif _PROMPT_INJECTION_RE.search(text):
        result.update(decision="HANDOFF", replyText="", confidence=1.0,
                      reasonCode="PROMPT_INJECTION")
    elif _MEDICAL_RISK_RE.search(text):
        result.update(decision="HANDOFF", replyText="", confidence=1.0,
                      reasonCode="MEDICAL_RISK")
    elif not manifest.get("items"):
        result.update(decision="HANDOFF", replyText="", confidence=1.0,
                      reasonCode="OUT_OF_CORPUS")
    else:
        corpus = _relevant_corpus(manifest, text)
        if not corpus:
            result.update(decision="HANDOFF", replyText="", confidence=1.0,
                          reasonCode="OUT_OF_CORPUS")
        elif any((_MEDICAL_RISK_RE.search(
                f"{item.get('title', '')} {item.get('question', '')} {item.get('answer', '')}")
                or _PROMPT_INJECTION_RE.search(
                f"{item.get('title', '')} {item.get('question', '')} {item.get('answer', '')}"))
                 for item in corpus):
            # Tenant-authored knowledge is untrusted policy input. A service
            # question must not make medical advice or prompt-injection text
            # eligible for model input merely because the item was published.
            result.update(decision="HANDOFF", replyText="", confidence=1.0,
                          reasonCode="POLICY_BLOCKED")
        else:
            if model_call is None:
                from agent.protocol.models import LLMRequest
                from agent.registry import get_agent_registry
                from bridge.agent_bridge import AgentLLMModel
                from bridge.bridge import Bridge
                session_id = f"source2:{tenant_id}:{payload.get('conversationId', request_id)}"
                profile = get_agent_registry().get(agent_id)
                # Medical requests use a purpose-built model adapter, not a full Agent.
                # This prevents tool loading, schedulers, memory writes and self-evolution.
                medical_model = AgentLLMModel(
                    Bridge(), model_name=profile.model,
                    bot_type_override=profile.bot_type)
                medical_model.channel_type = "source2_medical"
                medical_model.session_id = session_id
                medical_model.agent_id = agent_id
                def model_call(system: str, user: str):
                    return medical_model.call(LLMRequest(
                        messages=[{"role": "user", "content": user}], system=system,
                        tools=[], temperature=0.1, max_tokens=800, stream=False))
            system = (
                "你是口腔机构服务客服分流器。只可依据给定机构语料回答挂号、营业时间、"
                "地址、费用说明、医保流程和材料准备。不得诊断、给出治疗或用药建议；"
                "任何医疗风险、信息不足或低置信度必须 HANDOFF。仅输出 JSON："
                '{"decision":"REPLY|HANDOFF","replyText":"",'
                '"confidence":0.0,"reasonCode":"CORPUS_MATCH|OUT_OF_CORPUS|'
                'LOW_CONFIDENCE|POLICY_BLOCKED","citationIds":[]}。'
            )
            # First-phase automatic replies are institution-service questions.
            # Patient/record context is retained in the tenant's encrypted
            # archive, but is not needed by or disclosed to the commercial LLM.
            user = _canonical_json({"question": text, "corpus": corpus}).decode("utf-8")
            decision = _parse_model_result(_extract_model_text(model_call(system, user)))
            confidence = max(0.0, min(1.0, float(decision.get("confidence", 0))))
            reply_text = str(decision.get("replyText") or "").strip()
            if decision["decision"] == "REPLY" and (confidence < 0.7 or not reply_text):
                decision.update(decision="HANDOFF", replyText="", reasonCode="LOW_CONFIDENCE")
            elif (decision["decision"] == "REPLY"
                  and (_MEDICAL_RISK_RE.search(reply_text)
                       or _PROMPT_INJECTION_RE.search(reply_text))):
                decision.update(decision="HANDOFF", replyText="", reasonCode="POLICY_BLOCKED")
            elif decision["decision"] == "REPLY":
                decision["reasonCode"] = "CORPUS_MATCH"
            elif decision.get("reasonCode") not in {
                    "OUT_OF_CORPUS", "LOW_CONFIDENCE", "POLICY_BLOCKED"}:
                decision["reasonCode"] = "POLICY_BLOCKED"
            allowed_ids = {str(item.get("id")): item for item in corpus}
            citations = []
            seen_citation_ids = set()
            if decision["decision"] == "REPLY":
                for citation_id in decision.get("citationIds", []):
                    normalized_id = str(citation_id)
                    item = allowed_ids.get(normalized_id)
                    if item and normalized_id not in seen_citation_ids:
                        citations.append({"id": item.get("id"), "title": item.get("title")})
                        seen_citation_ids.add(normalized_id)
            if decision["decision"] == "REPLY" and not citations:
                # A high model confidence is not evidence of grounding. Automatic
                # replies require at least one citation from the exact retrieved
                # corpus subset; missing or invented IDs must go to a doctor.
                decision.update(decision="HANDOFF", replyText="", reasonCode="OUT_OF_CORPUS")
            result.update(decision=decision["decision"], replyText=str(decision.get("replyText") or ""),
                          confidence=confidence, reasonCode=str(decision.get("reasonCode") or ""),
                          citations=citations)

    _encrypt_archive(binding, idempotency_key,
                     {"requestHash": request_hash, "request": archived_request,
                      "response": result, "state": "COMPLETED",
                      "archivedAt": int(time.time())})
    return result


def _validate_chat_request(payload: Mapping[str, Any], idempotency_key: str) -> None:
    if not isinstance(payload, Mapping):
        raise Source2IntegrationError(400, "INVALID_JSON_OBJECT", "请求 JSON 必须是对象")
    if len(_canonical_json(payload)) > MAX_CHAT_PAYLOAD_BYTES:
        raise Source2IntegrationError(413, "REQUEST_TOO_LARGE", "聊天请求超过大小限制")
    _reject_unknown_fields(payload, _CHAT_REQUEST_FIELDS, "request")
    request_id = payload.get("requestId")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise Source2IntegrationError(400, "INVALID_REQUEST_ID", "requestId 格式错误")
    for field, maximum in (("tenantId", 64), ("agentId", 64)):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise Source2IntegrationError(
                400, "INVALID_CHAT_REQUEST", f"{field} 格式错误")
    # These fields are optional for pre-corpus risk handoff and for bindings
    # that do not pin a named model profile. When present they still must obey
    # the JSON string contract; numbers and booleans are never coerced.
    for field, maximum in (("modelProfile", 64), ("corpusVersion", 64)):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise Source2IntegrationError(
                400, "INVALID_CHAT_REQUEST", f"{field} 格式错误")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
            or len(idempotency_key) > 200:
        raise Source2IntegrationError(400, "INVALID_IDEMPOTENCY_KEY", "幂等键格式错误")
    message = payload.get("message")
    if not isinstance(message, Mapping):
        raise Source2IntegrationError(400, "INVALID_MESSAGE", "message 必须是对象")
    _reject_unknown_fields(message, _MESSAGE_FIELDS, "message")
    for field, maximum in (("content", 10000), ("transcript", 10000),
                           ("mediaUrl", 4096), ("type", 32)):
        value = message.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise Source2IntegrationError(400, "INVALID_MESSAGE", f"message.{field} 格式错误")
    context = payload.get("context", {})
    if not isinstance(context, Mapping):
        raise Source2IntegrationError(400, "INVALID_CONTEXT", "context 必须是对象")
    _validate_context_schema(context)
    # Enforce the aggregate deduplicated media limit before lock acquisition or
    # any remote download, not just each individual context collection limit.
    _media_urls(payload)
    conversation_id = payload.get("conversationId")
    if conversation_id is not None and (
            not isinstance(conversation_id, str) or len(conversation_id) > 512):
        raise Source2IntegrationError(400, "INVALID_CONVERSATION_ID", "conversationId 格式错误")


def _reject_unknown_fields(value: Mapping[str, Any], allowed: frozenset,
                           path: str) -> None:
    unexpected = sorted(str(key) for key in value.keys() if key not in allowed)
    if unexpected:
        raise Source2IntegrationError(
            400, "UNEXPECTED_FIELD", f"{path} 包含未允许字段: {', '.join(unexpected)}")


def _validate_context_schema(context: Mapping[str, Any]) -> None:
    _reject_unknown_fields(context, _CONTEXT_FIELDS, "context")
    schema_version = context.get("schemaVersion")
    if schema_version is not None and schema_version != "source2-medical-context-v1":
        raise Source2IntegrationError(400, "INVALID_CONTEXT_SCHEMA", "医疗上下文版本不受支持")
    _validate_optional_context_object(context.get("patient"), _PATIENT_FIELDS,
                                      "context.patient")
    _validate_optional_context_object(context.get("familyMember"), _FAMILY_MEMBER_FIELDS,
                                      "context.familyMember")
    _validate_optional_context_object(context.get("doctor"), _DOCTOR_FIELDS,
                                      "context.doctor")
    _validate_optional_context_object(context.get("emergency"), _EMERGENCY_FIELDS,
                                      "context.emergency")

    appointments = _validate_context_list(
        context.get("appointments", []), MAX_CONTEXT_APPOINTMENTS, "context.appointments")
    for index, appointment in enumerate(appointments):
        _validate_required_context_object(
            appointment, _APPOINTMENT_FIELDS, f"context.appointments[{index}]")

    recent_messages = _validate_context_list(
        context.get("recentMessages", []), MAX_CONTEXT_RECENT_MESSAGES,
        "context.recentMessages")
    for index, message in enumerate(recent_messages):
        path = f"context.recentMessages[{index}]"
        _validate_required_context_object(message, _RECENT_MESSAGE_FIELDS, path)
        _validate_limited_string(message.get("content"), 10000, f"{path}.content")
        _validate_limited_string(message.get("transcript"), 10000, f"{path}.transcript")
        _validate_limited_string(message.get("mediaUrl"), 4096, f"{path}.mediaUrl")

    record = context.get("authorizedRecord")
    if record is not None:
        _validate_required_context_object(record, _RECORD_FIELDS, "context.authorizedRecord")
        attachments = _validate_context_list(
            record.get("attachments", []), MAX_MEDIA_ARCHIVES_PER_REQUEST,
            "context.authorizedRecord.attachments")
        for index, attachment in enumerate(attachments):
            path = f"context.authorizedRecord.attachments[{index}]"
            _validate_required_context_object(attachment, _ATTACHMENT_FIELDS, path)
            _validate_limited_string(attachment.get("mediaUrl"), 4096, f"{path}.mediaUrl")

    _validate_context_text_lengths(context, "context")


def _validate_optional_context_object(value: Any, allowed: frozenset, path: str) -> None:
    if value is not None:
        _validate_required_context_object(value, allowed, path)


def _validate_required_context_object(value: Any, allowed: frozenset, path: str) -> None:
    if not isinstance(value, Mapping):
        raise Source2IntegrationError(400, "INVALID_CONTEXT", f"{path} 必须是对象")
    _reject_unknown_fields(value, allowed, path)


def _validate_context_list(value: Any, maximum: int, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise Source2IntegrationError(400, "INVALID_CONTEXT", f"{path} 必须是数组")
    if len(value) > maximum:
        raise Source2IntegrationError(400, "INVALID_CONTEXT", f"{path} 数量超过限制")
    return value


def _validate_limited_string(value: Any, maximum: int, path: str) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > maximum):
        raise Source2IntegrationError(400, "INVALID_CONTEXT", f"{path} 格式错误")


def _validate_context_text_lengths(value: Any, path: str) -> None:
    if isinstance(value, str):
        if len(value) > MAX_CONTEXT_TEXT_CHARS:
            raise Source2IntegrationError(400, "INVALID_CONTEXT", f"{path} 文本超过限制")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_context_text_lengths(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_context_text_lengths(child, f"{path}[{index}]")


def clear_nonce_cache() -> None:
    """Test helper; production replay entries expire automatically."""
    with _NONCE_LOCK:
        _NONCES.clear()
