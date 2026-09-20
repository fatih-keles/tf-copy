"""Write offline, non-executable instructions for manually completing routing."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from collections import OrderedDict


NOT_DEPLOYED = "NOT DEPLOYED"
MANUAL = "MANUAL / UNRESOLVED"

CSV_FIELDS = (
    "source_route_table_name", "source_route_table_id", "terraform_route_table_label",
    "destination_route_table_id", "source_rule_index", "destination", "destination_type",
    "description", "source_target_private_ip_id", "target_ip_address",
    "source_subnet_id", "source_subnet_name", "source_subnet_cidr", "source_vlan_id",
    "destination_subnet_id", "destination_target_private_ip_id", "target_status", "reason",
    "customer_vm_name", "customer_vnic_id", "customer_target_private_ip_id",
    "customer_forwarding_verified", "customer_skip_source_dest_check_verified",
    "customer_rule_added", "customer_connectivity_verified", "customer_completed_at",
    "customer_notes",
)


def _required_text(value, context):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Manual routing handoff: missing {context}.")
    return value


def _output_map(outputs, name, required):
    if outputs is None or not required:
        return {}
    item = outputs.get(name)
    if not isinstance(item, dict) or not isinstance(item.get("value"), dict):
        raise ValueError(f"Manual routing handoff: missing Terraform output {name}.")
    return item["value"]


def _destination_id(mapping, label, kind, source_id):
    entry = mapping.get(label)
    if not isinstance(entry, dict):
        raise ValueError(f"Manual routing handoff: missing {kind} output for {label!r}.")
    value = _required_text(entry.get("id"), f"{kind} ID for {label!r}")
    if value == source_id:
        raise ValueError(f"Manual routing handoff: destination {kind} ID equals its source ID.")
    return value, entry


def _markdown(value):
    if value is None or value == "":
        return "—"
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("|", "&#124;")
            .replace("`", "&#96;").replace("\r", " ").replace("\n", "<br>"))


def _csv_value(value):
    if value is None:
        return ""
    text = str(value)
    # Keep source names/descriptions from becoming formulas in a spreadsheet.
    if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + text
    return text


def _target_rows(inventory, details, outputs):
    targets = details.get("private_ip_targets", [])
    reservations = _output_map(outputs, "reserved_private_ips", any(t.get("status") == "reserved" for t in targets))
    subnets = {s["id"]: s for s in inventory.get("subnets", [])}
    rows = OrderedDict()
    for target in targets:
        source_id = _required_text(target.get("source_id"), "source private-IP ID")
        if source_id in rows:
            raise ValueError(f"Manual routing handoff: duplicate source target {source_id}.")
        status = target.get("status")
        if status not in ("reserved", "manual_vlan"):
            raise ValueError(f"Manual routing handoff: unsupported target status {status!r}.")
        ip_address = _required_text(target.get("ip_address"), "target IP address")
        subnet_id = target.get("subnet_id")
        subnet = subnets.get(subnet_id, {})
        row = {
            "source_target_private_ip_id": source_id,
            "target_ip_address": ip_address,
            "source_subnet_id": subnet_id,
            "source_subnet_name": subnet.get("display-name"),
            "source_subnet_cidr": subnet.get("cidr-block"),
            "source_vlan_id": target.get("vlan_id"),
            "terraform_label": target.get("terraform_label"),
            "target_status": status,
            "reason": target.get("reason", ""),
            "destination_subnet_id": None,
            "destination_target_private_ip_id": None,
            "resolution": "manual_unresolved" if status == "manual_vlan" else "not_deployed",
        }
        if status == "reserved":
            label = _required_text(target.get("terraform_label"), "reserved private-IP Terraform label")
            if outputs is not None:
                destination_id, entry = _destination_id(reservations, label, "reserved private-IP", source_id)
                if entry.get("ip_address") != ip_address:
                    raise ValueError(f"Manual routing handoff: reserved IP address does not match {label!r}.")
                destination_subnet = _required_text(entry.get("subnet_id"), f"destination subnet for {label!r}")
                if destination_subnet == subnet_id:
                    raise ValueError("Manual routing handoff: destination subnet ID equals its source ID.")
                row.update(destination_target_private_ip_id=destination_id,
                           destination_subnet_id=destination_subnet, resolution="reserved_attachment_unverified")
        elif target.get("terraform_label"):
            raise ValueError("Manual routing handoff: VLAN targets must not claim a Terraform reservation.")
        rows[source_id] = row
    return rows


def _route_rows(rules, route_tables, targets, deployed, private_targets):
    rows = []
    seen = set()
    for rule in rules:
        source_table_id = _required_text(rule.get("source_route_table_id"), "source route-table ID")
        label = _required_text(rule.get("terraform_route_table_label"), "route-table Terraform label")
        source_target = _required_text(rule.get("source_target_id"), "source route target ID")
        index = rule.get("rule_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 1:
            raise ValueError("Manual routing handoff: source rule index must be a positive integer.")
        identity = (source_table_id, index)
        if identity in seen:
            raise ValueError("Manual routing handoff: duplicate source route-table/rule index.")
        seen.add(identity)
        destination_id = None
        if deployed:
            destination_id, _ = _destination_id(route_tables, label, "route-table", source_table_id)
        row = {
            "source_route_table_name": rule.get("route_table_name", ""),
            "source_route_table_id": source_table_id,
            "terraform_route_table_label": label,
            "destination_route_table_id": destination_id,
            "source_rule_index": index,
            "destination": _required_text(rule.get("destination"), "route destination"),
            "destination_type": _required_text(rule.get("destination_type"), "route destination type"),
            "description": rule.get("description"),
            "source_target_id": source_target,
        }
        if private_targets:
            if source_target not in targets:
                raise ValueError(f"Manual routing handoff: no target details for {source_target}.")
            row.update(targets[source_target])
        rows.append(row)
    return rows


def _display_id(value, deployed, manual=False):
    return value or (MANUAL if manual else NOT_DEPLOYED if not deployed else "UNRESOLVED")


def _render_markdown(report):
    deployed = report["deployment_outputs_available"]
    rules, targets = report["pending_routes"], report["private_ip_targets"]
    lines = ["# Manual routing handoff", "",
             f"Pending private-IP route rules: **{len(rules)}**. Target addresses: **{len(targets)}**.", "",
             f"Source region: {_markdown(report['source_region'])}. Destination region: {_markdown(report['destination_region'])}.",
             f"Source snapshot exported at: {_markdown(report['source_exported_at'])}.",
             f"Report generated at: {_markdown(report['generated_at'])}.", ""]
    if deployed:
        lines += ["Destination IDs below come from Terraform outputs. Reserved addresses still need appliance attachment and configuration; this report does not verify their current attachment or readiness.", ""]
    else:
        lines += ["**NOT DEPLOYED:** destination IDs are unavailable. After a successful apply, the scripts regenerate this report with destination IDs. Source OCIDs are reference information and must never be entered as destination targets.", ""]
    lines += [
        "## Complete the routing", "",
        "1. Review the source snapshot and the pending rules below. This is an export-time snapshot, not a validation of the current source or destination network.",
        "2. Deploy the required appliance VMs in the corresponding destination subnets. For each reserved address, use the existing destination private-IP OCID from this report when creating the VM's primary VNIC: supply it as privateIpId in [CreateVnicDetails](https://docs.oracle.com/en-us/iaas/tools/java/latest/com/oracle/bmc/core/model/CreateVnicDetails.Builder.html#privateIpId(java.lang.String)). Do not request a new allocation of the same IPv4 address. Confirm the reservation is attached to the intended appliance.",
        "3. Resolve every VLAN target manually. No VLAN or private-IP reservation was created for these targets. Provision the required destination network/appliance and record its actual destination private-IP OCID; the source address alone is not a valid destination target.",
        "4. Enable IP forwarding in the appliance operating system and enable skip source/destination check on the routing VNIC. Configure the appliance, security rules, return routes, and next hops needed for the intended traffic.",
        "5. In the destination OCI Console, open each route table using its destination route-table OCID below. Choose Add Route Rules and add each listed rule individually with target type Private IP and its resolved destination private-IP OCID. Copy the destination, destination type, and description. Preserve all existing rules. Do not replace the route table's complete rule list to add these entries.",
        "6. Verify traffic in both directions, then fill the customer completion columns in manual-routing.csv. Keep a separate copy of the completed CSV: regenerating this report overwrites the blank checklist.", "",
        "Terraform ignores route_rules changes on the owned route tables so that manual additions can remain. Reserved private-IP resources also use lifecycle ignore_changes = all so customer assignments and settings can remain. A successful run.sh check / No changes result does not verify these pending rules, manually edited routes, private-IP assignments or settings, appliance readiness, or connectivity.", "",
        "## Before cleanup", "",
        "Remove the customer-managed appliances and unassign their reserved private IPs. Remove the manually added private-IP route rules that reference those addresses before running ./cleanup.sh destroy. Those manual route dependencies are outside Terraform's resource graph. Cleanup refuses destruction while a tracked reserved private IP remains attached to a VNIC; it does not remove customer-managed appliances for you. Keep the Terraform state until destruction succeeds. Cleanup destroys the project's owned route tables and any remaining rules in them.", "",
        "## Target addresses", "",
        "| Address | Source private-IP OCID | Source subnet / VLAN | Destination subnet OCID | Destination private-IP OCID | Status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for target in targets:
        manual = target["target_status"] == "manual_vlan"
        source_network = target["source_vlan_id"] if manual else " / ".join(str(x) for x in (target["source_subnet_name"], target["source_subnet_cidr"], target["source_subnet_id"]) if x)
        values = [target["target_ip_address"], target["source_target_private_ip_id"], source_network,
                  _display_id(target["destination_subnet_id"], deployed, manual),
                  _display_id(target["destination_target_private_ip_id"], deployed, manual),
                  target["resolution"] + (": " + target["reason"] if target["reason"] else "")]
        lines.append("| " + " | ".join(_markdown(v) for v in values) + " |")
    if not targets:
        lines.append("| None | — | — | — | — | — |")
    lines += ["", "## Pending private-IP rules", ""]
    grouped = OrderedDict()
    for rule in rules:
        grouped.setdefault(rule["source_route_table_id"], []).append(rule)
    for grouped_rules in grouped.values():
        first = grouped_rules[0]
        lines += [f"### {_markdown(first['source_route_table_name'])}", "",
                  f"Source route table: {_markdown(first['source_route_table_id'])}.",
                  f"Destination route table: {_markdown(_display_id(first['destination_route_table_id'], deployed))}.",
                  f"Terraform label: {_markdown(first['terraform_route_table_label'])}.", "",
                  "| Source rule # | Destination | Destination type | Target address | Destination private-IP OCID | Description |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for row in grouped_rules:
            values = [row["source_rule_index"], row["destination"], row["destination_type"], row["target_ip_address"],
                      _display_id(row["destination_target_private_ip_id"], deployed, row["target_status"] == "manual_vlan"), row["description"]]
            lines.append("| " + " | ".join(_markdown(v) for v in values) + " |")
        lines.append("")
    if not rules:
        lines += ["No private-IP route rules are pending in this snapshot.", ""]
    lines += ["## Excluded DRG connectivity", "",
              f"Excluded DRG route rules: **{len(report['excluded_drg_routes'])}**. Excluded DRG attachments: **{len(report['excluded_drg_attachments'])}**.",
              "These omissions are intentional. No destination DRG, DRG attachment, or replacement DRG route is created. Design and configure any required external connectivity separately; private-IP routing completion does not restore it.", "",
              "| Source route table | Source rule # | Destination | Destination type | Source DRG OCID | Description |",
              "| --- | --- | --- | --- | --- | --- |"]
    for rule in report["excluded_drg_routes"]:
        values = [rule["source_route_table_name"], rule["source_rule_index"], rule["destination"], rule["destination_type"], rule["source_target_id"], rule["description"]]
        lines.append("| " + " | ".join(_markdown(v) for v in values) + " |")
    if not report["excluded_drg_routes"]:
        lines.append("| None | — | — | — | — | — |")
    for attachment in report["excluded_drg_attachments"]:
        lines.append(f"- Source attachment: {_markdown(attachment.get('display-name'))} / {_markdown(attachment.get('id'))}; DRG: {_markdown(attachment.get('drg-id'))}.")
    lines += ["", "## Gateway route-table associations", ""]
    for association in report["gateway_associations"]:
        lines.append(f"- {_markdown(association.get('gateway_type'))} {_markdown(association.get('gateway_name'))}: source route table {_markdown(association.get('source_route_table_id'))}; action {_markdown(association.get('action'))}. The internet gateway's association with an originally empty route table was preserved. This does not establish appliance ingress routing.")
    if not report["gateway_associations"]:
        lines.append("No gateway route-table associations require a handoff in this snapshot.")
    if report["warnings"]:
        lines += ["", "## Additional snapshot notes", ""]
        lines += ["- " + _markdown(note) for note in report["warnings"]]
    lines += ["", "manual-routing.json retains the machine-readable source and destination mapping. manual-routing.csv contains one row per pending private-IP route plus blank customer completion fields. CSV cells beginning with spreadsheet formula characters are prefixed with an apostrophe; JSON retains the original text.", ""]
    return "\n".join(lines)


def _write_private(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    # O_TRUNC also handles regeneration; explicit chmod tightens older files.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)


def write_handoff(directory, inventory, details, outputs=None):
    """Write reports using only inventory, preparation details, and TF outputs.

    ``outputs=None`` means pre-deployment. Any supplied outputs must contain all
    expected destination mappings; incomplete post-apply reports are rejected.
    The returned dictionary is also serialized to manual-routing.json.
    """
    if outputs is not None and not isinstance(outputs, dict):
        raise ValueError("Manual routing handoff: Terraform outputs must be an object.")
    targets = _target_rows(inventory, details, outputs)
    pending = details.get("deferred_routes", [])
    excluded = details.get("excluded_drg_routes", [])
    route_tables = _output_map(outputs, "route_tables", bool(pending or excluded))
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_exported_at": inventory.get("exported_at"),
        "source_region": inventory.get("source_region"),
        "destination_region": details.get("destination_region"),
        "deployment_outputs_available": outputs is not None,
        "validation": "Snapshot and Terraform output mapping only; current attachments, manual routes, and connectivity are not verified.",
        "private_ip_targets": list(targets.values()),
        "pending_routes": _route_rows(pending, route_tables, targets, outputs is not None, True),
        "excluded_drg_routes": _route_rows(excluded, route_tables, targets, outputs is not None, False),
        "excluded_drg_attachments": details.get("excluded_drg_attachments", []),
        "gateway_associations": details.get("gateway_associations", []),
        "warnings": details.get("warnings", []),
    }
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for rule in report["pending_routes"]:
        row = dict(rule)
        manual = rule["target_status"] == "manual_vlan"
        row["destination_route_table_id"] = _display_id(rule["destination_route_table_id"], outputs is not None)
        row["destination_subnet_id"] = _display_id(rule["destination_subnet_id"], outputs is not None, manual)
        row["destination_target_private_ip_id"] = _display_id(rule["destination_target_private_ip_id"], outputs is not None, manual)
        writer.writerow({key: _csv_value(value) for key, value in row.items()})
    rendered = {"manual-routing.json": json.dumps(report, indent=2, sort_keys=True) + "\n",
                "manual-routing.md": _render_markdown(report),
                "manual-routing.csv": csv_buffer.getvalue()}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        _write_private(directory / name, content)
    return report
