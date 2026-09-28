import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


SWITCH_AT = "2026-09-27T09:00:00Z"


class LotSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "switch.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")

    def tearDown(self):
        self.tmp.cleanup()

    def _base(self, expires_at="2099-01-01", cert_due="2099-01-01"):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": expires_at},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {
                "name": "Analyzer A",
                "serial": "A-100",
                "calibration_due": cert_due,
                "certificate_id": "CERT-1",
                "calibrated_at": "2026-09-01T00:00:00Z",
            },
        )
        return assay, lot, instrument

    def _accepted_run(self, assay, lot, instrument, value, run_at):
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": run_at,
            },
        )
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})
        self.assertEqual(run["status"], "accepted")
        return run

    def _waiting_batch(self, assay, instrument, run, run_at, count=3):
        return self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": run_at,
                "patient_count": count,
            },
        )

    def _switch(self, assay, first, lot_no="LOT-2", target=5.1):
        second = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": lot_no, "target": target, "sd": 0.1, "expires_at": "2099-06-01"},
        )
        return self.service.transition(
            self.supervisor,
            second["id"],
            "switch_in",
            {"previous_lot_id": first["id"], "switched_at": SWITCH_AT},
        )

    def test_switch_takes_over_and_retires_old_lot_atomically(self):
        assay, first, instrument = self._base()
        second = self._switch(assay, first)

        self.assertEqual(second["status"], "active")
        self.assertEqual(second["data"]["replaces_lot_id"], first["id"])
        self.assertEqual(second["data"]["activated_at"], SWITCH_AT)

        old = self.service.get(first["id"])
        self.assertEqual(old["status"], "retired")
        self.assertEqual(old["data"]["retired_at"], SWITCH_AT)
        self.assertEqual(old["data"]["replaced_by_lot_id"], second["id"])

        active = [lot for lot in self.service.list("qc_lot", status="active") if lot["data"]["assay_id"] == assay["id"]]
        self.assertEqual([lot["id"] for lot in active], [second["id"]])

        audits = self.service.audit_log()
        retire = [entry for entry in audits if entry["entity_id"] == first["id"] and entry["action"] == "retire"]
        take_over = [entry for entry in audits if entry["entity_id"] == second["id"] and entry["action"] == "switch_in"]
        self.assertEqual(len(retire), 1)
        self.assertEqual(retire[0]["from_status"], "active")
        self.assertEqual(retire[0]["to_status"], "retired")
        self.assertEqual(retire[0]["detail"]["switched_at"], SWITCH_AT)
        self.assertEqual(retire[0]["detail"]["replaced_by_lot_id"], second["id"])
        self.assertEqual(len(take_over), 1)

    def test_retired_lot_cannot_be_previous_lot_again(self):
        assay, first, instrument = self._base()
        self._switch(assay, first)
        third = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-3", "target": 5.2, "sd": 0.1, "expires_at": "2099-06-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.supervisor,
                third["id"],
                "switch_in",
                {"previous_lot_id": first["id"], "switched_at": SWITCH_AT},
            )

    def test_pre_switch_results_still_release_against_old_lot(self):
        assay, first, instrument = self._base()
        run = self._accepted_run(assay, first, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._waiting_batch(assay, instrument, run, "2026-09-27T08:30:00Z")

        self._switch(assay, first)

        released = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(released["status"], "released")

        audits = {entry["entity_id"]: entry for entry in self.service.audit_log() if entry["action"] == "release"}
        detail = audits[batch["id"]]["detail"]
        self.assertEqual(detail["qc_lot_id"], first["id"])
        self.assertEqual(detail["lot_no"], "LOT-1")
        self.assertEqual(detail["lot_status"], "retired")
        self.assertEqual(detail["qc_certificate_id"], "CERT-1")
        self.assertEqual(detail["current_certificate_id"], "CERT-1")
        self.assertEqual(detail["qc_run_id"], run["id"])

    def test_post_switch_results_must_use_new_lot(self):
        assay, first, instrument = self._base()
        old_run = self._accepted_run(assay, first, instrument, 5.02, "2026-09-27T08:00:00Z")
        second = self._switch(assay, first)

        # New QC work must not be booked on the retired lot.
        with self.assertRaises(ValidationError):
            self.service.create(
                self.supervisor,
                "qc_run",
                {
                    "assay_id": assay["id"],
                    "qc_lot_id": first["id"],
                    "instrument_id": instrument["id"],
                    "value": 5.03,
                    "run_at": "2026-09-27T09:10:00Z",
                },
            )

        # A patient run after the switch cannot ride the pre-switch QC run.
        late_batch = self._waiting_batch(assay, instrument, old_run, "2026-09-27T09:30:00Z")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.supervisor, late_batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertIn("retired before the result run time", str(ctx.exception))
        self.assertEqual(self.service.get(late_batch["id"])["status"], "waiting")

        # QC and patient results on the new lot release normally.
        new_run = self._accepted_run(assay, second, instrument, 5.11, "2026-09-27T09:10:00Z")
        new_batch = self._waiting_batch(assay, instrument, new_run, "2026-09-27T09:20:00Z")
        released = self.service.transition(self.supervisor, new_batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(released["status"], "released")

    def test_expired_lot_blocks_release_and_keeps_waiting(self):
        assay, lot, instrument = self._base(expires_at="2026-09-20")
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._waiting_batch(assay, instrument, run, "2026-09-27T08:05:00Z")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

    def test_certificate_renewal_between_qc_and_release_blocks(self):
        assay, lot, instrument = self._base()
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._waiting_batch(assay, instrument, run, "2026-09-27T08:30:00Z")

        recalibrated = self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {
                "certificate_id": "CERT-2",
                "calibration_due": "2099-12-31",
                "calibrated_at": "2026-09-27T08:15:00Z",
            },
        )
        self.assertEqual(
            [entry["certificate_id"] for entry in recalibrated["data"]["calibration_history"]],
            ["CERT-1", "CERT-2"],
        )

        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        message = str(ctx.exception)
        self.assertIn("certificate changed", message)
        self.assertIn("CERT-1", message)
        self.assertIn("CERT-2", message)
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

        # QC rerun under the new certificate restores release.
        fresh_run = self._accepted_run(assay, lot, instrument, 5.01, "2026-09-27T08:20:00Z")
        fresh_batch = self._waiting_batch(assay, instrument, fresh_run, "2026-09-27T08:25:00Z")
        released = self.service.transition(self.supervisor, fresh_batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(released["status"], "released")
        self.assertEqual(
            released["data"]["release_validation"]["qc_certificate_id"], "CERT-2"
        )

    def test_expired_certificate_at_result_time_blocks_release(self):
        assay, lot, instrument = self._base(cert_due="2026-09-20")
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        batch = self._waiting_batch(assay, instrument, run, "2026-09-27T08:05:00Z")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertIn("expired at result run time", str(ctx.exception))
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

    def test_open_out_of_control_run_on_lot_blocks_release(self):
        assay, lot, instrument = self._base()
        bad = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": 9.9,
                "run_at": "2026-09-27T07:50:00Z",
            },
        )
        bad = self.service.transition(self.supervisor, bad["id"], "evaluate", {"evaluated_by": "qc-1"})
        self.assertEqual(bad["status"], "rejected")

        good = self._accepted_run(assay, lot, instrument, 5.01, "2026-09-27T08:00:00Z")
        batch = self._waiting_batch(assay, instrument, good, "2026-09-27T08:05:00Z")

        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertIn("unresolved out-of-control", str(ctx.exception))
        self.assertIn(bad["id"], str(ctx.exception))
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

        # Closing the failure (investigate then resolve) clears the hold.
        bad = self.service.transition(
            self.supervisor, bad["id"], "investigate", {"reason": "probe contamination"}
        )
        self.service.transition(
            self.supervisor, bad["id"], "resolve", {"resolution": "new reagent bottle fitted"}
        )
        released = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(released["status"], "released")

    def test_switch_is_allowed_while_old_lot_has_open_failure(self):
        assay, lot, instrument = self._base()
        bad = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": 9.9,
                "run_at": "2026-09-27T07:50:00Z",
            },
        )
        self.service.transition(self.supervisor, bad["id"], "evaluate", {"evaluated_by": "qc-1"})
        # Switching lots does not silently close an investigation; the old lot
        # is retired but its failure stays open and remains auditable.
        second = self._switch(assay, lot)
        self.assertEqual(second["status"], "active")
        self.assertEqual(self.service.get(lot["id"])["status"], "retired")
        self.assertEqual(self.service.get(bad["id"])["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
