import hashlib
import hmac
import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "source2_medical_preflight.py"
_SPEC = importlib.util.spec_from_file_location("source2_medical_preflight", _SCRIPT)
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)


def test_signature_matches_java_and_cowagent_cross_language_vector():
    body = (b'{"tenantId":"10","version":"v1","checksum":"empty",'
            b'"items":[]}')

    assert preflight.build_signature(
        "PUT", "/api/integrations/source2/v1/knowledge/clinic-10", "10",
        "1788962400", "0123456789abcdef0123456789abcdef", "corpus-v1",
        body, "source2-cross-language-secret-32-bytes",
    ) == "734ac4b6428a06a030391a3df39a3b725c6e254ad82619777038160d79963c74"


def test_response_media_type_requires_json_and_utf8_without_echoing_header():
    preflight.validate_json_response_content_type(
        'Application/JSON; charset="UTF-8"; profile=health')

    for value in (None, "text/html; patient=张三", "application/json; charset=utf-16",
                  "application/json; charset=utf-8; charset=utf-8"):
        with pytest.raises(preflight.PreflightError) as exc_info:
            preflight.validate_json_response_content_type(value)
        assert "张三" not in str(exc_info.value)
        assert "text/html" not in str(exc_info.value)


@pytest.mark.parametrize("value", [None, "", "identity", " Identity "])
def test_response_content_encoding_accepts_only_identity(value):
    assert preflight.validate_identity_response_content_encoding(value) is None


@pytest.mark.parametrize("value", ["gzip", "br", "deflate", "identity, identity"])
def test_response_content_encoding_rejects_encoded_or_ambiguous_body(value):
    with pytest.raises(preflight.PreflightError, match="Content-Encoding"):
        preflight.validate_identity_response_content_encoding(value)


def test_strict_health_json_rejects_duplicates_non_finite_and_non_object():
    for raw in (b'{"status":"UP","status":"DOWN"}',
                b'{"timestamp":NaN}', b'[]'):
        with pytest.raises(preflight.PreflightError):
            preflight._strict_json_object(raw)


@pytest.mark.parametrize("url", [
    "http://cowagent.example.com",
    "https://user:secret@cowagent.example.com",
    "https://cowagent.example.com/proxy",
    "https://cowagent.example.com?token=secret",
])
def test_normalize_base_url_rejects_unsafe_endpoint(url):
    with pytest.raises(preflight.PreflightError):
        preflight.normalize_base_url(url)


def test_validate_health_rejects_binding_and_governance_mismatch():
    valid = {
        "status": "UP", "tenantId": "10", "agentId": "clinic-10",
        "modelProfile": "medical-prod", "corpusVersion": "v1",
        "encryptionKeyId": "kms/source2/10/v2",
        "mediaHostAllowlist": ["api.example.com"],
        "policyVersion": "medical-service-v1",
        "complianceApprovalRef": "PRIVACY-2026-001", "externalSecrets": True,
        "timestamp": 1000,
    }
    assert preflight.validate_health(
        valid, "10", "clinic-10", "medical-prod", "v1",
        "PRIVACY-2026-001", "api.example.com", now=1000)["status"] == "UP"

    for field, invalid_value in (
            ("tenantId", "20"), ("agentId", "clinic-20"),
            ("policyVersion", "unsafe-policy"), ("encryptionKeyId", "")):
        invalid = dict(valid, **{field: invalid_value})
        with pytest.raises(preflight.PreflightError):
            preflight.validate_health(
                invalid, "10", "clinic-10", "medical-prod", "v1",
                "PRIVACY-2026-001", "api.example.com", now=1000)

    with pytest.raises(preflight.PreflightError, match="未允许字段"):
        preflight.validate_health(
            dict(valid, untrustedExtra="patient content"), "10", "clinic-10",
            "medical-prod", "v1", "PRIVACY-2026-001", "api.example.com",
            now=1000)


@pytest.mark.parametrize("field,invalid_value", [
    ("tenantId", 10),
    ("agentId", True),
    ("modelProfile", 2026),
    ("corpusVersion", False),
    ("encryptionKeyId", 10),
    ("policyVersion", 1),
    ("complianceApprovalRef", True),
    ("timestamp", "1000"),
    ("timestamp", 1000.0),
])
def test_validate_health_rejects_implicit_scalar_coercion(field, invalid_value):
    response = {
        "status": "UP", "tenantId": "10", "agentId": "clinic-10",
        "modelProfile": "medical-prod", "corpusVersion": "v1",
        "encryptionKeyId": "kms/source2/10/v2",
        "mediaHostAllowlist": ["api.example.com"],
        "policyVersion": "medical-service-v1",
        "complianceApprovalRef": "PRIVACY-2026-001", "externalSecrets": True,
        "timestamp": 1000,
    }
    response[field] = invalid_value

    with pytest.raises(preflight.PreflightError):
        preflight.validate_health(
            response, "10", "clinic-10", "medical-prod", "v1",
            "PRIVACY-2026-001", "api.example.com", now=1000)


