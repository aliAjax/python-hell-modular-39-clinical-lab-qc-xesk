from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def calibration_is_valid(calibration_due, as_of):
    return str(calibration_due)[:10] >= str(as_of)[:10]


# QC runs still out of control: rejected, under investigation or being retested.
OPEN_QC_FAILURE_STATUSES = ("rejected", "investigated", "retesting")
EPOCH = "1970-01-01T00:00:00Z"


def calibration_history(instrument):
    """Calibration certificate timeline of an instrument, oldest first."""
    history = list((instrument or {}).get("data", {}).get("calibration_history") or [])
    if history:
        return history
    due = (instrument or {}).get("data", {}).get("calibration_due")
    if due:
        # Records created before certificate history was tracked: treat the
        # stored due date as an epoch-valid baseline so it keeps working.
        return [{"certificate_id": None, "calibration_due": due, "calibrated_at": EPOCH}]
    return []


def effective_calibration(instrument, as_of):
    """Certificate in force on the instrument at a given moment."""
    moment = str(as_of or "")
    current = None
    for entry in calibration_history(instrument):
        if str(entry.get("calibrated_at", EPOCH)) <= moment:
            current = entry
    return current


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
    # Seed the certificate timeline so audits always know which certificate
    # covered the first runs on the instrument.
    calibrated_at = data.get("calibrated_at") or EPOCH
    history = [
        {
            "certificate_id": data.get("certificate_id"),
            "calibration_due": data.get("calibration_due"),
            "calibrated_at": calibrated_at,
        }
    ]
    return {"calibration_due": data.get("calibration_due"), "calibration_history": history}


