import unittest

from deploy.environments.event_pipeline.projection_definitions import (
    PROJECTIONS,
    projection_names,
    staging_schema_sql,
)
from deploy.environments.event_pipeline.flink_sql import render


class ProjectionDefinitionTests(unittest.TestCase):
    def test_every_projection_has_matching_flink_sink_and_staging_tables(self):
        environment = {
            "PIPELINE_RUN_ID": "dev-20261026-01",
            "APP_ENVIRONMENT": "development",
            "MYSQL_HOST": "dev-derived-mysql",
            "MYSQL_DATABASE": "Dev_EventPipeline",
            "MYSQL_USER": "dev_pipeline",
            "MYSQL_PASSWORD": "a" * 48,
            "REDIS_PASSWORD": "b" * 48,
            "REDIS_HOST": "dev-derived-redis",
            "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9092,kafka-2:9092,kafka-3:9092",
            "BACKEND_REDIS_URL": "redis://dev-derived-redis:6379",
        }
        sql = render(environment)
        schema = staging_schema_sql()
        for name in projection_names():
            self.assertIn(f"INSERT INTO {name}", sql)
            self.assertIn(f"CREATE TABLE {name}", sql)
        self.assertEqual(len(PROJECTIONS), 11)
        self.assertIn("CREATE TABLE dev_online_source_projection_python LIKE", schema)


if __name__ == "__main__":
    unittest.main()
