from datetime import datetime, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ReleaseBlocked, ValidationError


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def day_prefix(value):
    return str(value or "")[:10]


def calibration_is_valid(calibration_due, as_of):
    return day_prefix(calibration_due) >= day_prefix(as_of)


def lot_is_valid_at(lot, as_of):
    """A lot is usable at ``as_of`` while it has not expired and was not switched out yet.

    The new lot also records ``switched_at`` (its takeover moment); only a lot
    carrying ``replaced_by_lot_id`` has actually been switched out."""
    if not lot:
        return False
    data = lot.get("data") or {}
    if day_prefix(data.get("expires_at")) < day_prefix(as_of):
        return False
    if data.get("replaced_by_lot_id"):
        switched_at = data.get("switched_at")
        if switched_at and str(as_of) >= str(switched_at):
            return False
    return True


def _calibration_history(instrument):
    """Calibration entries newest-last. Instruments created before this feature
    get a synthetic baseline so historical runs keep a certificate."""
    data = instrument["data"]
    history = list(data.get("calibration_history") or [])
    if not history:
        history.append(
            {
                "certificate_id": data.get("certificate_id") or "initial",
                "calibration_due": data.get("calibration_due"),
                "calibrated_at": "",
            }
        )
    history.sort(key=lambda item: str(item.get("calibrated_at") or ""))
    return history


def calibration_at(instrument, as_of):
    """Return (entry, replaced) for the certificate in force at ``as_of``.

    ``replaced`` is True when the instrument has since been recalibrated, i.e.
    the certificate in force at run time is no longer the current one."""
    history = _calibration_history(instrument)
    effective = history[0]
    index = 0
    for position, entry in enumerate(history):
        if str(entry.get("calibrated_at") or "") <= str(as_of):
            effective = entry
            index = position
        else:
            break
    return effective, (index < len(history) - 1)


def _open_qc_failure_runs(lookup, lot_id, instrument_id=None):
    """QC runs whose out-of-control case is still open for a lot (and instrument)."""
    open_statuses = ("rejected", "investigated", "retesting")
    runs = []
    for run in lookup("qc_run", "qc_lot_id", lot_id) or []:
        if run["status"] not in open_statuses:
            continue
        if instrument_id and run["data"].get("instrument_id") != instrument_id:
            continue
        runs.append(run)
    return runs


