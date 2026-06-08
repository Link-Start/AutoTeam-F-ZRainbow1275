from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from autoteam import api, config
from autoteam import dns_diagnostics as dns


class _Resp:
    def __init__(self, payload, *, status_error=None):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        if self._payload is _MISSING:
            raise ValueError("non-json")
        return self._payload


class _Request:
    def __init__(self, headers=None, host="127.0.0.200"):
        self.headers = headers or {}
        self.client = SimpleNamespace(host=host)


_MISSING = object()


def test_check_admin_dns_matches_expected_mail_and_openai_records():
    observed = {
        ("mail.example.com", "A"): ["203.0.113.10"],
        ("example.com", "MX"): ["10 mail.example.com"],
        ("example.com", "TXT"): ['"v=spf1 include:_spf.example.com ~all"', "openai-domain-verification=abc"],
    }

    result = dns.check_admin_dns(
        {
            "domain": "example.com",
            "mail_host": "mail.example.com.",
            "mail_ip": "203.0.113.10",
            "spf_value": "v=spf1 include:_spf.example.com ~all",
            "openai_domain_verification": "openai-domain-verification=abc",
        },
        lookup=lambda name, rtype: observed.get((name, rtype), []),
    )

    assert result["safe_read_only"] is True
    assert result["all_ok"] is True
    assert [item["type"] for item in result["checks"]] == ["A", "MX", "TXT", "TXT"]


def test_check_admin_dns_reports_missing_and_lookup_errors():
    def failing_lookup(name, rtype):
        if rtype == "TXT":
            raise RuntimeError("resolver unavailable")
        return []

    result = dns.check_admin_dns(
        {"domain": "example.com"},
        extra_checks=[
            {"type": "MX", "name": "example.com", "expected": "10 mail.example.com"},
            {"type": "TXT", "name": "example.com", "expected": "openai-domain-verification=abc"},
        ],
        lookup=failing_lookup,
    )

    assert result["all_ok"] is False
    assert result["checks"][0]["observed"] == []
    assert result["checks"][1]["error"] == "resolver unavailable"


def test_lookup_public_dns_normalizes_doh_answers(monkeypatch):
    def fake_get(url, **kwargs):
        assert url == dns.DOH_URL
        assert kwargs["params"] == {"name": "example.com", "type": "MX"}
        return _Resp({"Answer": [{"data": "10 mail.example.com."}]})

    monkeypatch.setattr(dns.requests, "get", fake_get)

    assert dns.lookup_public_dns("Example.com.", "MX") == ["10 mail.example.com"]


def test_lookup_public_dns_rejects_non_json(monkeypatch):
    monkeypatch.setattr(dns.requests, "get", lambda *args, **kwargs: _Resp(_MISSING))

    with pytest.raises(dns.DNSDiagnosticError, match="non-JSON"):
        dns.lookup_public_dns("example.com", "TXT")


def test_setup_dns_check_endpoint_is_read_only(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(dns, "lookup_public_dns", lambda name, rtype: ["v=spf1 -all"])

    response = api.post_setup_dns_check(
        api.DNSDiagnosticRequest(
            domain="example.com",
            records=[api.DNSRecordCheck(type="TXT", name="example.com", expected="v=spf1 -all")],
        ),
        _Request(),
    )

    assert response.ok is True
    assert response.safe_read_only is True
    assert response.checks[0].ok is True


def test_setup_dns_check_endpoint_requires_bearer_after_setup(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "secret")

    with pytest.raises(HTTPException) as exc:
        api.post_setup_dns_check(
            api.DNSDiagnosticRequest(
                domain="example.com",
                records=[api.DNSRecordCheck(type="TXT", name="example.com", expected="v=spf1 -all")],
            ),
            _Request(),
        )

    assert exc.value.status_code == 401
