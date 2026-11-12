"""Public queue and CLI contracts; all fixtures are synthetic and no sends occur."""

import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


DEFAULT_SCRIPT = Path(__file__).resolve().parents[1] / "skills/brand-protection/scripts/followup_queue.py"
# The same regressions can be run against an immutable baseline without editing it.
SCRIPT = Path(os.environ.get("FOLLOWUP_QUEUE_SCRIPT", DEFAULT_SCRIPT))
spec = importlib.util.spec_from_file_location("followup_queue", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 11, 12, 4, tzinfo=timezone.utc)
NOW_TEXT = "2026-11-12T04:00:00Z"


def record():
    return {
        "case_id": "synthetic", "follow_up": {"requested": True, "enabled": True},
        "targets": [{"id": "target-1", "status": "open", "channel_plan": [
            {"provider_id": "provider-1", "route": "email"}]}],
        "providers": [{"id": "provider-1"}],
        "actions": [{"id": "notice-1", "status": "acknowledged",
                     "target_ids": ["target-1"], "provider_id": "provider-1", "route": "email"}],
        "followup_tasks": [], "outbound_attempts": [], "deadlines": [], "runs": [],
    }


def task(id="task-1", **overrides):
    value = {"id": id, "action_id": "notice-1", "kind": "check_feedback",
             "status": "planned", "due_at": "2026-11-12T12:00:00+08:00"}
    value.update(overrides)
    return value


def outbound(id="task-1", **overrides):
    value = task(id, kind="remind", dedupe_key=id + "-round-1")
    value.update(overrides)
    return value


def attempt(task_record, **overrides):
    value = {"id": "attempt-1", "task_id": task_record["id"],
             "action_id": task_record["action_id"], "dedupe_key": task_record["dedupe_key"],
             "status": "outcome_unknown"}
    value.update(overrides)
    return value


def urgent_deadline():
    return {"id": "urgent", "action_id": "notice-1", "status": "open",
            "due_at": "2026-11-12T05:00:00Z"}


def running_case(kind="check_feedback", run_status="running", lease="2026-11-12T05:00:00Z"):
    case = record()
    case["runs"] = [{"id": "run-1", "status": run_status, "started_at": "2026-11-12T03:00:00Z"}]
    case["followup_tasks"] = [task("busy", kind=kind, status="in_progress", run_id="run-1",
                                    claimed_at="2026-11-12T03:00:00Z", lease_expires_at=lease)]
    if run_status == "interrupted":
        case["runs"][0].update(interrupted_at="2026-11-12T03:30:00Z",
                               interruption_evidence="Supervisor confirmed worker exit; synthetic evidence")
    if kind in {"remind", "resubmit", "send_supplement", "send_escalation", "send_correction"}:
        case["followup_tasks"][0]["dedupe_key"] = "busy-round-1"
    return case


class QueueAssertions(unittest.TestCase):
    def assert_error(self, result, section):
        errors = result.get("errors", [])
        self.assertTrue(any(e.get("section") == section and e.get("error") for e in errors), errors)

    def assert_urgent_preserved(self, result):
        self.assertIn("urgent", [d["id"] for d in result["deadline_alerts"]])

    def assert_recovery(self, result, task_id, kind):
        self.assertIn((task_id, kind), [(r.get("task_id"), r.get("kind"))
                                       for r in result.get("recovery_tasks", [])])


