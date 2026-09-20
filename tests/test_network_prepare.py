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

    def test_reservation_and_exact_manual_handoff_preserve_gateway_rules(self):
        before = deepcopy(self.inventory)
        config, details = self.build()
        self.assertEqual(self.inventory, before)
        self.assertEqual(build_config(self.inventory, self.destination), config)
        target = details["private_ip_targets"][0]
        self.assertEqual(target["status"], "reserved")
        self.assertEqual(target["ip_address"], "10.0.1.10")
        reservation = config["resource"]["oci_core_private_ip"][target["terraform_label"]]
        self.assertEqual(reservation["lifetime"], "RESERVED")
        self.assertEqual(reservation["lifecycle"], {"ignore_changes": "all"})
        self.assertNotIn("vnic_id", reservation)
        self.assertNotIn("compartment_id", reservation)
        self.assertEqual(reservation["hostname_label"], "firewall")
        self.assertTrue(reservation["subnet_id"].startswith("${oci_core_subnet."))
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
        self.assertIn(target["terraform_label"], config["output"]["reserved_private_ips"]["value"])
        self.assertNotIn("oc1.source-region", json.dumps(config))

    def test_all_private_routes_deferred_even_when_only_rules_in_default_table(self):
        ip_rule = self.inventory["route_tables"][0]["route-rules"][2]
        self.inventory["route_tables"][1]["route-rules"] = [deepcopy(ip_rule), {**ip_rule, "destination": "10.21.0.0/16", "description": None}]
        config, details = self.build()
        table = config["resource"]["oci_core_default_route_table"]["default"]
        self.assertEqual(table["route_rules"], [])
        self.assertEqual(table["lifecycle"]["ignore_changes"], ["route_rules"])
        self.assertEqual(len(details["deferred_routes"]), 3)
        self.assertEqual(len(config["resource"]["oci_core_private_ip"]), 1)

    def test_missing_private_ip_export_fails_with_reexport_instruction(self):
        for value in (None, []):
            with self.subTest(value=value):
                self.inventory = preparation_inventory()
                if value is None:
                    del self.inventory["private_ips"]
                else:
                    self.inventory["private_ips"] = value
                with self.assertRaisesRegex(NetworkConfigError, "re-export"):
                    self.build()

    def test_vlan_target_is_manual_only_and_never_guessed_from_subnet(self):
        target = self.inventory["private_ips"][0]
        target.update({"subnet-id": None, "vlan-id": "ocid1.vlan.oc1.source-region.vmware", "vnic-id": None, "ip-address": "10.0.50.10"})
        self.inventory["route_target_vlans"] = [{
            "id": target["vlan-id"], "vcn-id": self.inventory["vcn"]["id"], "cidr-block": "10.0.50.0/24",
        }]
        config, details = self.build()
        self.assertNotIn("oci_core_private_ip", config["resource"])
        self.assertEqual(config["output"]["reserved_private_ips"]["value"], {})
        self.assertEqual(details["private_ip_targets"][0]["status"], "manual_vlan")
        self.assertIsNone(details["private_ip_targets"][0]["terraform_label"])
        self.assertEqual(len(details["deferred_routes"]), 1)
        self.assertTrue(details["warnings"])
        self.inventory["route_target_vlans"][0]["vcn-id"] = "wrong-vcn"
        with self.assertRaisesRegex(NetworkConfigError, "another VCN"):
            self.build()

    def test_bad_private_ip_details_fail_closed(self):
        cases = [
            ("ip-address", "10.0.2.10", "outside"),
            ("ip-address", "10.0.1.0", "reserved for OCI"),
            ("ip-address", "10.0.1.1", "reserved for OCI"),
            ("ip-address", "10.0.1.255", "reserved for OCI"),
            ("ip-address", "2001:db8::1", "IPv6"),
            ("subnet-id", "missing", "subnet is missing"),
            ("vlan-id", "ocid1.vlan.oc1.source-region.other", "exactly one"),
            ("cidr-prefix-length", 24, "single IPv4"),
            ("ipv4-subnet-cidr-at-creation", "10.0.2.0/24", "creation CIDR"),
            ("future-field", "special", "unsupported populated"),
        ]
        for key, value, message in cases:
            with self.subTest(key=key, value=value):
                self.inventory = preparation_inventory()
                self.inventory["private_ips"][0][key] = value
                with self.assertRaisesRegex(NetworkConfigError, message):
                    self.build()

    def test_duplicate_private_ip_records_and_addresses_are_rejected(self):
        self.inventory["private_ips"].append(deepcopy(self.inventory["private_ips"][0]))
        with self.assertRaisesRegex(NetworkConfigError, "Duplicate private-IP detail ID"):
            self.build()
        other = self.inventory["private_ips"][1]
        other["id"] += "second"
        self.inventory["route_tables"][0]["route-rules"].append({
            "destination": "10.40.0.0/16", "network-entity-id": other["id"],
        })
        with self.assertRaisesRegex(NetworkConfigError, "Duplicate private-IP target address"):
            self.build()

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
