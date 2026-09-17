"""Offline Cloud Shell authentication tests; never contact OCI or IMDS."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_network_config import example_inventory


LIB = Path(__file__).resolve().parents[1] / 'lib'
with patch.object(sys, 'path', [str(LIB), *sys.path]):
    spec = importlib.util.spec_from_file_location('cloud_shell_workflow_tests', LIB / 'workflow.py')
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)


class CloudShellTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.token = self.root / 'delegation_token'
        self.token.write_text('synthetic-test-token-not-a-credential')
        self.cloud_config = self.root / 'cloud-config'
        self.cloud_config.write_text(
            f'[me-dubai-1]\ntenancy=ocid1.tenancy.oc1..fixture\n'
            f'delegation_token_file={self.token}\n'
            f'[me-abudhabi-1]\ntenancy=ocid1.tenancy.oc1..fixture\n'
            f'delegation_token_file={self.token}\n')
        default_config = self.root / '.oci/config'
        default_config.parent.mkdir()
        default_config.write_text('[DEFAULT]\ntenancy=ocid1.tenancy.oc1..fixture\n')
        self.environment = {
            'SOURCE_REGION': 'me-dubai-1', 'DESTINATION_REGION': 'me-abudhabi-1',
            'SOURCE_COMPARTMENT_OCID': 'ocid1.compartment.oc1..fixture', 'OCI_PROFILE': '',
            'OCI_CLI_AUTH': 'instance_obo_user', 'OCI_CLI_CONFIG_FILE': str(self.cloud_config),
            'OCI_CLI_PROFILE': 'me-abudhabi-1',
        }
        self.start_patch(patch.dict(os.environ, self.environment, clear=True))
        self.start_patch(patch.object(workflow.Path, 'home', return_value=self.root))
        self.start_patch(patch.object(workflow, 'DEST', self.root / 'destination'))
        self.start_patch(patch.object(workflow, 'require_program'))
        self.run = self.start_patch(patch.object(workflow.subprocess, 'run', side_effect=AssertionError('Unexpected external call')))
        self.popen = self.start_patch(patch.object(workflow.subprocess, 'Popen', side_effect=AssertionError('Unexpected external call')))

    def start_patch(self, context):
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def test_cloud_profile_prefers_source_region_and_remains_stable(self):
        before = self.cloud_config.read_bytes()
        config = workflow.settings()
        self.assertEqual(config['auth_type'], 'instance_obo_user')
        self.assertEqual(config['profile'], 'me-dubai-1')
        self.assertEqual(config['config_file'], str(self.cloud_config))
        self.assertEqual(config['delegation_token_file'], str(self.token))
        os.environ['OCI_CLI_PROFILE'] = 'me-dubai-1'
        self.assertEqual(workflow.settings(), config)
        self.assertEqual(self.cloud_config.read_bytes(), before)
        self.assertNotIn('synthetic-test-token', json.dumps(config))

    def test_cloud_profile_uses_active_profile_when_source_profile_absent(self):
        self.cloud_config.write_text(
            f'[me-abudhabi-1]\ntenancy=ocid1.tenancy.oc1..fixture\n'
            f'delegation_token_file={self.token}\n')
        self.assertEqual(workflow.settings()['profile'], 'me-abudhabi-1')

    def test_explicit_profile_and_delegation_path_override_are_respected(self):
        override = self.root / 'override-token'
        override.write_text('synthetic-override')
        os.environ['OCI_PROFILE'] = 'me-abudhabi-1'
        os.environ['OCI_CLI_DELEGATION_TOKEN_FILE'] = str(override)
        config = workflow.settings()
        self.assertEqual(config['profile'], 'me-abudhabi-1')
        self.assertEqual(config['delegation_token_file'], str(override))

    def test_cloud_config_and_token_symlink_rotation_preserves_binding_paths(self):
        config_link = self.root / 'stable-config'
        token_link = self.root / 'stable-token'
        config_link.symlink_to(self.cloud_config)
        token_link.symlink_to(self.token)
        os.environ['OCI_CLI_CONFIG_FILE'] = str(config_link)
        os.environ['OCI_CLI_DELEGATION_TOKEN_FILE'] = str(token_link)
        before = workflow.settings()
        new_config = self.root / 'new-session-config'
        new_config.write_bytes(self.cloud_config.read_bytes())
        new_token = self.root / 'new-session-token'
        new_token.write_text('synthetic-renewed-token')
        config_link.unlink()
        config_link.symlink_to(new_config)
        token_link.unlink()
        token_link.symlink_to(new_token)
        self.assertEqual(workflow.settings(), before)
        self.assertEqual(before['config_file'], str(config_link))
        self.assertEqual(before['delegation_token_file'], str(token_link))

    def test_missing_empty_or_unreadable_token_fails_before_external_calls(self):
        for value in ('', str(self.root / 'missing-token')):
            with self.subTest(value=value):
                self.cloud_config.write_text(
                    '[me-dubai-1]\ntenancy=ocid1.tenancy.oc1..fixture\n'
                    f'delegation_token_file={value}\n')
                with self.assertRaisesRegex(ValueError, 'delegation.*file'):
                    workflow.settings()
        self.cloud_config.write_text(
            '[me-dubai-1]\ntenancy=ocid1.tenancy.oc1..fixture\n'
            f'delegation_token_file={self.token}\n')
        with patch.object(workflow.os, 'access', return_value=False):
            with self.assertRaisesRegex(ValueError, 'token file is unavailable'):
                workflow.settings()
        self.token.write_text('')
        with self.assertRaisesRegex(ValueError, 'token file is unavailable'):
            workflow.settings()
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_cloud_cli_uses_explicit_obo_auth_config_profile_and_region(self):
        config = workflow.settings()
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout='{"data": []}', stderr='')
        self.assertEqual(workflow.oci(config, 'me-abudhabi-1', 'network', 'vcn', 'list'), [])
        command = self.run.call_args.args[0]
        for option, value in [('--auth', 'instance_obo_user'), ('--config-file', str(self.cloud_config)),
                              ('--profile', 'me-dubai-1'), ('--region', 'me-abudhabi-1')]:
            self.assertEqual(command[command.index(option) + 1], value)

    def test_builder_uses_native_auth_without_token_contents_or_cloud_profile(self):
        destination = workflow.destination_settings(workflow.settings())
        generated = workflow.build_config(example_inventory(), destination)
        self.assertEqual(generated['provider']['oci'], {
            'auth': 'InstancePrincipal', 'region': 'me-abudhabi-1', 'config_file_profile': '',
        })
        serialized = json.dumps(generated)
        self.assertNotIn(str(self.token), serialized)
        self.assertNotIn('synthetic-test-token', serialized)
        self.assertNotIn(str(self.cloud_config), serialized)

    def test_terraform_receives_only_verified_token_path_environment(self):
        config = workflow.settings()
        os.environ['TF_VAR_use_obo_token'] = 'false'
        os.environ['TF_VAR_obo_token_path'] = '/wrong/path'
        os.environ['TF_VAR_obo_token'] = 'unwanted-inline-token'
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout='{}', stderr='')
        workflow.tf(config, 'show', '-json', capture=True)
        environment = self.run.call_args.kwargs['env']
        self.assertEqual(environment['TF_VAR_use_obo_token'], 'true')
        self.assertEqual(environment['TF_VAR_obo_token_path'], str(self.token))
        self.assertNotIn('TF_VAR_obo_token', environment)
        self.assertNotIn('synthetic-test-token', json.dumps(environment))
        self.assertEqual(os.environ['TF_VAR_use_obo_token'], 'false')
        self.popen.assert_not_called()

    def test_api_key_settings_and_builder_remain_compatible(self):
        os.environ.pop('OCI_CLI_AUTH')
        config = workflow.settings()
        self.assertEqual(config, {
            'source_region': 'me-dubai-1', 'destination_region': 'me-abudhabi-1',
            'source_compartment_id': 'ocid1.compartment.oc1..fixture',
            'destination_compartment_id': 'ocid1.compartment.oc1..fixture',
            'source_vcn_id': '', 'vcn_name': '', 'profile': 'DEFAULT',
            'config_file': str(self.root / '.oci/config'),
            'tenancy_id': 'ocid1.tenancy.oc1..fixture', 'provider_version': '9.2.0',
        })
        destination = workflow.destination_settings(config)
        self.assertNotIn('auth_type', destination)
        self.assertNotIn('delegation_token_file', destination)
        generated = workflow.build_config(example_inventory(), destination)
        self.assertEqual(generated['provider']['oci'], {
            'region': 'me-abudhabi-1', 'config_file_profile': 'DEFAULT',
        })


if __name__ == '__main__':
    unittest.main()