class QueueBehavior(QueueAssertions):
    def test_due_boundary_timezone_and_record_unchanged(self):
        case = record()
        case["followup_tasks"] = [task(), task("future", due_at="2026-11-12T04:00:01Z"),
                                    task("done", status="completed")]
        before = copy.deepcopy(case)
        result = module.build_queue(case, NOW)
        self.assertEqual([t["id"] for t in result["due_tasks"]], ["task-1"])
        self.assertEqual(case, before)
        self.assertTrue(result["queue_only"])
        self.assertTrue(result["requires_live_verification"])

    def test_sent_or_uncertain_matching_attempt_is_held(self):
        for status in ("in_flight", "outcome_unknown", "sent"):
            with self.subTest(status=status):
                case = record()
                current = outbound()
                case["followup_tasks"] = [current]
                case["outbound_attempts"] = [attempt(current, status=status)]
                result = module.build_queue(case, NOW)
                self.assertEqual(result["due_tasks"], [])
                self.assertIn("task-1", [t["id"] for t in result["held_tasks"]])

    def test_terminal_action_suppresses_old_reminder_but_allows_recheck(self):
        case = record()
        case["actions"][0]["status"] = "verified_effective"
        case["followup_tasks"] = [outbound(), task("review", kind="verify_effect")]
        result = module.build_queue(case, NOW)
        self.assertEqual([t["id"] for t in result["due_tasks"]], ["review"])
        self.assertEqual(result["held_tasks"][0]["reason"], "action_terminal")

    def test_new_round_does_not_bypass_unresolved_send(self):
        case = record()
        old = outbound("old", status="completed")
        case["followup_tasks"] = [old, outbound("new")]
        case["outbound_attempts"] = [attempt(old)]
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assertIn("new", [t["id"] for t in result["held_tasks"]])

    def test_response_window_and_reminder_cooldown_defer_stale_reminder(self):
        for field in ("expected_response_due_at", "cooldown_until"):
            with self.subTest(field=field):
                case = record()
                case["actions"][0]["followup"] = {field: "2026-11-20T17:00:00-05:00"}
                case["followup_tasks"] = [outbound()]
                result = module.build_queue(case, NOW)
                self.assertEqual(result["due_tasks"], [])
                self.assertIn("task-1", [t["id"] for t in result.get("deferred_tasks", [])])

    def test_not_before_and_live_run_do_not_requeue(self):
        case = running_case()
        case["followup_tasks"].append(task("later", not_before="2026-11-13T04:00:00Z"))
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assertIn("busy", [t["id"] for t in result["held_tasks"]])
        self.assertEqual(result.get("recovery_tasks", []), [])

    def test_bad_tasks_leave_valid_read_and_deadline_available(self):
        invalid_tasks = [None, task("bad", due_at=None), task("bad", due_at="2026-11-12T12:00:00"),
                         task("bad", kind="remind", action_id=None), task("bad", action_id="missing")]
        for invalid in invalid_tasks:
            with self.subTest(invalid=invalid):
                case = record()
                case["followup_tasks"] = [invalid, task("read")]
                case["deadlines"] = [urgent_deadline()]
                result = module.build_queue(case, NOW)
                self.assertEqual([t["id"] for t in result["due_tasks"]], ["read"])
                self.assert_error(result, "followup_tasks")
                self.assert_urgent_preserved(result)

    def test_duplicate_task_ids_are_reported_without_hiding_deadlines(self):
        case = record()
        case["followup_tasks"] = [task(), task(), task("read")]
        case["deadlines"] = [urgent_deadline()]
        result = module.build_queue(case, NOW)
        self.assert_error(result, "followup_tasks")
        self.assertIn("read", [t["id"] for t in result["due_tasks"]])
        self.assertLessEqual([t["id"] for t in result["due_tasks"]].count("task-1"), 1)
        self.assert_urgent_preserved(result)

    def test_bad_deadline_or_task_collection_does_not_erase_valid_deadline(self):
        case = record()
        case["followup_tasks"] = None
        case["deadlines"] = [{"id": "invalid", "due_at": "not-a-date"}, urgent_deadline()]
        result = module.build_queue(case, NOW)
        self.assert_error(result, "followup_tasks")
        self.assert_error(result, "deadlines")
        self.assert_urgent_preserved(result)

    def test_legal_deadlines_and_unknown_dates_are_not_operationally_guessed(self):
        case = record()
        case["deadlines"] = [
            {"id": "unknown", "due_at": None},
            urgent_deadline(),
            {"id": "expired", "due_at": "2026-11-12T03:00:00Z"},
            {"id": "exact-48h", "due_at": "2026-11-14T12:00:00+08:00"},
            {"id": "future", "due_at": "2026-11-14T04:00:01Z"},
            {"id": "resolved", "due_at": "2026-11-11T05:00:00Z", "status": "resolved"},
        ]
        result = module.build_queue(case, NOW)
        self.assertEqual([(d["id"], d["state"]) for d in result["deadline_alerts"]],
                         [("expired", "overdue"), ("urgent", "due_soon"), ("exact-48h", "due_soon")])
        self.assertEqual(result["unresolved_deadlines"], [{"id": "unknown", "reason": "due_at_unknown"}])

    def test_followup_is_not_enabled_by_template_alone(self):
        case = record()
        case["follow_up"]["enabled"] = False
        case["followup_tasks"] = [task()]
        result = module.build_queue(case, NOW)
        self.assertEqual(result["state"], "not_enabled")
        self.assertEqual(result["due_tasks"], [])


