"""The ordered approved-target list: selection, refusal, and what it costs.

The pinned hyperstack A100 went out of stock, so the config now records an
ordered list of Sol-approved exact targets instead of one. The list is the
policy: it is walked in order, the first entry the live catalogue still offers
*exactly* is taken, and nothing else is ever taken. `AGENTS.md` forbids
silently switching a reviewed input, and renting an instance nobody approved is
exactly that -- so the interesting half of this file is negative. A cent off
the price, a neighbouring region, a different SXM4 instance type, a different
OS image, an interruptible offer, or a catalogue with nothing approved in it
must all end at a typed refusal with USD 0.00 spent and no key minted.

The other half is equivalence: with the primary present, the run must be the
run it was before the list existed. That is asserted against the exact figures
the single-target config produced, not against a recomputation of the new code.

No network, no provider, no process launch, no spend.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from scripts import j1m_dry_run as dry_run
from scripts import j1m_orchestrator as orchestrator
from scripts import j1m_runner, shadeform_lifecycle as sf

ROOT = Path(__file__).resolve().parents[2]

# What `build_plan` produced before the approved list existed. Recomputing
# these from the new code would prove only that the new code agrees with
# itself, so they are written down: primary rate 1.35/h against the reviewed
# mode clocks.
PRIMARY_HOURLY_USD = 1.35
MAIN_PLAN_KEYS = {
    "schema", "created_at_utc", "mutation", "candidate", "active_run_cost_usd",
    "provider_backstop_cost_usd", "commands", "artifact_allowlist",
    "receipt_allowlist", "required_scratch_gib",
}
NEW_PLAN_KEYS = {"approved_targets", "selected_target", "target_selection",
                 "os_image_policy"}
MAIN_PLAN_COSTS = {
    "prove": (0.3375, 0.4219),
    "build": (1.6875, 2.1094),
    "eval": (2.619, 3.2738),
    "canary": (0.3375, 0.4219),
}


def base_config() -> dict:
    return j1m_runner.load_config()


def candidate_for(config: dict, index: int, **overrides):
    return dry_run._candidate(config, index, **overrides)


class ApprovedTargetConfigTests(unittest.TestCase):
    """The recorded list itself: shape, order, and derived arithmetic."""

    def setUp(self):
        self.config = base_config()
        self.targets = self.config["shadeform_targets"]

    def test_three_ordered_approved_targets_cheapest_first(self):
        self.assertEqual(len(self.targets), 3)
        self.assertEqual(
            [(item["cloud"], item["region"], item["hourly_usd"]) for item in self.targets],
            [("hyperstack", "montreal-canada-2", 1.35),
             ("denvr", "houston-usa-1", 1.50),
             ("crusoe", "culpeper-usa-1", 1.65)])
        self.assertTrue(all(item["approved"] for item in self.targets))

    def test_the_primary_entry_is_the_one_that_was_already_approved(self):
        """Everything the single-target config pinned is still pinned."""

        primary = j1m_runner.primary_shadeform_target(self.config)
        self.assertEqual(
            {key: value for key, value in primary.items() if key != "approved"},
            {"cloud": "hyperstack", "region": "montreal-canada-2", "gpu": "A100_80G",
             "gpu_count": 1, "vram_gib": 80, "hourly_usd": 1.35,
             "proving_run_hours": 0.25, "active_run_cost_usd": 0.3375,
             "provider_backstop_hours": 0.3125, "host_shutdown_backstop_hours": 0.30,
             "external_watchdog_seconds": 1080})

    def test_the_singular_accessor_still_returns_the_primary(self):
        """Every pre-existing reader of `shadeform_target` is unaffected."""

        self.assertEqual(self.config["shadeform_target"],
                         j1m_runner.primary_shadeform_target(self.config))
        self.assertIs(self.config["shadeform_target"], self.config["shadeform_targets"][0])

    def test_every_alternate_declares_its_exact_instance_type_and_image(self):
        denvr, crusoe = self.targets[1], self.targets[2]
        self.assertEqual(denvr["instance_type"], "A100_sxm4_80G")
        self.assertEqual(denvr["os_image"], "ubuntu22.04_cuda12.4_shade_os")
        self.assertEqual(crusoe["instance_type"], "A100_80G")
        self.assertEqual(crusoe["os_image"], "ubuntu22.04_cuda12.2_shade_os")
        self.assertFalse(denvr["interruptible"])
        self.assertFalse(crusoe["interruptible"])

    def test_each_entry_derives_its_own_cost_from_its_own_rate(self):
        for entry in self.targets:
            self.assertEqual(
                entry["active_run_cost_usd"],
                round(entry["hourly_usd"] * entry["proving_run_hours"], 6),
                entry["cloud"])
        self.assertEqual([entry["active_run_cost_usd"] for entry in self.targets],
                         [0.3375, 0.375, 0.4125])

    def test_every_entry_shares_one_reviewed_clock_envelope(self):
        for field in ("proving_run_hours", "provider_backstop_hours",
                      "host_shutdown_backstop_hours", "external_watchdog_seconds"):
            self.assertEqual({entry[field] for entry in self.targets},
                             {self.targets[0][field]}, field)

    def test_the_recorded_per_run_cap_is_the_one_adr_0005_records(self):
        self.assertEqual(j1m_runner.per_run_cap_usd(self.config), 10.0)

    def test_worst_case_of_every_approved_entry_is_inside_the_per_run_cap(self):
        """3 h x 1.35 / 1.50 / 1.65 = 4.05 / 4.50 / 4.95, all under USD 10."""

        cap = j1m_runner.per_run_cap_usd(self.config)
        for entry in self.targets:
            self.assertLessEqual(round(entry["hourly_usd"] * 3.0, 6), cap, entry["cloud"])


class ApprovedTargetValidationTests(unittest.TestCase):
    """Config validation fails closed on anything it was not shown."""

    def setUp(self):
        self.config = base_config()

    def mutate(self, mutator):
        payload = json.loads(json.dumps(self.config))
        payload.pop("shadeform_target", None)
        mutator(payload)
        return payload

    def assert_refused(self, mutator, message):
        payload = self.mutate(mutator)
        with mock.patch.object(j1m_runner, "_bounded_json_file", return_value=payload):
            with self.assertRaises(ValueError) as caught:
                j1m_runner.load_config()
        self.assertIn(message, str(caught.exception))

    def test_a_missing_required_field_is_refused(self):
        self.assert_refused(lambda payload: payload["shadeform_targets"][1].pop("hourly_usd"),
                            "missing a required field")

    def test_an_unapproved_field_is_refused(self):
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][1].update({"spot_discount": 0.5}),
            "unapproved field")

    def test_a_non_a100_entry_is_refused(self):
        """B-004: the eval lane's own probes cannot pass on anything else."""

        self.assert_refused(
            lambda payload: payload["shadeform_targets"][1].update({"gpu": "H100_80G"}),
            "must be an A100 profile")

    def test_a_multi_gpu_or_small_vram_entry_is_refused(self):
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][1].update({"gpu_count": 2}),
            "single 80 GiB GPU")
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][2].update({"vram_gib": 40}),
            "single 80 GiB GPU")

    def test_an_entry_whose_cost_is_not_derived_from_its_rate_is_refused(self):
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][2].update({"active_run_cost_usd": 0.3375}),
            "not derived from its own hourly rate")

    def test_a_list_that_is_not_cheapest_first_is_refused(self):
        def reorder(payload):
            payload["shadeform_targets"] = list(reversed(payload["shadeform_targets"]))
        self.assert_refused(reorder, "ordered cheapest first")

    def test_a_duplicated_exact_identity_is_refused(self):
        def duplicate(payload):
            payload["shadeform_targets"][2] = json.loads(
                json.dumps(payload["shadeform_targets"][1]))
        self.assert_refused(duplicate, "distinct exact identities")

    def test_an_entry_that_changes_the_clock_envelope_is_refused(self):
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][2].update(
                {"external_watchdog_seconds": 7700}),
            "one reviewed clock envelope")

    def test_an_interruptible_entry_is_refused(self):
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][2].update({"interruptible": True}),
            "must not be interruptible")

    def test_an_unapproved_entry_must_say_why(self):
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][2].update({"approved": False}),
            "must record why it is refused")

    def test_an_approved_entry_must_not_carry_a_refusal_reason(self):
        self.assert_refused(
            lambda payload: payload["shadeform_targets"][2].update(
                {"unapproved_reason": "left over from an earlier review"}),
            "must not carry a refusal reason")

    def test_a_list_with_nothing_approved_is_refused(self):
        def disapprove(payload):
            for entry in payload["shadeform_targets"]:
                entry["approved"] = False
                entry["unapproved_reason"] = "withdrawn pending a fresh catalogue"
        self.assert_refused(disapprove, "no approved Shadeform target")

    def test_the_singular_key_may_not_be_supplied_as_input(self):
        def revert(payload):
            payload["shadeform_target"] = payload["shadeform_targets"][0]
        self.assert_refused(revert, "record approved targets as the ordered list")

    def test_a_mode_cost_that_drifts_from_the_primary_rate_is_refused(self):
        self.assert_refused(
            lambda payload: payload["modes"]["eval"].update({"active_cost_usd": 3.201}),
            "not derived from the primary target rate")

    def test_a_per_run_cap_above_the_project_total_is_refused(self):
        self.assert_refused(
            lambda payload: payload["budget_policy"].update({"per_run_cap_usd": 60.0}),
            "within the project total")

    def test_an_unapproved_entry_is_skipped_without_renumbering_the_others(self):
        payload = self.mutate(lambda payload: payload["shadeform_targets"][1].update(
            {"approved": False, "unapproved_reason": "image not reviewed for this lane"}))
        with mock.patch.object(j1m_runner, "_bounded_json_file", return_value=payload):
            config = j1m_runner.load_config()
        self.assertEqual([index for index, _ in j1m_runner.approved_shadeform_targets(config)],
                         [0, 2])


