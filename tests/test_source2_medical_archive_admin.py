import hashlib
import hmac
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "source2_medical_archive_admin.py"
_SPEC = importlib.util.spec_from_file_location("source2_medical_archive_admin", _SCRIPT)
admin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(admin)


def test_signature_matches_cowagent_no_body_contract():
    path = admin.API_PREFIX + "medical-chat-201"

    actual = admin.build_signature(
        "DELETE", path, "10", "1788962400",
        "0123456789abcdef0123456789abcdef", "medical-chat-201",
        "source2-admin-secret-10-32-bytes")
    canonical = "\n".join((
        "DELETE", path, "10", "1788962400",
        "0123456789abcdef0123456789abcdef", "medical-chat-201",
        hashlib.sha256(b"").hexdigest(),
    ))
    expected = hmac.new(
        b"source2-admin-secret-10-32-bytes", canonical.encode(),
        hashlib.sha256).hexdigest()

    assert actual == expected


def test_response_media_type_requires_json_and_utf8_without_echoing_header():
    admin.validate_json_response_content_type(
        'Application/JSON; charset="UTF-8"; profile=archive')

    for value in (None, "text/html; patient=张三", "application/json; charset=utf-16",
                  "application/json; charset=utf-8; charset=utf-8"):
        with pytest.raises(admin.ArchiveAdminError) as exc_info:
            admin.validate_json_response_content_type(value)
        assert "张三" not in str(exc_info.value)
        assert "text/html" not in str(exc_info.value)


@pytest.mark.parametrize("value", [None, "", "identity", " Identity "])
def test_response_content_encoding_accepts_only_identity(value):
    assert admin.validate_identity_response_content_encoding(value) is None


@pytest.mark.parametrize("value", ["gzip", "br", "deflate", "identity, identity"])
def test_response_content_encoding_rejects_encoded_or_ambiguous_body(value):
    with pytest.raises(admin.ArchiveAdminError, match="Content-Encoding"):
        admin.validate_identity_response_content_encoding(value)


@pytest.mark.parametrize("key", ["", "../archive", "message/key", "x" * 201])
def test_operation_rejects_unsafe_idempotency_key_before_network(key):
    with pytest.raises(admin.ArchiveAdminError, match="幂等键"):
        admin.operate_archive(
            "https://cowagent.example.com", "10", "admin-10",
            "source2-admin-secret-10-32-bytes", key, "reencrypt")


def test_delete_requires_matching_sha256_before_network():
    with pytest.raises(admin.ArchiveAdminError, match="删除确认摘要"):
        admin.operate_archive(
            "https://cowagent.example.com", "10", "admin-10",
            "source2-admin-secret-10-32-bytes", "medical-chat-201", "delete",
            confirm_delete_sha256="0" * 64)


def test_delete_sends_signed_empty_request_and_returns_only_safe_fields():
    tenant_id = "10"
    app_key = "admin-10"
    secret = "source2-admin-secret-10-32-bytes"
    idempotency_key = "medical-chat-201"
    expected_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_DELETE(self):
            captured["path"] = self.path
            captured["headers"] = dict(self.headers)
            body = json.dumps({
                "status": "DELETED", "idempotencyKeyHash": expected_hash,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = admin.operate_archive(
            f"http://127.0.0.1:{server.server_port}", tenant_id, app_key,
            secret, idempotency_key, "delete",
            confirm_delete_sha256=expected_hash, allow_insecure_localhost=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    headers = captured["headers"]
    path = admin.API_PREFIX + idempotency_key
    assert captured["path"] == path
    assert headers["X-App-Key"] == app_key
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Idempotency-Key"] == idempotency_key
    assert hmac.compare_digest(
        headers["X-Signature"],
        admin.build_signature(
            "DELETE", path, tenant_id, headers["X-Timestamp"],
            headers["X-Nonce"], idempotency_key, secret))
    assert result == {"status": "DELETED", "idempotencyKeyHash": expected_hash}


def test_operation_rejects_encoded_response_before_body_read():
    idempotency_key = "medical-chat-201"

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
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
        with pytest.raises(admin.ArchiveAdminError, match="Content-Encoding") as exc_info:
            admin.operate_archive(
                f"http://127.0.0.1:{server.server_port}", "10", "admin-10",
                "source2-admin-secret-10-32-bytes", idempotency_key,
                "reencrypt", allow_insecure_localhost=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "patient-content" not in str(exc_info.value)


def test_archive_confirmation_rejects_unexpected_top_level_field():
    idempotency_key = "medical-chat-201"
    response = {
        "status": "DELETED",
        "idempotencyKeyHash": hashlib.sha256(idempotency_key.encode()).hexdigest(),
        "untrustedExtra": "patient content",
    }

    with pytest.raises(admin.ArchiveAdminError, match="未允许字段") as exc_info:
        admin._validate_result("delete", response, idempotency_key)
    assert "patient content" not in str(exc_info.value)


@pytest.mark.parametrize("response", [
    {"status": "REENCRYPTED", "idempotencyKeyHash": "0" * 64,
     "keyId": "kms/source2/10/v2", "archives": ["request"]},
    {"status": "REENCRYPTED", "keyId": "kms/source2/10/v2",
     "archives": ["request"]},
])
def test_reencrypt_rejects_unbound_confirmation(response):
    with pytest.raises(admin.ArchiveAdminError, match="摘要不匹配"):
        admin._validate_result("reencrypt", response, "medical-chat-201")


@pytest.mark.parametrize("archives", [
    "request", ["request", "request"], ["media:32"], [10],
])
def test_reencrypt_rejects_malformed_archive_confirmation(archives):
    idempotency_key = "medical-chat-201"
    response = {
        "status": "REENCRYPTED",
        "idempotencyKeyHash": hashlib.sha256(idempotency_key.encode()).hexdigest(),
        "keyId": "kms/source2/10/v2", "archives": archives,
    }

    with pytest.raises(admin.ArchiveAdminError, match="确认字段"):
        admin._validate_result("reencrypt", response, idempotency_key)


def test_operation_rejects_redirect_without_forwarding_admin_credentials():
    redirected = {"requested": False}

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            if self.path.startswith(admin.API_PREFIX):
                self.send_response(302)
                self.send_header("Location", "/credential-trap")
                self.end_headers()
                return
            redirected["requested"] = True
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            redirected["requested"] = True
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(admin.ArchiveAdminError, match="HTTP 302"):
            admin.operate_archive(
                f"http://127.0.0.1:{server.server_port}", "10", "admin-10",
                "source2-admin-secret-10-32-bytes", "medical-chat-201",
                "reencrypt", allow_insecure_localhost=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert redirected["requested"] is False


def test_main_requires_secret_from_environment(monkeypatch, capsys):
    monkeypatch.delenv("MISSING_SOURCE2_ADMIN_SECRET", raising=False)

    result = admin.main([
        "reencrypt", "--base-url", "https://cowagent.example.com",
        "--tenant-id", "10", "--app-key", "admin-10",
        "--secret-env", "MISSING_SOURCE2_ADMIN_SECRET",
        "--idempotency-key", "medical-chat-201",
    ])

    assert result == 2
    assert "MISSING_SOURCE2_ADMIN_SECRET" in capsys.readouterr().err