class CooldownAndDelivery(QueueAssertions):
    def test_supplement_and_escalation_do_not_inherit_reminder_cooldown(self):
        for kind in ("send_supplement", "send_escalation"):
            with self.subTest(kind=kind):
                case = record()
                case["actions"][0].update(status="needs_information", followup={
                    "last_sent_followup_at": "2026-11-11T04:00:00Z",
                    "cooldown_until": "2026-11-18T04:00:00Z"})
                case["followup_tasks"] = [outbound("reply", kind=kind)]
                case["deadlines"] = [urgent_deadline()]
                result = module.build_queue(case, NOW)
                self.assertEqual([t["id"] for t in result["due_tasks"]], ["reply"])
                self.assert_urgent_preserved(result)

    def test_reminder_and_resubmit_cooldown_are_visible_with_deadline_conflict(self):
        for kind in ("remind", "resubmit"):
            with self.subTest(kind=kind):
                case = record()
                case["actions"][0]["followup"] = {"cooldown_until": "2026-11-18T04:00:00Z"}
                case["followup_tasks"] = [outbound("later", kind=kind)]
                case["deadlines"] = [urgent_deadline()]
                result = module.build_queue(case, NOW)
                self.assertEqual(result["due_tasks"], [])
                deferred = result.get("deferred_tasks", [])
                self.assertEqual([t["id"] for t in deferred], ["later"])
                self.assertEqual(deferred[0]["eligible_at"], "2026-11-18T04:00:00+00:00")
                self.assertIn(("later", "urgent"), [(c.get("task_id"), c.get("deadline_id"))
                                                     for c in result.get("deadline_conflicts", [])])

    def test_provider_wide_window_still_defers_supplement_and_reports_conflict(self):
        case = record()
        case["actions"][0]["followup"] = {"all_outbound_not_before": "2026-11-18T04:00:00Z"}
        case["followup_tasks"] = [outbound("supplement", kind="send_supplement")]
        case["deadlines"] = [urgent_deadline()]
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assertEqual([t["id"] for t in result.get("deferred_tasks", [])], ["supplement"])
        self.assertIn(("supplement", "urgent"), [(c.get("task_id"), c.get("deadline_id"))
                                                  for c in result.get("deadline_conflicts", [])])
        self.assert_urgent_preserved(result)

    def test_untrusted_attempt_blocks_all_outbound_but_preserves_reads_and_deadlines(self):
        old = outbound("old", status="completed")
        good_attempt = attempt(old)
        corruptions = []
        for missing in ("id", "task_id", "action_id", "dedupe_key"):
            invalid = dict(good_attempt)
            invalid.pop(missing)
            corruptions.append(invalid)
        for changes in ({"task_id": "missing-task"}, {"action_id": "missing-action"},
                        {"dedupe_key": "does-not-match-task"}, {"status": "failed_maybe_sent"}):
            corruptions.append({**good_attempt, **changes})
        for invalid in corruptions:
            with self.subTest(invalid=invalid):
                case = record()
                case["actions"].append({**case["actions"][0], "id": "notice-2"})
                case["followup_tasks"] = [old, outbound("new", action_id="notice-2"), task("read")]
                case["outbound_attempts"] = [invalid]
                case["deadlines"] = [urgent_deadline()]
                result = module.build_queue(case, NOW)
                self.assertEqual([t["id"] for t in result["due_tasks"]], ["read"])
                self.assertIn("new", [t["id"] for t in result["held_tasks"]])
                self.assert_error(result, "outbound_attempts")
                self.assert_urgent_preserved(result)

    def test_prepared_attempt_requires_reconciliation_before_new_round(self):
        case = record()
        old = outbound("old", status="completed")
        case["followup_tasks"] = [old, outbound("new"), task("read")]
        case["outbound_attempts"] = [attempt(old, status="prepared")]
        result = module.build_queue(case, NOW)
        self.assertEqual([t["id"] for t in result["due_tasks"]], ["read"])
        self.assert_recovery(result, "old", "reconcile_send")

    def test_pending_attempt_dedupe_key_holds_candidate_on_another_action(self):
        for status in ("prepared", "in_flight", "outcome_unknown"):
            with self.subTest(status=status):
                case = record()
                case["actions"].append({**case["actions"][0], "id": "notice-2"})
                old = outbound("old", status="completed", dedupe_key="shared-key")
                candidate = outbound("new", action_id="notice-2", dedupe_key="shared-key")
                case["followup_tasks"] = [old, candidate, task("read")]
                case["outbound_attempts"] = [attempt(old, status=status)]
                result = module.build_queue(case, NOW)
                self.assertEqual([t["id"] for t in result["due_tasks"]], ["read"])
                self.assertIn("new", [t["id"] for t in result["held_tasks"]])


