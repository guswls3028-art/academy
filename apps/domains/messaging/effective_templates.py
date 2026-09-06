from __future__ import annotations

from dataclasses import dataclass

from apps.domains.messaging.alimtalk_content_builders import get_solapi_template_id, get_template_type


@dataclass(frozen=True)
class EffectiveTemplateStatus:
    solapi_template_id: str
    solapi_status: str
    source: str
    template_type: str = ""

    @property
    def is_approved(self) -> bool:
        return bool(self.solapi_template_id and self.solapi_status == "APPROVED")


def prime_effective_owner_templates(configs) -> list:
    """Materialize configs without resolving any provider-template fallback."""
    return list(configs)


def resolve_effective_template_status(config) -> EffectiveTemplateStatus:
    """Resolve the public template actually used for an AutoSendConfig."""
    content_template = getattr(config, "template", None)
    if not content_template:
        return EffectiveTemplateStatus(
            solapi_template_id="",
            solapi_status="",
            source="content_template_missing",
        )
    if getattr(content_template, "retired_at", None) is not None:
        return EffectiveTemplateStatus(
            solapi_template_id="",
            solapi_status="",
            source="content_template_retired",
        )
    if int(content_template.tenant_id) != int(config.tenant_id):
        return EffectiveTemplateStatus(
            solapi_template_id="",
            solapi_status="",
            source="content_template_tenant_mismatch",
        )
    unified_template_type = get_template_type(config.trigger) or ""
    unified_template_id = (get_solapi_template_id(config.trigger) or "").strip()
    if unified_template_id:
        return EffectiveTemplateStatus(
            solapi_template_id=unified_template_id,
            solapi_status="APPROVED",
            source="unified",
            template_type=unified_template_type,
        )
    if unified_template_type:
        return EffectiveTemplateStatus(
            solapi_template_id="",
            solapi_status="",
            source="unified_missing",
            template_type=unified_template_type,
        )

    # 매핑되지 않은 이벤트는 DB 행, 이름, 최신/기본 행으로 승인 봉투를
    # 대체하지 않는다. 공급사 계약을 등록할 때까지 발송 0건이다.
    return EffectiveTemplateStatus(
        solapi_template_id="",
        solapi_status="",
        source="provider_contract_missing",
    )
