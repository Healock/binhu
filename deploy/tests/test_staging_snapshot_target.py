import unittest
from types import SimpleNamespace
from deploy.environments.staging_data.target import database_names, target_settings, qualified, DOMAINS, KEYS
from deploy.environments.staging_data.codec import SnapshotError


class TargetTests(unittest.TestCase):
    def settings(self):
        result = SimpleNamespace(APP_ENVIRONMENT='staging', SESSION_COOKIE_NAME='binhu_staging_session',
            MYSQL_HOST='environment-mysql', MYSQL_PORT=3306, MYSQL_USER='environment_app',
            MYSQL_DOMAIN_DATABASES_ENABLED=True, PLATFORM_DOMAIN_ACTIVE=True,
            REGISTRY_ADDRESS_DOMAIN_ACTIVE=True, TXDOCS_ENABLED=False)
        for domain,key in zip(DOMAINS,KEYS):
            setattr(result,'MYSQL_'+key+'_DB','Staging_'+domain)
        return result

    def test_eight_candidate_databases_are_separate_from_current(self):
        current, candidate = target_settings(self.settings(),'staging-'+'a'*16)
        self.assertEqual(len(set(candidate.values())),8)
        self.assertFalse(set(current.values()) & set(candidate.values()))
        self.assertTrue(all(len(name)<=64 for name in candidate.values()))

    def test_production_development_and_live_candidate_are_rejected(self):
        for field, value in [('APP_ENVIRONMENT','production'), ('APP_ENVIRONMENT','development'),
                              ('MYSQL_HOST','mysql'), ('MYSQL_ONLINE_DATA_DB','OnlineData'),
                              ('SESSION_COOKIE_NAME','binhu_session'), ('TXDOCS_ENABLED',True)]:
            settings = self.settings()
            setattr(settings,field,value)
            with self.assertRaises(SnapshotError):
                target_settings(settings,'staging-'+'a'*16)
        settings = self.settings()
        settings.MYSQL_ONLINE_DATA_DB = database_names('staging-'+'a'*16)['OnlineData']
        with self.assertRaises(SnapshotError):
            target_settings(settings,'staging-'+'a'*16)

    def test_qualified_identifiers_reject_injection_and_wrong_names(self):
        candidate = database_names('staging-'+'a'*16)
        self.assertEqual(qualified(candidate,'OnlineData.t_fullchain'),
                         '`Staging_saaaaaaaaaaaaaaaa_OnlineData`.`t_fullchain`')
        for name in ['mysql.user', 'OnlineData.t;DROP', 'OnlineData.t`', '../OnlineData.t']:
            with self.assertRaises(SnapshotError):
                qualified(candidate,name)


if __name__ == '__main__':unittest.main()
