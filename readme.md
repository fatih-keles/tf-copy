# Copy an OCI VCN between regions

## Quick start with Cloud Shell

```bash
git clone https://github.com/fatih-keles/tf-copy.git
cd tf-copy
cp .env.example .env
nano .env
```

Set these values in `.env`:

```dotenv
SOURCE_REGION=me-dubai-1
DESTINATION_REGION=me-abudhabi-1
SOURCE_COMPARTMENT_OCID=ocid1.compartment.oc1..REPLACE_ME
PREPARE_NETWORK_ONLY=true
```

Leave `OCI_PROFILE` blank. Leave `DESTINATION_COMPARTMENT_OCID` blank to use the
same compartment. If the source compartment has multiple VCNs, also set
`SOURCE_VCN_OCID`. Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

Export, review, and preview the deployment:

```bash
./export.sh
./review.sh
cat .work/manual-routing.md
./run.sh plan
```

This mode prepares the supported network, reserves the IPv4 addresses used by
supported private-IP route targets, and leaves appliance deployment and those
routes for you to finish manually. DRG attachments and DRG routes are excluded.
Check the CIDRs, resource counts, and manual-routing report. A fresh plan should
show resources to add, with **0 changed and 0 destroyed**. Then deploy:

```bash
./run.sh apply
./run.sh handoff
cat .work/manual-routing.md
./run.sh check
```

