"""Run the deploy webhook steps against a local TLS fake, without network.

The steps are read from .github/workflows/deploy.yml so the tests exercise the
shipped code, not a copy of it. The fake server listens on 127.0.0.1 only.
"""

import http.server
import os
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
HOOK_PATH = "/hooks/ai-ops-agent-update"
TOKEN = "test-deploy-token"
NEEDS_TLS_TOOLS = pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("openssl")),
    reason="needs curl and openssl",
)


def load_step(name):
    text = (WORKFLOWS / "deploy.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["deploy"]["steps"]
    return next(step["run"] for step in steps if step["name"] == name)


def run_validation(url, token=TOKEN):
    return subprocess.run(
        [sys.executable, "-"],
        input=load_step("Validate deploy webhook settings"),
        capture_output=True,
        text=True,
        env={"DEPLOY_WEBHOOK_URL": url, "DEPLOY_WEBHOOK_TOKEN": token},
        check=False,
    )


def run_call(url, ca_file, token=TOKEN):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DEPLOY_WEBHOOK_URL": url,
        "DEPLOY_WEBHOOK_TOKEN": token,
        "CURL_CA_BUNDLE": str(ca_file),
    }
    script = load_step("Call deploy webhook")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_status(self, status, location=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TlsHandler(FakeHandler):
    def do_POST(self):
        authorization = self.headers.get("Authorization")
        self.server.received.append((self.path, authorization))
        if self.path == "/redirect":
            self.send_status(301, location=self.server.plain_url + HOOK_PATH)
        elif self.path == HOOK_PATH and authorization == f"Bearer {TOKEN}":
            self.send_status(200)
        else:
            self.send_status(401)


class PlainHandler(FakeHandler):
    def do_POST(self):
        self.server.received.append((self.path, self.headers.get("Authorization")))
        self.send_status(200)


@pytest.fixture
def webhook(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )

    plain = http.server.HTTPServer(("127.0.0.1", 0), PlainHandler)
    plain.received = []
    tls = http.server.HTTPServer(("127.0.0.1", 0), TlsHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    tls.socket = context.wrap_socket(tls.socket, server_side=True)
    tls.received = []
    tls.plain_url = f"http://127.0.0.1:{plain.server_address[1]}"

    servers = [plain, tls]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(
            url=f"https://127.0.0.1:{tls.server_address[1]}",
            plain_url=tls.plain_url,
            ca=cert,
            received=tls.received,
            plain_received=plain.received,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize(
    "url",
    [
        "http://deploy.example.com/hooks/ai-ops-agent-update",
        "HTTP://deploy.example.com/hooks/ai-ops-agent-update",
        "http://127.0.0.1:9000/hooks/ai-ops-agent-update",
    ],
)
def test_validation_rejects_plain_http_urls(url):
    result = run_validation(url)
    assert result.returncode == 1
    assert "DEPLOY_WEBHOOK_URL must use https://" in result.stdout


def test_validation_rejects_secret_in_query_string():
    result = run_validation(f"https://deploy.example.com{HOOK_PATH}?secret=old")
    assert result.returncode == 1
    assert "must not contain a query string" in result.stdout


def test_validation_rejects_missing_token():
    result = run_validation(f"https://deploy.example.com{HOOK_PATH}", token="")
    assert result.returncode == 1
    assert "DEPLOY_WEBHOOK_TOKEN is empty" in result.stdout


def test_validation_accepts_https_base_url():
    result = run_validation(f"https://deploy.example.com{HOOK_PATH}")
    assert result.returncode == 0, result.stdout
    assert "Deploy webhook settings OK" in result.stdout


def test_call_sends_token_in_header_and_masks_it():
    script = load_step("Call deploy webhook")
    assert 'Authorization: Bearer $DEPLOY_WEBHOOK_TOKEN"' in script
    assert "secret=" not in script
    assert "::add-mask::$DEPLOY_WEBHOOK_TOKEN" in script


@NEEDS_TLS_TOOLS
def test_authenticated_https_call_is_accepted(webhook):
    result = run_call(webhook.url + HOOK_PATH, webhook.ca)
    assert result.returncode == 0, result.stderr
    # The secret travels in the header only; the request path has no query.
    assert webhook.received == [(HOOK_PATH, f"Bearer {TOKEN}")]


@NEEDS_TLS_TOOLS
def test_wrong_token_fails_the_step(webhook):
    result = run_call(webhook.url + HOOK_PATH, webhook.ca, token="old-or-wrong")
    assert result.returncode == 22  # CURLE_HTTP_RETURNED_ERROR from --fail
    assert webhook.plain_received == []


@NEEDS_TLS_TOOLS
def test_call_refuses_plain_http(webhook):
    result = run_call(webhook.plain_url + HOOK_PATH, webhook.ca)
    assert result.returncode == 1  # CURLE_UNSUPPORTED_PROTOCOL
    assert webhook.plain_received == []


@NEEDS_TLS_TOOLS
def test_call_does_not_follow_redirect_to_plain_http(webhook):
    result = run_call(webhook.url + "/redirect", webhook.ca)
    assert result.returncode == 47  # CURLE_TOO_MANY_REDIRECTS with --max-redirs 0
    assert webhook.plain_received == []