class RecoveryAndCorrection(QueueAssertions):
    def test_confirmed_interruption_only_proposes_read_only_requeue(self):
        case = running_case(run_status="interrupted")
        before = copy.deepcopy(case)
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assert_recovery(result, "busy", "requeue_read_only")
        self.assertEqual(case, before)

    def test_expired_lease_does_not_prove_worker_interrupted(self):
        case = running_case(lease="2026-11-12T03:30:00Z")
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assert_recovery(result, "busy", "review_run")
        self.assertNotIn("requeue_read_only", [r["kind"] for r in result.get("recovery_tasks", [])])

    def test_outbound_expired_lease_without_attempt_requires_run_review_first(self):
        case = running_case(kind="remind", lease="2026-11-12T03:30:00Z")
        before = copy.deepcopy(case)
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assert_recovery(result, "busy", "review_run")
        task_recovery = [r["kind"] for r in result.get("recovery_tasks", []) if r.get("task_id") == "busy"]
        self.assertNotIn("reconcile_send", task_recovery)
        self.assertNotIn("requeue_read_only", task_recovery)
        self.assertEqual(case, before)

    def test_interruption_without_evidence_does_not_requeue(self):
        for missing in ("interrupted_at", "interruption_evidence"):
            with self.subTest(missing=missing):
                case = running_case(run_status="interrupted", lease="2026-11-12T03:30:00Z")
                case["runs"][0].pop(missing)
                result = module.build_queue(case, NOW)
                self.assertEqual(result["due_tasks"], [])
                self.assert_recovery(result, "busy", "review_run")
                self.assertNotIn("requeue_read_only", [r["kind"] for r in result.get("recovery_tasks", [])])

    def test_interrupted_outbound_is_reconciled_not_requeued(self):
        for state in (None, "prepared", "in_flight", "outcome_unknown"):
            with self.subTest(attempt_state=state):
                case = running_case(kind="remind", run_status="interrupted")
                if state:
                    case["outbound_attempts"] = [attempt(case["followup_tasks"][0], status=state)]
                result = module.build_queue(case, NOW)
                self.assertEqual(result["due_tasks"], [])
                self.assert_recovery(result, "busy", "reconcile_send")
                self.assertNotIn("requeue_read_only", [r["kind"] for r in result.get("recovery_tasks", [])])

    def test_blocked_task_due_for_review_is_not_executed(self):
        case = record()
        case["followup_tasks"] = [task("blocked", status="blocked", reason="feedback access unavailable",
                                        review_at="2026-11-12T03:00:00Z")]
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assert_recovery(result, "blocked", "review_blocker")

    def correction_case(self):
        case = record()
        case["actions"][0]["status"] = "withdrawn"
        case["actions"].append({**case["actions"][0], "id": "correction-1", "status": "authorized",
                                "corrects_action_id": "notice-1"})
        case["followup_tasks"] = [outbound("correction", kind="send_correction", action_id="correction-1")]
        return case

    def test_independent_correction_can_follow_closed_original(self):
        case = self.correction_case()
        result = module.build_queue(case, NOW)
        self.assertEqual([t["id"] for t in result["due_tasks"]], ["correction"])
        self.assertTrue(result["due_tasks"][0]["external_action_candidate"])

    def test_correction_requires_distinct_existing_original(self):
        for original in (None, "missing", "correction-1"):
            with self.subTest(original=original):
                case = self.correction_case()
                case["actions"][1]["corrects_action_id"] = original
                case["deadlines"] = [urgent_deadline()]
                result = module.build_queue(case, NOW)
                self.assertEqual(result["due_tasks"], [])
                self.assertTrue(result.get("errors"), result)
                self.assert_urgent_preserved(result)

    def test_correction_own_terminal_status_blocks_old_correction_task(self):
        case = self.correction_case()
        case["actions"][1]["status"] = "withdrawn"
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assertIn("correction", [t["id"] for t in result["held_tasks"]])

    def test_frozen_original_blocks_reminder_but_not_independent_correction(self):
        case = self.correction_case()
        case["actions"][0].update(status="acknowledged", claim_frozen=True)
        correction_task = case["followup_tasks"][0]
        case["followup_tasks"] = [outbound("original-reminder")]
        result = module.build_queue(case, NOW)
        self.assertEqual(result["due_tasks"], [])
        self.assertIn("original-reminder", [t["id"] for t in result["held_tasks"]])

        case["followup_tasks"].append(correction_task)
        result = module.build_queue(case, NOW)
        self.assertEqual([t["id"] for t in result["due_tasks"]], ["correction"])
        self.assertIn("original-reminder", [t["id"] for t in result["held_tasks"]])


