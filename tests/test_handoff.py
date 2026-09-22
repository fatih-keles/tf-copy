"""Synthetic, offline tests for the customer's manual route completion report."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import stat
import tempfile
import unittest

from lib.handoff import CSV_FIELDS, MANUAL, NOT_DEPLOYED, UNKNOWN_IP, write_handoff


def fixture():
    subnet_id = "ocid1.subnet.oc1.source.fixture"
    inventory = {
        "source_region": "source-region", "exported_at": "2026-01-01T12:00:00+00:00",
        "subnets": [{"id": subnet_id, "display-name": "appliances", "cidr-block": "10.0.1.0/24"}],
    }
    targets = [
        {"source_id": "ocid1.privateip.oc1.source.fixtureone", "ip_address": "10.0.1.10", "subnet_id": subnet_id,
         "vlan_id": None, "vnic_id": "ocid1.vnic.oc1.source.fixture", "status": "manual", "terraform_label": None,
         "subnet_terraform_label": "subnet_one", "lookup_status": "found", "lookup_error": None,
         "reason": "Manually create the source target address in the copied subnet."},
        {"source_id": "ocid1.privateip.oc1.source.fixturetwo", "ip_address": None, "subnet_id": None,
         "vlan_id": None, "vnic_id": None, "status": "manual", "terraform_label": None,
         "subnet_terraform_label": None, "lookup_status": "unavailable", "lookup_error": "404 NotAuthorizedOrNotFound",
         "reason": "Source metadata unavailable; resolve the numeric IP and destination target manually."},
        {"source_id": "ocid1.privateip.oc1.source.fixturevlan", "ip_address": "10.0.8.10", "subnet_id": None,
         "vlan_id": "ocid1.vlan.oc1.source.fixture", "vnic_id": None, "status": "manual", "terraform_label": None,
         "subnet_terraform_label": None, "lookup_status": "found", "lookup_error": None,
         "reason": "Source private IP belongs to a VLAN; destination provisioning is manual."},
    ]
    rules = []
    for number in range(47):
        suffix = "one" if number % 2 == 0 else "two"
        rules.append({
            "source_route_table_id": f"ocid1.routetable.oc1.source.fixture{suffix}",
            "route_table_name": f"route-{suffix}", "terraform_route_table_label": f"route_{suffix}",
            "rule_index": number // 2 + 1, "source_target_id": targets[number % 3]["source_id"],
            "destination": f"10.20.{number}.0/24", "destination_type": "CIDR_BLOCK",
            "description": f"route {number + 1}",
        })
    excluded = dict(rules[0], rule_index=100, source_target_id="ocid1.drg.oc1.source.fixture", destination="172.16.0.0/16")
    details = {
        "destination_region": "destination-region", "private_ip_targets": targets,
        "deferred_routes": rules, "excluded_drg_routes": [excluded],
        "excluded_drg_attachments": [{"id": "ocid1.drgattachment.oc1.source.fixture", "display-name": "external-link", "drg-id": excluded["source_target_id"]}],
        "gateway_associations": [{"gateway_type": "internet_gateways", "gateway_name": "appliance-igw",
                                  "source_route_table_id": rules[0]["source_route_table_id"], "action": "preserved_empty"}],
        "warnings": ["Review the snapshot before adding any routes."],
    }
    outputs = {
        "route_tables": {"value": {
            f"route_{suffix}": {"id": f"ocid1.routetable.oc1.destination.fixture{suffix}", "name": f"route-{suffix}"}
            for suffix in ("one", "two")}},
        "subnets": {"value": {
            "subnet_one": {"id": "ocid1.subnet.oc1.destination.fixture", "name": "destination-appliances", "cidr": "10.0.1.0/24"},
        }},
    }
    return inventory, details, outputs


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "reports"
        self.inventory, self.details, self.outputs = fixture()

    def render(self, deployed=False):
        return write_handoff(self.directory, self.inventory, self.details, self.outputs if deployed else None)

    def csv_rows(self):
        with (self.directory / "manual-routing.csv").open(newline="") as stream:
            reader = csv.DictReader(stream)
            return reader.fieldnames, list(reader)

    def test_preapply_preserves_all_47_rules_without_source_ids_as_destinations(self):
        report = self.render()
        self.assertEqual(len(report["pending_routes"]), 47)
        fields, rows = self.csv_rows()
        self.assertEqual(fields, list(CSV_FIELDS))
        self.assertEqual(len(rows), 47)
        for expected, machine, row in zip(self.details["deferred_routes"], report["pending_routes"], rows):
            self.assertEqual(machine["destination"], expected["destination"])
            self.assertEqual(machine["source_rule_index"], expected["rule_index"])
            self.assertEqual(machine["source_target_private_ip_id"], expected["source_target_id"])
            self.assertEqual(machine["source_route_table_id"], expected["source_route_table_id"])
            self.assertIsNone(machine["destination_route_table_id"])
            self.assertIsNone(machine["destination_target_private_ip_id"])
            self.assertEqual(row["destination_route_table_id"], NOT_DEPLOYED)
            self.assertEqual(row["destination_target_private_ip_id"], MANUAL)
            self.assertEqual(machine["target_status"], "manual")
        markdown = (self.directory / "manual-routing.md").read_text()
        self.assertIn("Pending private-IP route rules: **47**", markdown)
        self.assertIn("**NOT DEPLOYED:**", markdown)

    def test_postapply_joins_route_tables_and_known_subnets_without_private_ip_outputs(self):
        report = self.render(deployed=True)
        self.assertNotIn("reserved_private_ips", self.outputs)
        for rule in report["pending_routes"]:
            label = rule["terraform_route_table_label"]
            self.assertEqual(rule["destination_route_table_id"], self.outputs["route_tables"]["value"][label]["id"])
            self.assertEqual(rule["destination_route_table_name"], self.outputs["route_tables"]["value"][label]["name"])
            self.assertIsNone(rule["destination_target_private_ip_id"])
            self.assertEqual(rule["resolution"], "manual_unresolved")
            if rule["subnet_terraform_label"]:
                expected = self.outputs["subnets"]["value"][rule["subnet_terraform_label"]]
                self.assertEqual(rule["destination_subnet_id"], expected["id"])
                self.assertEqual(rule["destination_subnet_name"], expected["name"])
                self.assertEqual(rule["destination_subnet_cidr"], expected["cidr"])
                self.assertEqual(rule["source_subnet_name"], "appliances")
                self.assertEqual(rule["source_subnet_cidr"], "10.0.1.0/24")
                self.assertEqual(rule["source_vnic_id"], "ocid1.vnic.oc1.source.fixture")
            else:
                self.assertIsNone(rule["destination_subnet_id"])
        self.assertEqual(json.loads((self.directory / "manual-routing.json").read_text()), report)
        self.assertTrue(report["deployment_outputs_available"])

    def test_vlan_targets_remain_unresolved_after_apply_with_no_fake_reservation(self):
        report = self.render(deployed=True)
        manual = [row for row in report["pending_routes"] if row["source_vlan_id"]]
        self.assertEqual(len(manual), 15)
        for row in manual:
            self.assertEqual(row["resolution"], "manual_unresolved")
            self.assertIsNone(row["destination_target_private_ip_id"])
            self.assertIsNone(row["destination_subnet_id"])
            self.assertEqual(row["source_vlan_id"], "ocid1.vlan.oc1.source.fixture")
        _, rows = self.csv_rows()
        self.assertTrue(all(r["destination_target_private_ip_id"] == MANUAL for r in rows))
        self.assertIn("These scripts do not copy VLANs or allocate their addresses", (self.directory / "manual-routing.md").read_text())

    def test_unknown_numeric_ip_is_explicit_without_losing_any_route(self):
        report = self.render(deployed=True)
        missing_id = self.details["private_ip_targets"][1]["source_id"]
        missing = [row for row in report["pending_routes"] if row["source_target_private_ip_id"] == missing_id]
        self.assertEqual(len(missing), 16)
        for row in missing:
            self.assertIsNone(row["target_ip_address"])
            self.assertEqual(row["numeric_ip_status"], "could_not_be_determined")
            self.assertEqual(row["lookup_status"], "unavailable")
            self.assertEqual(row["lookup_error"], "404 NotAuthorizedOrNotFound")
            self.assertIsNone(row["source_subnet_id"])
            self.assertIsNone(row["source_vlan_id"])
        _, rows = self.csv_rows()
        self.assertTrue(all(row["target_ip_address"] == UNKNOWN_IP for row in rows if row["source_target_private_ip_id"] == missing_id))
        markdown = (self.directory / "manual-routing.md").read_text()
        self.assertIn(UNKNOWN_IP, markdown)
        self.assertIn("404 NotAuthorizedOrNotFound", markdown)
        self.assertEqual(len(report["pending_routes"]), len(self.details["deferred_routes"]))

    def test_csv_has_blank_customer_completion_columns_and_no_excluded_routes(self):
        self.render(deployed=True)
        fields, rows = self.csv_rows()
        completion = [field for field in fields if field.startswith("customer_")]
        self.assertGreaterEqual(len(completion), 6)
        for row in rows:
            self.assertTrue(all(row[field] == "" for field in completion))
            self.assertNotEqual(row["destination"], "172.16.0.0/16")

    def test_drg_omissions_and_gateway_associations_are_explicit(self):
        report = self.render(deployed=True)
        self.assertEqual(len(report["excluded_drg_routes"]), 1)
        self.assertEqual(report["excluded_drg_attachments"], self.details["excluded_drg_attachments"])
        markdown = (self.directory / "manual-routing.md").read_text()
        for fragment in ("Excluded DRG route rules: **1**", "Excluded DRG attachments: **1**", "172.16.0.0/16",
                         "ocid1.drg.oc1.source.fixture", "preserved_empty", "appliance-igw", "originally empty route table"):
            self.assertIn(fragment, markdown)

    def test_instructions_preserve_rules_and_explain_incomplete_connectivity(self):
        self.render(deployed=True)
        markdown = (self.directory / "manual-routing.md").read_text()
        for fragment in ("IP forwarding", "skip source/destination check", "Add Route Rules", "Preserve all existing rules",
                         "does not verify", "No changes", "ignores route_rules", "export-time snapshot", "overwrites the blank checklist"):
            self.assertIn(fragment, markdown)
        self.assertNotIn("oci network route-table update", markdown)
        self.assertNotIn("terraform apply", markdown)

    def test_instructions_require_manual_allocations_and_cleanup_dependencies(self):
        self.render(deployed=True)
        markdown = (self.directory / "manual-routing.md").read_text()
        for fragment in ("No private IPs are automatically allocated, reserved, or attached", "Manually allocate",
                         "newly created destination private-IP OCID", "customer_target_private_ip_id",
                         "private-IP assignments or settings", "customer-managed appliances, their VNICs",
                         "Remove the manually added private-IP route rules", "before running ./cleanup.sh destroy"):
            self.assertIn(fragment, markdown)
        for incorrect in ("privateIpId", "lifecycle ignore_changes = all", "Reserved addresses still need", "tracked reserved private IP"):
            self.assertNotIn(incorrect, markdown)
        self.assertNotIn("association was preserved with pending", markdown)

    def test_incomplete_postapply_outputs_fail_without_overwriting_existing_report(self):
        self.render()
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for changed in ({}, {"route_tables": self.outputs["route_tables"]},
                        {"subnets": self.outputs["subnets"]}):
            with self.subTest(outputs=changed):
                with self.assertRaisesRegex(ValueError, "missing Terraform output"):
                    write_handoff(self.directory, self.inventory, self.details, changed)
                self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_missing_network_output_labels_or_inconsistent_subnet_mapping_fail_closed(self):
        for mutate in (
            lambda o: o["route_tables"]["value"].pop("route_two"),
            lambda o: o["subnets"]["value"].pop("subnet_one"),
            lambda o: o["subnets"]["value"]["subnet_one"].update(cidr="10.0.99.0/24"),
            lambda o: o["subnets"]["value"]["subnet_one"].pop("id"),
            lambda o: o["route_tables"]["value"]["route_one"].pop("id"),
        ):
            with self.subTest(mutation=mutate):
                outputs = deepcopy(self.outputs)
                mutate(outputs)
                with self.assertRaises(ValueError):
                    write_handoff(self.directory, self.inventory, self.details, outputs)
                self.assertFalse(self.directory.exists())

    def test_source_resource_ids_cannot_be_reported_as_destination_ids(self):
        for output_name, label, field, source_id in (
            ("route_tables", "route_one", "id", self.details["deferred_routes"][0]["source_route_table_id"]),
            ("subnets", "subnet_one", "id", self.details["private_ip_targets"][0]["subnet_id"]),
        ):
            with self.subTest(field=field, output=output_name):
                outputs = deepcopy(self.outputs)
                outputs[output_name]["value"][label][field] = source_id
                with self.assertRaisesRegex(ValueError, "equals its source ID"):
                    write_handoff(self.directory, self.inventory, self.details, outputs)

    def test_unknown_duplicate_and_falsely_managed_targets_are_rejected(self):
        for mutate in (
            lambda d: d["private_ip_targets"].append(deepcopy(d["private_ip_targets"][0])),
            lambda d: d["deferred_routes"][0].update(source_target_id="ocid1.privateip.oc1.source.missing"),
            lambda d: d["private_ip_targets"][2].update(terraform_label="fake_reservation"),
            lambda d: d["deferred_routes"].append(deepcopy(d["deferred_routes"][0])),
            lambda d: d["private_ip_targets"][0].update(status="reserved"),
        ):
            with self.subTest(mutation=mutate):
                details = deepcopy(self.details)
                mutate(details)
                with self.assertRaises(ValueError):
                    write_handoff(self.directory, self.inventory, details)

    def test_source_text_is_preserved_in_json_and_escaped_for_markdown_and_csv(self):
        original = '=HYPERLINK("x") | <tag>\nsecond line'
        self.details["deferred_routes"][0]["description"] = original
        self.details["private_ip_targets"][1]["lookup_error"] = original
        report = self.render()
        self.assertEqual(report["pending_routes"][0]["description"], original)
        _, rows = self.csv_rows()
        self.assertEqual(rows[0]["description"], "'" + original)
        self.assertEqual(rows[1]["lookup_error"], "'" + original)
        markdown = (self.directory / "manual-routing.md").read_text()
        self.assertIn("&#124; &lt;tag&gt;<br>second line", markdown)

    def test_report_permissions_and_input_data_remain_private_and_unchanged(self):
        before = deepcopy((self.inventory, self.details, self.outputs))
        self.render(deployed=True)
        self.assertEqual((self.inventory, self.details, self.outputs), before)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"manual-routing.md", "manual-routing.csv", "manual-routing.json"})
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_no_pending_routes_needs_no_destination_outputs_and_no_hardcoded_count(self):
        details = {"private_ip_targets": [], "deferred_routes": [], "excluded_drg_routes": []}
        report = write_handoff(self.directory, self.inventory, details, {})
        self.assertEqual(report["pending_routes"], [])
        self.assertEqual(self.csv_rows()[1], [])
        self.assertIn("Pending private-IP route rules: **0**", (self.directory / "manual-routing.md").read_text())

    def test_only_unknown_targets_need_no_subnets_output(self):
        self.details["private_ip_targets"] = [self.details["private_ip_targets"][1]]
        source_id = self.details["private_ip_targets"][0]["source_id"]
        self.details["deferred_routes"] = [r for r in self.details["deferred_routes"] if r["source_target_id"] == source_id]
        del self.outputs["subnets"]
        report = self.render(deployed=True)
        self.assertEqual(len(report["pending_routes"]), 16)
        self.assertTrue(all(row["target_ip_address"] is None for row in report["pending_routes"]))

    def test_unnamed_destination_resources_use_source_names_or_labels(self):
        self.outputs["subnets"]["value"]["subnet_one"].pop("name")
        self.outputs["route_tables"]["value"]["route_one"]["name"] = ""
        self.outputs["route_tables"]["value"]["route_two"]["name"] = None
        for rule in self.details["deferred_routes"]:
            if rule["terraform_route_table_label"] == "route_two":
                rule["route_table_name"] = ""
        report = self.render(deployed=True)
        self.assertEqual(report["private_ip_targets"][0]["destination_subnet_name"], "appliances")
        self.assertEqual(report["pending_routes"][0]["destination_route_table_name"], "route-one")
        self.assertEqual(report["pending_routes"][1]["destination_route_table_name"], "route_two")
        self.inventory["subnets"][0]["display-name"] = None
        report = self.render(deployed=True)
        self.assertEqual(report["private_ip_targets"][0]["destination_subnet_name"], "subnet_one")

    def test_partially_unavailable_metadata_preserves_independently_known_ip(self):
        target = self.details["private_ip_targets"][0]
        target.update(lookup_status="unavailable", lookup_error="VNIC metadata unavailable")
        report = self.render(deployed=True)
        row = report["private_ip_targets"][0]
        self.assertEqual(row["target_ip_address"], "10.0.1.10")
        self.assertEqual(row["numeric_ip_status"], "determined")
        self.assertEqual(row["lookup_status"], "unavailable")
        self.assertEqual(row["lookup_error"], "VNIC metadata unavailable")
        self.assertEqual(row["destination_subnet_id"], "ocid1.subnet.oc1.destination.fixture")


if __name__ == "__main__":
    unittest.main()