`apply` runs without another confirmation and refreshes the report with the
created destination OCIDs. `handoff` lets you regenerate it later. `check` should
report **No changes** for Terraform-managed settings; it does not verify pending
manual routes or forwarding. Follow [Complete manual routing](#complete-manual-routing)
before using paths that depend on appliances. Keep `.env`, including
`PREPARE_NETWORK_ONLY`, unchanged and preserve `.work/` while the network exists.

When you want to delete the copied network, first remove your manually deployed
appliances, unassign the reserved private IPs, and remove the manual routes that
reference them. Then remove the copied network and generated local files:

```bash
./cleanup.sh all
# Review the destroy plan and type yes.
```

To test again after cleanup, repeat the export, review, plan, apply, and check
commands. If you never applied, `./cleanup.sh local` removes just the local files.

## Manual installation on Compute

Export one VCN and its supported network configuration, review the inventory,
and create a separate network in another OCI region using Terraform. Source and
destination regions, compartments, and the OCI profile are configured in `.env`.
Both regions must belong to the same tenancy and use the same OCI profile.

### Requirements

- Git, Bash, Python 3.10 or later, and Terraform 1.4 or later.
- OCI CLI with working API-key authentication in `~/.oci/config`, outside this
  repository. OCI CLI and Terraform use the same named profile in that file.
- Access to read the source network and manage networking in the destination
  compartment. The tenancy must be subscribed to the destination region.
- In preparation mode, access to read private-IP records referenced by source
  route rules, their VNIC/VLAN details, and to create reserved private IPs in the
  destination subnets.

OCI provider `9.2.0` is the example version; the selected version is pinned in
the generated Terraform configuration. These scripts use local Terraform state.
Remote backends are not supported.

Install the prerequisites using the official [OCI CLI guide](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm)
and [Terraform guide](https://developer.hashicorp.com/terraform/install).

### Configure

```bash
git clone https://github.com/fatih-keles/tf-copy.git
cd tf-copy
cp .env.example .env
nano .env
```

| Setting | Meaning |
| --- | --- |
| `OCI_PROFILE` | API-key profile in the OCI configuration file; defaults to `DEFAULT`. |
| `SOURCE_REGION` | Region to export, for example `me-dubai-1`. |
| `DESTINATION_REGION` | Region to create the copy in, for example `me-abudhabi-1`; must differ from the source. |
| `SOURCE_COMPARTMENT_OCID` | Compartment containing the source VCN and its network resources. |
| `DESTINATION_COMPARTMENT_OCID` | Destination compartment; blank uses the source compartment. |
| `SOURCE_VCN_OCID` | VCN to export; blank automatically selects the VCN only when the source compartment contains exactly one. |
| `DESTINATION_VCN_NAME` | Display name for the new VCN; blank preserves the source display name. |
| `PREPARE_NETWORK_ONLY` | `true` prepares supported networking and a manual-routing handoff. `false`, the default when omitted, retains strict copy validation. The example opts into `true`. |
| `OCI_PROVIDER_VERSION` | Exact OCI Terraform provider version, for example `9.2.0`. |

Keep credentials in the OCI configuration file and key file, outside this
repository. `.env` and all generated data under `.work/` are ignored by Git.

### Export and review

```bash
./export.sh
./review.sh
```

`export.sh` reads the selected VCN through OCI CLI and saves the network snapshot
to `.work/export.json`. It does not create or change OCI resources. Discovery is
limited to the selected source compartment and does not recurse into child
compartments. In preparation mode it also reads private-IP records referenced
by route rules and their VNIC/VLAN details. These referenced objects can be read
by OCID across compartments; this is not recursive compartment discovery.
The source snapshot is preserved; the copied configuration and
manual-routing reports are generated separately.

`review.sh` summarizes the exported inventory and writes `.work/review.txt`.
Review the VCN, subnet CIDRs, security rules, route tables, and gateway
dependencies in `.work/export.json` before planning. Review also checks whether
the snapshot is supported, but does not compare it with live OCI or prove
connectivity. It does not change OCI resources.

With `PREPARE_NETWORK_ONLY=true`, review also generates
`.work/manual-routing.md`, `.work/manual-routing.csv`, and
`.work/manual-routing.json`. These list deferred private-IP routes and excluded
DRG routes before deployment. Destination OCIDs are filled in after apply.
Older exports without the referenced private-IP details must be exported again
with `./export.sh` before planning this mode. Missing addresses are never guessed.

### Plan, apply, and check

```bash
./run.sh plan
# Read the Terraform plan printed above before applying it.
./run.sh apply
./run.sh check
```

`plan` builds `.work/destination/main.tf.json` from the export, initializes and
validates Terraform, and saves a destination plan. References between copied
resources use the new Terraform resources; service gateway references are
resolved for the destination region.

`apply` executes the saved plan without another confirmation prompt. It verifies
that the configuration and environment still match the saved plan. The first
plan binds the workspace to its source snapshot and destination settings. Keep
those settings, including `PREPARE_NETWORK_ONLY`, unchanged until cleanup; to
use different settings, destroy and
clean the existing workspace first, or use a separate checkout. Generated
Terraform files are not intended for manual editing.

`check` compares the managed destination network with the configuration. It exits
with `0` when no changes are needed, `2` when differences exist, and `1` on error.
In preparation mode, route tables with deferred rules give ownership of their
entire rule list to the manual operator using Terraform `ignore_changes` on
`route_rules`. **No changes does not validate those rule lists, complete the
pending routes, audit customer-managed reserved-IP settings, or prove
connectivity.**

Terraform state stays in `.work/destination/`, including after a failed or
partially completed apply. Keep that directory to manage or destroy the created
resources. Use a separate checkout for another independently managed copy.

### Complete manual routing

With `PREPARE_NETWORK_ONLY=true`, apply creates reserved private IPs for supported
targets in the copied subnets, keeping the source IPv4 addresses. It does not
deploy appliances, attach those IPs to VNICs, or activate deferred private-IP
routes. The report records VLAN targets as blocked manual work because this
workflow cannot copy VLANs or reserve their addresses in a copied subnet.
After creation, reservation attachment and lifecycle settings are managed by
the customer so Terraform does not reset them during later operations. Verify
those settings in OCI; `check` does not audit their readiness.

After applying, regenerate and read the handoff:

```bash
./run.sh handoff
cat .work/manual-routing.md
```

`handoff` reads the current Terraform outputs and regenerates all three report
formats. It does not change routing. Use the destination OCIDs in the report;
source OCIDs identify the original resources only.

1. Resolve any blocked targets listed in the report. VLAN targets require a
   separate customer design and are not ready for rule activation.
2. Deploy the required appliance VMs in the destination. For each supported
   reserved address, use the **existing destination private-IP OCID** from the
   report when creating the VM's primary VNIC (`privateIpId` in
   [CreateVnicDetails](https://docs.oracle.com/en-us/iaas/tools/java/latest/com/oracle/bmc/core/model/CreateVnicDetails.Builder.html#privateIpId(java.lang.String))).
   Do not request a new allocation of the same IPv4 address. Configure the
   appliance and its VNICs for the intended forwarding behavior.
3. In the destination OCI Console, open each route table identified in the
   report and **add** the listed deferred private-IP rules using their actual
   destination target OCIDs. Preserve the rules already present in the table;
   do not replace its entire rule list. Add a rule only after its target is
   deployed, attached, and able to forward traffic. Excluded DRG rules require
   separately prepared destination connectivity.
4. Test the intended traffic paths and appliance forwarding. Keep the completed
   report with your deployment records.

Forwarding through the appliances is unavailable until they and their manual
rules are complete. Routes that remain through a NAT or internet gateway may
send traffic along a different path in the meantime. A successful Terraform
apply or **No changes** check does not mean this network is fully ready.

### Remove a copy or local files

```bash
# Show a destroy plan, then type yes to delete tracked destination resources.
./cleanup.sh destroy

# Delete local generated files only after no managed resources remain in state.
./cleanup.sh local

# Perform both steps in order.
./cleanup.sh all
```

`destroy` acts only on destination resources tracked by this workspace's
Terraform state. It does not discover resources to delete by name or remove the
source VCN. `local` refuses to delete a workspace whose state still tracks
managed resources. Existing Terraform state is retained when destruction fails.

In preparation mode, cleanup checks the reserved private IPs in OCI and refuses
destruction while any are attached to a VNIC. Remove the customer-managed
appliances and unassign their reserved IPs, then remove the manually added routes
that reference those IPs before running cleanup. Those manual route dependencies
are outside Terraform's resource graph. The scripts do not destroy manually
deployed workloads for you.

If a reservation was deleted outside Terraform, cleanup may stop because it
cannot verify that resource. First check read permissions and confirm the
deletion, then reconcile the state using a reviewed Terraform refresh-only
plan before retrying. Keep `.work/`; a failed lookup is not proof of deletion.

For automation, `./cleanup.sh destroy --yes` and `./cleanup.sh all --yes` skip
the confirmation after the destroy plan is generated and displayed.

### Supported network scope

The copy includes the selected IPv4 VCN, regional IPv4 subnets, route tables,
security lists, network security groups and their rules, DHCP options, and
supported internet, NAT, and service gateways. CIDRs are preserved. Private IPs
are reserved only for supported referenced route targets when
`PREPARE_NETWORK_ONLY=true`; other workload IP allocations are not copied.

Compute instances, storage, public IP allocations, DRGs, peering,
VPN/FastConnect, and custom DNS resources are outside this workflow. IPv6,
availability-domain-specific subnets, and other unsupported or missing network
references still fail preparation. Resources in other compartments are not
automatically included.

The supported subset also excludes multi-CIDR subnets, ZPR networks, blocked
service gateways, and nonempty gateway ingress routing. Preparation mode allows
an internet gateway's empty ingress route-table association and maps it to the
copied table. Other gateway ingress configurations remain unsupported.

With `PREPARE_NETWORK_ONLY=false` or omitted, strict copy validation remains in
effect: routes through DRGs, peering gateways, or workload private IPs fail
preparation, as do gateway ingress route-table associations. No private-IP
reservations or manual routing exceptions are introduced in that mode.

With `PREPARE_NETWORK_ONLY=true`, DRG attachments and their routes are excluded,
all private-IP routes are deferred, and unsupported VLAN private-IP targets are
marked blocked in the handoff while the supported subnet network is prepared.
Other unsupported configurations still fail. Service gateways support the
regional **All Services** and **Object Storage** service categories. Defined
tags are omitted; freeform tags are retained. NAT gateways receive new public
IPs. Custom DHCP DNS-server addresses are preserved; provide reachable DNS
servers at those addresses before using such subnets.

Matching CIDRs require an address plan before connecting the source and
destination VCNs. This workflow does not configure connectivity between them or
adopt an existing destination network.

Generated snapshots, configuration, plans, state, and logs contain infrastructure
details and belong outside the shared repository. Review `git status` before
publishing changes. Never delete state to start over while resources still exist.

### Tests

```bash
python3 -m unittest discover -s tests -v
bash -n export.sh review.sh run.sh cleanup.sh lib/common.sh
```

The tests use synthetic inventory and mocked cloud commands. Export, review,
Terraform validation, and planning were also checked against a live IPv4 VCN,
and the API-key workflow has been manually tested. Review the plan before
applying it in your environment. The new preparation and manual-routing mode
has not yet been tested end to end against live OCI.