class CoverageAndInputBounds(QueueAssertions):
    def test_target_without_channel_plan_and_channel_without_action_are_visible(self):
        case = record()
        case["targets"].append({"id": "target-2", "status": "open", "channel_plan": []})
        case["actions"] = []
        result = module.build_queue(case, NOW)
        gaps = result.get("coverage_gaps", [])
        self.assertTrue(any(g.get("target_id") == "target-2" for g in gaps), gaps)
        self.assertTrue(any(g.get("target_id") == "target-1" and g.get("provider_id") == "provider-1"
                            and g.get("route") == "email" for g in gaps), gaps)

    def test_action_on_another_target_does_not_cover_planned_channel(self):
        case = record()
        case["actions"][0]["target_ids"] = ["other-target"]
        case["followup_tasks"] = [task()]
        result = module.build_queue(case, NOW)
        self.assertTrue(any(g.get("target_id") == "target-1" and g.get("provider_id") == "provider-1"
                            for g in result.get("coverage_gaps", [])))

    def test_task_for_one_target_does_not_hide_other_target_next_step_gap(self):
        case = record()
        case["targets"].append({**copy.deepcopy(case["targets"][0]), "id": "target-2"})
        case["actions"][0]["target_ids"] = ["target-1", "target-2"]
        case["followup_tasks"] = [task(target_ids=["target-1"])]
        result = module.build_queue(case, NOW)
        gaps = result.get("coverage_gaps", [])
        self.assertTrue(any(g.get("target_id") == "target-2" and g.get("action_id") == "notice-1"
                            for g in gaps), gaps)
        self.assertFalse(any(g.get("target_id") == "target-1" for g in gaps), gaps)

    def test_open_action_requires_usable_next_step_or_reviewable_blocker(self):
        invalid_next_steps = [[], [task(status="completed")], [task(due_at=None)],
                              [task(status="blocked", review_at="2026-11-13T04:00:00Z")],
                              [task(status="blocked", reason="awaiting access")]]
        for next_steps in invalid_next_steps:
            with self.subTest(next_steps=next_steps):
                case = record()
                case["followup_tasks"] = next_steps
                result = module.build_queue(case, NOW)
                self.assertTrue(any(g.get("action_id") == "notice-1"
                                    for g in result.get("coverage_gaps", [])), result)

    def test_valid_pending_work_and_reviewable_blocker_cover_open_action(self):
        for state in ("planned", "ready", "in_progress", "blocked"):
            with self.subTest(state=state):
                case = running_case() if state == "in_progress" else record()
                if state != "in_progress":
                    case["followup_tasks"] = [task(status=state)]
                if state == "blocked":
                    case["followup_tasks"][0].update(reason="awaiting access", review_at="2026-11-13T04:00:00Z")
                result = module.build_queue(case, NOW)
                self.assertEqual(result.get("coverage_gaps", []), [])

    def test_time_overflow_is_item_error_and_other_work_survives(self):
        case = record()
        case["followup_tasks"] = [task("overflow", due_at="0001-01-01T00:00:00+01:00"), task("read")]
        case["deadlines"] = [urgent_deadline()]
        result = module.build_queue(case, NOW)
        self.assertEqual([t["id"] for t in result["due_tasks"]], ["read"])
        self.assert_error(result, "followup_tasks")
        self.assert_urgent_preserved(result)

    def test_nonfinite_or_negative_horizon_returns_structured_error(self):
        for horizon in (float("inf"), float("nan"), -1, 1e308):
            with self.subTest(horizon=horizon):
                case = record()
                case["deadlines"] = [urgent_deadline()]
                result = module.build_queue(case, NOW, horizon)
                self.assertTrue(result.get("errors"), result)


