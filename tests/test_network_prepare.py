"""Network preparation keeps appliance routing under explicit manual ownership."""

from copy import deepcopy
import json
import unittest

from lib.network_config import NetworkConfigError, build_config, build_preparation
from test_network_config import example_destination, example_inventory


def preparation_inventory():
    inventory = example_inventory()
    private_id = "ocid1.privateip.oc1.source-region.appliance"
    drg_id = "ocid1.drg.oc1.source-region.omitted"
    attachment = {
        "id": "ocid1.drgattachment.oc1.source-region.omitted",
        "drg-id": drg_id, "vcn-id": inventory["vcn"]["id"], "lifecycle-state": "ATTACHED",
        "network-details": {"id": inventory["vcn"]["id"], "type": "VCN"},
    }
    inventory["unsupported_resources"]["drg_attachments"] = [attachment]
    inventory["private_ips"] = [{
        "id": private_id, "ip-address": "10.0.1.10", "subnet-id": inventory["subnets"][1]["id"],
        "vlan-id": None, "vnic-id": "ocid1.vnic.oc1.source-region.appliance",
        "hostname-label": "firewall", "display-name": "firewall forwarding IP",
        "lifetime": "EPHEMERAL", "is-primary": True, "ip-state": "ASSIGNED",
        "freeform-tags": {"purpose": "firewall"},
    }]
    inventory["route_tables"][0]["route-rules"].extend([
        {"destination": "10.20.0.0/16", "destination-type": "CIDR_BLOCK",
         "network-entity-id": private_id, "description": "forward via appliance"},
        {"destination": "10.30.0.0/16", "destination-type": "CIDR_BLOCK",
         "network-entity-id": drg_id, "description": "on premises"},
    ])
    return inventory