@pytest.mark.parametrize("media_hosts", [
    None, [], "api.example.com", ["https://api.example.com"],
    ["api.example.com:443"], [10], ["API.example.com"],
    ["api.example.com", "api.example.com"],
])
def test_validate_health_rejects_invalid_media_host_allowlist(media_hosts):
    response = {
        "status": "UP", "tenantId": "10", "agentId": "clinic-10",
        "modelProfile": "medical-prod", "corpusVersion": "v1",
        "encryptionKeyId": "kms/source2/10/v2",
        "mediaHostAllowlist": media_hosts,
        "policyVersion": "medical-service-v1",
        "complianceApprovalRef": "PRIVACY-2026-001", "externalSecrets": True,
        "timestamp": 1000,
    }

    with pytest.raises(preflight.PreflightError):
        preflight.validate_health(
            response, "10", "clinic-10", "medical-prod", "v1",
            "PRIVACY-2026-001", "api.example.com", now=1000)


def test_validate_health_rejects_proxy_host_not_in_cowagent_allowlist():
    response = {
        "status": "UP", "tenantId": "10", "agentId": "clinic-10",
        "modelProfile": "medical-prod", "corpusVersion": "v1",
        "encryptionKeyId": "kms/source2/10/v2",
        "mediaHostAllowlist": ["other.example.com"],
        "policyVersion": "medical-service-v1",
        "complianceApprovalRef": "PRIVACY-2026-001", "externalSecrets": True,
        "timestamp": 1000,
    }

    with pytest.raises(preflight.PreflightError, match="不在 CowAgent 白名单"):
        preflight.validate_health(
            response, "10", "clinic-10", "medical-prod", "v1",
            "PRIVACY-2026-001", "api.example.com", now=1000)


def test_validate_health_rejects_unexpected_active_encryption_key_version():
    response = {
        "status": "UP", "tenantId": "10", "agentId": "clinic-10",
        "modelProfile": "medical-prod", "corpusVersion": "v1",
        "encryptionKeyId": "kms/source2/10/v1",
        "mediaHostAllowlist": ["api.example.com"],
        "policyVersion": "medical-service-v1",
        "complianceApprovalRef": "PRIVACY-2026-001", "timestamp": 1000,
    }

    with pytest.raises(preflight.PreflightError, match="密钥版本不匹配"):
        preflight.validate_health(
            response, "10", "clinic-10", "medical-prod", "v1",
            "PRIVACY-2026-001", "api.example.com", now=1000,
            expected_encryption_key_id="kms/source2/10/v2")


def test_validate_health_requires_external_secret_enforcement():
    response = {
        "status": "UP", "tenantId": "10", "agentId": "clinic-10",
        "modelProfile": "medical-prod", "corpusVersion": "v1",
        "encryptionKeyId": "kms/source2/10/v2",
        "mediaHostAllowlist": ["api.example.com"],
        "policyVersion": "medical-service-v1",
        "complianceApprovalRef": "PRIVACY-2026-001", "externalSecrets": False,
        "timestamp": 1000,
    }

    with pytest.raises(preflight.PreflightError, match="外部注入"):
        preflight.validate_health(
            response, "10", "clinic-10", "medical-prod", "v1",
            "PRIVACY-2026-001", "api.example.com", now=1000,
            expected_encryption_key_id="kms/source2/10/v2")


@pytest.mark.parametrize("field,value", [
    ("tenant_id", ""),
    ("tenant_id", "t" * 65),
    ("app_key", ""),
    ("app_key", "a" * 129),
    ("timeout_seconds", 0),
    ("timeout_seconds", 61),
])
def test_probe_health_rejects_invalid_request_bounds_before_network(field, value):
    arguments = {
        "base_url": "https://cowagent.example.com",
        "tenant_id": "10",
        "agent_id": "clinic-10",
        "app_key": "app-10",
        "secret": "source2-app-secret-10-32-bytes!!",
        "timeout_seconds": 10,
    }
    arguments[field] = value

    with pytest.raises(preflight.PreflightError):
        preflight.probe_health(**arguments)


