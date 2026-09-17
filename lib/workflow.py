#!/usr/bin/env python3
"""CLI inventory and a local-state Terraform lifecycle for one VCN copy."""
from __future__ import annotations

import argparse
import configparser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from network_config import build_config

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / '.work'
DEST = WORK / 'destination'
INVENTORY = WORK / 'export.json'
BINDING = WORK / 'deployment.json'


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot read {path}: {exc}') from exc


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_program(name):
    if not shutil.which(name):
        raise ValueError(f'{name} is required; see readme.md for prerequisites.')


def settings():
    required = ('SOURCE_REGION', 'DESTINATION_REGION', 'SOURCE_COMPARTMENT_OCID')
    for key in required:
        if not os.environ.get(key, '').strip() or 'REPLACE' in os.environ[key]:
            raise ValueError(f'Set {key} in .env (copy .env.example first).')
    source = os.environ['SOURCE_REGION'].strip()
    destination = os.environ['DESTINATION_REGION'].strip()
    if source == destination:
        raise ValueError('SOURCE_REGION and DESTINATION_REGION must be different.')
    for region in (source, destination):
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)+', region):
            raise ValueError(f'Invalid region identifier: {region}')
    compartment = os.environ['SOURCE_COMPARTMENT_OCID'].strip()
    destination_compartment = os.environ.get('DESTINATION_COMPARTMENT_OCID', '').strip() or compartment
    for cid in (compartment, destination_compartment):
        if not cid.startswith(('ocid1.compartment.', 'ocid1.tenancy.')):
            raise ValueError('Compartment settings must contain compartment or tenancy OCIDs.')
    version = os.environ.get('OCI_PROVIDER_VERSION', '9.2.0').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('OCI_PROVIDER_VERSION must be an exact version, e.g. 9.2.0.')
    profile = os.environ.get('OCI_PROFILE', 'DEFAULT').strip() or 'DEFAULT'
    config_file = Path.home() / '.oci' / 'config'
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_file)
    if profile not in parser or not parser[profile].get('tenancy'):
        raise ValueError(f'Configure API-key profile {profile!r} in {config_file}.')
    return {
        'source_region': source, 'destination_region': destination,
        'source_compartment_id': compartment,
        'destination_compartment_id': destination_compartment,
        'source_vcn_id': os.environ.get('SOURCE_VCN_OCID', '').strip(),
        'vcn_name': os.environ.get('DESTINATION_VCN_NAME', '').strip(),
        'profile': profile, 'config_file': str(config_file.resolve()),
        'tenancy_id': parser[profile]['tenancy'], 'provider_version': version,
    }


def destination_settings(config):
    return {'region': config['destination_region'],
            'compartment_id': config['destination_compartment_id'],
            'profile': config['profile'], 'config_file': config['config_file'],
            'provider_version': config['provider_version'], 'vcn_name': config['vcn_name']}


def check_paths():
    if WORK.is_symlink() or DEST.is_symlink():
        raise ValueError('Refusing a symlinked .work or destination directory.')
    WORK.mkdir(mode=0o700, exist_ok=True)


def oci(config, region, *args):
    require_program('oci')
    cmd = ['oci', '--config-file', config['config_file'], '--profile', config['profile'],
           '--auth', 'api_key', '--region', region, '--output', 'json', '--no-retry', *args]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}'
        raise ValueError(f'OCI command failed ({" ".join(args[:3])}):\n{detail}')
    # OCI CLI's paginated list commands can print nothing when there are no items.
    if not result.stdout.strip() and 'list' in args:
        return []
    try:
        return json.loads(result.stdout)['data']
    except (ValueError, KeyError) as exc:
        raise ValueError(f'OCI CLI returned an unexpected response for {" ".join(args[:4])}.') from exc


def list_network(config, region, group, compartment, vcn=None):
    args = ['network', group, 'list', '--compartment-id', compartment, '--all']
    if vcn:
        args += ['--vcn-id', vcn]
    return oci(config, region, *args)


def state_has_resources(path):
    state = read_json(path)
    if not isinstance(state, dict) or 'resources' not in state:
        raise ValueError(f'Unrecognized Terraform state: {path}; refusing local removal.')
    return any(r.get('mode') == 'managed' and r.get('instances') for r in state['resources'])