class ApprovedTargetSelectionTests(unittest.TestCase):
    """Order is the policy; exactness is the guard."""

    def setUp(self):
        self.config = base_config()

    def select(self, catalogue):
        return orchestrator.select_approved_target(self.config, catalogue)

    def test_the_primary_wins_when_it_is_present(self):
        chosen = self.select([candidate_for(self.config, 0)])
        self.assertEqual(chosen["index"], 0)
        self.assertEqual(chosen["selection"]["approved_target_index"], 0)
        self.assertTrue(chosen["selection"]["is_primary"])
        self.assertEqual(chosen["selection"]["considered"], [])

    def test_the_first_alternate_wins_when_the_primary_is_absent(self):
        chosen = self.select([candidate_for(self.config, 1)])
        self.assertEqual(chosen["index"], 1)
        self.assertEqual(chosen["candidate"].cloud, "denvr")
        self.assertEqual(chosen["candidate"].instance_type, "A100_sxm4_80G")
        self.assertEqual(chosen["selection"]["hourly_usd"], 1.50)
        self.assertFalse(chosen["selection"]["is_primary"])
        self.assertEqual([item["approved_target_index"] for item in chosen["selection"]["considered"]],
                         [0])

    def test_the_second_alternate_wins_only_when_both_cheaper_entries_are_absent(self):
        chosen = self.select([candidate_for(self.config, 2)])
        self.assertEqual(chosen["index"], 2)
        self.assertEqual(chosen["candidate"].cloud, "crusoe")
        self.assertEqual(chosen["selection"]["hourly_usd"], 1.65)
        self.assertEqual([item["approved_target_index"] for item in chosen["selection"]["considered"]],
                         [0, 1])

    def test_catalogue_order_never_decides(self):
        """The catalogue is evidence about availability, not preference."""

        catalogue = [candidate_for(self.config, 2), candidate_for(self.config, 1),
                     candidate_for(self.config, 0)]
        self.assertEqual(self.select(catalogue)["index"], 0)
        self.assertEqual(self.select(list(reversed(catalogue)))["index"], 0)

    def test_an_empty_or_unapproved_catalogue_is_a_typed_refusal(self):
        for catalogue in ([], [dry_run._unapproved_candidate()]):
            with self.assertRaises(sf.ShadeformError) as caught:
                self.select(catalogue)
            self.assertIn("no approved J1M target is an eligible current catalogue candidate",
                          str(caught.exception))

    def test_a_cheaper_unapproved_substitute_is_never_taken(self):
        """The whole point: cheaper is not the same as approved."""

        cheaper = dry_run._candidate(self.config, 2, hourly_usd=0.99)
        with self.assertRaises(sf.ShadeformError):
            self.select([cheaper])

    def test_every_declared_dimension_must_match_exactly(self):
        near_misses = {
            "price": dict(hourly_usd=1.55),
            "region": dict(region="houston-usa-2"),
            "cloud": dict(cloud="denvrdata"),
            "gpu": dict(gpu="A100_40G"),
            "instance_type": dict(instance_type="A100_80G"),
            "os_image": dict(os_image="ubuntu22.04_cuda11.8_shade_os"),
            "vram": dict(vram_gb=40),
            "interruptible": dict(interruptible=True),
        }
        for label, override in near_misses.items():
            with self.subTest(dimension=label):
                with self.assertRaises(sf.ShadeformError):
                    self.select([candidate_for(self.config, 1, **override)])

    def test_a_refusal_names_every_entry_and_the_dimension_it_failed_on(self):
        """ADR-0006 claims the refusal says what was considered; prove it."""

        catalogue = [
            candidate_for(self.config, 0, region="montreal-canada-9"),
            candidate_for(self.config, 1, hourly_usd=1.55),
            candidate_for(self.config, 2, interruptible=True),
        ]
        with self.assertRaises(sf.ShadeformError) as caught:
            self.select(catalogue)
        considered = caught.exception.considered
        self.assertEqual([item["approved_target_index"] for item in considered], [0, 1, 2])
        self.assertEqual([item["status"] for item in considered],
                         ["not_in_catalogue", "price_mismatch", "interruptible"])
        self.assertEqual([item["cloud"] for item in considered],
                         ["hyperstack", "denvr", "crusoe"])
        # The message carries the same verdicts, so a log line is enough.
        for fragment in ("#0 hyperstack/montreal-canada-2 not_in_catalogue",
                         "#1 denvr/houston-usa-1 price_mismatch",
                         "#2 crusoe/culpeper-usa-1 interruptible"):
            self.assertIn(fragment, str(caught.exception))

    def test_the_reported_reason_is_the_deepest_dimension_any_row_reached(self):
        """"Here but priced differently" is not the same fact as "not here"."""

        catalogue = [dry_run._unapproved_candidate(),
                     candidate_for(self.config, 2, instance_type="A100_sxm4_80G")]
        with self.assertRaises(sf.ShadeformError) as caught:
            self.select(catalogue)
        statuses = {item["approved_target_index"]: item["status"]
                    for item in caught.exception.considered}
        self.assertEqual(statuses[0], "not_in_catalogue")
        self.assertEqual(statuses[2], "instance_type_mismatch")

    def test_an_unapproved_entry_is_reported_as_such_with_its_reason(self):
        payload = json.loads(json.dumps(self.config))
        payload["shadeform_targets"][2].update(
            {"approved": False, "unapproved_reason": "image not reviewed for this lane"})
        with self.assertRaises(sf.ShadeformError) as caught:
            orchestrator.select_approved_target(payload, [dry_run._unapproved_candidate()])
        crusoe = caught.exception.considered[2]
        self.assertEqual(crusoe["status"], "not_approved")
        self.assertEqual(crusoe["detail"], "image not reviewed for this lane")

    def test_a_selection_carries_the_skipped_prefix_it_walked_past(self):
        chosen = self.select([candidate_for(self.config, 0, hourly_usd=1.30),
                              candidate_for(self.config, 2)])
        self.assertEqual(chosen["index"], 2)
        self.assertEqual([(item["approved_target_index"], item["status"])
                          for item in chosen["selection"]["considered"]],
                         [(0, "price_mismatch"), (1, "not_in_catalogue")])

    def test_an_env_image_override_is_named_distinctly_and_still_refused(self):
        """`SHADEFORM_IMAGE` makes every candidate report one image."""

        env = {"SHADEFORM_IMAGE": "ubuntu22.04_cuda12.2_shade_os"}
        # With the override set, every candidate reports the crusoe image, so
        # denvr -- which declares 12.4 -- can never match, and the remedy is to
        # unset the override rather than to wait for stock.
        overridden = candidate_for(self.config, 1,
                                   os_image="ubuntu22.04_cuda12.2_shade_os")
        with self.assertRaises(sf.ShadeformError) as caught:
            orchestrator.select_approved_target(self.config, [overridden], env=env)
        self.assertEqual(caught.exception.considered[1]["status"], "os_image_env_override")
        self.assertEqual(caught.exception.os_image_source, "env:SHADEFORM_IMAGE")
        # Without the override the same mismatch is a fact about the provider.
        with self.assertRaises(sf.ShadeformError) as plain:
            orchestrator.select_approved_target(self.config, [overridden])
        self.assertEqual(plain.exception.considered[1]["status"], "os_image_mismatch")
        self.assertEqual(plain.exception.os_image_source, "catalogue")

    def test_the_selection_records_which_os_image_source_applied(self):
        chosen = self.select([candidate_for(self.config, 2)])
        self.assertEqual(chosen["selection"]["os_image_source"], "catalogue")
        env = {"SHADEFORM_IMAGE": "ubuntu22.04_cuda12.2_shade_os"}
        chosen = orchestrator.select_approved_target(
            self.config, [candidate_for(self.config, 2)], env=env)
        self.assertEqual(chosen["selection"]["os_image_source"], "env:SHADEFORM_IMAGE")

    def test_the_primary_matches_without_declaring_an_instance_type_or_image(self):
        """The pre-existing entry declares neither, so neither is compared."""

        primary = self.config["shadeform_targets"][0]
        self.assertNotIn("instance_type", primary)
        self.assertNotIn("os_image", primary)
        chosen = self.select([dry_run._candidate(
            self.config, 0, instance_type="A100_80G_pcie",
            os_image="ubuntu22.04_cuda12.6_shade_os")])
        self.assertEqual(chosen["index"], 0)


