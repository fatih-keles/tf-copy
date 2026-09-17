"""Behavior tests for the offline OCI inventory -> Terraform JSON conversion."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from lib.network_config import NetworkConfigError, build_config


def example_inventory():
    """Synthetic OCI CLI data; no account identifiers or credentials."""
    def ocid(kind, name):
        return f"ocid1.{kind}.oc1.source-region.fixture{name}"

    vcn_id = ocid("vcn", "vcn")

    def item(kind, name, **fields):
        return {
            "id": ocid(kind, name), "display-name": name, "vcn-id": vcn_id,
            "compartment-id": "ocid1.compartment.oc1..sourcefixture",
            "lifecycle-state": "AVAILABLE", "freeform-tags": {"purpose": "test"},
            "defined-tags": {"Oracle-Tags": {"CreatedOn": "old"}}, **fields,
        }

    default_route = item("routetable", "default-route", **{"route-rules": []})
    private_route = item("routetable", "private-route", **{"route-rules": []})
    default_security = item("securitylist", "default-security", **{
        "ingress-security-rules": [
            {"protocol": "6", "source": "10.0.0.0/16", "source-type": "CIDR_BLOCK", "is-stateless": False,
             "tcp-options": {"source-port-range": {"min": 1024, "max": 65535}, "destination-port-range": {"min": 22, "max": 22}}},
            {"protocol": "1", "source": "10.0.0.0/16", "source-type": "CIDR_BLOCK", "is-stateless": False,
             "icmp-options": {"type": 3, "code": -1}},
        ],
        "egress-security-rules": [{"protocol": "all", "destination": "0.0.0.0/0", "destination-type": "CIDR_BLOCK", "is-stateless": True}],
    })
    empty_security = item("securitylist", "empty", **{"ingress-security-rules": [], "egress-security-rules": []})
    dhcp = item("dhcpoptions", "default-dhcp", **{
        "domain-name-type": "CUSTOM_DOMAIN", "options": [
            {"type": "DomainNameServer", "server-type": "VcnLocalPlusInternet", "custom-dns-servers": []},
            {"type": "SearchDomain", "search-domain-names": ["example.oraclevcn.com"]},
        ],
    })
    internet = item("internetgateway", "internet", **{"is-enabled": True, "route-table-id": None})
    nat = item("natgateway", "nat", **{"block-traffic": False, "public-ip-id": ocid("publicip", "oldnat"), "nat-ip": "192.0.2.1", "route-table-id": None})
    all_service = {"id": ocid("service", "all"), "cidr-block": "all-src-services-in-oracle-services-network", "name": "All SRC Services In Oracle Services Network"}
    object_service = {"id": ocid("service", "object"), "cidr-block": "oci-src-objectstorage", "name": "OCI SRC Object Storage"}
    gateway = item("servicegateway", "service", **{"block-traffic": False, "route-table-id": None, "services": [{"service-id": all_service["id"], "service-name": all_service["name"]}]})
    default_route["route-rules"] = [{"destination": "0.0.0.0/0", "destination-type": "CIDR_BLOCK", "network-entity-id": internet["id"], "route-type": "STATIC"}]
    private_route["route-rules"] = [
        {"destination": "0.0.0.0/0", "destination-type": "CIDR_BLOCK", "network-entity-id": nat["id"], "route-type": "STATIC"},
        {"destination": all_service["cidr-block"], "destination-type": "SERVICE_CIDR_BLOCK", "network-entity-id": gateway["id"], "route-type": "STATIC"},
    ]
    first_nsg = item("networksecuritygroup", "frontend", **{"security-rules": []})
    second_nsg = item("networksecuritygroup", "backend", **{"security-rules": [{
        "id": "ABC123", "direction": "INGRESS", "protocol": "6", "is-stateless": False,
        "source-type": "NETWORK_SECURITY_GROUP", "source": first_nsg["id"],
        "destination": None, "destination-type": None,
        "tcp-options": {"source-port-range": {"min": 1024, "max": 65535}, "destination-port-range": {"min": 8080, "max": 8081}},
        "is-valid": True,
    }, {
        "id": "EGRESS42", "direction": "EGRESS", "protocol": "17", "is-stateless": True,
        "destination-type": "NETWORK_SECURITY_GROUP", "destination": first_nsg["id"],
        "udp-options": {"destination-port-range": {"min": 53, "max": 53}},
    }]})
    subnets = []
    for name, cidr, route, private in [("public", "10.0.0.0/24", default_route, False), ("private", "10.0.1.0/24", private_route, True)]:
        subnets.append(item("subnet", name, **{
            "availability-domain": None, "cidr-block": cidr, "ipv4-cidr-blocks": [cidr],
            "dns-label": name, "dhcp-options-id": dhcp["id"], "route-table-id": route["id"],
            "security-list-ids": [default_security["id"], empty_security["id"]],
            "prohibit-internet-ingress": private, "prohibit-public-ip-on-vnic": private,
            "ipv6-cidr-block": None, "ipv6-cidr-blocks": None,
        }))
    return {
        "schema_version": 1, "source_region": "source-region", "compartment_id": "ocid1.compartment.oc1..sourcefixture",
        "vcn": item("vcn", "vcn", **{
            "cidr-block": "10.0.0.0/16", "cidr-blocks": ["10.0.0.0/16"], "dns-label": "example",
            "default-route-table-id": default_route["id"], "default-security-list-id": default_security["id"],
            "default-dhcp-options-id": dhcp["id"], "is-zpr-only": False, "security-attributes": {},
        }),
        "subnets": subnets, "route_tables": [private_route, default_route],
        "security_lists": [default_security, empty_security], "dhcp_options": [dhcp],
        "internet_gateways": [internet], "nat_gateways": [nat], "service_gateways": [gateway],
        "network_security_groups": [first_nsg, second_nsg], "services": [all_service, object_service],
        "unsupported_resources": {"local_peering_gateways": [], "drg_attachments": []},
    }


def example_destination():
    return {
        "region": "destination-region", "compartment_id": "ocid1.compartment.oc1..destinationfixture",
        "profile": "TEST", "config_file": str(Path.home() / ".oci/config"),
        "provider_version": "9.2.0", "vcn_name": "copied-network",
    }


class NetworkConfigTests(unittest.TestCase):
    def setUp(self):
        self.inventory = example_inventory()
        self.destination = example_destination()

    def build(self):
        return build_config(self.inventory, self.destination)

    def test_nested_references_and_default_resources_are_remapped(self):
        config = self.build()
        resources = config["resource"]
        default_route = resources["oci_core_default_route_table"]["default"]
        self.assertEqual(default_route["manage_default_resource_id"], "${oci_core_vcn.network.default_route_table_id}")
        self.assertTrue(default_route["route_rules"][0]["network_entity_id"].startswith("${oci_core_internet_gateway."))
        for subnet in resources["oci_core_subnet"].values():
            self.assertEqual(subnet["vcn_id"], "${oci_core_vcn.network.id}")
            self.assertEqual(subnet["dhcp_options_id"], "${oci_core_default_dhcp_options.default.id}")
            self.assertIn("${oci_core_default_security_list.default.id}", subnet["security_list_ids"])
        rules = list(resources["oci_core_network_security_group_security_rule"].values())
        ingress = next(r for r in rules if r["direction"] == "INGRESS")
        egress = next(r for r in rules if r["direction"] == "EGRESS")
        self.assertTrue(ingress["source"].startswith("${oci_core_network_security_group."))
        self.assertEqual(ingress["source"], egress["destination"])
        self.assertNotEqual(ingress["source"], ingress["network_security_group_id"])

    def test_no_source_infrastructure_ids_or_historical_defined_tags_survive(self):
        result = json.dumps(self.build())
        self.assertNotIn("oc1.source-region", result)
        self.assertNotIn("defined_tags", result)
        self.assertNotIn("public_ip_id", result)
        self.assertNotIn("192.0.2.1", result)
        self.assertIn('"purpose": "test"', result)

    def test_rule_ports_stateless_and_icmp_are_preserved(self):
        resources = self.build()["resource"]
        rule = resources["oci_core_default_security_list"]["default"]["ingress_security_rules"][0]
        self.assertEqual(rule["tcp_options"], [{"min": 22, "max": 22, "source_port_range": [{"min": 1024, "max": 65535}]}])
        rules = list(resources["oci_core_network_security_group_security_rule"].values())
        ingress = next(r for r in rules if r["direction"] == "INGRESS")
        self.assertEqual(ingress["tcp_options"][0]["destination_port_range"], [{"min": 8080, "max": 8081}])
        self.assertFalse(ingress["stateless"])
        self.assertTrue(next(r for r in rules if r["direction"] == "EGRESS")["stateless"])
        icmp = resources["oci_core_default_security_list"]["default"]["ingress_security_rules"][1]
        self.assertEqual(icmp["icmp_options"], [{"type": 3}])
        self.inventory["security_lists"][0]["ingress-security-rules"][1]["icmp-options"]["code"] = 0
        icmp = self.build()["resource"]["oci_core_default_security_list"]["default"]["ingress_security_rules"][1]
        self.assertEqual(icmp["icmp_options"], [{"type": 3, "code": 0}])

    def test_empty_security_lists_and_route_tables_stay_explicitly_empty(self):
        self.inventory["route_tables"][1]["route-rules"] = []
        self.inventory["security_lists"][0]["ingress-security-rules"] = []
        self.inventory["security_lists"][0]["egress-security-rules"] = []
        resources = self.build()["resource"]
        for kind in ("oci_core_default_security_list", "oci_core_security_list"):
            for rules in resources[kind].values():
                self.assertEqual(rules["ingress_security_rules"], [])
                self.assertEqual(rules["egress_security_rules"], [])
        self.assertEqual(resources["oci_core_default_route_table"]["default"]["route_rules"], [])

    def test_services_use_destination_data_and_preserve_categories(self):
        result = self.build()
        self.assertIn("oci_core_services", result["data"])
        self.assertIn("service_all", result["locals"])
        self.assertNotIn("service_objectstorage", result["locals"])
        sg = next(iter(result["resource"]["oci_core_service_gateway"].values()))
        self.assertEqual(sg["services"], [{"service_id": "${local.service_all.id}"}])
        object_service = self.inventory["services"][1]
        self.inventory["service_gateways"][0]["services"][0]["service-id"] = object_service["id"]
        self.inventory["route_tables"][0]["route-rules"][1]["destination"] = object_service["cidr-block"]
        result = self.build()
        self.assertIn("service_objectstorage", result["locals"])
        self.assertNotIn("service_all", result["locals"])

    def test_service_cidr_security_rule_is_remapped(self):
        self.inventory["security_lists"][0]["egress-security-rules"] = [{
            "protocol": "6", "destination-type": "SERVICE_CIDR_BLOCK",
            "destination": self.inventory["services"][1]["cidr-block"], "is-stateless": False,
        }]
        rules = self.build()["resource"]["oci_core_default_security_list"]["default"]["egress_security_rules"]
        self.assertEqual(rules[0]["destination"], "${local.service_objectstorage.cidr_block}")

    def test_unsupported_route_targets_fail(self):
        for kind in ("privateip", "drg", "localpeeringgateway", "unknown"):
            with self.subTest(kind=kind):
                self.inventory["route_tables"][0]["route-rules"][0]["network-entity-id"] = f"ocid1.{kind}.oc1.source-region.unmapped"
                with self.assertRaisesRegex(NetworkConfigError, "Unsupported route target"):
                    self.build()

    def test_unknown_service_category_and_unknown_id_fail(self):
        self.inventory["services"][0]["cidr-block"] = "new-unsupported-service"
        with self.assertRaisesRegex(NetworkConfigError, "Unsupported service category"):
            self.build()
        self.inventory = example_inventory()
        self.inventory["service_gateways"][0]["services"][0]["service-id"] = "unmapped-service"
        with self.assertRaisesRegex(NetworkConfigError, "Unknown source service ID"):
            self.build()

    def test_missing_nested_nsg_and_subnet_references_fail(self):
        self.inventory["network_security_groups"][1]["security-rules"][0]["source"] = "unmapped-nsg"
        with self.assertRaisesRegex(NetworkConfigError, "Missing referenced resource"):
            self.build()
        self.inventory = example_inventory()
        self.inventory["subnets"][0]["security-list-ids"].append("unmapped-security-list")
        with self.assertRaisesRegex(NetworkConfigError, "Missing referenced resource"):
            self.build()

    def test_missing_default_resource_fails(self):
        self.inventory["dhcp_options"] = []
        with self.assertRaisesRegex(NetworkConfigError, "default-dhcp-options-id.*missing"):
            self.build()

    def test_ad_ipv6_zpr_and_multicidr_are_rejected(self):
        mutations = [
            ("subnets", "availability-domain", "tenant:SOURCE-AD-1", "AD-specific"),
            ("subnets", "ipv6-cidr-block", "2001:db8::/64", "IPv6"),
            ("subnets", "ipv4-cidr-blocks", ["10.0.0.0/24", "10.0.2.0/24"], "Multi-CIDR"),
            ("vcn", "security-attributes", {"namespace": {"zone": "x"}}, "ZPR"),
        ]
        for group, field, value, message in mutations:
            with self.subTest(field=field):
                self.inventory = example_inventory()
                obj = self.inventory[group] if group == "vcn" else self.inventory[group][0]
                obj[field] = value
                with self.assertRaisesRegex(NetworkConfigError, message):
                    self.build()

    def test_gateway_ingress_routes_and_blocked_service_gateway_fail(self):
        self.inventory["nat_gateways"][0]["route-table-id"] = self.inventory["route_tables"][0]["id"]
        with self.assertRaisesRegex(NetworkConfigError, "ingress routing"):
            self.build()
        self.inventory = example_inventory()
        self.inventory["service_gateways"][0]["block-traffic"] = True
        with self.assertRaisesRegex(NetworkConfigError, "Blocked service gateway"):
            self.build()

    def test_unsupported_resources_and_unknown_populated_fields_fail(self):
        self.inventory["unsupported_resources"]["drg_attachments"] = [{"id": "drg-attachment"}]
        with self.assertRaisesRegex(NetworkConfigError, "drg_attachments"):
            self.build()
        self.inventory = example_inventory()
        self.inventory["vcn"]["future-routing-mode"] = "SPECIAL"
        with self.assertRaisesRegex(NetworkConfigError, "unsupported populated fields"):
            self.build()

    def test_order_independent_labels_and_input_is_not_modified(self):
        before = deepcopy(self.inventory)
        expected = self.build()
        self.assertEqual(self.inventory, before)
        for value in self.inventory.values():
            if isinstance(value, list):
                value.reverse()
        for nsg in self.inventory["network_security_groups"]:
            nsg["security-rules"].reverse()
        self.assertEqual(self.build(), expected)

    def test_source_text_cannot_inject_terraform_templates(self):
        self.destination["vcn_name"] = "literal ${file(\"/secret\")} %{if true}"
        self.inventory["vcn"]["freeform-tags"]["template"] = "${sensitive(\"text\")}"
        vcn = self.build()["resource"]["oci_core_vcn"]["network"]
        self.assertEqual(vcn["display_name"], 'literal $${file("/secret")} %%{if true}')
        self.assertEqual(vcn["freeform_tags"]["template"], '$${sensitive("text")}')

    def test_provider_is_pinned_and_custom_config_file_is_rejected(self):
        result = self.build()
        self.assertEqual(result["provider"]["oci"], {"region": "destination-region", "config_file_profile": "TEST"})
        self.assertEqual(result["terraform"]["required_providers"]["oci"], {"source": "oracle/oci", "version": "9.2.0"})
        self.destination["config_file"] = "/tmp/other-config"
        with self.assertRaisesRegex(NetworkConfigError, "custom config_file paths"):
            self.build()

    def test_custom_dns_and_private_subnet_restrictions_are_preserved(self):
        self.inventory["dhcp_options"][0]["options"][0] = {
            "type": "DomainNameServer", "server-type": "CustomDnsServer", "custom-dns-servers": ["10.0.1.20"],
        }
        resources = self.build()["resource"]
        dns = resources["oci_core_default_dhcp_options"]["default"]["options"][0]
        self.assertEqual(dns["custom_dns_servers"], ["10.0.1.20"])
        subnets = {s["display_name"]: s for s in resources["oci_core_subnet"].values()}
        self.assertTrue(subnets["private"]["prohibit_public_ip_on_vnic"])
        self.assertFalse(subnets["public"]["prohibit_public_ip_on_vnic"])
        self.assertEqual(subnets["private"]["cidr_block"], "10.0.1.0/24")

    def test_empty_network_has_no_fixed_resource_count(self):
        for group in ("subnets", "internet_gateways", "nat_gateways", "service_gateways", "network_security_groups"):
            self.inventory[group] = []
        self.inventory["route_tables"] = [self.inventory["route_tables"][1]]
        self.inventory["route_tables"][0]["route-rules"] = []
        self.inventory["security_lists"] = [self.inventory["security_lists"][0]]
        config = self.build()
        self.assertEqual(sum(len(v) for v in config["resource"].values()), 4)
        self.assertNotIn("data", config)
        self.assertEqual(config["output"]["subnets"]["value"], {})


if __name__ == "__main__":
    unittest.main()
