import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ReleaseBlocked, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, calibration_at
from src.service import DomainService


class LotSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "lot_switch.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.operator = Actor("qc-operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _assay(self, name="Glucose"):
        return self.service.create(
            self.supervisor,
            "assay",
            {"name": name, "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )

    def _lot(self, assay, lot_no, expires_at="2099-01-01", target=5.0):
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {
                "assay_id": assay["id"],
                "lot_no": lot_no,
                "target": target,
                "sd": 0.1,
                "expires_at": expires_at,
            },
        )
        return self.service.transition(
            self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"}
        )

    def _instrument(self, calibration_due="2099-01-01", certificate_id="CERT-1"):
        return self.service.create(
            self.supervisor,
            "instrument",
            {
                "name": "Analyzer A",
                "serial": "A-100",
                "calibration_due": calibration_due,
                "certificate_id": certificate_id,
            },
        )

    def _accepted_run(self, assay, lot, instrument, value, run_at):
        run = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": run_at,
            },
        )
        run = self.service.transition(
            self.operator, run["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        self.assertEqual(run["status"], "accepted")
        return run

    def _batch(self, assay, instrument, run, run_at, patient_count=4):
        return self.service.create(
            self.operator,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": run_at,
                "patient_count": patient_count,
            },
        )

    def _switch(self, assay, first, second_no, switched_at, expires_at="2099-06-01"):
        second = self.service.create(
            self.supervisor,
            "qc_lot",
            {
                "assay_id": assay["id"],
                "lot_no": second_no,
                "target": 5.1,
                "sd": 0.1,
                "expires_at": expires_at,
            },
        )
        second = self.service.transition(
            self.supervisor,
            second["id"],
            "switch_in",
            {"previous_lot_id": first["id"], "switched_at": switched_at},
        )
        return self.service.get(first["id"]), second

    def test_switch_deactivates_old_lot_and_records_handover(self):
        assay = self._assay()
        first = self._lot(assay, "LOT-1")
        old, second = self._switch(assay, first, "LOT-2", "2026-09-27T09:00:00Z")

        self.assertEqual(second["status"], "active")
        self.assertEqual(second["data"]["replaces_lot_id"], first["id"])
        self.assertEqual(second["data"]["switched_at"], "2026-09-27T09:00:00Z")

        self.assertEqual(old["status"], "switched_out")
        self.assertEqual(old["data"]["replaced_by_lot_id"], second["id"])
        self.assertEqual(old["data"]["switched_at"], "2026-09-27T09:00:00Z")

        # Only the new lot is active for the assay after the handover.
        active = [lot for lot in self.service.list("qc_lot") if lot["status"] == "active"]
        self.assertEqual([lot["id"] for lot in active], [second["id"]])

        # Audit shows the takeover from both sides.
        actions = {entry["action"]: entry for entry in self.service.audit_log()}
        self.assertIn("switch_in", actions)
        self.assertIn("switch_out", actions)
        detail = actions["switch_out"]["detail"]
        self.assertEqual(detail["previous_lot_id"], first["id"])
        self.assertEqual(detail["replacement_lot_id"], second["id"])
        self.assertEqual(detail["previous_lot_no"], "LOT-1")
        self.assertEqual(detail["replacement_lot_no"], "LOT-2")
        self.assertEqual(actions["switch_out"]["from_status"], "active")
        self.assertEqual(actions["switch_out"]["to_status"], "switched_out")

    def test_pre_switch_run_still_released_against_old_lot(self):
        assay = self._assay()
        first = self._lot(assay, "LOT-1")
        instrument = self._instrument()
        run = self._accepted_run(assay, first, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z")

        self._switch(assay, first, "LOT-2", "2026-09-27T09:00:00Z")

        released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["release_qc_lot_id"], first["id"])
        self.assertEqual(released["data"]["release_qc_lot_no"], "LOT-1")
        self.assertEqual(released["data"]["release_certificate_id"], "CERT-1")

    def test_post_switch_run_against_old_lot_is_blocked(self):
        assay = self._assay()
        first = self._lot(assay, "LOT-1")
        instrument = self._instrument()
        # Backfill the run record after the switch, timestamped in the new window.
        old, second = self._switch(assay, first, "LOT-2", "2026-09-27T09:00:00Z")
        with self.assertRaises(ValidationError):
            self._accepted_run(assay, old, instrument, 5.02, "2026-09-27T10:00:00Z")

        # A run stamped in the takeover window but already present must not release.
        run = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": first["id"],
                "instrument_id": instrument["id"],
                "value": 5.02,
                "run_at": "2026-09-27T08:30:00Z",
            },
        )
        # Force the run timestamp past the handover to simulate stale data.
        run_data = dict(run["data"])
        run_data["run_at"] = "2026-09-27T10:30:00Z"
        self.service.repository.update_entity(run["id"], run["version"], run["status"], run_data)
        run = self.service.get(run["id"])
        run = self.service.transition(
            self.operator, run["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        batch = self._batch(assay, instrument, run, "2026-09-27T10:35:00Z")
        with self.assertRaises(ReleaseBlocked) as caught:
            self.service.transition(
                self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
            )
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")
        self.assertTrue(any("switched out" in reason for reason in caught.exception.reasons))

    def test_new_lot_takes_over_runs_after_switch(self):
        assay = self._assay()
        first = self._lot(assay, "LOT-1")
        instrument = self._instrument()
        _, second = self._switch(assay, first, "LOT-2", "2026-09-27T09:00:00Z")
        run = self._accepted_run(assay, second, instrument, 5.11, "2026-09-27T09:30:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T09:35:00Z")
        released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["release_qc_lot_id"], second["id"])

    def test_expired_lot_blocks_release_and_keeps_status(self):
        assay = self._assay()
        lot = self._lot(assay, "OLD", expires_at="2026-09-20")
        instrument = self._instrument()
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z")

        with self.assertRaises(ReleaseBlocked) as caught:
            self.service.transition(
                self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
            )
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")
        self.assertTrue(any("expired" in reason for reason in caught.exception.reasons))
        self.assertEqual(caught.exception.context["qc_lot_no"], "OLD")

        blocked = [
            entry for entry in self.service.audit_log(batch["id"])
            if entry["action"] == "release_blocked"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["from_status"], "waiting")
        self.assertEqual(blocked[0]["to_status"], "waiting")
        self.assertEqual(blocked[0]["detail"]["qc_lot_id"], lot["id"])
        self.assertEqual(blocked[0]["detail"]["certificate_id"], "CERT-1")
        self.assertTrue(blocked[0]["detail"]["reasons"])

    def test_calibration_replacement_after_run_blocks_release(self):
        assay = self._assay()
        lot = self._lot(assay, "LOT-1")
        instrument = self._instrument(calibration_due="2099-01-01", certificate_id="CERT-1")
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z")

        self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {
                "calibration_due": "2099-12-01",
                "certificate_id": "CERT-2",
                "calibrated_at": "2026-09-27T12:00:00Z",
            },
        )

        with self.assertRaises(ReleaseBlocked) as caught:
            self.service.transition(
                self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
            )
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")
        self.assertTrue(any("replaced" in reason for reason in caught.exception.reasons))
        self.assertEqual(caught.exception.context["certificate_id"], "CERT-1")

        # A run after recalibration uses the new certificate and may release.
        later_run = self._accepted_run(assay, lot, instrument, 5.01, "2026-09-27T12:30:00Z")
        later_batch = self._batch(assay, instrument, later_run, "2026-09-27T12:35:00Z")
        released = self.service.transition(
            self.supervisor, later_batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["release_certificate_id"], "CERT-2")

    def test_calibration_history_picks_certificate_at_run_time(self):
        assay = self._assay()
        instrument = self._instrument(calibration_due="2026-06-01", certificate_id="CERT-1")
        self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {
                "calibration_due": "2099-01-01",
                "certificate_id": "CERT-2",
                "calibrated_at": "2026-09-01T00:00:00Z",
            },
        )
        instrument = self.service.get(instrument["id"])
        before, replaced_before = calibration_at(instrument, "2026-08-01T08:00:00Z")
        after, replaced_after = calibration_at(instrument, "2026-09-20T08:00:00Z")
        self.assertEqual(before["certificate_id"], "CERT-1")
        self.assertTrue(replaced_before)
        self.assertEqual(after["certificate_id"], "CERT-2")
        self.assertFalse(replaced_after)

    def test_expired_certificate_at_run_time_blocks_release(self):
        assay = self._assay()
        lot = self._lot(assay, "LOT-1")
        instrument = self._instrument(calibration_due="2026-09-26", certificate_id="CERT-1")
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z")
        with self.assertRaises(ReleaseBlocked) as caught:
            self.service.transition(
                self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
            )
        self.assertTrue(
            any("not valid at run time" in reason for reason in caught.exception.reasons)
        )
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

    def test_open_out_of_control_on_lot_blocks_release_until_resolved(self):
        assay = self._assay()
        lot = self._lot(assay, "LOT-1")
        instrument = self._instrument()

        # One accepted run supports the batch; one rejected run on the same
        # lot/instrument keeps the out-of-control case open.
        good = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T07:00:00Z")
        bad = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": 9.9,
                "run_at": "2026-09-27T07:30:00Z",
            },
        )
        bad = self.service.transition(
            self.operator, bad["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        self.assertEqual(bad["status"], "rejected")

        batch = self._batch(assay, instrument, good, "2026-09-27T08:05:00Z")
        with self.assertRaises(ReleaseBlocked) as caught:
            self.service.transition(
                self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
            )
        self.assertTrue(
            any("unresolved out-of-control" in reason for reason in caught.exception.reasons)
        )
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

        # Close the case: investigate then resolve the rejected run.
        bad = self.service.transition(
            self.supervisor,
            bad["id"],
            "investigate",
            {"reason": "reagent bottle replaced"},
        )
        bad = self.service.transition(
            self.supervisor,
            bad["id"],
            "resolve",
            {"resolution": "recalibrated and verified"},
        )
        self.assertEqual(bad["status"], "resolved")

        released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        self.assertEqual(released["status"], "released")

    def test_release_audit_records_lot_and_certificate(self):
        assay = self._assay()
        lot = self._lot(assay, "LOT-1")
        instrument = self._instrument(calibration_due="2099-01-01", certificate_id="CERT-1")
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z")
        self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        entry = self.service.audit_log(batch["id"])[-1]
        self.assertEqual(entry["action"], "release")
        self.assertEqual(entry["to_status"], "released")
        self.assertEqual(entry["detail"]["qc_lot_id"], lot["id"])
        self.assertEqual(entry["detail"]["qc_lot_no"], "LOT-1")
        self.assertEqual(entry["detail"]["certificate_id"], "CERT-1")
        self.assertEqual(entry["detail"]["instrument_id"], instrument["id"])
        self.assertEqual(entry["detail"]["run_at"], "2026-09-27T08:05:00Z")

    def test_switch_into_expired_lot_is_rejected_old_stays_active(self):
        assay = self._assay()
        first = self._lot(assay, "LOT-1")
        second = self.service.create(
            self.supervisor,
            "qc_lot",
            {
                "assay_id": assay["id"],
                "lot_no": "LOT-BAD",
                "target": 5.1,
                "sd": 0.1,
                "expires_at": "2026-09-01",
            },
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.supervisor,
                second["id"],
                "switch_in",
                {"previous_lot_id": first["id"], "switched_at": "2026-09-27T09:00:00Z"},
            )
        self.assertEqual(self.service.get(first["id"])["status"], "active")
        self.assertEqual(self.service.get(second["id"])["status"], "registered")

    def test_calibrate_requires_new_certificate_id(self):
        instrument = self._instrument(certificate_id="CERT-1")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.supervisor,
                instrument["id"],
                "calibrate",
                {
                    "calibration_due": "2099-12-01",
                    "certificate_id": "CERT-1",
                    "calibrated_at": "2026-09-27T12:00:00Z",
                },
            )

    def test_release_blocked_is_a_conflict(self):
        self.assertTrue(issubclass(ReleaseBlocked, ConflictError))


if __name__ == "__main__":
    unittest.main()
