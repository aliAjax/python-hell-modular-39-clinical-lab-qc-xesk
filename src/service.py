from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ReleaseBlocked
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        try:
            next_status, patch, markers = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), self._lookup
            )
        except ReleaseBlocked as blocked:
            # The batch keeps its original status; record why release was refused.
            detail = {"reasons": blocked.reasons}
            detail.update(blocked.context)
            self.audit.record(
                entity_id,
                actor,
                "release_blocked",
                entity["status"],
                entity["status"],
                detail,
            )
            raise
        merged = dict(entity["data"])
        merged.update(patch)

        side_effect = markers.get("_side_effect")
        if side_effect and side_effect.get("kind") == "deactivate_previous_lot":
            updated = self._switch_lot(actor, entity, expected, next_status, merged, side_effect)
        else:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
            detail = {"patch": patch}
            if markers.get("_audit_detail"):
                detail.update(markers["_audit_detail"])
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                detail,
            )
        return updated

    def _switch_lot(self, actor, new_lot, expected_version, next_status, merged, side_effect):
        """Activate the new lot and deactivate the old one atomically, with two
        audit entries describing the takeover."""
        previous_id = side_effect["previous_lot_id"]
        previous = self.repository.get_entity(previous_id)
        if not previous:
            raise NotFoundError("entity not found: " + previous_id)
        # Re-check inside the operation: another switch must not sneak in.
        if previous["status"] != "active":
            raise ConflictError("previous lot is no longer active: " + previous_id)
        previous_merged = dict(previous["data"])
        previous_merged.update(
            {
                "replaced_by_lot_id": new_lot["id"],
                "switched_at": side_effect["switched_at"],
                "switched_by": actor.user_id,
            }
        )
        updated_new, updated_old = self.repository.update_two_atomically(
            new_lot["id"],
            expected_version,
            next_status,
            merged,
            previous_id,
            previous["version"],
            "switched_out",
            previous_merged,
        )
        audit_detail = {
            "previous_lot_id": previous_id,
            "previous_lot_no": side_effect.get("previous_lot_no"),
            "replacement_lot_id": new_lot["id"],
            "replacement_lot_no": side_effect.get("replacement_lot_no"),
            "switched_at": side_effect["switched_at"],
        }
        self.audit.record(
            new_lot["id"],
            actor,
            "switch_in",
            new_lot["status"],
            updated_new["status"],
            audit_detail,
        )
        self.audit.record(
            previous_id,
            actor,
            "switch_out",
            previous["status"],
            updated_old["status"],
            audit_detail,
        )
        return updated_new

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
