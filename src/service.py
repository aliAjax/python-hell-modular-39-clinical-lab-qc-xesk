from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import RuleEngine, calibration_history


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
        kind = self.rules.normalize_kind(entity["kind"])
        payload = dict(data or {})
        if kind == "instrument" and action == "calibrate" and not payload.get("calibrated_at"):
            payload["calibrated_at"] = utcnow()

        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        audit_extra = patch.pop("_audit", None)

        if kind == "instrument" and action == "calibrate":
            return self._calibrate(actor, entity, expected, next_status, patch, audit_extra)
        if kind == "qc_lot" and action == "switch_in":
            return self._switch_lot(actor, entity, expected, next_status, patch)

        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        detail = {"patch": patch}
        if audit_extra:
            detail.update(audit_extra)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            detail,
        )
        return updated

    def _calibrate(self, actor, instrument, expected, next_status, patch, audit_extra):
        """Append the new certificate to the instrument timeline atomically."""
        history = calibration_history(instrument)
        history.append(
            {
                "certificate_id": patch.get("certificate_id"),
                "calibration_due": patch.get("calibration_due"),
                "calibrated_at": patch.get("calibrated_at"),
            }
        )
        merged = dict(instrument["data"])
        merged.update(patch)
        merged["calibration_history"] = history
        updated = self.repository.update_entity(
            instrument["id"], expected, next_status, merged
        )
        detail = {"patch": patch}
        if audit_extra:
            detail.update(audit_extra)
        self.audit.record(
            instrument["id"],
            actor,
            "calibrate",
            instrument["status"],
            updated["status"],
            detail,
        )
        return updated

    def _switch_lot(self, actor, new_lot, expected, next_status, patch):
        """Take over atomically: activate the new lot and retire the old one.

        The old lot keeps its switch timestamp on record so results produced
        before that moment are still judged against the old lot afterwards.
        """
        switched_at = patch.get("switched_at")
        previous_id = patch["replaces_lot_id"]
        previous = self.repository.get_entity(previous_id)
        if not previous:
            raise NotFoundError("entity not found: " + previous_id)

        new_data = dict(new_lot["data"])
        new_data.update(patch)
        old_data = dict(previous["data"])
        old_data.update(
            {
                "retired_at": switched_at,
                "replaced_by_lot_id": new_lot["id"],
                "retired_reason": "superseded by lot %s at %s" % (new_lot["id"], switched_at),
            }
        )
        new_updated, old_updated = self.repository.update_entities_many(
            [
                (new_lot["id"], expected, next_status, new_data),
                (previous["id"], previous["version"], "retired", old_data),
            ]
        )
        self.audit.record(
            new_lot["id"],
            actor,
            "switch_in",
            new_lot["status"],
            new_updated["status"],
            {"patch": patch},
        )
        self.audit.record(
            previous["id"],
            actor,
            "retire",
            previous["status"],
            old_updated["status"],
            {
                "reason": "lot takeover",
                "replaced_by_lot_id": new_lot["id"],
                "switched_at": switched_at,
            },
        )
        return new_updated

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
