"""Offline tests for deployment binding and local-state recovery safeguards."""

from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


LIB = Path(__file__).resolve().parents[1] / "lib"
# workflow.py is also an executable script with a sibling module import.
with patch.object(sys, "path", [str(LIB), *sys.path]):
    spec = importlib.util.spec_from_file_location("network_copy_workflow_tests", LIB / "workflow.py")
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / ".work"
        self.destination = self.work / "destination"
        self.inventory = self.work / "export.json"
        self.binding = self.work / "deployment.json"
        self.work.mkdir()
        self.config = {
            "source_region": "me-dubai-1", "destination_region": "me-abudhabi-1",
            "source_compartment_id": "ocid1.compartment.oc1..fixture",
            "destination_compartment_id": "ocid1.compartment.oc1..fixture",
            "source_vcn_id": "", "vcn_name": "",
            "profile": "TEST", "config_file": str(self.root / "oci-config"),
            "tenancy_id": "ocid1.tenancy.oc1..fixture", "provider_version": "9.2.0",
        }
        self.start_patch(patch.multiple(workflow, WORK=self.work, DEST=self.destination,
                                        INVENTORY=self.inventory, BINDING=self.binding))
        # Any unmocked external call is a test failure; these tests never access OCI.
        self.process_run = self.start_patch(patch.object(workflow.subprocess, "run", side_effect=AssertionError("Unexpected subprocess.run")))
        self.process_popen = self.start_patch(patch.object(workflow.subprocess, "Popen", side_effect=AssertionError("Unexpected subprocess.Popen")))
        self.start_patch(patch.object(workflow, "require_program"))
        self.start_patch(redirect_stdout(io.StringIO()))

    def start_patch(self, context):
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def state(self, active=True, path=None):
        data = {"version": 4, "resources": []}
        if active:
            data["resources"] = [{
                "mode": "managed", "type": "oci_core_vcn", "name": "network",
                "instances": [{"attributes": {"id": "ocid1.vcn.oc1.auh.fixture"}}],
            }]
        self.write_json(path or self.destination / "terraform.tfstate", data)

    def bound_deployment(self, may_have_resources=False):
        self.destination.mkdir(exist_ok=True)
        self.write_json(self.inventory, {"fixture": "source inventory"})
        self.write_json(self.destination / "main.tf.json", {"fixture": "destination config"})
        binding = {
            "settings": deepcopy(self.config),
            "inventory_sha256": workflow.digest(self.inventory),
            "configuration_sha256": workflow.digest(self.destination / "main.tf.json"),
            "may_have_resources": may_have_resources,
        }
        self.write_json(self.binding, binding)
        return binding

    def saved_plan(self):
        binding = self.bound_deployment()
        saved = self.destination / "deploy.tfplan"
        saved.write_bytes(b"synthetic Terraform plan")
        self.write_json(self.destination / "plan-meta.json", {
            "binding": binding, "plan_sha256": workflow.digest(saved),
        })

    def test_local_cleanup_refuses_live_state_even_without_binding(self):
        self.state()
        with self.assertRaisesRegex(ValueError, "Managed resources still exist"):
            workflow.local_cleanup()
        self.assertTrue((self.destination / "terraform.tfstate").exists())

    def test_local_cleanup_refuses_recovery_marker_without_current_state(self):
        self.bound_deployment(may_have_resources=True)
        with self.assertRaisesRegex(ValueError, "state is missing"):
            workflow.local_cleanup()
        self.assertTrue(self.binding.exists())

    def test_local_cleanup_refuses_orphaned_backup_with_resources(self):
        self.state(path=self.destination / "terraform.tfstate.backup")
        with self.assertRaisesRegex(ValueError, "backup remains"):
            workflow.local_cleanup()
        self.assertTrue(self.work.exists())

    def test_local_cleanup_accepts_empty_current_state_with_historical_backup(self):
        self.bound_deployment(may_have_resources=True)
        self.state(active=False)
        self.state(path=self.destination / "terraform.tfstate.backup")
        preserved = self.root / ".env"
        preserved.write_text("SOURCE_REGION=me-dubai-1\n")
        workflow.local_cleanup()
        self.assertFalse(self.work.exists())
        self.assertEqual(preserved.read_text(), "SOURCE_REGION=me-dubai-1\n")

    def test_local_cleanup_refuses_corrupted_or_unrecognized_current_state(self):
        for contents in ("{broken", "[]", '{"version":4}'):
            with self.subTest(contents=contents):
                self.destination.mkdir(exist_ok=True)
                state = self.destination / "terraform.tfstate"
                state.write_text(contents)
                with self.assertRaises(ValueError):
                    workflow.local_cleanup()
                self.assertEqual(state.read_text(), contents)

    def test_cleanup_does_not_follow_symlinked_work_directory(self):
        self.work.rmdir()
        outside = self.root / "keep"
        outside.mkdir()
        marker = outside / "marker"
        marker.write_text("keep")
        self.work.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            workflow.local_cleanup()
        self.assertTrue(marker.exists())

    def test_env_binding_change_blocks_apply_and_destroy_before_terraform(self):
        self.saved_plan()
        self.state()
        with patch.object(workflow, "tf") as terraform:
            for key, value in (
                ("source_region", "eu-frankfurt-1"),
                ("destination_region", "me-dubai-1"),
                ("destination_compartment_id", "ocid1.compartment.oc1..other"),
                ("profile", "OTHER"), ("tenancy_id", "ocid1.tenancy.oc1..other"),
            ):
                changed = dict(self.config, **{key: value})
                for operation in (workflow.apply, workflow.destroy):
                    with self.subTest(key=key, operation=operation.__name__):
                        with self.assertRaisesRegex(ValueError, "differs from this deployment"):
                            operation(changed)
            terraform.assert_not_called()

    def test_mutated_saved_plan_is_never_applied(self):
        self.saved_plan()
        (self.destination / "deploy.tfplan").write_bytes(b"substituted plan")
        with patch.object(workflow, "tf") as terraform:
            with self.assertRaisesRegex(ValueError, "Saved plan does not match"):
                workflow.apply(self.config)
            terraform.assert_not_called()
        self.assertFalse(workflow.read_json(self.binding)["may_have_resources"])

    def test_changed_export_or_configuration_blocks_apply(self):
        for filename in ("export.json", "main.tf.json"):
            with self.subTest(filename=filename):
                self.saved_plan()
                path = self.inventory if filename == "export.json" else self.destination / filename
                path.write_text('{"changed": true}')
                with patch.object(workflow, "tf") as terraform:
                    with self.assertRaisesRegex(ValueError, "changed after preparation"):
                        workflow.apply(self.config)
                    terraform.assert_not_called()

    def test_extra_terraform_file_blocks_destroy(self):
        self.bound_deployment(may_have_resources=True)
        self.state()
        (self.destination / "other.tf").write_text('terraform { backend "http" {} }')
        with patch.object(workflow, "tf") as terraform:
            with self.assertRaisesRegex(ValueError, "Unexpected Terraform configuration"):
                workflow.destroy(self.config, yes=True)
            terraform.assert_not_called()

    def test_failed_new_plan_invalidates_previous_approved_plan(self):
        self.saved_plan()
        with patch.object(workflow, "tf", side_effect=ValueError("init failed")):
            with self.assertRaisesRegex(ValueError, "init failed"):
                workflow.plan(self.config)
        self.assertFalse((self.destination / "plan-meta.json").exists())
        with patch.object(workflow, "tf") as terraform:
            with self.assertRaises(ValueError):
                workflow.apply(self.config)
            terraform.assert_not_called()

    def test_failed_apply_retains_recovery_marker_and_consumes_plan(self):
        self.saved_plan()
        with patch.object(workflow, "tf", side_effect=ValueError("partial apply")):
            with self.assertRaisesRegex(ValueError, "partial apply"):
                workflow.apply(self.config)
        self.assertTrue(workflow.read_json(self.binding)["may_have_resources"])
        self.assertFalse((self.destination / "plan-meta.json").exists())
        with self.assertRaisesRegex(ValueError, "state is missing"):
            workflow.local_cleanup()
        with patch.object(workflow, "tf") as terraform:
            with self.assertRaisesRegex(ValueError, "state is missing"):
                workflow.destroy(self.config, yes=True)
            terraform.assert_not_called()

    def test_missing_state_after_started_deployment_blocks_fresh_plan(self):
        self.bound_deployment(may_have_resources=True)
        with patch.object(workflow, "tf") as terraform:
            with self.assertRaisesRegex(ValueError, "state is missing"):
                workflow.plan(self.config)
            terraform.assert_not_called()

    def test_export_refuses_to_overwrite_prepared_or_active_workspace(self):
        self.bound_deployment(may_have_resources=True)
        original = self.inventory.read_bytes()
        with patch.object(workflow, "list_network") as query:
            with self.assertRaisesRegex(ValueError, "destination workspace exists"):
                workflow.export_network(self.config)
            query.assert_not_called()
        self.assertEqual(self.inventory.read_bytes(), original)

    def test_export_reads_only_source_region_and_selected_vcn(self):
        vcn = {"id": "ocid1.vcn.oc1.dxb.fixture", "display-name": "fixture", "lifecycle-state": "AVAILABLE"}

        def listing(config, region, group, compartment, vcn_id=None):
            self.assertEqual(region, self.config["source_region"])
            self.assertEqual(compartment, self.config["source_compartment_id"])
            if group == "vcn":
                return [vcn]
            self.assertEqual(vcn_id, vcn["id"])
            return []

        with patch.object(workflow, "list_network", side_effect=listing), patch.object(workflow, "oci", return_value=[]) as oci:
            workflow.export_network(self.config)
        self.assertEqual(oci.call_args.args[1], self.config["source_region"])
        self.assertEqual(workflow.read_json(self.inventory)["vcn"], vcn)
        self.assertFalse(self.destination.exists())

    def test_oci_empty_successful_list_is_normalized_to_empty_inventory(self):
        self.process_run.side_effect = None
        self.process_run.return_value = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        result = workflow.oci(self.config, "me-dubai-1", "network", "nat-gateway", "list", "--all")
        self.assertEqual(result, [])
        command = self.process_run.call_args.args[0]
        self.assertEqual(command[command.index("--region") + 1], "me-dubai-1")
        self.assertEqual(command[command.index("--profile") + 1], "TEST")

    def test_oci_empty_nonlist_and_failed_list_are_not_silently_accepted(self):
        self.process_run.side_effect = None
        self.process_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with self.assertRaisesRegex(ValueError, "unexpected response for network vcn get"):
            workflow.oci(self.config, "me-dubai-1", "network", "vcn", "get", "--vcn-id", "fixture")
        self.process_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="permission denied")
        with self.assertRaisesRegex(ValueError, "permission denied"):
            workflow.oci(self.config, "me-dubai-1", "network", "vcn", "list", "--all")

    def test_destroy_applies_only_saved_destroy_plan_and_retains_state(self):
        self.bound_deployment(may_have_resources=True)
        self.state()
        calls = []

        def terraform(config, *args, **kwargs):
            self.assertEqual(config, self.config)
            self.assertEqual(config["destination_region"], "me-abudhabi-1")
            calls.append(args)
            if args[0] == "show":
                return json.dumps({"resource_changes": [{"mode": "managed", "change": {"actions": ["delete"]}}]})
            if args[0] == "apply":
                self.assertEqual(args[-1], "destroy.tfplan")
                self.state(active=False)
            return 0

        with patch.object(workflow, "tf", side_effect=terraform), patch.object(workflow, "oci") as oci:
            workflow.destroy(self.config, yes=True)
            oci.assert_not_called()
        self.assertEqual([args[0] for args in calls], ["init", "plan", "show", "apply"])
        self.assertIn("-destroy", calls[1])
        self.assertTrue((self.destination / "terraform.tfstate").exists())
        self.assertFalse(workflow.read_json(self.binding)["may_have_resources"])

    def test_failed_destroy_preserves_binding_and_active_state(self):
        self.bound_deployment(may_have_resources=True)
        self.state()

        def terraform(config, *args, **kwargs):
            if args[0] == "show":
                return json.dumps({"resource_changes": [{"mode": "managed", "change": {"actions": ["delete"]}}]})
            if args[0] == "apply":
                raise ValueError("destroy interrupted")
            return 0

        with patch.object(workflow, "tf", side_effect=terraform):
            with self.assertRaisesRegex(ValueError, "destroy interrupted"):
                workflow.destroy(self.config, yes=True)
        self.assertTrue(workflow.read_json(self.binding)["may_have_resources"])
        self.assertTrue(workflow.state_has_resources(self.destination / "terraform.tfstate"))

    def test_destroy_rejects_replacement_or_creation_in_destroy_plan(self):
        self.bound_deployment(may_have_resources=True)
        self.state()
        for actions in (["create"], ["delete", "create"], ["update"]):
            with self.subTest(actions=actions):
                data = {"resource_changes": [{"mode": "managed", "change": {"actions": actions}}]}
                with patch.object(workflow, "tf", side_effect=[0, 0, json.dumps(data)]) as terraform:
                    with self.assertRaisesRegex(ValueError, "Unexpected actions"):
                        workflow.destroy(self.config, yes=True)
                    self.assertNotIn("apply", [call.args[1] for call in terraform.call_args_list])

    def test_terraform_command_scopes_directory_and_removes_cli_argument_overrides(self):
        self.process_run.side_effect = None
        self.process_run.return_value = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with patch.dict(workflow.os.environ, {"TF_WORKSPACE": "default", "TF_CLI_ARGS": "-chdir=/elsewhere", "TF_CLI_ARGS_plan": "-destroy", "TF_DATA_DIR": ""}, clear=True):
            workflow.tf(self.config, "show", "-json", "destroy.tfplan", capture=True)
        args, kwargs = self.process_run.call_args
        self.assertEqual(args[0], ["terraform", f"-chdir={self.destination}", "show", "-json", "destroy.tfplan"])
        self.assertNotIn("TF_CLI_ARGS", kwargs["env"])
        self.assertNotIn("TF_CLI_ARGS_plan", kwargs["env"])

    def test_terraform_refuses_nondefault_workspace_or_external_data_directory(self):
        for env in ({"TF_WORKSPACE": "production"}, {"TF_DATA_DIR": "/elsewhere"}):
            with self.subTest(env=env), patch.dict(workflow.os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "default workspace"):
                    workflow.tf(self.config, "plan", capture=True)
        self.process_run.assert_not_called()
        self.process_popen.assert_not_called()

    def test_terraform_refuses_workspace_selected_in_local_environment_file(self):
        selected = self.destination / ".terraform" / "environment"
        selected.parent.mkdir(parents=True)
        selected.write_text("production")
        with patch.dict(workflow.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "workspace"):
                workflow.tf(self.config, "plan", capture=True)
        self.process_run.assert_not_called()
        self.process_popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
