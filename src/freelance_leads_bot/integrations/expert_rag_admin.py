from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .expert_rag import APPROVED, DEPRECATED, NEEDS_REVIEW, ExpertAnswer, ExpertRagStore, infer_metadata


DEFAULT_RAG_ADMIN_PLANS_PATH = Path("data/expert_rag_admin_plans.json")
DEFAULT_RAG_ADMIN_AUDIT_PATH = Path("data/expert_rag_admin_audit.jsonl")
PRICE_RE = re.compile(r"(?<!\d)(\d[\d\s]{2,})(?!\d)")
PERCENT_RE = re.compile(r"(?iu)(?:подним\w*|увелич\w*|индексир\w*)[^\d]{0,40}(\d+(?:[.,]\d+)?)\s*%")


@dataclass(frozen=True)
class RagAdminChange:
    source_id: int
    action: str
    old_answer: str = ""
    new_answer: str = ""
    old_status: str = ""
    new_status: str = ""
    note: str = ""


@dataclass(frozen=True)
class RagAdminPlan:
    id: str
    command: str
    status: str
    summary: str
    requires_confirmation: bool = True
    changes: list[RagAdminChange] = field(default_factory=list)
    created_by: str = "olga"
    created_at: str = ""
    applied_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "status": self.status,
            "summary": self.summary,
            "requires_confirmation": self.requires_confirmation,
            "changes": [change.__dict__ for change in self.changes],
            "created_by": self.created_by,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "metadata": self.metadata,
        }


