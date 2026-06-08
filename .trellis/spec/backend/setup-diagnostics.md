# Setup Diagnostics Contracts

## Scenario: Read-only DNS diagnostics

### 1. Scope / Trigger

- Trigger: any change to setup-time DNS checks, `/api/setup/dns/check`, or
  `src/autoteam/dns_diagnostics.py`.
- Goal: help operators verify mail-domain and OpenAI-domain DNS records without
  mutating Cloudflare, a registrar, or any DNS provider.
- Boundary: diagnostics may query public DNS, but must not call DNS-provider
  write APIs such as create, update, delete, upsert, or zone-record patch.

### 2. Signatures

- `normalize_domain(value: str | None) -> str`
- `normalize_dns_value(rtype: str, value: str) -> str`
- `expected_dns_checks(admin: dict | None, *, extra_checks: list[dict] | None = None) -> list[dict[str, str]]`
- `lookup_public_dns(name: str, rtype: str, *, timeout: float = 10.0) -> list[str]`
- `check_admin_dns(admin: dict | None, *, lookup=None, extra_checks: list[dict] | None = None) -> dict`
- `POST /api/setup/dns/check`

### 3. Contracts

- Supported DNS record types are `A`, `AAAA`, `CNAME`, `MX`, and `TXT`.
- Request fields:
  - `domain`: root mail/OpenAI verification domain.
  - `mail_host`: optional host for A-record checks.
  - `mail_ip`: optional expected IP for `mail_host`.
  - `mx_target`: optional MX target; defaults to `mail_host` when provided.
  - `spf_value`: optional expected SPF TXT value.
  - `openai_domain_verification`: optional expected OpenAI verification TXT value.
  - `records`: optional explicit checks with `{type, name, expected}`.
- Response fields:
  - `ok`: equal to `all_ok`.
  - `safe_read_only`: must always be `true` for this endpoint.
  - `checks[]`: per-record `{type, name, expected, observed, ok, error?}` rows.
- Auth matches setup probes: before `API_KEY` exists, allow setup calls with IP
  rate limiting; after `API_KEY` exists, require `Authorization: Bearer <API_KEY>`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Unsupported DNS type | Return `ok=false`, `error_code="INVALID_REQUEST"` |
| Empty record name or expected value | Return `ok=false`, `error_code="INVALID_REQUEST"` |
| Public DoH lookup returns no answer | Return one check row with `observed=[]`, `ok=false` |
| Public DoH lookup raises or returns non-JSON | Keep endpoint alive; include a diagnostic `error` on that check row when possible |
| `API_KEY` configured and Bearer missing/wrong | HTTP `401` |
| Setup-stage client exceeds probe rate limit | HTTP `429` |

### 5. Good/Base/Bad Cases

- Good: a user passes the OpenAI TXT verification value and receives a read-only
  row showing whether the value is present in public DNS.
- Base: a DNS record is still propagating; the endpoint returns `ok=false` with
  `observed=[]`, not an exception.
- Bad: importing target `cloudflare_dns.py` wholesale and calling
  `upsert_record()` or `ensure_admin_dns()` from setup, because that mutates
  external DNS without explicit operator intent.

### 6. Tests Required

- `tests/unit/test_dns_diagnostics.py`
  - DNS value normalization for `TXT` and `MX`.
  - `check_admin_dns()` returns `safe_read_only=true` and correct `all_ok`.
  - Missing records and resolver errors become diagnostic rows.
  - `/api/setup/dns/check` accepts setup-stage calls and requires Bearer after setup.
- Related setup probe behavior should continue to pass when auth helper logic is
  shared with `/api/mail-provider/probe`.

### 7. Wrong vs Correct

#### Wrong

```python
ensure_admin_dns(admin_payload)
```

This may create or update Cloudflare records during a diagnostic action.

#### Correct

```python
check_admin_dns(admin_payload, extra_checks=[{"type": "TXT", "name": domain, "expected": verification_txt}])
```

Diagnostics only observe public DNS state. DNS-provider mutation requires a
separate explicit feature and operator approval.