def test_probe_health_uses_signed_empty_body_and_validates_live_response():
    tenant_id = "10"
    app_key = "app-10"
    agent_id = "clinic-10"
    secret = "source2-app-secret-10-32-bytes!!"
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            captured["path"] = self.path
            captured["headers"] = dict(self.headers)
            response = {
                "status": "UP", "tenantId": tenant_id, "agentId": agent_id,
                "modelProfile": "medical-prod", "corpusVersion": "v1",
                "encryptionKeyId": "kms/source2/10/v2",
                "mediaHostAllowlist": ["api.example.com"],
                "policyVersion": "medical-service-v1",
            "complianceApprovalRef": "PRIVACY-2026-001",
            "externalSecrets": True,
            "timestamp": int(time.time()),
            }
            body = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = preflight.probe_health(
            f"http://127.0.0.1:{server.server_port}", tenant_id, agent_id,
            app_key, secret, allow_insecure_localhost=True,
            expected_model_profile="medical-prod", expected_corpus_version="v1",
            expected_compliance_ref="PRIVACY-2026-001",
            expected_media_host="api.example.com",
            expected_encryption_key_id="kms/source2/10/v2")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    headers = captured["headers"]
    canonical = "\n".join((
        "GET", preflight.API_PATH, tenant_id, headers["X-Timestamp"],
        headers["X-Nonce"], headers["Idempotency-Key"],
        hashlib.sha256(b"").hexdigest(),
    ))
    expected_signature = hmac.new(
        secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert captured["path"] == preflight.API_PATH
    assert headers["X-App-Key"] == app_key
    assert headers["Accept-Encoding"] == "identity"
    assert hmac.compare_digest(headers["X-Signature"], expected_signature)
    assert result["encryptionKeyId"] == "kms/source2/10/v2"


def test_probe_health_rejects_encoded_response_before_body_read():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"untrusted":"patient-content"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(preflight.PreflightError, match="Content-Encoding") as exc_info:
            preflight.probe_health(
                f"http://127.0.0.1:{server.server_port}", "10", "clinic-10",
                "app-10", "source2-app-secret-10-32-bytes!!",
                allow_insecure_localhost=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "patient-content" not in str(exc_info.value)


def test_probe_health_rejects_redirect_without_forwarding_credentials():
    redirected = {"requested": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == preflight.API_PATH:
                self.send_response(302)
                self.send_header("Location", "/credential-trap")
                self.end_headers()
                return
            redirected["requested"] = True
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(preflight.PreflightError, match="HTTP 302"):
            preflight.probe_health(
                f"http://127.0.0.1:{server.server_port}", "10", "clinic-10",
                "app-10", "source2-app-secret-10-32-bytes!!",
                allow_insecure_localhost=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert redirected["requested"] is False


def test_main_requires_secret_from_environment(monkeypatch, capsys):
    monkeypatch.delenv("MISSING_SOURCE2_SECRET", raising=False)

    result = preflight.main([
        "--base-url", "https://cowagent.example.com",
        "--tenant-id", "10", "--agent-id", "clinic-10",
        "--app-key", "app-10", "--secret-env", "MISSING_SOURCE2_SECRET",
        "--expected-media-host", "api.example.com",
        "--expected-model-profile", "medical-prod",
        "--expected-corpus-version", "v1",
        "--expected-compliance-ref", "PRIVACY-2026-001",
        "--expected-encryption-key-id", "kms/source2/10/v2",
    ])

    assert result == 2
    assert "MISSING_SOURCE2_SECRET" in capsys.readouterr().err


def test_cli_requires_every_production_governance_expectation():
    required_options = [
        ("--expected-model-profile", "medical-prod"),
        ("--expected-corpus-version", "v1"),
        ("--expected-compliance-ref", "PRIVACY-2026-001"),
        ("--expected-encryption-key-id", "kms/source2/10/v2"),
    ]
    base_args = [
        "--base-url", "https://cowagent.example.com",
        "--tenant-id", "10", "--agent-id", "clinic-10",
        "--app-key", "app-10", "--secret-env", "SOURCE2_SECRET",
        "--expected-media-host", "api.example.com",
    ]
    for option, value in required_options:
        base_args.extend((option, value))

    for missing_option, _value in required_options:
        args = list(base_args)
        index = args.index(missing_option)
        del args[index:index + 2]
        with pytest.raises(SystemExit) as exc_info:
            preflight._parser().parse_args(args)
        assert exc_info.value.code == 2