def assert_local_cleanup_safe():
    # Examine current states; historical backups may contain already-destroyed resources.
    for path in WORK.rglob('terraform.tfstate'):
        if '.terraform' in path.relative_to(WORK).parts:
            continue
        if state_has_resources(path):
            raise ValueError('Managed resources still exist in state. Run ./cleanup.sh destroy first.')
    current = DEST / 'terraform.tfstate'
    if not current.exists():
        if BINDING.exists() and read_json(BINDING).get('may_have_resources'):
            raise ValueError('Deployment state is missing; restore it before local cleanup.')
        for backup in WORK.rglob('*.tfstate.backup'):
            if state_has_resources(backup):
                raise ValueError('Only a state backup remains with managed resources; restore it before cleanup.')


def export_network(config):
    check_paths()
    if DEST.exists() or BINDING.exists():
        raise ValueError('A destination workspace exists. Use cleanup.sh before starting a fresh export.')
    region, compartment = config['source_region'], config['source_compartment_id']
    vcns = list_network(config, region, 'vcn', compartment)
    vcns = [v for v in vcns if v.get('lifecycle-state') == 'AVAILABLE']
    if config['source_vcn_id']:
        vcns = [v for v in vcns if v['id'] == config['source_vcn_id']]
    if len(vcns) != 1:
        raise ValueError(f'Found {len(vcns)} matching available VCNs. Set SOURCE_VCN_OCID to select exactly one.')
    vcn = vcns[0]
    print(f'Exporting {vcn["display-name"]} from {region} (read-only).', flush=True)
    inventory = {'schema_version': 1, 'source_region': region, 'compartment_id': compartment,
                 'tenancy_id': config['tenancy_id'], 'vcn': vcn,
                 'exported_at': datetime.now(timezone.utc).isoformat()}
    groups = {
        'subnets': 'subnet', 'route_tables': 'route-table', 'security_lists': 'security-list',
        'dhcp_options': 'dhcp-options', 'internet_gateways': 'internet-gateway',
        'nat_gateways': 'nat-gateway', 'service_gateways': 'service-gateway',
        'network_security_groups': 'nsg',
    }
    for key, group in groups.items():
        items = list_network(config, region, group, compartment, vcn['id'])
        inventory[key] = [x for x in items if x.get('lifecycle-state') not in ('TERMINATING', 'TERMINATED')]
        print(f'  {key}: {len(inventory[key])}', flush=True)
    for group in inventory['network_security_groups']:
        group['security-rules'] = oci(config, region, 'network', 'nsg', 'rules', 'list',
                                      '--nsg-id', group['id'], '--all')
    inventory['services'] = oci(config, region, 'network', 'service', 'list', '--all')
    inventory['unsupported_resources'] = {
        'local_peering_gateways': list_network(config, region, 'local-peering-gateway', compartment, vcn['id']),
        'drg_attachments': list_network(config, region, 'drg-attachment', compartment, vcn['id']),
    }
    write_json(INVENTORY, inventory)
    print(f'Export saved: {INVENTORY}\nNext: ./review.sh', flush=True)


def load_inventory(config):
    if not INVENTORY.exists():
        raise ValueError('Run ./export.sh first.')
    inventory = read_json(INVENTORY)
    pairs = (('source_region', 'source_region'), ('compartment_id', 'source_compartment_id'), ('tenancy_id', 'tenancy_id'))
    for field, setting in pairs:
        if inventory.get(field) != config[setting]:
            raise ValueError(f'Export does not match current {setting}; export again after cleanup.')
    if config['source_vcn_id'] and config['source_vcn_id'] != inventory['vcn']['id']:
        raise ValueError('Export does not match SOURCE_VCN_OCID.')
    return inventory


def review(config):
    inventory = load_inventory(config)
    generated = build_config(inventory, destination_settings(config))
    counts = {kind: len(items) for kind, items in generated['resource'].items()}
    lines = [f'Source: {config["source_region"]} / {inventory["vcn"]["display-name"]}',
             f'Destination: {config["destination_region"]} / {config["vcn_name"] or inventory["vcn"]["display-name"]}',
             f'Exported at: {inventory["exported_at"]}',
             f'VCN CIDRs: {inventory["vcn"].get("cidr-blocks") or [inventory["vcn"]["cidr-block"]]}',
             f'Prepared Terraform resource blocks: {sum(counts.values())}']
    lines += [f'  {kind}: {count}' for kind, count in sorted(counts.items())]
    lines += [f'  Subnet {s["display-name"]}: {s["cidr-block"]}' for s in inventory['subnets']]
    lines += ['CIDRs and security rules are retained. VM private IPs are assigned later.',
              'Matching CIDRs prevent direct peering of source and destination VCNs.',
              'No workload instances, storage, existing public/private IP allocations or private DNS resources are copied.',
              'Preparation checks passed. Next: ./run.sh plan (OCI validates destination settings).']
    report = '\n'.join(lines) + '\n'
    (WORK / 'review.txt').write_text(report)
    print(report, end='')