class ExpertRagAdminService:
    """Human-friendly admin layer over ExpertRagStore.

    The service never mutates knowledge while building a plan. Olga can speak in
    free form, Codex can call the planning tool, and actual DB changes happen
    only through apply_plan.
    """

    def __init__(
        self,
        store: ExpertRagStore,
        *,
        plans_path: Path | str = DEFAULT_RAG_ADMIN_PLANS_PATH,
        audit_path: Path | str = DEFAULT_RAG_ADMIN_AUDIT_PATH,
    ) -> None:
        self.store = store
        self.plans_path = Path(plans_path)
        self.audit_path = Path(audit_path)

    def search(self, query: str, *, status: str = APPROVED, limit: int = 20) -> list[ExpertAnswer]:
        query_text = str(query or "").strip()
        if not query_text:
            return self.store.list_answers(status=status, limit=limit)
        matches = self.store.search(query_text, status=status, limit=limit, min_score=0.01)
        if matches:
            return [item for item, _score in matches]
        # Fallback: substring search across the current status. This is useful
        # for admin commands like "цены на ягодицы", where the canonical user
        # text may be short but exact service words exist in answers.
        lowered = query_text.casefold().replace("ё", "е")
        candidates = self.store.list_answers(status=status, limit=max(limit * 5, 50))
        found = [
            item
            for item in candidates
            if lowered in _haystack(item) or any(token in _haystack(item) for token in _tokens(lowered))
        ]
        return found[:limit]

    def plan_change(
        self,
        command: str,
        *,
        query: str = "",
        actor: str = "olga",
        status: str = APPROVED,
        limit: int = 20,
    ) -> RagAdminPlan:
        command = str(command or "").strip()
        query = str(query or "").strip() or _query_from_command(command)
        matched = self.search(query or command, status=status, limit=limit)
        metadata = {"query": query, "matched_ids": [item.id for item in matched], "kind": "unknown"}
        changes: list[RagAdminChange] = []

        percent = _extract_percent(command)
        if percent is not None:
            metadata["kind"] = "price_percent_increase"
            for item in _price_items(matched):
                new_answer = _apply_percent_to_prices(item.answer_client, percent)
                if new_answer != item.answer_client:
                    changes.append(
                        RagAdminChange(
                            source_id=item.id,
                            action="replace",
                            old_answer=item.answer_client,
                            new_answer=new_answer,
                            old_status=item.status,
                            new_status=APPROVED,
                            note=f"prices +{percent:g}%",
                        )
                    )
            summary = _summary_for_price_plan(changes, percent, query)
            status_value = "pending" if changes else "needs_clarification"
            return self._save_plan(command, status_value, summary, changes, actor=actor, metadata=metadata)

        policy_answer = _policy_answer_from_command(command)
        if policy_answer:
            metadata["kind"] = "policy"
            changes.append(
                RagAdminChange(
                    source_id=0,
                    action="create",
                    new_answer=policy_answer,
                    new_status=APPROVED,
                    note="new Olga policy from free-form command",
                )
            )
            summary = "Создам новое подтверждённое правило для будущих ответов."
            return self._save_plan(command, "pending", summary, changes, actor=actor, metadata=metadata)

        if _looks_deprecate_command(command):
            metadata["kind"] = "deprecate"
            for item in matched:
                changes.append(
                    RagAdminChange(
                        source_id=item.id,
                        action="deprecate",
                        old_answer=item.answer_client,
                        old_status=item.status,
                        new_status=DEPRECATED,
                        note="Olga marked matching knowledge as outdated/forbidden",
                    )
                )
            summary = f"Нашла {len(changes)} знаний, которые будут помечены устаревшими."
            status_value = "pending" if changes else "needs_clarification"
            return self._save_plan(command, status_value, summary, changes, actor=actor, metadata=metadata)

        summary = "Не смогла безопасно понять, какие знания нужно изменить. Нужна более конкретная формулировка."
        return self._save_plan(command, "needs_clarification", summary, [], actor=actor, metadata=metadata)

    def get_plan(self, plan_id: str) -> RagAdminPlan | None:
        return _plan_from_dict(self._load_plans().get(str(plan_id)))

    def update_plan_from_text(self, plan_id: str, text: str, *, actor: str = "olga") -> RagAdminPlan:
        existing = self.get_plan(plan_id)
        command = str(text or "").strip()
        if existing:
            command = f"{existing.command}\nПравка Ольги: {command}"
            query = str(existing.metadata.get("query") or "")
        else:
            query = ""
        return self.plan_change(command, query=query, actor=actor)

    def cancel_plan(self, plan_id: str, *, actor: str = "olga") -> RagAdminPlan | None:
        plan = self.get_plan(plan_id)
        if not plan:
            return None
        cancelled = _replace_plan(plan, status="cancelled", metadata={**plan.metadata, "cancelled_by": actor})
        self._persist_plan(cancelled)
        return cancelled

    def apply_plan(self, plan_id: str, *, actor: str = "olga") -> RagAdminPlan:
        plan = self.get_plan(plan_id)
        if not plan:
            raise KeyError(f"RAG admin plan {plan_id} not found")
        if plan.status == "applied":
            return plan
        if plan.status != "pending":
            raise ValueError(f"RAG admin plan {plan_id} is not pending")
        created_ids: list[int] = []
        for change in plan.changes:
            if change.action == "replace":
                previous = self.store.get(change.source_id)
                if not previous:
                    continue
                self.store.deprecate(previous.id)
                metadata = {
                    **(previous.metadata or {}),
                    "source": "expert_rag_admin",
                    "replaces_id": previous.id,
                    "admin_plan_id": plan.id,
                    "autoanswer_allowed": True,
                }
                created = self.store.upsert_from_handoff(
                    question=previous.question_canonical,
                    answer_client=change.new_answer,
                    answer_internal=previous.answer_internal,
                    source_chat_id=previous.source_chat_id,
                    source_message_id=previous.source_message_id,
                    olga_reply_message_id=previous.olga_reply_message_id,
                    approved_by=actor,
                    status=APPROVED,
                    metadata=metadata,
                )
                created_ids.append(created.id)
            elif change.action == "deprecate" and change.source_id:
                self.store.deprecate(change.source_id)
            elif change.action == "create":
                metadata = {
                    "source": "expert_rag_admin",
                    "admin_plan_id": plan.id,
                    "autoanswer_allowed": False,
                    "kind": "policy",
                }
                inferred = infer_metadata(change.new_answer)
                created = self.store.upsert_from_handoff(
                    question=plan.command,
                    answer_client=change.new_answer,
                    answer_internal=f"Olga free-form RAG policy: {plan.command}",
                    approved_by=actor,
                    status=APPROVED,
                    metadata={**metadata, **inferred},
                )
                created_ids.append(created.id)
        applied = _replace_plan(
            plan,
            status="applied",
            applied_at=_now(),
            metadata={**plan.metadata, "applied_by": actor, "created_ids": created_ids},
        )
        self._persist_plan(applied)
        self._append_audit(plan, applied, actor=actor)
        return applied

    def _save_plan(
        self,
        command: str,
        status: str,
        summary: str,
        changes: list[RagAdminChange],
        *,
        actor: str,
        metadata: dict[str, Any],
    ) -> RagAdminPlan:
        plan = RagAdminPlan(
            id=uuid4().hex[:12],
            command=command,
            status=status,
            summary=summary,
            changes=changes,
            created_by=actor,
            created_at=_now(),
            metadata=metadata,
        )
        self._persist_plan(plan)
        return plan

    def _persist_plan(self, plan: RagAdminPlan) -> None:
        plans = self._load_plans()
        plans[plan.id] = plan.to_dict()
        self.plans_path.parent.mkdir(parents=True, exist_ok=True)
        self.plans_path.write_text(json.dumps(plans, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _load_plans(self) -> dict[str, Any]:
        if not self.plans_path.exists():
            return {}
        try:
            raw = json.loads(self.plans_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _append_audit(self, previous: RagAdminPlan, applied: RagAdminPlan, *, actor: str) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "created_at": _now(),
            "action": "apply_plan",
            "actor": actor,
            "plan_id": applied.id,
            "previous": previous.to_dict(),
            "current": applied.to_dict(),
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def format_rag_admin_plan(plan: RagAdminPlan, *, details: bool = False) -> str:
    lines = [plan.summary, "", f"План: {plan.id}", f"Статус: {plan.status}"]
    if plan.status == "needs_clarification":
        lines.append("Ничего не применяю. Напишите точнее: услуга/город/какое значение заменить.")
        return "\n".join(lines).strip()
    if not plan.changes:
        lines.append("Изменений нет.")
        return "\n".join(lines).strip()
    lines.append("")
    for index, change in enumerate(plan.changes[:10], start=1):
        if change.action == "replace":
            lines.append(f"{index}. Заменить знание #{change.source_id}:")
            lines.append(f"Было: {change.old_answer}")
            lines.append(f"Станет: {change.new_answer}")
        elif change.action == "deprecate":
            lines.append(f"{index}. Пометить устаревшим знание #{change.source_id}: {change.old_answer}")
        elif change.action == "create":
            lines.append(f"{index}. Создать правило: {change.new_answer}")
        if details and change.note:
            lines.append(f"Причина: {change.note}")
        lines.append("")
    if len(plan.changes) > 10:
        lines.append(f"…и ещё {len(plan.changes) - 10}.")
    if plan.status == "pending":
        lines.append("Применить изменения?")
    return "\n".join(lines).strip()


def rag_admin_plan_keyboard(plan_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Применить", "callback_data": f"ragplan:{plan_id}:apply"},
                {"text": "❌ Отмена", "callback_data": f"ragplan:{plan_id}:cancel"},
            ],
            [
                {"text": "🔍 Подробнее", "callback_data": f"ragplan:{plan_id}:details"},
                {"text": "✏️ Исправить", "callback_data": f"ragplan:{plan_id}:edit"},
            ],
        ]
    }


def parse_rag_admin_callback(data: str) -> tuple[str, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "ragplan":
        return None
    return parts[1], parts[2]


def rag_admin_plan_from_dict(raw: Any) -> RagAdminPlan | None:
    return _plan_from_dict(raw)


def _replace_plan(plan: RagAdminPlan, **changes: Any) -> RagAdminPlan:
    data = plan.to_dict()
    data.update(changes)
    return _plan_from_dict(data) or plan


def _plan_from_dict(raw: Any) -> RagAdminPlan | None:
    if not isinstance(raw, dict):
        return None
    return RagAdminPlan(
        id=str(raw.get("id") or ""),
        command=str(raw.get("command") or ""),
        status=str(raw.get("status") or ""),
        summary=str(raw.get("summary") or ""),
        requires_confirmation=bool(raw.get("requires_confirmation", True)),
        changes=[
            RagAdminChange(
                source_id=int(row.get("source_id") or 0),
                action=str(row.get("action") or ""),
                old_answer=str(row.get("old_answer") or ""),
                new_answer=str(row.get("new_answer") or ""),
                old_status=str(row.get("old_status") or ""),
                new_status=str(row.get("new_status") or ""),
                note=str(row.get("note") or ""),
            )
            for row in raw.get("changes") or []
            if isinstance(row, dict)
        ],
        created_by=str(raw.get("created_by") or "olga"),
        created_at=str(raw.get("created_at") or ""),
        applied_at=str(raw.get("applied_at") or ""),
        metadata=dict(raw.get("metadata") or {}),
    )


def _extract_percent(command: str) -> float | None:
    match = PERCENT_RE.search(command)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _query_from_command(command: str) -> str:
    lowered = command.casefold()
    for service in ("ягодиц", "ягодицы", "груд", "губ", "tesoro", "тесоро", "ботокс"):
        if service in lowered:
            return service
    return command


def _looks_deprecate_command(command: str) -> bool:
    lowered = command.casefold()
    return any(marker in lowered for marker in ("устарел", "не актуаль", "больше не использ", "не говори", "не отвечай так"))


def _policy_answer_from_command(command: str) -> str:
    lowered = command.casefold()
    if "очн" in lowered and ("не говор" in lowered or "не рекоменд" in lowered or "не склон" in lowered):
        return (
            "Не рекомендовать очную консультацию как следующий шаг по умолчанию. "
            "Если нужна дополнительная оценка, один раз предложить онлайн-разбор с Ольгой и запросить недостающие данные/фото."
        )
    if "tesoro" in lowered or "тесоро" in lowered:
        return command
    return ""


def _price_items(items: list[ExpertAnswer]) -> list[ExpertAnswer]:
    return [item for item in items if PRICE_RE.search(item.answer_client)]


def _apply_percent_to_prices(text: str, percent: float) -> str:
    factor = 1 + percent / 100.0

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1)
        compact = raw.replace(" ", "")
        try:
            value = int(compact)
        except ValueError:
            return raw
        if value < 1000:
            return raw
        new_value = value * factor
        if new_value.is_integer():
            rendered = str(int(new_value))
        else:
            rendered = f"{new_value:.2f}".rstrip("0").rstrip(".")
        return _group_number(rendered)

    return PRICE_RE.sub(repl, text)


def _group_number(value: str) -> str:
    if "." in value:
        integer, fractional = value.split(".", 1)
    else:
        integer, fractional = value, ""
    sign = "-" if integer.startswith("-") else ""
    integer = integer.lstrip("-")
    groups: list[str] = []
    while integer:
        groups.append(integer[-3:])
        integer = integer[:-3]
    rendered = sign + " ".join(reversed(groups or ["0"]))
    return rendered + ("," + fractional if fractional else "")


def _summary_for_price_plan(changes: list[RagAdminChange], percent: float, query: str) -> str:
    if not changes:
        return f"Не нашла подтверждённых цен для изменения на +{percent:g}% по запросу «{query}»."
    return f"Нашла {len(changes)} знаний с ценами по запросу «{query}». Подниму цены на {percent:g}%."


def _haystack(item: ExpertAnswer) -> str:
    return " ".join([item.question_canonical, item.answer_client, item.service, item.city, item.topic]).casefold().replace("ё", "е")


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-zа-я0-9]+", text.casefold().replace("ё", "е")) if len(token) > 2]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
