"""Compatibility facade for Solapi template API helpers.

Implementation lives in academy.adapters.messaging.solapi_template_client.
"""

from __future__ import annotations

from academy.adapters.messaging.solapi_template_client import (
    _create_auth_header,
    clone_kakao_template,
    create_kakao_template,
    list_kakao_templates,
    request_kakao_template_inspection,
    validate_template_variables,
)

__all__ = [
    "_create_auth_header",
    "clone_kakao_template",
    "create_kakao_template",
    "list_kakao_templates",
    "request_kakao_template_inspection",
    "validate_template_variables",
]
