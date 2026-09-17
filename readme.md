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
```

Leave `OCI_PROFILE` blank. Leave `DESTINATION_COMPARTMENT_OCID` blank to use the
same compartment. If the source compartment has multiple VCNs, also set
`SOURCE_VCN_OCID`. Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

Export, review, and preview the deployment:

```bash
./export.sh
./review.sh
./run.sh plan
```

Check the CIDRs and resource counts. A fresh plan should show resources to add,
with **0 changed and 0 destroyed**. Then deploy and verify:

```bash
./run.sh apply
./run.sh check
```

`apply` runs without another confirmation. `check` should report **No changes**.
Select the destination region and compartment in the OCI Console to see the
copied VCN. Keep `.env` unchanged and preserve `.work/` while the network exists.

When you want to delete the copied network and generated local files:

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
compartments.

`review.sh` summarizes the exported inventory and writes `.work/review.txt`.
Review the VCN, subnet CIDRs, security rules, route tables, and gateway
dependencies in `.work/export.json` before planning. Review also checks whether
the snapshot is supported, but does not compare it with live OCI or prove
connectivity. It does not change OCI resources.

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
those settings unchanged until cleanup; to use different settings, destroy and
clean the existing workspace first, or use a separate checkout. Generated
Terraform files are not intended for manual editing.

`check` compares the managed destination network with the configuration. It exits
with `0` when no changes are needed, `2` when differences exist, and `1` on error.

Terraform state stays in `.work/destination/`, including after a failed or
partially completed apply. Keep that directory to manage or destroy the created
resources. Use a separate checkout for another independently managed copy.

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

For automation, `./cleanup.sh destroy --yes` and `./cleanup.sh all --yes` skip
the confirmation after the destroy plan is generated and displayed.

### Supported network scope

The copy includes the selected IPv4 VCN, regional IPv4 subnets, route tables,
security lists, network security groups and their rules, DHCP options, and
supported internet, NAT, and service gateways. CIDRs are preserved. Private IPs
can be assigned when destination workloads are created; workload IP allocations
are not copied or reserved by these scripts.

Compute instances, storage, public/private IP allocations, DRGs, peering,
VPN/FastConnect, and custom DNS resources are outside this workflow. IPv6,
availability-domain-specific subnets, and unsupported or missing network
references cause an explicit failure rather than an incomplete configuration.
Resources in other compartments are not automatically included.

The supported subset also excludes multi-CIDR subnets, ZPR networks, blocked
service gateways, and gateway ingress routing. Routes through a DRG, peering
gateway, or workload private IP fail preparation. Service gateways support the
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
applying it in your environment.
