"""Build Terraform JSON for an independent, IPv4 OCI VCN copy.

The caller supplies an OCI CLI inventory and writes the returned dictionary to
main.tf.json. This module performs no I/O or OCI calls. Custom DHCP DNS server
addresses are preserved: the caller must ensure those services are reachable
from the destination. Workload IPs, defined tags, and source NAT public IPs are
deliberately not copied.
"""

from hashlib import sha256
from copy import deepcopy
from ipaddress import ip_address, ip_network
from pathlib import Path
import json
import re


class NetworkConfigError(ValueError):
    """Inventory cannot be copied faithfully within the supported scope."""


_BASE = {
    "id", "compartment-id", "display-name", "freeform-tags", "defined-tags",
    "lifecycle-state", "time-created", "vcn-id",
}
_FIELDS = {
    "vcn": {
        "cidr-block", "cidr-blocks", "default-dhcp-options-id",
        "default-route-table-id", "default-security-list-id", "dns-label",
        "vcn-domain-name", "byoipv6-cidr-blocks", "ipv6-private-cidr-blocks",
        "ipv6-cidr-blocks", "security-attributes", "is-zpr-only",
    },
    "subnets": {
        "availability-domain", "cidr-block", "ipv4-cidr-blocks", "dns-label",
        "dhcp-options-id", "route-table-id", "security-list-ids",
        "ipv6-cidr-block", "ipv6-cidr-blocks", "ipv6-virtual-router-ip",
        "prohibit-internet-ingress", "prohibit-public-ip-on-vnic",
        "subnet-domain-name", "virtual-router-ip", "virtual-router-mac",
    },
    "route_tables": {"route-rules"},
    "security_lists": {"ingress-security-rules", "egress-security-rules"},
    "dhcp_options": {"options", "domain-name-type"},
    "internet_gateways": {"is-enabled", "route-table-id"},
    "nat_gateways": {"block-traffic", "nat-ip", "public-ip-id", "route-table-id"},
    "service_gateways": {"block-traffic", "route-table-id", "services"},
    "network_security_groups": {"security-rules"},
}
_KINDS = {
    "subnets": ("oci_core_subnet", "subnet"),
    "route_tables": ("oci_core_route_table", "route"),
    "security_lists": ("oci_core_security_list", "security"),
    "dhcp_options": ("oci_core_dhcp_options", "dhcp"),
    "internet_gateways": ("oci_core_internet_gateway", "internet"),
    "nat_gateways": ("oci_core_nat_gateway", "nat"),
    "service_gateways": ("oci_core_service_gateway", "service"),
    "network_security_groups": ("oci_core_network_security_group", "nsg"),
}
_DEFAULTS = {
    "route_tables": ("default-route-table-id", "default_route_table_id"),
    "security_lists": ("default-security-list-id", "default_security_list_id"),
    "dhcp_options": ("default-dhcp-options-id", "default_dhcp_options_id"),
}
_SERVICE_PATTERNS = {
    "all": r"^all-[a-z0-9-]+-services-in-oracle-services-network$",
    "objectstorage": r"^oci-[a-z0-9-]+-objectstorage$",
}


def _error(message):
    raise NetworkConfigError(message)


def _required(obj, field, context):
    value = obj.get(field)
    if value is None or value == "":
        _error(f"{context}: missing {field}")
    return value


def _fields(obj, allowed, context):
    if not isinstance(obj, dict):
        _error(f"{context}: expected an object")
    unknown = sorted(k for k in obj if k not in allowed and obj[k] not in (None, [], {}, ""))
    if unknown:
        _error(f"{context}: unsupported populated fields: {', '.join(unknown)}")


def _list(obj, field, context):
    value = _required(obj, field, context)
    if not isinstance(value, list):
        _error(f"{context}: {field} must be an array")
    return value


def _string(value, context):
    if not isinstance(value, str) or not value:
        _error(f"{context}: expected a nonempty string")
    return value


def _literal(value):
    """Escape Terraform template syntax in source text, including tag values."""
    if isinstance(value, str):
        return value.replace("${", "$${").replace("%{", "%%{")
    if isinstance(value, list):
        return [_literal(x) for x in value]
    if isinstance(value, dict):
        return {k: _literal(v) for k, v in value.items()}
    return value


def _bool(value, context):
    if not isinstance(value, bool):
        _error(f"{context}: expected a boolean")
    return value