class SelectedTargetCostTests(unittest.TestCase):
    """Every dollar figure follows the selected entry, never a constant."""

    def setUp(self):
        self.config = base_config()

    def test_mode_costs_are_computed_from_the_given_rate(self):
        self.assertEqual(j1m_runner.mode_active_cost_usd(self.config, "eval", 1.35), 2.619)
        self.assertEqual(j1m_runner.mode_active_cost_usd(self.config, "eval", 1.50), 2.91)
        self.assertEqual(j1m_runner.mode_active_cost_usd(self.config, "eval", 1.65), 3.201)
        self.assertEqual(j1m_runner.mode_active_cost_usd(self.config, "prove", 1.65), 0.4125)

    def test_the_comparator_budget_prices_the_selected_entry(self):
        selection = ("q8",)
        default = orchestrator._comparator_budget(self.config, selection)
        crusoe = orchestrator._comparator_budget(self.config, selection, hourly_usd=1.65)
        self.assertEqual(default["hourly_usd"], 1.35)
        self.assertEqual(crusoe["hourly_usd"], 1.65)
        required = default["required_seconds"]
        self.assertEqual(crusoe["projected_marginal_cost_usd"],
                         round(1.65 * required / 3600.0, 6))
        self.assertEqual(crusoe["authorized_active_cost_usd"], 3.201)
        # The clock verdicts are time-based and must not move with the rate.
        for field in ("required_seconds", "static_slack_seconds", "fits_static_worst_case",
                      "arms", "requested"):
            self.assertEqual(default[field], crusoe[field], field)

    def test_every_comparator_re_derivation_passes_the_selected_rate(self):
        """The in-run phase is derived a second time; it must not lose the rate."""

        source = (ROOT / "scripts" / "j1m_orchestrator.py").read_text(encoding="utf-8")
        derivations = source.count("_comparator_phase(")
        # One definition, one pre-catalogue refusal gate at the primary rate,
        # one post-selection, one in-run, one plan-mode. A sixth would be a new
        # derivation site this test has not checked for the rate.
        self.assertEqual(derivations, 5)
        in_run = source.split("The Q4 job is already fixed above", 1)[1][:900]
        self.assertIn('hourly_usd=float(target["hourly_usd"])', in_run)
        post_selection = source.split("Re-derive the phase evidence at the selected rate", 1)[1][:400]
        self.assertIn('hourly_usd=float(target["hourly_usd"])', post_selection)

    def test_an_in_run_re_derivation_at_the_selected_rate_keeps_its_figures(self):
        deadline = orchestrator.time.monotonic() + 100000.0
        primary = orchestrator._comparator_phase(
            self.config, ("q8_0",), execution_deadline=deadline)
        crusoe = orchestrator._comparator_phase(
            self.config, ("q8_0",), execution_deadline=deadline, hourly_usd=1.65)
        self.assertEqual(primary["budget"]["hourly_usd"], 1.35)
        self.assertEqual(crusoe["budget"]["hourly_usd"], 1.65)
        self.assertEqual(crusoe["budget"]["authorized_active_cost_usd"], 3.201)
        self.assertEqual(crusoe["status"], primary["status"])

    def test_the_deadline_ceiling_is_time_based_and_rate_independent(self):
        ceiling = orchestrator._eval_deadline_ceiling(self.config)
        self.assertEqual(ceiling["run_seconds"], 1.94 * 3600.0)
        self.assertEqual(ceiling["watchdog_seconds"], 7800.0)
        self.assertEqual(ceiling["provider_seconds"], 2.425 * 3600.0)
        self.assertNotIn("hourly_usd", ceiling)

    def test_the_per_run_cap_is_checked_at_the_selected_rate(self):
        env = {"SHADEFORM_AUTO_TERMINATE_HOURS": "3"}
        projection = orchestrator._assert_selected_target_within_per_run_cap(
            self.config, mode="eval", hourly_usd=1.65, env=env)
        self.assertEqual(projection["worst_case_hours"], 3.0)
        self.assertEqual(projection["worst_case_usd"], 4.95)
        self.assertEqual(projection["per_run_cap_usd"], 10.0)
        self.assertEqual(projection["mode_active_cost_usd"], 3.201)
        self.assertEqual(
            orchestrator._assert_selected_target_within_per_run_cap(
                self.config, mode="eval", hourly_usd=1.50, env=env)["worst_case_usd"], 4.50)
        self.assertEqual(
            orchestrator._assert_selected_target_within_per_run_cap(
                self.config, mode="eval", hourly_usd=1.35, env=env)["worst_case_usd"], 4.05)

    def test_a_worst_case_above_the_cap_is_refused_before_anything_is_created(self):
        env = {"SHADEFORM_AUTO_TERMINATE_HOURS": "8"}
        with self.assertRaises(sf.ShadeformError) as caught:
            orchestrator._assert_selected_target_within_per_run_cap(
                self.config, mode="eval", hourly_usd=1.65, env=env)
        self.assertIn("exceeds the recorded per-run cap", str(caught.exception))

    def test_the_backstop_floor_applies_when_the_ceiling_is_shorter(self):
        projection = orchestrator._assert_selected_target_within_per_run_cap(
            self.config, mode="eval", hourly_usd=1.65, env={})
        self.assertEqual(projection["worst_case_hours"], 2.5)