class CommandLineBehavior(unittest.TestCase):
    def run_cli(self, cases, extra_args=()):
        with tempfile.TemporaryDirectory(prefix="brand-protection-audit-queue-test-") as directory:
            command = [sys.executable, "-B", str(SCRIPT), "--now", NOW_TEXT]
            originals = {}
            for index, case in enumerate(cases):
                path = Path(directory) / ("case-%s.json" % index)
                path.write_text(json.dumps(case), encoding="utf-8")
                originals[path] = path.read_bytes()
                command.extend(["--case", str(path)])
            command.extend(extra_args)
            completed = subprocess.run(command, capture_output=True, text=True, timeout=15,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original, "CLI must not mutate case records")
        self.assertTrue(completed.stdout.strip(), "CLI must retain a JSON report: " + completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        return completed, json.loads(completed.stdout)

    def test_batch_preserves_bad_case_deadline_and_good_case_after_item_error(self):
        bad, good = record(), record()
        bad["case_id"], good["case_id"] = "bad", "good"
        bad["followup_tasks"] = [task(due_at=None)]
        bad["deadlines"] = [urgent_deadline()]
        good["followup_tasks"] = [task("read")]
        completed, report = self.run_cli([bad, good])
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(report["errors"])
        by_case = {c["case_id"]: c for c in report["cases"]}
        self.assertEqual(set(by_case), {"bad", "good"})
        self.assertEqual([d["id"] for d in by_case["bad"]["deadline_alerts"]], ["urgent"])
        self.assertEqual([t["id"] for t in by_case["good"]["due_tasks"]], ["read"])

    def test_batch_overflow_does_not_discard_previously_processed_case(self):
        good, bad = record(), record()
        good["case_id"], bad["case_id"] = "good", "overflow"
        good["deadlines"] = [urgent_deadline()]
        bad["followup_tasks"] = [task(due_at="0001-01-01T00:00:00+01:00")]
        completed, report = self.run_cli([good, bad])
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(report["errors"])
        by_case = {c["case_id"]: c for c in report["cases"]}
        self.assertIn("good", by_case)
        self.assertEqual([d["id"] for d in by_case["good"]["deadline_alerts"]], ["urgent"])

    def test_same_action_and_dedupe_ids_do_not_leak_between_cases(self):
        sent, unsent = record(), record()
        sent["case_id"], unsent["case_id"] = "sent", "unsent"
        sent["followup_tasks"] = [outbound()]
        sent["outbound_attempts"] = [attempt(sent["followup_tasks"][0], status="sent")]
        unsent["followup_tasks"] = [outbound()]
        completed, report = self.run_cli([sent, unsent])
        self.assertEqual(completed.returncode, 0, report)
        by_case = {c["case_id"]: c for c in report["cases"]}
        self.assertEqual(by_case["sent"]["due_tasks"], [])
        self.assertEqual([t["id"] for t in by_case["unsent"]["due_tasks"]], ["task-1"])

    def test_invalid_horizon_has_structured_cli_error(self):
        case = record()
        case["deadlines"] = [urgent_deadline()]
        completed, report = self.run_cli([case], ["--horizon-hours", "inf"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(report["errors"])


if __name__ == "__main__":
    unittest.main()
