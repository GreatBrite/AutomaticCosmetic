from __future__ import annotations

from .agent_tools import AutomationToolbox
from .agent_trace import JsonlAgentTraceLogger
from .avito_consultant import AvitoAgentPlanner, CodexToolLoopPlanner
from .care_crm import CareCrmStore
from .codex_planner import CodexPlannerRunner
from .config import IntegrationSettings
from .expert_rag import ExpertRagStore
from .rag_retrieval import RagRetrievalService
from .roles import CodexRole, RoleProfile, role_profile
from .service_catalog import ServiceCatalogStore
from .handoff_notify import HandoffNotifier
from .yclients import DryRunYClientsGateway, LiveReadDryRunYClientsGateway, YClientsGateway, YClientsHttpGateway


def booking_from_settings(settings: IntegrationSettings) -> YClientsGateway:
    if settings.yclients_ready:
        live_gateway = YClientsHttpGateway(settings)
        if settings.yclients_allow_mutations:
            return live_gateway
        return LiveReadDryRunYClientsGateway(live_gateway)
    return DryRunYClientsGateway()


def toolbox_from_settings(
    settings: IntegrationSettings,
    booking: YClientsGateway | None = None,
    profile: RoleProfile | CodexRole | str | None = None,
    care_crm: CareCrmStore | None = None,
    operations_notifier: HandoffNotifier | None = None,
) -> AutomationToolbox:
    parsed_profile = profile if isinstance(profile, RoleProfile) else role_profile(profile) if profile else None
    return AutomationToolbox(
        booking or booking_from_settings(settings),
        role_profile=parsed_profile,
        care_crm=care_crm,
        operations_notifier=operations_notifier,
    )


def codex_planner_from_settings(
    settings: IntegrationSettings,
    *,
    enabled: bool,
) -> AvitoAgentPlanner | None:
    if not enabled:
        return None
    return CodexToolLoopPlanner(
        CodexPlannerRunner(timeout_seconds=settings.avito_codex_timeout_seconds),
        max_steps=settings.avito_codex_max_steps,
        trace_logger=JsonlAgentTraceLogger(),
    )


def rag_retrieval_from_settings(settings: IntegrationSettings) -> RagRetrievalService | None:
    if not (settings.rag_retrieval_enabled and settings.rag_shared_retrieval_enabled):
        return None
    catalog = ServiceCatalogStore(settings.rag_service_catalog_path) if settings.rag_service_catalog_enabled else ServiceCatalogStore()
    return RagRetrievalService(ExpertRagStore(settings.rag_expert_db_path), service_catalog=catalog)