def _number(value, context, low=0, high=65535):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _error(f"{context}: expected integer in {low}..{high}")
    return value


def _ipv4(value, context):
    if not isinstance(value, str):
        _error(f"{context}: expected an IPv4 CIDR string")
    try:
        network = ip_network(value, strict=True)
    except (ValueError, TypeError):
        _error(f"{context}: invalid IPv4 CIDR {value!r}")
    if network.version != 4:
        _error(f"{context}: IPv6 is not supported")
    return str(network)


def _label(prefix, source_id):
    return f"{prefix}_{sha256(source_id.encode()).hexdigest()[:12]}"


def _expr(address):
    return "${" + address + "}"


def _metadata(obj, compartment_id):
    result = {"compartment_id": _literal(compartment_id)}
    if obj.get("display-name") is not None:
        result["display_name"] = _literal(obj["display-name"])
    tags = obj.get("freeform-tags") or {}
    if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in tags.items()):
        _error("freeform-tags must be a map of strings")
    result["freeform_tags"] = _literal(tags)
    return result


def _port_range(value, context):
    _fields(value, {"min", "max"}, context)
    result = {k: _number(_required(value, k, context), f"{context}.{k}") for k in ("min", "max")}
    if result["min"] > result["max"]:
        _error(f"{context}: minimum port exceeds maximum")
    return result