def _validate_qc_run(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", data.get("qc_lot_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not assay or not lot or not instrument:
        raise ValidationError("assay, qc lot and instrument are required")
    if lot["data"].get("assay_id") != assay["id"]:
        raise ValidationError("qc lot does not belong to the assay")
    if lot["status"] in ("retired", "suspended"):
        raise ValidationError("qc lot is not active: " + lot["status"])
    if lot["status"] == "active" and lot["data"].get("activated_at"):
        # From the takeover moment the new lot owns new runs; the retired lot
        # must not be used even though its historical runs stay valid.
        if str(data.get("run_at", "")) < str(lot["data"]["activated_at"]):
            raise ValidationError("qc run predates the lot takeover time")
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        raise ValidationError("qc result value must be numeric")
    return {"value": value}


def _validate_result_batch(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    run = _find_one(lookup, "qc_run", "id", data.get("qc_run_id"))
    if not assay:
        raise ValidationError("assay does not exist")
    if not instrument:
        raise ValidationError("instrument does not exist")
    if not run:
        raise ValidationError("qc run does not exist")
    if run["data"].get("assay_id") != assay["id"]:
        raise ValidationError("qc run does not belong to the assay")
    if run["data"].get("instrument_id") != instrument["id"]:
        raise ValidationError("qc run was not performed on the instrument")
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


def _release_snapshot(batch, run, assay, lot, instrument, run_certificate, batch_certificate):
    return {
        "assay_id": batch["data"].get("assay_id"),
        "instrument_id": batch["data"].get("instrument_id"),
        "qc_run_id": run["id"],
        "qc_lot_id": lot["id"],
        "lot_no": lot["data"].get("lot_no"),
        "lot_status": lot["status"],
        "lot_takeover_at": lot["data"].get("activated_at")
        or lot["data"].get("switched_at"),
        "lot_retires_at": lot["data"].get("retired_at"),
        "lot_expires_at": lot["data"].get("expires_at"),
        "qc_run_at": run["data"].get("run_at"),
        "result_run_at": batch["data"].get("run_at"),
        "qc_certificate_id": (run_certificate or {}).get("certificate_id"),
        "qc_certificate_due": (run_certificate or {}).get("calibration_due"),
        "current_certificate_id": (batch_certificate or {}).get("certificate_id"),
        "current_certificate_due": (batch_certificate or {}).get("calibration_due"),
        "instrument_status": instrument["status"] if instrument else None,
    }


def _validate_release(actor, entity, data, lookup):
    batch_assay_id = entity["data"].get("assay_id")
    instrument_id = entity["data"].get("instrument_id")
    run_at = str(entity["data"].get("run_at", ""))
    run = _find_one(lookup, "qc_run", "id", entity["data"].get("qc_run_id"))
    assay = _find_one(lookup, "assay", "id", batch_assay_id)
    instrument = _find_one(lookup, "instrument", "id", instrument_id)
    if not run or run["status"] != "accepted":
        raise ConflictError("result batch can only be released with an accepted QC run")
    if not assay or run["data"].get("assay_id") != assay["id"]:
        raise ConflictError("qc run assay does not match the result batch assay")
    if not instrument or run["data"].get("instrument_id") != instrument["id"]:
        raise ConflictError("qc run instrument does not match the result batch instrument")
    if assay["status"] != "active":
        raise ConflictError("assay is not active")
    if instrument["status"] != "ready":
        raise ConflictError("instrument is not ready")

    lot = _find_one(lookup, "qc_lot", "id", run["data"].get("qc_lot_id"))
    if not lot:
        raise ConflictError("qc lot used by the run no longer exists")
    takeover_at = lot["data"].get("activated_at") or lot["data"].get("switched_at")
    lot_run_at = str(run["data"].get("run_at", ""))

    if lot["status"] == "active":
        # New lot only governs runs performed from the takeover moment; earlier
        # results still belong to the previous lot and must use that lot's run.
        if takeover_at and lot_run_at < str(takeover_at):
            raise ConflictError("qc run predates the lot takeover time")
    elif lot["status"] == "retired":
        retired_at = lot["data"].get("retired_at")
        if not retired_at:
            raise ConflictError("retired qc lot has no recorded switch time")
        # Results from before the switch are still judged under the old lot;
        # anything after it must ride the new lot that took over.
        if lot_run_at >= str(retired_at) or run_at >= str(retired_at):
            raise ConflictError("qc lot was retired before the result run time; use the new lot")
    else:
        raise ConflictError("qc lot is not active: " + lot["status"])

    if str(lot["data"].get("expires_at", ""))[:10] < run_at[:10]:
        raise ConflictError("qc lot had expired at result run time")

    # Open out-of-control runs of the same lot on the same instrument keep the
    # lot's results on hold until the failure is closed.
    open_failures = []
    for other in lookup("qc_run", "instrument_id", instrument_id) or []:
        if other["data"].get("qc_lot_id") != lot["id"]:
            continue
        if other["data"].get("assay_id") != batch_assay_id:
            continue
        if other["status"] in OPEN_QC_FAILURE_STATUSES:
            open_failures.append(other["id"])
    if open_failures:
        raise ConflictError(
            "qc lot has unresolved out-of-control runs: " + ", ".join(sorted(open_failures))
        )

    run_certificate = effective_calibration(instrument, run["data"].get("run_at"))
    batch_certificate = effective_calibration(instrument, run_at)
    if not batch_certificate:
        raise ConflictError("instrument has no calibration certificate covering the result run time")
    if not calibration_is_valid(batch_certificate.get("calibration_due"), run_at):
        raise ConflictError("calibration certificate is expired at result run time")
    if not run_certificate:
        raise ConflictError("instrument has no calibration certificate covering the qc run time")
    if not calibration_is_valid(run_certificate.get("calibration_due"), run["data"].get("run_at")):
        raise ConflictError("calibration certificate was expired when qc was run")
    # A certificate renewal between QC and release invalidates the QC evidence:
    # the instrument must be re-qualified under the new certificate first.
    if run_certificate.get("certificate_id") != batch_certificate.get("certificate_id"):
        raise ConflictError(
            "calibration certificate changed after qc run (qc used %s, current %s); rerun qc"
            % (run_certificate.get("certificate_id"), batch_certificate.get("certificate_id"))
        )

    active_holds = []
    for batch in lookup("result_batch", "instrument_id", instrument_id) or []:
        if batch["id"] != entity["id"] and batch["status"] == "intercepted":
            active_holds.append(batch)
    if active_holds:
        raise ConflictError("an intercepted result batch must be resolved first")

    snapshot = _release_snapshot(
        entity, run, assay, lot, instrument, run_certificate, batch_certificate
    )
    return {"released_by": actor.user_id, "release_validation": snapshot, "_audit": snapshot}


def _validate_calibrate(actor, entity, data, lookup):
    calibrated_at = data.get("calibrated_at")
    history = calibration_history(entity)
    last = history[-1] if history else None
    if last and calibrated_at and str(calibrated_at) < str(last.get("calibrated_at", EPOCH)):
        raise ValidationError("calibration cannot be older than the previous one")
    if data.get("calibration_due") and calibrated_at:
        if str(data["calibration_due"])[:10] < str(calibrated_at)[:10]:
            raise ValidationError("calibration due date cannot precede calibration time")
    return {}


def _validate_qc_retest(actor, entity, data, lookup):
    replacement = _find_one(lookup, "qc_run", "id", data.get("replacement_run_id"))
    if not replacement or replacement["status"] != "accepted":
        raise ValidationError("a replacement run must exist and be accepted")
    if replacement["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("replacement run belongs to another assay")
    return {"replacement_run_id": replacement["id"]}


def _validate_switch_lot(actor, entity, data, lookup):
    previous = _find_one(lookup, "qc_lot", "id", data.get("previous_lot_id"))
    if not previous or previous["status"] != "active":
        raise ValidationError("previous active lot is required")
    if previous["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("lots must belong to the same assay")
    switched_at = data.get("switched_at")
    return {
        "replaces_lot_id": previous["id"],
        "switched_at": switched_at,
        "activated_at": switched_at,
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
            "retire": (("active", "suspended"), "retired"),
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
        if extra.get("_next_status"):
            next_status = extra.pop("_next_status")
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