class NetworkPreparationTests(unittest.TestCase):
    def setUp(self):
        self.inventory = preparation_inventory()
        self.destination = {**example_destination(), "prepare_network_only": True}

    def build(self):
        return build_preparation(self.inventory, self.destination)

    def test_opt_in_is_required_and_plain_network_config_is_unchanged(self):
        inventory = example_inventory()
        destination = example_destination()
        strict = build_config(inventory, destination)
        self.assertEqual(strict, build_config(inventory, {**destination, "prepare_network_only": False}))
        self.assertNotIn("route_tables", strict["output"])
        for enabled in (False, None):
            with self.subTest(enabled=enabled):
                dest = dict(destination)
                if enabled is not None:
                    dest["prepare_network_only"] = enabled
                with self.assertRaisesRegex(NetworkConfigError, "drg_attachments"):
                    build_config(self.inventory, dest)
        with self.assertRaisesRegex(NetworkConfigError, "boolean"):
            build_config(inventory, {**destination, "prepare_network_only": "false"})

    def test_manual_targets_and_exact_handoff_preserve_gateway_rules(self):
        before = deepcopy(self.inventory)
        config, details = self.build()
        self.assertEqual(self.inventory, before)
        self.assertEqual(build_config(self.inventory, self.destination), config)
        target = details["private_ip_targets"][0]
        self.assertEqual(target["status"], "manual")
        self.assertEqual(target["lookup_status"], "found")
        self.assertIsNone(target["lookup_error"])
        self.assertEqual(target["ip_address"], "10.0.1.10")
        self.assertEqual(target["vnic_id"], self.inventory["private_ips"][0]["vnic-id"])
        self.assertIsNone(target["terraform_label"])
        self.assertNotIn("oci_core_private_ip", config["resource"])
        self.assertNotIn("reserved_private_ips", config["output"])
        subnet = config["output"]["subnets"]["value"][target["subnet_terraform_label"]]
        self.assertEqual(subnet["name"], "private")
        self.assertEqual(subnet["cidr"], "10.0.1.0/24")
        self.assertTrue(subnet["id"].startswith("${oci_core_subnet."))
        self.assertNotIn("subnet_details", config["output"])
        self.assertEqual(details["destination_region"], self.destination["region"])
        self.assertEqual(len(details["deferred_routes"]), 1)
        route = details["deferred_routes"][0]
        self.assertEqual(route["rule_index"], 3)
        self.assertEqual(route["source_target_id"], target["source_id"])
        self.assertEqual(route["source_route_table_id"], self.inventory["route_tables"][0]["id"])
        self.assertEqual(route["description"], "forward via appliance")
        table = config["resource"]["oci_core_route_table"][route["terraform_route_table_label"]]
        self.assertEqual(len(table["route_rules"]), 2)
        self.assertEqual(table["lifecycle"], {"ignore_changes": ["route_rules"]})
        self.assertNotIn("lifecycle", config["resource"]["oci_core_default_route_table"]["default"])
        self.assertEqual(details["excluded_drg_routes"][0]["rule_index"], 4)
        self.assertEqual(details["excluded_drg_attachments"], self.inventory["unsupported_resources"]["drg_attachments"])
        self.assertIn(route["terraform_route_table_label"], config["output"]["route_tables"]["value"])
        self.assertNotIn("oc1.source-region", json.dumps(config))

    def test_all_private_routes_deferred_even_when_only_rules_in_default_table(self):
        ip_rule = self.inventory["route_tables"][0]["route-rules"][2]
        self.inventory["route_tables"][1]["route-rules"] = [deepcopy(ip_rule), {**ip_rule, "destination": "10.21.0.0/16", "description": None}]
        config, details = self.build()
        table = config["resource"]["oci_core_default_route_table"]["default"]
        self.assertEqual(table["route_rules"], [])
        self.assertEqual(table["lifecycle"]["ignore_changes"], ["route_rules"])
        self.assertEqual(len(details["deferred_routes"]), 3)
        self.assertNotIn("oci_core_private_ip", config["resource"])

    def test_missing_private_ip_export_produces_manual_handoff_with_unknown_ip(self):
        for value in (None, []):
            with self.subTest(value=value):
                self.inventory = preparation_inventory()
                if value is None:
                    del self.inventory["private_ips"]
                else:
                    self.inventory["private_ips"] = value
                rule = self.inventory["route_tables"][0]["route-rules"][2]
                rule["description"] = "Firewall target might be 10.0.1.10"
                config, details = self.build()
                target = details["private_ip_targets"][0]
                self.assertEqual(target["status"], "manual")
                self.assertEqual(target["lookup_status"], "unavailable")
                for field in ("ip_address", "subnet_id", "vlan_id", "vnic_id", "terraform_label", "subnet_terraform_label"):
                    self.assertIsNone(target[field])
                self.assertIn("missing", target["lookup_error"])
                self.assertTrue(details["warnings"])
                self.assertEqual(details["deferred_routes"][0]["description"], rule["description"])
                self.assertEqual(details["deferred_routes"][0]["destination"], rule["destination"])
                self.assertNotIn("oci_core_private_ip", config["resource"])

    def test_failed_private_ip_lookup_retains_error_without_blocking_routes(self):
        source_id = self.inventory["private_ips"][0]["id"]
        self.inventory["private_ips"] = []
        error = "GetPrivateIp returned 404 NotAuthorizedOrNotFound"
        self.inventory["private_ip_lookup_errors"] = [{"id": source_id, "error": error}]
        config, details = self.build()
        target = details["private_ip_targets"][0]
        self.assertEqual(target["lookup_error"], error)
        self.assertEqual(target["lookup_status"], "unavailable")
        self.assertIsNone(target["ip_address"])
        self.assertIn(error, " ".join(details["warnings"]))
        self.assertEqual(len(details["deferred_routes"]), 1)
        self.assertNotIn("oci_core_private_ip", config["resource"])

    def test_vlan_target_is_manual_only_and_never_guessed_from_subnet(self):
        target = self.inventory["private_ips"][0]
        target.update({"subnet-id": None, "vlan-id": "ocid1.vlan.oc1.source-region.vmware", "vnic-id": None, "ip-address": "10.0.50.10"})
        config, details = self.build()
        self.assertNotIn("oci_core_private_ip", config["resource"])
        self.assertNotIn("reserved_private_ips", config["output"])
        self.assertEqual(details["private_ip_targets"][0]["status"], "manual")
        self.assertEqual(details["private_ip_targets"][0]["lookup_status"], "found")
        self.assertEqual(details["private_ip_targets"][0]["vlan_id"], target["vlan-id"])
        self.assertIsNone(details["private_ip_targets"][0]["terraform_label"])
        self.assertIsNone(details["private_ip_targets"][0]["subnet_terraform_label"])
        self.assertEqual(len(details["deferred_routes"]), 1)

    def test_invalid_private_ip_metadata_warns_without_blocking_preparation(self):
        cases = [
            ("ip-address", None), ("ip-address", "not-an-address"),
            ("ip-address", "10.0.1.10/32"), ("ip-address", 1234),
            ("subnet-id", ["invalid"]), ("vnic-id", 42),
            ("vlan-id", "ocid1.vlan.oc1.source-region.other"),
            ("vcn-id", "ocid1.vcn.oc1.source-region.other"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.inventory = preparation_inventory()
                self.inventory["private_ips"][0][key] = value
                config, details = self.build()
                target = details["private_ip_targets"][0]
                self.assertEqual(target["lookup_status"], "unavailable")
                self.assertTrue(target["lookup_error"])
                self.assertTrue(details["warnings"])
                self.assertEqual(len(details["deferred_routes"]), 1)
                self.assertNotIn("oci_core_private_ip", config["resource"])
                if key == "ip-address":
                    self.assertIsNone(target["ip_address"])

    def test_reporting_metadata_ignores_reservation_and_hostname_restrictions(self):
        record = self.inventory["private_ips"][0]
        record.update({"hostname-label": "not a valid reservation hostname", "cidr-prefix-length": 24,
                       "future-field": "special", "ipv4-subnet-cidr-at-creation": "10.0.2.0/24"})
        config, details = self.build()
        self.assertEqual(details["private_ip_targets"][0]["lookup_status"], "found")
        self.assertNotIn("oci_core_private_ip", config["resource"])
        self.assertNotIn("special", json.dumps(config))

    def test_subnet_mapping_requires_exact_source_id_never_ip_range_guessing(self):
        self.inventory["private_ips"][0]["subnet-id"] = "ocid1.subnet.oc1.source-region.unexported"
        _, details = self.build()
        target = details["private_ip_targets"][0]
        self.assertEqual(target["lookup_status"], "found")
        self.assertEqual(target["ip_address"], "10.0.1.10")
        self.assertIsNone(target["subnet_terraform_label"])

    def test_duplicate_private_ip_records_are_unavailable_without_failing(self):
        self.inventory["private_ips"].append(deepcopy(self.inventory["private_ips"][0]))
        _, details = self.build()
        target = details["private_ip_targets"][0]
        self.assertEqual(target["lookup_status"], "unavailable")
        self.assertIsNone(target["ip_address"])
        self.assertIn("duplicate", target["lookup_error"])

    def test_duplicate_numeric_addresses_are_reported_without_reservation_conflicts(self):
        self.inventory["private_ips"].append(deepcopy(self.inventory["private_ips"][0]))
        other = self.inventory["private_ips"][1]
        other["id"] += "second"
        self.inventory["route_tables"][0]["route-rules"].append({
            "destination": "10.40.0.0/16", "network-entity-id": other["id"],
        })
        config, details = self.build()
        self.assertEqual(len(details["private_ip_targets"]), 2)
        self.assertTrue(all(target["lookup_status"] == "found" for target in details["private_ip_targets"]))
        self.assertNotIn("oci_core_private_ip", config["resource"])

    def test_only_matching_vcn_drg_attachments_authorize_drg_omission(self):
        for mutation in ("other_vcn", "unknown_drg", "attachment_target", "lpg"):
            with self.subTest(mutation=mutation):
                self.inventory = preparation_inventory()
                attachment = self.inventory["unsupported_resources"]["drg_attachments"][0]
                rule = self.inventory["route_tables"][0]["route-rules"][3]
                if mutation == "other_vcn":
                    attachment["network-details"]["id"] = "other-vcn"
                elif mutation == "unknown_drg":
                    rule["network-entity-id"] += "unknown"
                elif mutation == "attachment_target":
                    rule["network-entity-id"] = attachment["id"]
                else:
                    self.inventory["unsupported_resources"]["local_peering_gateways"] = [{"id": "lpg"}]
                with self.assertRaises(NetworkConfigError):
                    self.build()

    def test_only_originally_empty_gateway_ingress_tables_are_preserved(self):
        table = deepcopy(self.inventory["route_tables"][0])
        table.update({"id": "ocid1.routetable.oc1.source-region.ingress", "route-rules": []})
        self.inventory["route_tables"].append(table)
        self.inventory["internet_gateways"][0]["route-table-id"] = table["id"]
        config, details = self.build()
        gateway = next(iter(config["resource"]["oci_core_internet_gateway"].values()))
        self.assertTrue(gateway["route_table_id"].startswith("${oci_core_route_table."))
        self.assertEqual(details["gateway_associations"][0]["action"], "preserved_empty")
        table["route-rules"] = [deepcopy(self.inventory["route_tables"][0]["route-rules"][2])]
        with self.assertRaisesRegex(NetworkConfigError, "originally empty"):
            self.build()

    def test_empty_nat_and_service_gateway_ingress_tables_remain_unsupported(self):
        for group in ("nat_gateways", "service_gateways"):
            with self.subTest(group=group):
                self.inventory = preparation_inventory()
                table = self.inventory["route_tables"][1]
                table["route-rules"] = []
                self.inventory[group][0]["route-table-id"] = table["id"]
                with self.assertRaisesRegex(NetworkConfigError, "ingress routing is not supported"):
                    self.build()

    def test_malformed_drg_attachment_entries_fail_with_domain_error(self):
        for malformed in (None, "invalid attachment", 12, []):
            with self.subTest(malformed=malformed):
                self.inventory = preparation_inventory()
                self.inventory["unsupported_resources"]["drg_attachments"].append(malformed)
                with self.assertRaisesRegex(NetworkConfigError, "DRG attachment must be an object"):
                    self.build()

    def test_source_drg_or_private_id_in_tags_is_rejected(self):
        for source_id in (self.inventory["private_ips"][0]["id"], self.inventory["unsupported_resources"]["drg_attachments"][0]["drg-id"]):
            with self.subTest(source_id=source_id):
                self.inventory["vcn"]["freeform-tags"]["source"] = source_id
                with self.assertRaisesRegex(NetworkConfigError, "source resource ID remains"):
                    self.build()


if __name__ == "__main__":
    unittest.main()
