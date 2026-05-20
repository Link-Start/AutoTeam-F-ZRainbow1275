"""Read-only DNS diagnostics for mail and OpenAI domain setup."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests

DOH_URL = "https://cloudflare-dns.com/dns-query"
SUPPORTED_RECORD_TYPES = {"A", "AAAA", "CNAME", "MX", "TXT"}


class DNSDiagnosticError(RuntimeError):
    """Raised for invalid DNS diagnostic inputs or resolver failures."""


def normalize_domain(value: str | None) -> str:
    """Normalize a user-entered DNS name without implying Cloudflare ownership."""
    return (value or "").strip().lower().removeprefix("@").rstrip(".")


def normalize_dns_value(rtype: str, value: str) -> str:
    """Normalize DNS answer text into the shape used for equality checks."""
    record_type = (rtype or "").strip().upper()
    normalized = str(value or "").strip()
    if record_type == "TXT":
        return normalized.strip('"').replace('" "', "")
    if record_type == "MX":
        parts = normalized.split(maxsplit=1)
        if len(parts) == 2:
            return f"{parts[0]} {normalize_domain(parts[1])}"
    if record_type in {"CNAME", "MX"}:
        return normalize_domain(normalized)
    return normalized.rstrip(".")


def _record_check(name: str, rtype: str, expected: str) -> dict[str, str]:
    record_type = (rtype or "").strip().upper()
    if record_type not in SUPPORTED_RECORD_TYPES:
        raise DNSDiagnosticError(f"unsupported DNS record type: {rtype!r}")
    normalized_name = normalize_domain(name)
    if not normalized_name:
        raise DNSDiagnosticError("DNS record name is empty")
    normalized_expected = normalize_dns_value(record_type, expected)
    if not normalized_expected:
        raise DNSDiagnosticError("DNS expected value is empty")
    return {
        "type": record_type,
        "name": normalized_name,
        "expected": normalized_expected,
    }


def expected_dns_checks(
    admin: dict[str, Any] | None,
    *,
    extra_checks: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Build read-only DNS checks from setup/admin metadata.

    This intentionally does not call Cloudflare's zone API and does not create,
    update, or delete records. Operators can pass explicit records when the
    exact OpenAI verification TXT value is known.
    """
    admin = admin or {}
    email_domain = normalize_domain(admin.get("email_domain") or admin.get("domain"))
    checks: list[dict[str, str]] = []

    mail_host = normalize_domain(admin.get("mail_host"))
    mail_ip = str(admin.get("mail_ip") or "").strip()
    mx_target = normalize_domain(admin.get("mx_target") or mail_host)
    spf_value = str(admin.get("spf_value") or "").strip()
    verification_txt = str(admin.get("openai_domain_verification") or "").strip()

    if mail_host and mail_ip:
        checks.append(_record_check(mail_host, "A", mail_ip))
    if mx_target:
        if not email_domain:
            raise DNSDiagnosticError("email_domain is empty")
        checks.append(_record_check(email_domain, "MX", f"10 {mx_target}"))
    if spf_value:
        if not email_domain:
            raise DNSDiagnosticError("email_domain is empty")
        checks.append(_record_check(email_domain, "TXT", spf_value))
    if verification_txt:
        if not email_domain:
            raise DNSDiagnosticError("email_domain is empty")
        checks.append(_record_check(email_domain, "TXT", verification_txt))

    for item in extra_checks or []:
        checks.append(
            _record_check(
                str(item.get("name") or ""),
                str(item.get("type") or ""),
                str(item.get("expected") or ""),
            )
        )

    return checks


def lookup_public_dns(name: str, rtype: str, *, timeout: float = 10.0) -> list[str]:
    """Resolve DNS via Cloudflare's public DNS-over-HTTPS JSON endpoint."""
    record_type = (rtype or "").strip().upper()
    if record_type not in SUPPORTED_RECORD_TYPES:
        raise DNSDiagnosticError(f"unsupported DNS record type: {rtype!r}")
    try:
        resp = requests.get(
            DOH_URL,
            params={"name": normalize_domain(name), "type": record_type},
            headers={"accept": "application/dns-json"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DNSDiagnosticError(f"DNS lookup failed: {exc}") from exc

    try:
        payload = resp.json()
    except Exception as exc:
        raise DNSDiagnosticError("DNS lookup returned non-JSON response") from exc

    answers = payload.get("Answer") if isinstance(payload, dict) else None
    if not isinstance(answers, list):
        return []
    return [
        normalize_dns_value(record_type, str(answer.get("data", "")))
        for answer in answers
        if isinstance(answer, dict) and answer.get("data") is not None
    ]


def check_admin_dns(
    admin: dict[str, Any] | None,
    *,
    lookup: Callable[[str, str], list[str]] | None = None,
    extra_checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check expected admin/setup DNS records using a read-only resolver."""
    email_domain = normalize_domain((admin or {}).get("email_domain") or (admin or {}).get("domain"))
    resolver = lookup or lookup_public_dns
    results: list[dict[str, Any]] = []
    for item in expected_dns_checks(admin, extra_checks=extra_checks):
        error = None
        try:
            observed = [normalize_dns_value(item["type"], value) for value in resolver(item["name"], item["type"])]
        except Exception as exc:  # noqa: BLE001 - return a diagnostic row instead of hiding other checks
            observed = []
            error = str(exc)
        row = {
            "type": item["type"],
            "name": item["name"],
            "expected": item["expected"],
            "observed": observed,
            "ok": item["expected"] in observed,
        }
        if error:
            row["error"] = error
        results.append(row)

    return {
        "domain": email_domain,
        "checks": results,
        "all_ok": bool(results) and all(item["ok"] for item in results),
        "safe_read_only": True,
    }
