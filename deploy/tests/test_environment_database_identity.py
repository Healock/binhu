import copy
import unittest
from deploy.environments.database_identity import INNER, validate_resources


class IdentityResourceTests(unittest.TestCase):
    def setUp(self):
        self.project = 'binhu-staging'
        self.network = self.project + '_internal'
        def container(role):
            return {'Id': role, 'Config': {'Labels': {
                'com.docker.compose.project': self.project,
                'com.docker.compose.service': role, 'binhu.environment': 'staging'}},
                'State': {'Running': True}, 'HostConfig': {},
                'NetworkSettings': {'Networks': {self.network: {}}}, 'Mounts': []}
        self.backend = container('backend')
        self.mysql = container('environment-mysql')
        self.mysql['Mounts'] = [{'Destination': '/var/lib/mysql', 'Type': 'volume',
            'Name': self.project + '_mysql', 'Source': '/isolated/staging/mysql'}]
        self.net = {'Containers': {'mysql': {'Name': self.project + '-environment-mysql-1'}}}
        self.all = [self.backend, self.mysql]

    def check(self):
        validate_resources('staging', self.backend, self.mysql, self.net, self.all)

    def test_isolated_resources_accepted(self):
        self.check()

    def test_shared_stopped_database_volume_rejected(self):
        self.all.append({'Id': 'production-stopped', 'Mounts': copy.deepcopy(self.mysql['Mounts'])})
        with self.assertRaisesRegex(ValueError, 'shared'):
            self.check()

    def test_bind_mount_rejected(self):
        self.mysql['Mounts'][0]['Type'] = 'bind'
        with self.assertRaisesRegex(ValueError, 'storage'):
            self.check()

    def test_foreign_network_rejected(self):
        self.backend['NetworkSettings']['Networks']['production'] = {}
        with self.assertRaisesRegex(ValueError, 'network'):
            self.check()

    def test_foreign_member_rejected(self):
        self.net['Containers']['other'] = {'Name': 'production-backend'}
        with self.assertRaisesRegex(ValueError, 'foreign'):
            self.check()

    def test_wrong_label_rejected(self):
        self.mysql['Config']['Labels']['binhu.environment'] = 'development'
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.check()

    def test_privileged_container_rejected(self):
        self.backend['HostConfig']['Privileged'] = True
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            self.check()

    def test_production_target_rejected(self):
        with self.assertRaises(ValueError):
            validate_resources('production', self.backend, self.mysql, self.net, self.all)

    def test_embedded_probe_compiles(self):
        for environment in ('staging', 'development'):
            for apply in (True, False):
                compile(INNER.replace('ENVIRONMENT', repr(environment), 1).replace('APPLY', repr(apply), 1), 'identity-probe', 'exec')


if __name__ == '__main__':
    unittest.main()
