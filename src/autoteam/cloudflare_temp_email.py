"""Compatibility facade for the packaged cf_temp_email mail provider.

The implementation source of truth stays in `autoteam.mail.cf_temp_email`.
This module preserves the target repo's public import path for tests and older
automation without moving the provider back to a top-level architecture.
"""

from __future__ import annotations

from autoteam.mail.base import decode_jwt_payload
from autoteam.mail.cf_temp_email import (
    CfTempEmailClient,
    normalize_cloudflare_temp_email_base_url,
)
from autoteam.runtime_config import get_register_domain


class CloudflareTempEmailClient(CfTempEmailClient):
    """Target-compatible wrapper around `CfTempEmailClient`."""

    def __init__(self):
        super().__init__()
        self.domain = get_register_domain()

    def _request(self, method: str, path: str, **kwargs):
        method = (method or "GET").upper()
        if method == "GET":
            response = self._admin_get(path, params=kwargs.get("params"))
        elif method == "POST":
            response = self._admin_post(path, kwargs.get("json") or kwargs.get("data"))
        elif method == "DELETE":
            response = self._admin_delete(path)
        else:
            raise ValueError(f"unsupported method: {method}")
        if response.status_code != 200:
            raise Exception(f"cf_temp_email request failed: HTTP {response.status_code} {(response.text or '')[:200]}")
        try:
            return response.json() or {}
        except Exception:
            return {}

    def create_temp_email(self, prefix=None, domain=None):
        domain = (domain or self.domain or get_register_domain() or "").lstrip("@").strip()
        if not domain:
            raise Exception("创建邮箱失败: 未配置注册域名")

        cleaned = self._sanitize_prefix(prefix)
        data = self._request(
            "POST",
            "/admin/new_address",
            json={"name": cleaned, "domain": domain, "enablePrefix": False},
        )
        if not isinstance(data, dict) or "address" not in data:
            raise Exception(f"创建邮箱响应不像 cf_temp_email(收到 {data!r})。")

        address = data.get("address")
        jwt = data.get("jwt") or ""
        payload = decode_jwt_payload(jwt) if jwt else {}
        address_id = data.get("address_id") or payload.get("address_id")
        if jwt and address:
            self._address_jwts[self._normalize_email(address)] = jwt
        return address_id, address

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        target = self._normalize_email(to_email)
        if not target:
            return []

        data = self._request(
            "GET",
            "/admin/mails",
            params={"limit": size, "offset": 0, "address": target},
        )
        out = []
        for row in data.get("results", []) if isinstance(data, dict) else []:
            row_addr = self._normalize_email(row.get("address"))
            if row_addr and row_addr != target:
                continue
            normalized = self._normalize_mail_record(row)
            if account_id is not None:
                normalized["accountId"] = account_id
            out.append(normalized)
        out.sort(key=lambda item: item.get("emailId") or 0, reverse=True)
        return out


__all__ = [
    "CloudflareTempEmailClient",
    "normalize_cloudflare_temp_email_base_url",
]