def build_preparation(inventory: dict, destination: dict) -> tuple[dict, dict]:
    """Return Terraform JSON and an offline manual-completion manifest.

    Resource labels are deterministic hashes of source IDs, with IDs sorted for
    stable serialization. No source infrastructure ID is emitted as a target.
    API-key profiles use ~/.oci/config. Cloud Shell uses instance principal
    authentication with an OBO token path supplied by the workflow subprocess.
    """
    if not isinstance(inventory, dict) or not isinstance(destination, dict):
        _error("inventory and destination must be objects")
    prepare = _bool(destination.get("prepare_network_only", False), "prepare_network_only")
    details = {
        "private_ip_targets": [], "deferred_routes": [], "excluded_drg_routes": [],
        "excluded_drg_attachments": [], "gateway_associations": [], "warnings": [],
    }
    if inventory.get("schema_version") != 1:
        _error("Unsupported inventory schema_version; expected 1")
    for field in ("source_region", "compartment_id"):
        _string(_required(inventory, field, "inventory"), f"inventory.{field}")
    for name, values in (inventory.get("unsupported_resources") or {}).items():
        if values and not (prepare and name == "drg_attachments"):
            _error(f"Unsupported network resources: {name}; no configuration generated")
    for field in ("region", "compartment_id", "profile", "config_file", "provider_version"):
        _string(_required(destination, field, "destination"), f"destination.{field}")
    details["destination_region"] = destination["region"]
    auth_type = destination.get("auth_type", "api_key")
    if auth_type not in ("api_key", "instance_obo_user"):
        _error("Unsupported authentication type")
    cloud_shell = auth_type == "instance_obo_user"
    if cloud_shell:
        _string(_required(destination, "delegation_token_file", "Cloud Shell"), "delegation_token_file")
    elif Path(destination["config_file"]).expanduser().resolve() != (Path.home() / ".oci/config").resolve():
        _error("OCI Terraform profiles require ~/.oci/config; custom config_file paths are not supported")
    if not re.fullmatch(r"\d+\.\d+\.\d+", destination["provider_version"]):
        _error("provider_version must be a pinned version such as 9.2.0")

    vcn = _required(inventory, "vcn", "inventory")
    _fields(vcn, _BASE | _FIELDS["vcn"], "VCN")
    vcn_id = _string(_required(vcn, "id", "VCN"), "VCN.id")
    for field in ("byoipv6-cidr-blocks", "ipv6-private-cidr-blocks", "ipv6-cidr-blocks", "security-attributes", "is-zpr-only"):
        if vcn.get(field):
            _error(f"VCN: unsupported {field}; IPv6 and ZPR networks need a separate migration")
    cidrs = vcn.get("cidr-blocks") or [vcn.get("cidr-block")]
    if not isinstance(cidrs, list) or not cidrs:
        _error("VCN: missing IPv4 CIDRs")
    cidrs = [_ipv4(c, "VCN") for c in cidrs]
    if vcn.get("cidr-block") and vcn["cidr-block"] not in cidrs:
        _error("VCN: cidr-block does not match cidr-blocks")

    resources = {}
    mappings = {}
    items = {}
    source_ids = {vcn_id}
    addresses = set()
    vcn_ref = _expr("oci_core_vcn.network.id")
    for group, (kind, prefix) in _KINDS.items():
        values = _list(inventory, group, "inventory")
        for obj in values:
            _fields(obj, _BASE | _FIELDS[group], group)
            _string(_required(obj, "id", group), f"{group}.id")
        values = sorted(values, key=lambda item: item["id"])
        items[group] = values
        mappings[group] = {}
        for obj in values:
            source_id = obj["id"]
            if source_id in source_ids:
                _error(f"Duplicate source resource ID in {group}: {source_id}")
            source_ids.add(source_id)
            if _required(obj, "vcn-id", group) != vcn_id:
                _error(f"{group}: resource belongs to another VCN")
            if obj.get("lifecycle-state", "AVAILABLE") != "AVAILABLE":
                _error(f"{group}: resource is not AVAILABLE")
            resource_kind, label = kind, _label(prefix, source_id)
            if group in _DEFAULTS and source_id == vcn.get(_DEFAULTS[group][0]):
                resource_kind = kind.replace("oci_core_", "oci_core_default_")
                label = "default"
            address = f"{resource_kind}.{label}"
            if address in addresses:
                _error("Resource label collision; cannot generate unambiguous Terraform addresses")
            addresses.add(address)
            mappings[group][source_id] = (resource_kind, label)
        if group in _DEFAULTS and _required(vcn, _DEFAULTS[group][0], "VCN") not in mappings[group]:
            _error(f"VCN default {_DEFAULTS[group][0]} is missing from inventory")
    if vcn.get("lifecycle-state", "AVAILABLE") != "AVAILABLE":
        _error("VCN is not AVAILABLE")

    excluded_drgs = set()
    if prepare:
        attachments = (inventory.get("unsupported_resources") or {}).get("drg_attachments", [])
        if not isinstance(attachments, list):
            _error("drg_attachments must be an array")
        attachment_ids = set()
        for attachment in sorted(attachments, key=lambda obj: str(obj.get("id", "")) if isinstance(obj, dict) else ""):
            if not isinstance(attachment, dict):
                _error("DRG attachment must be an object")
            attachment_id = _string(_required(attachment, "id", "DRG attachment"), "DRG attachment.id")
            drg_id = _string(_required(attachment, "drg-id", "DRG attachment"), "DRG attachment.drg-id")
            if not attachment_id.startswith("ocid1.drgattachment.") or not drg_id.startswith("ocid1.drg."):
                _error("DRG attachment has an invalid attachment or DRG OCID")
            if attachment_id in attachment_ids:
                _error("Duplicate DRG attachment ID")
            attachment_ids.add(attachment_id)
            network = attachment.get("network-details") or {}
            if not isinstance(network, dict) or network.get("type", "VCN") != "VCN":
                _error("DRG attachment does not describe a VCN attachment")
            attached_vcns = [x for x in (attachment.get("vcn-id"), network.get("id")) if x]
            if not attached_vcns or any(x != vcn_id for x in attached_vcns):
                _error("DRG attachment belongs to another VCN or has no VCN reference")
            if attachment.get("lifecycle-state", "ATTACHED") != "ATTACHED":
                _error("DRG attachment is not ATTACHED")
            ingress_tables = [x for x in (attachment.get("route-table-id"), network.get("route-table-id")) if x]
            if len(set(ingress_tables)) > 1 or any(x not in mappings["route_tables"] for x in ingress_tables):
                _error("DRG attachment has conflicting or missing ingress route table references")
            excluded_drgs.add(drg_id)
            source_ids.update((attachment_id, drg_id))
            source_ids.update(x for x in (attachment.get("drg-route-table-id"), attachment.get("export-drg-route-distribution-id")) if x)
            details["excluded_drg_attachments"].append(deepcopy(attachment))

    def reference(group, source_id, attribute="id"):
        if source_id not in mappings[group]:
            _error(f"Missing referenced resource in {group}: {source_id!r}")
        kind, label = mappings[group][source_id]
        return _expr(f"{kind}.{label}.{attribute}")

    def put(group, obj, body):
        kind, label = mappings[group][obj["id"]]
        resources.setdefault(kind, {})[label] = body

    def base(group, obj):
        result = _metadata(obj, destination["compartment_id"])
        if group in _DEFAULTS and obj["id"] == vcn[_DEFAULTS[group][0]]:
            result["manage_default_resource_id"] = _expr("oci_core_vcn.network." + _DEFAULTS[group][1])
        else:
            result["vcn_id"] = vcn_ref
        return result

    private_targets = {}
    if prepare:
        referenced_ips = set()
        for table in items["route_tables"]:
            for rule in _list(table, "route-rules", "route table"):
                if not isinstance(rule, dict):
                    _error("route rule: expected an object")
                target = _string(_required(rule, "network-entity-id", "route rule"), "route target")
                if target.startswith("ocid1.privateip."):
                    referenced_ips.add(target)
        records = inventory.get("private_ips", [])
        if not isinstance(records, list):
            _error("private_ips must be an array")
        errors = inventory.get("private_ip_lookup_errors", [])
        if not isinstance(errors, list):
            _error("private_ip_lookup_errors must be an array")
        by_id, lookup_errors, invalid_ids = {}, {}, set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                details["warnings"].append("Invalid private-IP metadata entry could not be matched to a route target and was ignored.")
                continue
            source_id = record["id"]
            if source_id in by_id:
                invalid_ids.add(source_id)
            by_id[source_id] = record
        for record in errors:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                details["warnings"].append("Invalid private-IP lookup error entry could not be matched to a route target and was ignored.")
                continue
            source_id = record["id"]
            if source_id in lookup_errors or source_id in by_id:
                invalid_ids.add(source_id)
            message = record.get("error")
            lookup_errors[source_id] = message if isinstance(message, str) and message else "Private-IP lookup failed without a valid error message."
        for source_id in sorted(referenced_ips):
            source_ids.add(source_id)
            obj = by_id.get(source_id)
            entry = {
                "source_id": source_id, "ip_address": None, "subnet_id": None,
                "vlan_id": None, "vnic_id": None, "terraform_label": None,
                "subnet_terraform_label": None, "status": "manual",
                "lookup_status": "unavailable", "lookup_error": None,
            }
            if source_id in invalid_ids:
                entry["lookup_error"] = "Conflicting or duplicate private-IP lookup records; source details could not be determined reliably."
            elif obj is None:
                entry["lookup_error"] = lookup_errors.get(source_id, "Private-IP details are missing from this export; the source address and owner could not be determined.")
            else:
                invalid_fields = []
                address = obj.get("ip-address")
                try:
                    if not isinstance(address, str):
                        raise ValueError("not a string")
                    entry["ip_address"] = str(ip_address(address))
                except ValueError:
                    invalid_fields.append("ip-address")
                for field, kind in (("subnet-id", "subnet"), ("vlan-id", "vlan"), ("vnic-id", "vnic")):
                    value = obj.get(field)
                    if value is None:
                        continue
                    if isinstance(value, str) and value.startswith(f"ocid1.{kind}."):
                        entry[field.replace("-", "_")] = value
                        source_ids.add(value)
                    else:
                        invalid_fields.append(field)
                if entry["subnet_id"] and entry["vlan_id"]:
                    invalid_fields.append("conflicting subnet-id and vlan-id")
                    entry["subnet_id"] = entry["vlan_id"] = None
                if obj.get("vcn-id") not in (None, vcn_id):
                    invalid_fields.append("vcn-id")
                    entry["subnet_id"] = entry["vlan_id"] = entry["vnic_id"] = None
                if invalid_fields:
                    entry["lookup_error"] = "Private-IP metadata has missing or invalid fields: " + ", ".join(invalid_fields) + "."
                else:
                    entry["lookup_status"] = "found"
                if obj.get("route-table-id"):
                    details["warnings"].append("Source private-IP route-table associations require manual per-IP routing configuration during appliance setup.")
                    if isinstance(obj["route-table-id"], str) and obj["route-table-id"].startswith("ocid1."):
                        source_ids.add(obj["route-table-id"])
            if entry["subnet_id"] in mappings["subnets"]:
                entry["subnet_terraform_label"] = mappings["subnets"][entry["subnet_id"]][1]
            if entry["lookup_status"] == "unavailable":
                details["warnings"].append(f"Private-IP target {source_id}: details unavailable. {entry['lookup_error']}")
                entry["reason"] = "Source details could not be fully determined. Identify the destination appliance and its private IP manually before completing these routes."
            elif entry["vlan_id"]:
                entry["reason"] = "VLAN-backed target: recreate the VMware/VLAN component and its private IP manually, then complete routes using the new private-IP OCID."
            else:
                entry["reason"] = "Create and configure the destination appliance and its private IP manually, then complete routes using the new private-IP OCID."
            private_targets[source_id] = entry
            details["private_ip_targets"].append(entry)
        details["warnings"] = sorted(set(details["warnings"]))

    services_by_id, services_by_cidr = {}, {}
    for service in _list(inventory, "services", "inventory"):
        _fields(service, {"id", "name", "description", "cidr-block"}, "service")
        service_id = _string(_required(service, "id", "service"), "service.id")
        cidr = _string(_required(service, "cidr-block", "service"), "service.cidr-block")
        if service_id in services_by_id or cidr in services_by_cidr:
            _error("Duplicate regional service ID or CIDR label")
        services_by_id[service_id] = cidr
        services_by_cidr[cidr] = service_id
        source_ids.add(service_id)
    used_services = set()

    def category(cidr):
        for name, pattern in _SERVICE_PATTERNS.items():
            if re.fullmatch(pattern, cidr):
                return name
        _error(f"Unsupported service category for source service CIDR {cidr!r}")

    def service_ref(value, attribute):
        if attribute == "id":
            if value not in services_by_id:
                _error(f"Unknown source service ID: {value!r}")
            cidr = services_by_id[value]
        else:
            if value not in services_by_cidr:
                _error(f"Unknown source service CIDR: {value!r}")
            cidr = value
        name = category(cidr)
        used_services.add(name)
        return _expr(f"local.service_{name}.{attribute}")

    def security_rule(rule, direction, nsg=False):
        allowed = {
            "description", "destination", "destination-type", "direction",
            "icmp-options", "id", "is-stateless", "is-valid", "protocol",
            "source", "source-type", "tcp-options", "time-created", "udp-options",
        }
        _fields(rule, allowed, "security rule")
        if direction not in ("INGRESS", "EGRESS"):
            _error(f"Unsupported security rule direction {direction!r}")
        if rule.get("direction") not in (None, direction):
            _error("Security rule direction conflicts with its containing list")
        if rule.get("is-valid") is False:
            _error("Invalid source NSG rule cannot be copied")
        protocol = str(_required(rule, "protocol", "security rule"))
        if protocol != "all" and (not protocol.isdecimal() or not 0 <= int(protocol) <= 255):
            _error(f"Unsupported protocol {protocol!r}")
        result = {"protocol": protocol, "stateless": _bool(rule.get("is-stateless", False), "security rule.is-stateless")}
        if rule.get("description") is not None:
            result["description"] = _literal(rule["description"])
        side, other = ("source", "destination") if direction == "INGRESS" else ("destination", "source")
        if rule.get(other) or rule.get(other + "-type"):
            _error(f"{direction} rule has unsupported {other} constraint")
        value = _required(rule, side, "security rule")
        value_type = rule.get(side + "-type") or "CIDR_BLOCK"
        if value_type == "CIDR_BLOCK":
            value = _ipv4(value, "security rule")
        elif value_type == "SERVICE_CIDR_BLOCK":
            value = service_ref(value, "cidr_block")
        elif value_type == "NETWORK_SECURITY_GROUP" and nsg:
            value = reference("network_security_groups", value)
        else:
            _error(f"Unsupported security rule {side} type: {value_type}")
        result[side], result[side + "_type"] = value, value_type
        for transport, expected_protocol in (("tcp", "6"), ("udp", "17")):
            options = rule.get(transport + "-options")
            if options is None:
                continue
            if protocol != expected_protocol:
                _error(f"{transport} options do not match rule protocol")
            _fields(options, {"source-port-range", "destination-port-range"}, transport + " options")
            converted = {}
            for port_side in ("source", "destination"):
                port_value = options.get(port_side + "-port-range")
                if port_value is not None:
                    port_range = _port_range(port_value, transport + " " + port_side)
                    if port_side == "destination" and not nsg:
                        converted.update(port_range)
                    else:
                        converted[port_side + "_port_range"] = [port_range]
            if converted:
                result[transport + "_options"] = [converted]
        icmp = rule.get("icmp-options")
        if icmp is not None:
            if protocol != "1":
                _error("ICMP options require IPv4 ICMP protocol 1")
            _fields(icmp, {"type", "code"}, "ICMP options")
            options = {"type": _number(_required(icmp, "type", "ICMP options"), "ICMP type", 0, 255)}
            if icmp.get("code") not in (None, -1):
                options["code"] = _number(icmp["code"], "ICMP code", 0, 255)
            result["icmp_options"] = [options]
        return result

    vcn_body = _metadata(vcn, destination["compartment_id"])
    vcn_body["cidr_blocks"] = cidrs
    if destination.get("vcn_name"):
        vcn_body["display_name"] = _literal(destination["vcn_name"])
    if vcn.get("dns-label"):
        vcn_body["dns_label"] = _literal(vcn["dns-label"])
    resources["oci_core_vcn"] = {"network": vcn_body}

    gateway_categories = {}
    for group in ("internet_gateways", "nat_gateways", "service_gateways"):
        for obj in items[group]:
            body = base(group, obj)
            if obj.get("route-table-id"):
                if not prepare or group != "internet_gateways":
                    _error(f"{group}: gateway route-table-id / ingress routing is not supported")
                ingress_id = obj["route-table-id"]
                ingress_ref = reference("route_tables", ingress_id)
                ingress = next(table for table in items["route_tables"] if table["id"] == ingress_id)
                if _list(ingress, "route-rules", "gateway ingress route table"):
                    _error(f"{group}: nonempty gateway ingress route tables require manual design; only originally empty tables can be preserved")
                body["route_table_id"] = ingress_ref
                details["gateway_associations"].append({
                    "gateway_type": group, "gateway_name": obj.get("display-name", ""),
                    "source_route_table_id": ingress_id, "action": "preserved_empty",
                })
            if group == "internet_gateways":
                body["enabled"] = _bool(_required(obj, "is-enabled", group), "internet gateway.is-enabled")
            elif group == "nat_gateways":
                body["block_traffic"] = _bool(_required(obj, "block-traffic", group), "NAT gateway.block-traffic")
            else:
                if _bool(_required(obj, "block-traffic", group), "service gateway.block-traffic"):
                    _error("Blocked service gateway cannot be preserved: block_traffic is computed-only")
                enabled = _list(obj, "services", group)
                if not enabled:
                    _error("Service gateway has no services")
                body["services"] = []
                gateway_categories[obj["id"]] = set()
                for service in enabled:
                    _fields(service, {"service-id", "service-name"}, "gateway service")
                    source_id = _required(service, "service-id", "gateway service")
                    body["services"].append({"service_id": service_ref(source_id, "id")})
                    gateway_categories[obj["id"]].add(category(services_by_id[source_id]))
            put(group, obj, body)

    for obj in items["route_tables"]:
        body = base("route_tables", obj)
        body["route_rules"] = []
        deferred = False
        for rule_index, rule in enumerate(_list(obj, "route-rules", "route table"), 1):
            _fields(rule, {"cidr-block", "destination", "destination-type", "network-entity-id", "description", "route-type"}, "route rule")
            route_type = rule.get("route-type") or "STATIC"
            if route_type != "STATIC":
                _error(f"Unsupported route type {route_type!r}")
            source_target = _required(rule, "network-entity-id", "route rule")
            matching = [g for g in ("internet_gateways", "nat_gateways", "service_gateways") if source_target in mappings[g]]
            special_target = prepare and (source_target in excluded_drgs or source_target in private_targets)
            if not matching and not special_target:
                _error(f"Unsupported route target {source_target!r}; only copied IGW, NAT and service gateways are supported (no DRG/LPG/private IP)")
            target_group = matching[0] if matching else None
            dest_type = rule.get("destination-type") or "CIDR_BLOCK"
            dest = rule.get("destination") or rule.get("cidr-block")
            if rule.get("cidr-block") and rule.get("destination") and rule["cidr-block"] != rule["destination"]:
                _error("Route has conflicting cidr-block and destination")
            if dest_type == "CIDR_BLOCK":
                dest = _ipv4(dest, "route destination")
                if target_group == "service_gateways":
                    _error("CIDR route to service gateway is unsupported")
            elif dest_type == "SERVICE_CIDR_BLOCK":
                converted = service_ref(dest, "cidr_block")
                if target_group != "service_gateways":
                    _error("Service CIDR route must target a copied service gateway")
                if not ({"all", category(dest)} & gateway_categories[source_target]):
                    _error("Service route category is not enabled on the target service gateway")
                dest = converted
            else:
                _error(f"Unsupported route destination type {dest_type!r}")
            if special_target:
                if rule.get("description") is not None and not isinstance(rule["description"], str):
                    _error("Route description must be a string or null")
                record = {
                    "source_route_table_id": obj["id"], "route_table_name": obj.get("display-name", ""),
                    "terraform_route_table_label": mappings["route_tables"][obj["id"]][1],
                    "rule_index": rule_index, "source_target_id": source_target,
                    "destination": dest, "destination_type": dest_type,
                    "description": rule.get("description"),
                }
                if source_target in excluded_drgs:
                    details["excluded_drg_routes"].append(record)
                else:
                    details["deferred_routes"].append(record)
                    deferred = True
                continue
            converted = {"destination": dest, "destination_type": dest_type, "network_entity_id": reference(target_group, source_target), "route_type": "STATIC"}
            if rule.get("description") is not None:
                converted["description"] = _literal(rule["description"])
            body["route_rules"].append(converted)
        if deferred:
            # Once the customer completes appliance routing, Terraform must not
            # erase those rules on a subsequent network preparation apply.
            body["lifecycle"] = {"ignore_changes": ["route_rules"]}
        put("route_tables", obj, body)

    for obj in items["security_lists"]:
        body = base("security_lists", obj)
        # Explicit empty lists also clear OCI's automatically created default rules.
        for side in ("ingress", "egress"):
            body[side + "_security_rules"] = [security_rule(rule, side.upper()) for rule in _list(obj, side + "-security-rules", "security list")]
        put("security_lists", obj, body)

    for obj in items["network_security_groups"]:
        put("network_security_groups", obj, base("network_security_groups", obj))
        rules = _list(obj, "security-rules", "NSG")
        labels = set()
        for rule in sorted(rules, key=lambda r: str(r.get("id", "")) + json.dumps(r, sort_keys=True)):
            body = security_rule(rule, _required(rule, "direction", "NSG rule"), nsg=True)
            body["direction"] = rule["direction"]
            body["network_security_group_id"] = reference("network_security_groups", obj["id"])
            identity = obj["id"] + ":" + str(rule.get("id") or json.dumps(rule, sort_keys=True))
            label = _label("rule", identity)
            if label in labels:
                _error("Duplicate NSG rule identity")
            labels.add(label)
            resources.setdefault("oci_core_network_security_group_security_rule", {})[label] = body

    for obj in items["dhcp_options"]:
        body = base("dhcp_options", obj)
        if obj.get("domain-name-type") is not None:
            if obj["domain-name-type"] not in ("SUBNET_DOMAIN", "VCN_DOMAIN", "CUSTOM_DOMAIN"):
                _error(f"Unsupported DHCP domain-name-type {obj['domain-name-type']!r}")
            body["domain_name_type"] = obj["domain-name-type"]
        body["options"] = []
        for option in _list(obj, "options", "DHCP options"):
            _fields(option, {"type", "server-type", "custom-dns-servers", "search-domain-names"}, "DHCP option")
            kind = _required(option, "type", "DHCP option")
            converted = {"type": kind}
            if kind == "DomainNameServer":
                server_type = _required(option, "server-type", "DHCP DNS option")
                if server_type not in ("VcnLocal", "VcnLocalPlusInternet", "CustomDnsServer"):
                    _error(f"Unsupported DHCP server type {server_type!r}")
                converted["server_type"] = server_type
                servers = option.get("custom-dns-servers") or []
                if (server_type == "CustomDnsServer") != bool(servers):
                    _error("Custom DHCP DNS servers do not match server-type")
                for server in servers:
                    try:
                        if ip_address(server).version != 4:
                            _error("IPv6 custom DNS servers are unsupported")
                    except ValueError:
                        _error("Invalid custom DNS server IP address")
                if servers:
                    converted["custom_dns_servers"] = list(servers)
                if option.get("search-domain-names"):
                    _error("DNS DHCP option has unexpected search domains")
            elif kind == "SearchDomain":
                converted["search_domain_names"] = _literal(_list(option, "search-domain-names", "DHCP search option"))
                if option.get("server-type") or option.get("custom-dns-servers"):
                    _error("SearchDomain DHCP option has unexpected DNS server settings")
            else:
                _error(f"Unsupported DHCP option type {kind!r}")
            body["options"].append(converted)
        if not body["options"]:
            _error("DHCP options array cannot be empty")
        put("dhcp_options", obj, body)

    for obj in items["subnets"]:
        if obj.get("availability-domain"):
            _error("AD-specific subnets are unsupported; map availability domains explicitly before copying")
        if any(obj.get(field) for field in ("ipv6-cidr-block", "ipv6-cidr-blocks", "ipv6-virtual-router-ip")):
            _error("IPv6 subnets are unsupported")
        cidr = _ipv4(_required(obj, "cidr-block", "subnet"), "subnet")
        if obj.get("ipv4-cidr-blocks") not in (None, [], [cidr]):
            _error("Multi-CIDR subnets are unsupported")
        if not any(ip_network(cidr).subnet_of(ip_network(parent)) for parent in cidrs):
            _error("Subnet CIDR is outside the copied VCN")
        body = base("subnets", obj)
        body["cidr_block"] = cidr
        body["prohibit_public_ip_on_vnic"] = _bool(_required(obj, "prohibit-public-ip-on-vnic", "subnet"), "subnet.prohibit-public-ip-on-vnic")
        if obj.get("prohibit-internet-ingress") is not None and _bool(obj["prohibit-internet-ingress"], "subnet.prohibit-internet-ingress") != body["prohibit_public_ip_on_vnic"]:
            _error("Subnet ingress and public-IP restrictions differ; unsupported for IPv4 copy")
        if obj.get("dns-label"):
            body["dns_label"] = _literal(obj["dns-label"])
        body["route_table_id"] = reference("route_tables", _required(obj, "route-table-id", "subnet"))
        body["dhcp_options_id"] = reference("dhcp_options", _required(obj, "dhcp-options-id", "subnet"))
        body["security_list_ids"] = [reference("security_lists", sid) for sid in _list(obj, "security-list-ids", "subnet")]
        put("subnets", obj, body)

    config = {
        "terraform": {"required_version": ">= 1.4.0", "required_providers": {"oci": {"source": "oracle/oci", "version": destination["provider_version"]}}},
        "provider": {"oci": {"region": _literal(destination["region"]), "config_file_profile": _literal(destination["profile"])}},
        "resource": resources,
        "output": {
            "region": {"value": _literal(destination["region"])},
            "compartment_id": {"value": _literal(destination["compartment_id"])},
            "vcn_id": {"value": vcn_ref},
            "subnets": {"value": {mappings["subnets"][o["id"]][1]: {"id": reference("subnets", o["id"]), "cidr": o["cidr-block"], "name": _literal(o.get("display-name", ""))} for o in items["subnets"]}},
            "gateways": {"value": {g: {mappings[g][o["id"]][1]: reference(g, o["id"]) for o in items[g]} for g in ("internet_gateways", "nat_gateways", "service_gateways")}},
            "nat_public_ips": {"value": {mappings["nat_gateways"][o["id"]][1]: reference("nat_gateways", o["id"], "nat_ip") for o in items["nat_gateways"]}},
        },
    }
    if cloud_shell:
        # Explicitly empty profile avoids the provider's ~/.oci/config lookup.
        # The delegation token is supplied through provider environment variables.
        config["provider"]["oci"] = {
            "auth": "InstancePrincipal", "region": _literal(destination["region"]),
            "config_file_profile": "",
        }
    if used_services:
        config["data"] = {"oci_core_services": {"destination": {}}}
        config["locals"] = {"service_" + name: _expr('one([for service in data.oci_core_services.destination.services : service if can(regex(' + json.dumps(_SERVICE_PATTERNS[name]) + ', service.cidr_block))])') for name in sorted(used_services)}
    if prepare:
        config["output"]["route_tables"] = {"value": {
            mappings["route_tables"][obj["id"]][1]: {
                "id": reference("route_tables", obj["id"]), "name": _literal(obj.get("display-name", "")),
            } for obj in items["route_tables"]
        }}
    serialized = json.dumps(config)
    if any(source_id in serialized for source_id in source_ids):
        _error("A source resource ID remains in generated text (possibly a name or tag); remove the source reference before preparing")
    return config, details


def build_config(inventory: dict, destination: dict) -> dict:
    """Return Terraform JSON; preparation mode requires an explicit opt-in."""
    return build_preparation(inventory, destination)[0]