def tf(config, *args, log=None, capture=False, allowed=(0,)):
    require_program('terraform')
    if os.environ.get('TF_WORKSPACE', 'default') != 'default' or os.environ.get('TF_DATA_DIR'):
        raise ValueError('This project requires the default workspace and local Terraform data directory.')
    workspace_file = DEST / '.terraform' / 'environment'
    if workspace_file.exists() and workspace_file.read_text().strip() != 'default':
        raise ValueError('Select the default Terraform workspace before continuing.')
    env = dict(os.environ, TF_IN_AUTOMATION='1', OCI_CONFIG_FILE=config['config_file'])
    for key in list(env):
        if key.startswith('TF_CLI_ARGS'):
            del env[key]
    cmd = ['terraform', f'-chdir={DEST}', *args]
    if capture:
        result = subprocess.run(cmd, text=True, capture_output=True, env=env)
        if result.returncode not in allowed:
            raise ValueError(result.stderr.strip() or 'Terraform command failed.')
        return result.stdout
    with (open(log, 'w') if log else open(os.devnull, 'w')) as logfile:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        for line in proc.stdout:
            print(line, end='', flush=True)
            logfile.write(line)
        code = proc.wait()
    if code not in allowed:
        raise ValueError(f'Terraform {args[0]} failed (exit {code}). Keep .work/ for recovery.')
    return code


def verify_binding(config):
    binding = read_json(BINDING)
    if binding.get('may_have_resources') and not (DEST / 'terraform.tfstate').exists():
        raise ValueError('Deployment state is missing; restore it before planning, applying, or cleanup.')
    if binding['settings'] != config:
        raise ValueError('.env/profile tenancy differs from this deployment. Restore its settings before continuing.')
    if binding['inventory_sha256'] != digest(INVENTORY):
        raise ValueError('Source export changed after preparation; restore the original export.')
    if binding['configuration_sha256'] != digest(DEST / 'main.tf.json'):
        raise ValueError('Generated configuration changed after preparation; restore it before continuing.')
    extras = list(DEST.glob('*.tf')) + [p for p in DEST.glob('*.tf.json') if p.name != 'main.tf.json']
    if extras:
        raise ValueError('Unexpected Terraform configuration files in destination directory.')
    return binding


def prepare(config):
    check_paths()
    if BINDING.exists():
        verify_binding(config)
        return
    if DEST.exists():
        raise ValueError('Unrecognized destination workspace; inspect it and use cleanup.sh local if empty.')
    inventory = load_inventory(config)
    generated = build_config(inventory, destination_settings(config))
    name = config['vcn_name'] or inventory['vcn']['display-name']
    existing = list_network(config, config['destination_region'], 'vcn', config['destination_compartment_id'])
    if any(v.get('display-name') == name and v.get('lifecycle-state') != 'TERMINATED' for v in existing):
        raise ValueError(f'Destination already has VCN {name!r}. Set DESTINATION_VCN_NAME or restore its state; it will not be adopted.')
    DEST.mkdir(mode=0o700)
    write_json(DEST / 'main.tf.json', generated)
    write_json(BINDING, {'settings': config, 'inventory_sha256': digest(INVENTORY),
                        'configuration_sha256': digest(DEST / 'main.tf.json'), 'may_have_resources': False})


def plan(config):
    prepare(config)
    (DEST / 'plan-meta.json').unlink(missing_ok=True)
    tf(config, 'init', '-input=false', '-no-color')
    tf(config, 'validate', '-no-color')
    tf(config, 'plan', '-input=false', '-no-color', '-out=deploy.tfplan', log=DEST / 'plan.log')
    data = json.loads(tf(config, 'show', '-json', 'deploy.tfplan', capture=True))
    write_json(DEST / 'plan.json', data)
    for change in data.get('resource_changes', []):
        if change.get('mode') == 'managed' and change['change']['actions'] not in (['create'], ['no-op'], ['update']):
            raise ValueError('Plan includes deletion/replacement; inspect it and use explicit cleanup instead.')
    write_json(DEST / 'plan-meta.json', {'binding': read_json(BINDING), 'plan_sha256': digest(DEST / 'deploy.tfplan')})
    print('Plan saved. Review .work/destination/plan.log, then run ./run.sh apply.')


