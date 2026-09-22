"""Lifecycle tests for read-only discovery and manual routing handoff."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_network_config import example_inventory

LIB = Path(__file__).resolve().parents[1] / 'lib'
with patch.object(sys, 'path', [str(LIB), *sys.path]):
    spec = importlib.util.spec_from_file_location('preparation_workflow_tests', LIB / 'workflow.py')
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)


class PreparationWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / '.work'
        self.work.mkdir()
        self.dest = self.work / 'destination'
        self.snapshot = self.work / 'export.json'
        self.binding = self.work / 'deployment.json'
        self.start_patch(patch.multiple(workflow, WORK=self.work, DEST=self.dest,
                                       INVENTORY=self.snapshot, BINDING=self.binding))
        self.start_patch(patch.object(workflow.subprocess, 'run', side_effect=AssertionError('Unexpected subprocess')))
        self.start_patch(patch.object(workflow.subprocess, 'Popen', side_effect=AssertionError('Unexpected subprocess')))
        self.start_patch(patch.object(workflow.Path, 'home', return_value=self.root))
        config_file = self.root / '.oci/config'
        config_file.parent.mkdir()
        config_file.write_text('[DEFAULT]\ntenancy=ocid1.tenancy.oc1..fixture\n')
        self.start_patch(patch.dict(os.environ, {
            'SOURCE_REGION': 'source-region', 'DESTINATION_REGION': 'destination-region',
            'SOURCE_COMPARTMENT_OCID': 'ocid1.compartment.oc1..sourcefixture',
            'PREPARE_NETWORK_ONLY': 'true',
        }, clear=True))
        self.config = workflow.settings()
        self.inventory = example_inventory()
        self.inventory.update(tenancy_id=self.config['tenancy_id'], exported_at='2026-09-18T00:00:00Z')
        self.inventory['private_ips'] = []
        self.snapshot.write_text(json.dumps(self.inventory))
        self.start_patch(redirect_stdout(io.StringIO()))

    def start_patch(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def bind(self, active=False):
        self.dest.mkdir(exist_ok=True)
        (self.dest / 'main.tf.json').write_text('{}')
        binding = {'settings': self.config, 'inventory_sha256': workflow.digest(self.snapshot),
                   'configuration_sha256': workflow.digest(self.dest / 'main.tf.json'),
                   'may_have_resources': active, 'private_ip_mode': workflow.PRIVATE_IP_MODE}
        self.binding.write_text(json.dumps(binding))
        return binding

    def ip_state(self):
        self.dest.mkdir(exist_ok=True)
        target = 'ocid1.privateip.oc1.destination-region.fixture'
        state = {'resources': [{'mode': 'managed', 'type': 'oci_core_private_ip', 'name': 'reserved',
                               'instances': [{'attributes': {'id': target}}]}]}
        (self.dest / 'terraform.tfstate').write_text(json.dumps(state))
        return target

    def test_mode_is_opt_in_and_old_binding_shape_is_preserved(self):
        self.assertTrue(self.config['prepare_network_only'])
        self.assertTrue(workflow.destination_settings(self.config)['prepare_network_only'])
        for value in ('false', ''):
            os.environ['PREPARE_NETWORK_ONLY'] = value
            config = workflow.settings()
            self.assertNotIn('prepare_network_only', config)
            self.assertNotIn('prepare_network_only', workflow.destination_settings(config))
        os.environ['PREPARE_NETWORK_ONLY'] = 'treu'
        with self.assertRaisesRegex(ValueError, 'true or false'):
            workflow.settings()

    def test_export_discovers_each_referenced_ip_once_without_related_lookups(self):
        private_ids = ['ocid1.privateip.oc1.source-region.' + name for name in ('one', 'two', 'vlan')]
        vnic = {'id': 'ocid1.vnic.oc1.source-region.shared', 'subnet-id': self.inventory['subnets'][0]['id']}
        vlan = {'id': 'ocid1.vlan.oc1.source-region.vmware', 'vcn-id': self.inventory['vcn']['id']}
        records = {pid: {'id': pid, 'ip-address': f'10.0.0.{10+i}',
                         **({'vlan-id': vlan['id']} if i == 2 else {'vnic-id': vnic['id']})}
                   for i, pid in enumerate(private_ids)}
        table = deepcopy(self.inventory['route_tables'][0])
        table['route-rules'] = [{'network-entity-id': pid} for pid in private_ids + private_ids[:1]]
        def listing(config, region, group, compartment, vcn_id=None):
            self.assertEqual(region, 'source-region')
            return {'vcn': [self.inventory['vcn']], 'route-table': [table]}.get(group, [])
        def lookup(config, region, *args):
            self.assertEqual(region, 'source-region')
            if args[:3] == ('network', 'service', 'list'):
                return []
            self.assertEqual(args[2], 'get')
            self.assertEqual(args[1], 'private-ip')
            return records[args[-1]]
        with patch.object(workflow, 'list_network', side_effect=listing), patch.object(workflow, 'oci', side_effect=lookup) as api:
            workflow.export_network(self.config)
        result = json.loads(self.snapshot.read_text())
        self.assertEqual(len(result['private_ips']), 3)
        self.assertNotIn('route_target_vnics', result)
        self.assertNotIn('route_target_vlans', result)
        self.assertEqual(result['private_ip_lookup_errors'], [])
        self.assertEqual(sum(call.args[3] == 'private-ip' for call in api.call_args_list), 3)
        self.assertEqual(result['route_tables'][0]['route-rules'], table['route-rules'])
        self.assertFalse(self.dest.exists())

    def test_failed_optional_discovery_continues_and_preserves_all_routes(self):
        private_ids = ['ocid1.privateip.oc1.source-region.' + name for name in ('a-denied', 'b-found', 'c-offline')]
        table = deepcopy(self.inventory['route_tables'][0])
        original_rules = deepcopy(table['route-rules'])
        table['route-rules'].extend([
            {'network-entity-id': target, 'destination': f'192.0.{i}.0/24',
             'destination-type': 'CIDR_BLOCK', 'description': f'forward target {i}'}
            for i, target in enumerate(private_ids)])
        found = {'id': private_ids[1], 'ip-address': '10.0.1.152',
                 'subnet-id': self.inventory['subnets'][1]['id']}
        groups = {'vcn': [self.inventory['vcn']], 'route-table': [table, self.inventory['route_tables'][1]]}
        for key, group in (('subnets', 'subnet'), ('security_lists', 'security-list'),
                           ('dhcp_options', 'dhcp-options'), ('internet_gateways', 'internet-gateway'),
                           ('nat_gateways', 'nat-gateway'), ('service_gateways', 'service-gateway')):
            groups[group] = self.inventory[key]
        def lookup(config, region, *args):
            if args[:3] == ('network', 'service', 'list'):
                return self.inventory['services']
            self.assertEqual(args[:3], ('network', 'private-ip', 'get'))
            target = args[-1]
            if target == private_ids[0]:
                raise ValueError('OCI command failed (network private-ip get):\nServiceError:\n' + json.dumps({
                    'code': 'NotAuthorizedOrNotFound', 'status': 404,
                    'message': 'Authorization failed or requested resource not found.',
                    'request_endpoint': 'unnecessary diagnostic endpoint',
                }))
            if target == private_ids[2]:
                raise OSError('connection unavailable')
            return found
        with patch.object(workflow, 'list_network', side_effect=lambda config, region, group, *a: groups.get(group, [])), patch.object(workflow, 'oci', side_effect=lookup) as api:
            workflow.export_network(self.config)
        result = json.loads(self.snapshot.read_text())
        self.assertEqual(result['private_ips'], [found])
        errors = result['private_ip_lookup_errors']
        self.assertEqual([error['id'] for error in errors], [private_ids[0], private_ids[2]])
        self.assertEqual(errors[0]['error'], 'NotAuthorizedOrNotFound (HTTP 404): Authorization failed or requested resource not found.')
        self.assertEqual(result['route_tables'][0]['route-rules'], table['route-rules'])
        self.assertEqual([call.args[-1] for call in api.call_args_list if call.args[3] == 'private-ip'], private_ids)
        with patch.object(workflow, 'oci', side_effect=AssertionError('Review must be offline')):
            workflow.review(self.config)
        report = json.loads((self.work / 'manual-routing.json').read_text())
        self.assertEqual(len(report['pending_routes']), 3)
        self.assertEqual({row['source_target_private_ip_id'] for row in report['pending_routes']}, set(private_ids))
        self.assertEqual(sum(t['lookup_status'] == 'unavailable' for t in report['private_ip_targets']), 2)
        self.assertIn('10.0.1.152', (self.work / 'manual-routing.md').read_text())
        generated, _ = workflow.configuration(self.config, result)
        self.assertNotIn('oci_core_private_ip', generated['resource'])
        routes = next(iter(generated['resource']['oci_core_route_table'].values()))['route_rules']
        self.assertEqual(len(routes), len(original_rules))
        self.assertFalse(self.dest.exists())

    def test_missing_or_mismatched_numeric_ip_is_a_nonfatal_lookup_warning(self):
        target = 'ocid1.privateip.oc1.source-region.target'
        def listing(config, region, group, *args):
            return {'vcn': [self.inventory['vcn']], 'route-table': [{'route-rules': [
                {'network-entity-id': target}]}]}.get(group, [])
        for response in (None, {'id': 'wrong', 'ip-address': '10.0.1.152'},
                         {'id': target}, {'id': target, 'ip-address': '10.0.0152'}):
            with self.subTest(response=response):
                def lookup(config, region, *args):
                    return [] if args[1] == 'service' else response
                with patch.object(workflow, 'list_network', side_effect=listing), patch.object(workflow, 'oci', side_effect=lookup):
                    workflow.export_network(self.config)
                result = json.loads(self.snapshot.read_text())
                self.assertEqual(result['private_ips'], [])
                self.assertEqual(result['private_ip_lookup_errors'][0]['id'], target)

    def test_failed_required_network_discovery_preserves_previous_snapshot(self):
        before = self.snapshot.read_bytes()
        vcn = self.inventory['vcn']
        def listing(config, region, group, *args):
            if group == 'route-table':
                raise ValueError('route-table access denied')
            return [vcn] if group == 'vcn' else []
        with patch.object(workflow, 'list_network', side_effect=listing):
            with self.assertRaisesRegex(ValueError, 'route-table access denied'):
                workflow.export_network(self.config)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_review_writes_offline_handoff_without_destination_queries(self):
        before = self.snapshot.read_bytes()
        with patch.object(workflow, 'oci', side_effect=AssertionError('No cloud calls')):
            workflow.review(self.config)
        self.assertTrue((self.work / 'manual-routing.md').exists())
        self.assertTrue((self.work / 'manual-routing.csv').exists())
        self.assertEqual(self.snapshot.read_bytes(), before)
        self.assertFalse(self.dest.exists())

    def test_prepare_binds_mode_and_writes_configuration_and_handoff(self):
        with patch.object(workflow, 'list_network', return_value=[]) as query:
            workflow.prepare(self.config)
        query.assert_called_once()
        self.assertEqual(query.call_args.args[1], 'destination-region')
        binding = json.loads(self.binding.read_text())
        self.assertTrue(binding['settings']['prepare_network_only'])
        self.assertEqual(binding['private_ip_mode'], workflow.PRIVATE_IP_MODE)
        self.assertTrue((self.work / 'manual-routing.json').exists())
        self.assertIn('route_tables', json.loads((self.dest / 'main.tf.json').read_text())['output'])
        config = dict(self.config)
        del config['prepare_network_only']
        with self.assertRaisesRegex(ValueError, 'differs from this deployment'):
            workflow.verify_binding(config)

    def test_handoff_reads_current_terraform_outputs_not_cached_outputs(self):
        self.bind()
        stale = self.dest / 'outputs.json'
        stale.write_text('{"old":true}')
        outputs = {'route_tables': {'value': {'default': {'id': 'destination-route', 'name': 'route'}}}}
        with patch.object(workflow, 'configuration', return_value=({}, {'fixture': True})), patch.object(workflow, 'tf', return_value=json.dumps(outputs)) as tf, patch.object(workflow, 'write_handoff') as report:
            workflow.handoff(self.config)
        self.assertEqual(tf.call_args.args[1:], ('output', '-json'))
        self.assertEqual(report.call_args.kwargs['outputs'], outputs)
        self.assertEqual(stale.read_text(), '{"old":true}')

    def test_apply_refreshes_manual_handoff_from_real_outputs(self):
        binding = self.bind()
        plan = self.dest / 'deploy.tfplan'
        plan.write_bytes(b'fixture-plan')
        (self.dest / 'plan-meta.json').write_text(json.dumps({'binding': binding, 'plan_sha256': workflow.digest(plan)}))
        outputs = {'route_tables': {'value': {}}}
        with patch.object(workflow, 'tf', side_effect=[0, json.dumps(outputs)]), patch.object(workflow, 'handoff') as report:
            workflow.apply(self.config)
        report.assert_called_once_with(self.config, outputs=outputs)
        self.assertTrue(json.loads(self.binding.read_text())['may_have_resources'])
        self.assertFalse((self.dest / 'plan-meta.json').exists())

    def test_old_preparation_workspace_cannot_apply_or_generate_a_new_handoff(self):
        binding = self.bind()
        del binding['private_ip_mode']
        self.binding.write_text(json.dumps(binding))
        before = self.binding.read_bytes()
        with patch.object(workflow, 'tf') as tf, patch.object(workflow, 'write_handoff') as report:
            for operation in (workflow.prepare, workflow.apply, workflow.handoff, workflow.review):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaisesRegex(ValueError, 'earlier private-IP reservation workflow'):
                        operation(self.config)
            tf.assert_not_called()
            report.assert_not_called()
        self.assertEqual(self.binding.read_bytes(), before)
        # Old bindings remain valid for explicit cleanup using the original configuration.
        self.assertEqual(workflow.verify_binding(self.config), binding)

    def test_cleanup_refuses_attached_or_unverified_ip_before_terraform(self):
        self.bind(active=True)
        target = self.ip_state()
        for extra in ({'vnic-id': 'customer-vnic', 'ip-state': 'ASSIGNED'}, {'ip-state': 'ASSIGNED'}, {}):
            with self.subTest(extra=extra), patch.object(workflow, 'oci', return_value={'id': target, **extra}) as api, patch.object(workflow, 'tf') as tf:
                with self.assertRaisesRegex(ValueError, 'attached or unavailable'):
                    workflow.destroy(self.config, yes=True)
                tf.assert_not_called()
                self.assertEqual(api.call_args.args[1], 'destination-region')
        self.assertTrue((self.dest / 'terraform.tfstate').exists())

    def test_cleanup_accepts_verified_available_reservations(self):
        target = self.ip_state()
        with patch.object(workflow, 'oci', return_value={'id': target, 'ip-state': 'AVAILABLE', 'vnic-id': None}):
            workflow.assert_reserved_ips_unassigned(self.config)

    def test_cleanup_lookup_failure_keeps_state_and_explains_recovery(self):
        self.ip_state()
        state = self.dest / 'terraform.tfstate'
        before = state.read_bytes()
        with patch.object(workflow, 'oci', side_effect=ValueError('NotAuthorizedOrNotFound')):
            with self.assertRaisesRegex(ValueError, 'refresh-only'):
                workflow.assert_reserved_ips_unassigned(self.config)
        self.assertEqual(state.read_bytes(), before)

    def test_cleanup_checks_again_after_plan_to_catch_new_attachment(self):
        self.bind(active=True)
        target = self.ip_state()
        available = {'id': target, 'ip-state': 'AVAILABLE'}
        assigned = {'id': target, 'ip-state': 'ASSIGNED', 'vnic-id': 'customer-vnic'}
        plan = {'resource_changes': [{'mode': 'managed', 'change': {'actions': ['delete']}}]}
        with patch.object(workflow, 'oci', side_effect=[available, assigned]), patch.object(workflow, 'tf', side_effect=[0, 0, json.dumps(plan)]) as tf:
            with self.assertRaisesRegex(ValueError, 'attached or unavailable'):
                workflow.destroy(self.config, yes=True)
        self.assertNotIn('apply', [call.args[1] for call in tf.call_args_list])


if __name__ == '__main__':
    unittest.main()