def evaluate_qc(history, value, target, sd, config=None):
    """Evaluate one QC value against numeric and multi-rule criteria."""
    config = dict(config or {})
    try:
        value = float(value)
        target = float(target)
        sd = float(sd)
    except (TypeError, ValueError):
        raise ValidationError("qc value, target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("qc sd must be positive")
    limit = float(config.get("limit_sd", 3.0))
    bias_n = int(config.get("consecutive_n", 4))
    bias_sd = float(config.get("consecutive_sd", 1.0))
    trend_n = int(config.get("trend_n", 4))
    z_score = round((value - target) / sd, 4)
    flags = []
    if abs(z_score) > limit:
        flags.append("1_3s")
    values = [float(item) for item in history] + [value]
    if len(values) >= bias_n:
        window = values[-bias_n:]
        if all(item > target + bias_sd * sd for item in window):
            flags.append("bias_high")
        if all(item < target - bias_sd * sd for item in window):
            flags.append("bias_low")
    if len(values) >= trend_n:
        window = values[-trend_n:]
        if all(window[index] < window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_up")
        if all(window[index] > window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_down")
    passed = not flags
    return {
        "accepted": passed,
        "flags": flags,
        "z_score": z_score,
        "rule_snapshot": {
            "limit_sd": limit,
            "consecutive_n": bias_n,
            "consecutive_sd": bias_sd,
            "trend_n": trend_n,
        },
    }


def unrecovered_rejection(batches):
    return [batch for batch in batches if batch.get("status") == "intercepted"]


def _validate_assay(actor, data, lookup):
    try:
        low = float(data.get("allowed_low"))
        high = float(data.get("allowed_high"))
    except (TypeError, ValueError):
        raise ValidationError("allowed_low and allowed_high must be numeric")
    if low >= high:
        raise ValidationError("allowed_low must be less than allowed_high")
    return {
        "rule_config": dict(data.get("rule_config") or {}),
    }


def _validate_qc_lot(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    try:
        target = float(data.get("target"))
        sd = float(data.get("sd"))
    except (TypeError, ValueError):
        raise ValidationError("target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("sd must be positive")
    duplicate = _find_one(lookup, "qc_lot", "lot_key", "%s:%s" % (data["assay_id"], data["lot_no"]))
    if duplicate:
        raise ConflictError("qc lot already exists for assay")
    return {"lot_key": "%s:%s" % (data["assay_id"], data["lot_no"]), "target": target, "sd": sd}


def _validate_instrument(actor, data, lookup):
    if not str(data.get("serial", "")).strip():
        raise ValidationError("instrument serial is required")
    calibration_due = data.get("calibration_due")
    certificate_id = str(data.get("certificate_id") or "initial")
    # Empty calibrated_at sorts before every real run time: the initial
    # certificate is the one in force until the first recalibration.
    return {
        "calibration_due": calibration_due,
        "certificate_id": certificate_id,
        "calibration_history": [
            {
                "certificate_id": certificate_id,
                "calibration_due": calibration_due,
                "calibrated_at": "",
            }
        ],
    }


def _validate_calibrate(actor, entity, data, lookup):
    certificate_id = str(data.get("certificate_id") or "").strip()
    if not certificate_id:
        raise ValidationError("certificate_id is required")
    calibrated_at = str(data.get("calibrated_at") or utc_now_iso())
    history = _calibration_history(entity)
    if certificate_id == (history[-1].get("certificate_id") if history else None):
        raise ValidationError("new calibration certificate must differ from the current one")
    latest_at = str(history[-1].get("calibrated_at") or "") if history else ""
    if latest_at and calibrated_at < latest_at:
        raise ValidationError("calibration cannot be older than the current certificate")
    calibration_due = data.get("calibration_due")
    if day_prefix(calibration_due) < day_prefix(calibrated_at):
        raise ValidationError("calibration certificate must not be already expired")
    entry = {
        "certificate_id": certificate_id,
        "calibration_due": calibration_due,
        "calibrated_at": calibrated_at,
    }
    history.append(entry)
    return {
        "calibration_due": calibration_due,
        "certificate_id": certificate_id,
        "calibration_history": history,
    }


def _validate_qc_run(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", data.get("qc_lot_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not assay or not lot or not instrument:
        raise ValidationError("assay, qc lot and instrument are required")
    if lot["data"].get("assay_id") != assay["id"]:
        raise ValidationError("qc lot does not belong to the assay")
    run_at = data.get("run_at")
    # A new lot takes over at switched_at: a switched-out lot may only backfill
    # runs that happened before the switch; retired lots can never receive runs.
    if lot["status"] == "switched_out":
        if not lot["data"].get("switched_at") or str(run_at) >= str(lot["data"]["switched_at"]):
            raise ValidationError("qc lot has been switched out and no longer accepts new runs")
    elif lot["status"] != "active":
        raise ValidationError("qc run requires an active qc lot")
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        raise ValidationError("qc result value must be numeric")
    return {"value": value}


def _validate_result_batch(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    if not _find_one(lookup, "instrument", "id", data.get("instrument_id")):
        raise ValidationError("instrument does not exist")
    if not _find_one(lookup, "qc_run", "id", data.get("qc_run_id")):
        raise ValidationError("qc run does not exist")
    if int(data.get("patient_count", 0)) < 0:
        raise ValidationError("patient_count cannot be negative")
    return {}


def _validate_evaluate(actor, entity, data, lookup):
    assay = _find_one(lookup, "assay", "id", entity["data"].get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", entity["data"].get("qc_lot_id"))
    if not assay or not lot:
        raise ValidationError("assay or qc lot disappeared")
    previous = []
    for run in lookup("qc_run", "instrument_id", entity["data"].get("instrument_id")) or []:
        if run["id"] == entity["id"] or run["status"] not in ("accepted", "rejected"):
            continue
        if run["data"].get("qc_lot_id") != entity["data"].get("qc_lot_id"):
            continue
        if str(run["data"].get("run_at", "")) < str(entity["data"].get("run_at", "")):
            previous.append(run["data"]["value"])
    result = evaluate_qc(
        previous,
        entity["data"].get("value"),
        lot["data"].get("target"),
        lot["data"].get("sd"),
        assay["data"].get("rule_config"),
    )
    if not result["accepted"] and not data.get("reject_reason"):
        result["reject_reason"] = "quality control rule violation"
    result["_next_status"] = "accepted" if result["accepted"] else "rejected"
    return result


def _validate_release(actor, entity, data, lookup):
    data = entity["data"]
    run_at = data.get("run_at")
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    run = _find_one(lookup, "qc_run", "id", data.get("qc_run_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not assay or not run or not instrument:
        raise ConflictError("assay, qc run and instrument must exist")
    # The release is verified for exactly this assay, instrument and run time.
    if run["data"].get("assay_id") != assay["id"]:
        raise ConflictError("qc run belongs to another assay")
    if run["data"].get("instrument_id") != instrument["id"]:
        raise ConflictError("qc run belongs to another instrument")
    if run["status"] != "accepted":
        raise ConflictError("result batch can only be released with an accepted QC run")
    if instrument["status"] != "ready":
        raise ConflictError("instrument is not ready")

    lot = _find_one(lookup, "qc_lot", "id", run["data"].get("qc_lot_id"))
    if not lot:
        raise ConflictError("qc lot of the accepted run is missing")

    # Certificate in force at the patient run time (not necessarily the current one).
    certificate, certificate_replaced = calibration_at(instrument, run_at)
    context = {
        "assay_id": assay["id"],
        "instrument_id": instrument["id"],
        "run_at": run_at,
        "qc_lot_id": lot["id"],
        "qc_lot_no": lot["data"].get("lot_no"),
        "qc_lot_status": lot["status"],
        "certificate_id": certificate.get("certificate_id"),
        "calibration_due": certificate.get("calibration_due"),
    }
    reasons = []

    # 1) The QC lot must be in force at run time: not expired, not switched out.
    if day_prefix(lot["data"].get("expires_at")) < day_prefix(run_at):
        reasons.append(
            "qc lot %s expired on %s before run time %s"
            % (lot["data"].get("lot_no"), day_prefix(lot["data"].get("expires_at")), run_at)
        )
    if lot["data"].get("replaced_by_lot_id"):
        switched_at = lot["data"].get("switched_at")
        if switched_at and str(run_at) >= str(switched_at):
            reasons.append(
                "qc lot %s was switched out at %s; this run must use the replacement lot"
                % (lot["data"].get("lot_no"), switched_at)
            )

    # 2) The old lot must not carry open (unresolved) out-of-control cases.
    open_failures = _open_qc_failure_runs(lookup, lot["id"], instrument["id"])
    if open_failures:
        context["open_failure_run_ids"] = [item["id"] for item in open_failures]
        reasons.append(
            "qc lot %s has %d unresolved out-of-control run(s): %s"
            % (
                lot["data"].get("lot_no"),
                len(open_failures),
                ", ".join(item["id"] for item in open_failures),
            )
        )

    # 3) Calibration certificate must cover the run time and still be current.
    if not calibration_is_valid(certificate.get("calibration_due"), run_at):
        reasons.append(
            "calibration certificate %s was not valid at run time %s (due %s)"
            % (certificate.get("certificate_id"), run_at, certificate.get("calibration_due"))
        )
    if certificate_replaced:
        reasons.append(
            "calibration certificate %s in force at run time has since been replaced by %s"
            % (certificate.get("certificate_id"), instrument["data"].get("certificate_id"))
        )

    # 4) Another intercepted batch on the instrument must be resolved first.
    active_holds = []
    for batch in lookup("result_batch", "instrument_id", instrument["id"]) or []:
        if batch["id"] != entity["id"] and batch["status"] == "intercepted":
            active_holds.append(batch)
    if active_holds:
        reasons.append(
            "an intercepted result batch must be resolved first: %s"
            % ", ".join(batch["id"] for batch in active_holds)
        )

    if reasons:
        # The batch keeps its current status; the service records a release_blocked audit entry.
        raise ReleaseBlocked(reasons, context)

    return {
        "released_by": actor.user_id,
        "release_qc_lot_id": lot["id"],
        "release_qc_lot_no": lot["data"].get("lot_no"),
        "release_certificate_id": certificate.get("certificate_id"),
        "_audit_detail": context,
    }


def _validate_qc_retest(actor, entity, data, lookup):
    replacement = _find_one(lookup, "qc_run", "id", data.get("replacement_run_id"))
    if not replacement or replacement["status"] != "accepted":
        raise ValidationError("a replacement run must exist and be accepted")
    if replacement["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("replacement run belongs to another assay")
    return {"replacement_run_id": replacement["id"]}


def _validate_switch_lot(actor, entity, data, lookup):
    if entity["status"] != "registered":
        raise ValidationError("replacement lot must be registered before switch-in")
    previous = _find_one(lookup, "qc_lot", "id", data.get("previous_lot_id"))
    if not previous or previous["status"] != "active":
        raise ValidationError("previous active lot is required")
    if previous["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("lots must belong to the same assay")
    switched_at = data.get("switched_at")
    if not switched_at:
        raise ValidationError("switched_at is required")
    switched_at = str(switched_at)
    # The new lot must itself be usable at the takeover moment.
    if day_prefix(entity["data"].get("expires_at")) < day_prefix(switched_at):
        raise ValidationError("replacement lot is already expired at the switch time")
    return {
        "replaces_lot_id": previous["id"],
        "switched_at": switched_at,
        "_side_effect": {
            "kind": "deactivate_previous_lot",
            "previous_lot_id": previous["id"],
            "previous_lot_no": previous["data"].get("lot_no"),
            "replacement_lot_id": entity["id"],
            "replacement_lot_no": entity["data"].get("lot_no"),
            "switched_at": switched_at,
        },
        "_audit_detail": {
            "previous_lot_id": previous["id"],
            "previous_lot_no": previous["data"].get("lot_no"),
            "replacement_lot_id": entity["id"],
            "replacement_lot_no": entity["data"].get("lot_no"),
            "switched_at": switched_at,
        },
    }


def _validate_correct(actor, entity, data, lookup):
    if not data.get("reason"):
        raise ValidationError("correction reason is required")
    history = list(entity["data"].get("correction_history") or [])
    history.append({"actor_id": actor.user_id, "reason": data["reason"], "from_status": entity["status"]})
    return {"correction_history": history}


class RuleEngine:
    ALIASES = {
        "assays": "assay",
        "qc_lots": "qc_lot",
        "instruments": "instrument",
        "qc_runs": "qc_run",
        "result_batches": "result_batch",
    }
    INITIAL_STATUS = {
        "assay": "active",
        "qc_lot": "registered",
        "instrument": "ready",
        "qc_run": "pending",
        "result_batch": "waiting",
    }
    TRANSITIONS = {
        "assay": {
            "suspend": (("active",), "suspended"),
            "restore": (("suspended",), "active"),
        },
        "qc_lot": {
            "activate": (("registered", "suspended"), "active"),
            "switch_in": (("registered",), "active"),
            "suspend": (("active",), "suspended"),
            "retire": (("active", "suspended", "switched_out"), "retired"),
        },
        "instrument": {
            "calibrate": (("ready", "maintenance", "failed"), "ready"),
            "fail": (("ready",), "failed"),
            "maintain": (("ready", "failed"), "maintenance"),
            "restore": (("maintenance", "failed"), "ready"),
        },
        "qc_run": {
            "evaluate": (("pending",), "pending"),
            "retest": (("rejected",), "retesting"),
            "investigate": (("rejected",), "investigated"),
            "resolve": (("investigated", "retesting"), "resolved"),
            "correct": (("accepted", "rejected", "investigated", "resolved"), "pending"),
        },
        "result_batch": {
            "release": (("waiting",), "released"),
            "intercept": (("waiting",), "intercepted"),
            "retest": (("intercepted",), "waiting"),
            "investigate": (("intercepted",), "investigating"),
            "resolve": (("investigating",), "resolved"),
            "correct": (("waiting", "intercepted", "investigating", "released", "resolved"), "waiting"),
        },
    }
    CREATE_REQUIRED = {
        "assay": ("name", "unit", "allowed_low", "allowed_high"),
        "qc_lot": ("assay_id", "lot_no", "target", "sd", "expires_at"),
        "instrument": ("name", "serial", "calibration_due"),
        "qc_run": ("assay_id", "qc_lot_id", "instrument_id", "value", "run_at"),
        "result_batch": ("assay_id", "instrument_id", "qc_run_id", "run_at", "patient_count"),
    }
    ACTION_REQUIRED = {
        ("assay", "suspend"): ("reason",),
        ("qc_lot", "switch_in"): ("previous_lot_id", "switched_at"),
        ("qc_lot", "suspend"): ("reason",),
        ("qc_lot", "retire"): ("reason",),
        ("instrument", "calibrate"): ("calibration_due", "certificate_id"),
        ("instrument", "fail"): ("reason",),
        ("instrument", "maintain"): ("reason",),
        ("qc_run", "evaluate"): ("evaluated_by",),
        ("qc_run", "retest"): ("reason",),
        ("qc_run", "investigate"): ("reason",),
        ("qc_run", "resolve"): ("resolution",),
        ("qc_run", "correct"): ("reason", "value"),
        ("result_batch", "release"): ("reviewer_id",),
        ("result_batch", "intercept"): ("reason",),
        ("result_batch", "retest"): ("replacement_run_id", "reason"),
        ("result_batch", "investigate"): ("reason",),
        ("result_batch", "resolve"): ("resolution",),
        ("result_batch", "correct"): ("reason",),
    }
    CREATE_ROLES = {
        "assay": ("supervisor", "admin"),
        "qc_lot": ("supervisor", "admin"),
        "instrument": ("supervisor", "admin"),
        "qc_run": ("operator", "supervisor", "admin"),
        "result_batch": ("operator", "supervisor", "admin"),
    }
    ROLE_ACTIONS = {
        "suspend": ("supervisor", "admin"),
        "restore": ("supervisor", "admin"),
        "activate": ("supervisor", "admin"),
        "switch_in": ("supervisor", "admin"),
        "retire": ("supervisor", "admin"),
        "calibrate": ("supervisor", "admin"),
        "fail": ("operator", "supervisor", "admin"),
        "maintain": ("operator", "supervisor", "admin"),
        "evaluate": ("operator", "supervisor", "admin"),
        "retest": ("operator", "supervisor", "admin"),
        "investigate": ("supervisor", "admin"),
        "resolve": ("supervisor", "admin"),
        "correct": ("supervisor", "admin"),
        "release": ("supervisor", "admin"),
        "intercept": ("operator", "supervisor", "admin"),
    }
    CUSTOM_CREATE = {
        "assay": _validate_assay,
        "qc_lot": _validate_qc_lot,
        "instrument": _validate_instrument,
        "qc_run": _validate_qc_run,
        "result_batch": _validate_result_batch,
    }
    CUSTOM_TRANSITIONS = {
        ("qc_run", "evaluate"): _validate_evaluate,
        ("result_batch", "release"): _validate_release,
        ("result_batch", "retest"): _validate_qc_retest,
        ("qc_lot", "switch_in"): _validate_switch_lot,
        ("instrument", "calibrate"): _validate_calibrate,
        ("qc_run", "correct"): _validate_correct,
        ("result_batch", "correct"): _validate_correct,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        markers = {}
        for key in ("_next_status", "_side_effect", "_audit_detail"):
            if extra.get(key) is not None:
                markers[key] = extra.pop(key)
        if markers.get("_next_status"):
            next_status = markers["_next_status"]
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch, markers