def apply(config):
    binding = verify_binding(config)
    meta = read_json(DEST / 'plan-meta.json')
    if meta['binding'] != binding or meta['plan_sha256'] != digest(DEST / 'deploy.tfplan'):
        raise ValueError('Saved plan does not match this deployment. Run ./run.sh plan again.')
    binding['may_have_resources'] = True
    write_json(BINDING, binding)
    # Keep recovery marker and state even after an interrupted or partially failed apply.
    (DEST / 'plan-meta.json').unlink()
    tf(config, 'apply', '-input=false', '-no-color', 'deploy.tfplan', log=DEST / 'apply.log')
    outputs = json.loads(tf(config, 'output', '-json', capture=True))
    write_json(DEST / 'outputs.json', outputs)


def destroy(config, yes=False):
    check_paths()
    if not BINDING.exists():
        assert_local_cleanup_safe()
        print('No tracked destination deployment to destroy.')
        return
    binding = verify_binding(config)
    state = DEST / 'terraform.tfstate'
    if not state.exists():
        if binding.get('may_have_resources'):
            raise ValueError('State is missing for a started deployment. Restore it; cleanup cannot recover resources automatically.')
        print('No deployment was applied.')
        return
    if not state_has_resources(state):
        print('No managed resources remain in destination state.')
        return
    tf(config, 'init', '-input=false', '-no-color')
    tf(config, 'plan', '-destroy', '-input=false', '-no-color', '-out=destroy.tfplan', log=DEST / 'destroy-plan.log')
    data = json.loads(tf(config, 'show', '-json', 'destroy.tfplan', capture=True))
    changes = [r for r in data.get('resource_changes', []) if r.get('mode') == 'managed']
    if any(r['change']['actions'] not in (['delete'], ['no-op']) for r in changes):
        raise ValueError('Unexpected actions in destroy plan.')
    print(f'Destroy only tracked destination resources in {config["destination_region"]}.')
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError('Noninteractive destruction requires --yes.')
        if input('Type yes to apply the destroy plan: ').strip() != 'yes':
            raise ValueError('Destruction cancelled; state and files retained.')
    tf(config, 'apply', '-input=false', '-no-color', 'destroy.tfplan', log=DEST / 'destroy.log')
    if state_has_resources(state):
        raise ValueError('Managed resources remain; preserving state.')
    binding['may_have_resources'] = False
    write_json(BINDING, binding)
    (DEST / 'plan-meta.json').unlink(missing_ok=True)


def local_cleanup():
    if not WORK.exists():
        print('No generated files to remove.')
        return
    check_paths()
    assert_local_cleanup_safe()
    shutil.rmtree(WORK)
    print('Removed .work/. Scripts, .env and credentials are unchanged.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('export', help='Read the selected source VCN into .work/export.json')
    subs.add_parser('review', help='Review local inventory and preparation checks')
    run = subs.add_parser('run', help='Prepare, plan, apply, or check the destination')
    run.add_argument('action', nargs='?', choices=('plan', 'apply', 'check'), default='plan')
    cleanup = subs.add_parser('cleanup', help='Destroy tracked destination resources and/or remove generated files')
    cleanup.add_argument('action', choices=('destroy', 'local', 'all'))
    cleanup.add_argument('--yes', action='store_true', help='Apply the destroy plan without a prompt')
    args = parser.parse_args()
    if args.command == 'cleanup' and args.action == 'local':
        local_cleanup()
        return 0
    config = settings()
    if args.command == 'export':
        export_network(config)
    elif args.command == 'review':
        review(config)
    elif args.command == 'run':
        if args.action == 'plan':
            plan(config)
        elif args.action == 'apply':
            apply(config)
        else:
            verify_binding(config)
            return tf(config, 'plan', '-input=false', '-no-color', '-detailed-exitcode',
                      log=DEST / 'check.log', allowed=(0, 2))
    else:
        destroy(config, args.yes)
        if args.action == 'all':
            local_cleanup()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nInterrupted; keep .work/ and its state for recovery.', file=sys.stderr)
        sys.exit(130)