class DefaultPlanEquivalenceTests(unittest.TestCase):
    """With the primary present, this is the run it always was."""

    def setUp(self):
        self.config = base_config()

    def test_the_plan_gains_exactly_four_keys_and_changes_nothing_else(self):
        for mode in ("prove", "build", "eval", "canary"):
            with self.subTest(mode=mode):
                plan = j1m_runner.build_plan(self.config, mode)
                expected = set(MAIN_PLAN_KEYS) | NEW_PLAN_KEYS
                if mode == "canary":
                    expected |= {"mode_policy", "lifecycle_guards"}
                self.assertEqual(set(plan), expected)

    def test_the_plan_costs_are_the_figures_the_single_target_config_produced(self):
        for mode, (active, backstop) in MAIN_PLAN_COSTS.items():
            with self.subTest(mode=mode):
                plan = j1m_runner.build_plan(self.config, mode)
                self.assertEqual(plan["active_run_cost_usd"], active)
                self.assertEqual(plan["provider_backstop_cost_usd"], backstop)

    def test_the_plan_candidate_is_still_the_primary_target_object(self):
        plan = j1m_runner.build_plan(self.config, "build")
        self.assertEqual(plan["candidate"]["cloud"], "hyperstack")
        self.assertEqual(plan["candidate"]["region"], "montreal-canada-2")
        self.assertEqual(plan["candidate"]["hourly_usd"], PRIMARY_HOURLY_USD)
        self.assertEqual(plan["candidate"], j1m_runner.primary_shadeform_target(self.config))

    def test_the_commands_and_uploads_do_not_depend_on_the_target_at_all(self):
        """A target is a machine to rent; it never enters a remote command."""

        commands = orchestrator._eval_remote_commands(self.config, "/scratch/j1m")
        uploads = orchestrator._eval_uploads(
            self.config, "/scratch/j1m", None,
            ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json")
        flattened = " ".join(part for command in commands for part in command)
        for entry in self.config["shadeform_targets"]:
            for value in (entry["cloud"], entry["region"], str(entry["hourly_usd"])):
                self.assertNotIn(value, flattened, value)
        self.assertEqual(len(uploads), int(
            orchestrator._eval_deadline_ceiling(self.config)["upload_count"]))
        self.assertEqual(j1m_runner.build_plan(self.config, "build")["commands"],
                         j1m_runner.command_plan(self.config))

    def test_a_plan_may_not_be_built_on_an_unapproved_entry(self):
        payload = json.loads(json.dumps(self.config))
        payload["shadeform_targets"][2].update(
            {"approved": False, "unapproved_reason": "image not reviewed for this lane"})
        with self.assertRaises(ValueError):
            j1m_runner.build_plan(payload, "eval", target_index=2)

    def test_the_plan_records_the_os_image_env_interplay_without_reading_it(self):
        """The plan path reads no environment, so it records the policy."""

        policy = j1m_runner.build_plan(self.config, "eval")["os_image_policy"]
        self.assertEqual(policy["env_key"], "SHADEFORM_IMAGE")
        self.assertIn("os_image_env_override", policy["effect"])
        self.assertIn("reads no environment", policy["source"])

    def test_the_plan_records_the_whole_approved_list_and_its_default(self):
        plan = j1m_runner.build_plan(self.config, "eval")
        self.assertEqual(plan["approved_targets"], self.config["shadeform_targets"])
        self.assertEqual(plan["selected_target"]["approved_target_index"], 0)
        self.assertEqual(plan["selected_target"]["approved_target_count"], 3)
        self.assertIn("first approved entry", plan["target_selection"])
        alternate = j1m_runner.build_plan(self.config, "eval", target_index=2)
        self.assertEqual(alternate["selected_target"]["approved_target_index"], 2)
        self.assertEqual(alternate["active_run_cost_usd"], 3.201)
        self.assertEqual(alternate["commands"], plan["commands"])


class SelectionIsRecordedEverywhereTests(unittest.TestCase):
    """Plan, receipt, ledger reservation and watchdog argv all say which one."""

    def setUp(self):
        self.config = base_config()

    def test_the_reservation_candidate_block_carries_the_index(self):
        candidate = candidate_for(self.config, 2)
        captured = {}

        def capture(canonical, payload, *, expected_budget_cap_usd):
            captured.update(canonical)

        with mock.patch.object(sf, "_append_reserved_cost_event", side_effect=capture):
            sf.reserve_create_attempt(
                "j1m-approved-target-test", "c" * 32, candidate,
                backstop_hours=0.3125, public_key_sha256="d" * 64,
                expected_budget_cap_usd=50.0, approved_target_index=2)
        self.assertEqual(captured["candidate"]["approved_target_index"], 2)
        self.assertEqual(captured["candidate"]["cloud"], "crusoe")
        self.assertEqual(captured["candidate"]["hourly_usd"], 1.65)
        # Priced at the selected entry's own rate, not the primary's.
        self.assertEqual(captured["estimated_cost_usd"], round(1.65 * 0.3125, 6))

    def test_an_out_of_range_index_is_refused_by_the_reservation(self):
        candidate = candidate_for(self.config, 0)
        with self.assertRaises(ValueError):
            sf.reserve_create_attempt(
                "j1m-approved-target-test", "c" * 32, candidate,
                backstop_hours=0.3125, public_key_sha256="d" * 64,
                expected_budget_cap_usd=50.0, approved_target_index=-1)

    def test_the_reservation_is_unchanged_when_no_index_is_supplied(self):
        candidate = candidate_for(self.config, 0)
        captured = {}
        with mock.patch.object(sf, "_append_reserved_cost_event",
                               side_effect=lambda canonical, payload, **kwargs: captured.update(canonical)):
            sf.reserve_create_attempt(
                "j1m-approved-target-test", "c" * 32, candidate,
                backstop_hours=0.3125, public_key_sha256="d" * 64,
                expected_budget_cap_usd=50.0)
        self.assertNotIn("approved_target_index", captured["candidate"])

    def test_the_orchestrator_passes_the_index_into_the_watchdog_argv(self):
        source = (ROOT / "scripts" / "j1m_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn('"--approved-target-index", str(approved_target_index),', source)
        self.assertIn("approved_target_index=approved_target_index,", source)
        # The selection is decided before the first key is ever generated.
        self.assertLess(source.index("chosen = select_approved_target("),
                        source.index("key_directory = sf.ephemeral_key_directory("))
        self.assertLess(source.index("_assert_selected_target_within_per_run_cap("),
                        source.index("key_directory = sf.ephemeral_key_directory("))

    def test_the_watchdog_accepts_and_bounds_the_index(self):
        watchdog = ROOT / "scripts" / "shadeform_watchdog.py"
        source = watchdog.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--approved-target-index", type=int)', source)
        self.assertIn("--approved-target-index is not a bounded list position", source)

    def test_the_lifecycle_receipt_field_survives_receipt_validation(self):
        """The receipt is persisted through `validate_persisted_receipt`."""

        selection = j1m_runner.selected_target_record(self.config, 2)
        selection["cost_projection"] = orchestrator._assert_selected_target_within_per_run_cap(
            self.config, mode="eval", hourly_usd=1.65, env={"SHADEFORM_AUTO_TERMINATE_HOURS": "3"})
        j1m_runner.validate_persisted_receipt(
            {"phase_id": "j1m-approved-target-test", "status": "starting",
             "mode": "eval", "selected_target": selection})
        self.assertEqual(selection["cloud"], "crusoe")
        self.assertEqual(selection["approved_target_index"], 2)
        self.assertEqual(selection["os_image"], "ubuntu22.04_cuda12.2_shade_os")


class AlternateImageToolchainTests(unittest.TestCase):
    """Would the alternate images pass the probes that run after the spend?

    Sol's instruction was not to ship an alternate that will predictably fail
    once the money is gone, so the pinned requirements are read here rather
    than assumed. Nothing in this lane pins a CUDA *minor* version: the floor
    is 12.0 and the driver version is recorded but never compared. Both
    alternate images clear it -- and crusoe's image is the same one the
    already-approved primary runs.
    """

    def setUp(self):
        self.config = base_config()
        self.probe = ROOT / "scripts" / "test" / "remote_toolchain_probe.py"

    def test_the_pinned_cuda_floor_is_twelve_zero_in_every_copy_of_it(self):
        expected = {"python3": (3, 8), "git": (2, 30), "cmake": (3, 18),
                    "g++": (9, 0), "nvcc": (12, 0)}
        probe = {}
        exec(compile(self.probe.read_text(encoding="utf-8").split("_PACKAGES")[0],
                     str(self.probe), "exec"), probe)
        self.assertEqual(probe["_REQUIRED"], expected)
        for path in ("scripts/j1m_orchestrator.py", "scripts/test/remote_model_eval.py"):
            source = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn('minimums = {"python3": (3, 8), "git": (2, 30), '
                          '"cmake": (3, 18), "g++": (9, 0), "nvcc": (12, 0)}', source)

    def test_both_alternate_images_clear_the_floor(self):
        floor = (12, 0)
        for entry in self.config["shadeform_targets"][1:]:
            image = entry["os_image"]
            self.assertTrue(image.startswith("ubuntu22.04_cuda"), image)
            version = image.split("_cuda", 1)[1].split("_", 1)[0]
            major, minor = (int(part) for part in version.split("."))
            self.assertGreaterEqual((major, minor), floor, image)

    def test_the_crusoe_image_is_the_one_the_primary_already_runs(self):
        self.assertEqual(self.config["shadeform_targets"][2]["os_image"],
                         "ubuntu22.04_cuda12.2_shade_os")
        self.assertEqual(dry_run._candidate(self.config, 0).os_image,
                         "ubuntu22.04_cuda12.2_shade_os")

    def test_the_nvcc_path_and_cuda_architecture_are_image_independent(self):
        self.assertEqual(self.config["modes"]["eval"]["cuda_compiler"],
                         "/usr/local/cuda/bin/nvcc")
        # sm_80 is the A100 itself, and every approved entry is an A100.
        self.assertEqual(self.config["modes"]["eval"]["cuda_architecture"], 80)
        for entry in self.config["shadeform_targets"]:
            self.assertIn("a100", entry["gpu"].lower())

    def test_the_host_packages_the_probe_demands_are_the_ones_bootstrap_installs(self):
        commands = orchestrator._eval_remote_commands(self.config, "/scratch/j1m")
        install = next(command for command in commands if "apt-get" in command and "install" in command)
        source = self.probe.read_text(encoding="utf-8")
        packages = source.split("_PACKAGES = (", 1)[1].split(")", 1)[0]
        for package in ("ca-certificates", "cmake", "build-essential", "git",
                        "python3", "python3-venv"):
            self.assertIn(package, packages)
            self.assertIn(package, install)

    def test_no_driver_version_is_ever_compared(self):
        """A driver pin would make an image choice a silent post-spend failure."""

        for path in ("scripts/j1m_orchestrator.py", "scripts/test/remote_model_eval.py",
                     "scripts/test/cuda_device_probe.py"):
            source = (ROOT / path).read_text(encoding="utf-8")
            self.assertNotIn('driver_version"] ==', source)
            self.assertNotIn("driver_version'] ==", source)

    def test_the_device_probe_demands_only_a_single_eighty_gig_a100(self):
        source = (ROOT / "scripts" / "test" / "cuda_device_probe.py").read_text(encoding="utf-8")
        self.assertIn('expected_memory_mib: int = 70000', source)
        self.assertIn('"a100" not in str(rows[0]["name"]).lower()', source)
        self.assertIn("expected_single_a100_80g_not_proven", source)


if __name__ == "__main__":
    unittest.main()
