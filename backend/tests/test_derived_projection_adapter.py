import unittest
from unittest.mock import AsyncMock

from services.derived_projection_adapter import selected_engine, read_derived_projection


class DerivedProjectionAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_flink_is_restricted_to_isolated_environments(self):
        self.assertEqual(
            selected_engine("development", environ={"ONLINE_SOURCE_PROJECTION_ENGINE": "flink"}),
            "flink",
        )
        with self.assertRaises(ValueError):
            selected_engine("production", environ={"ONLINE_SOURCE_PROJECTION_ENGINE": "flink"})

    async def test_read_is_parameterized_and_does_not_expose_source_values(self):
        cursor = AsyncMock()
        cursor.fetchone.return_value = (
            "dev-online-source-1", "t_fullchain:42", 42, 7, 2, 4, 3, 7, 4, 7, "task.saved",
        )
        result = await read_derived_projection(
            cursor, environment="development", run_id="dev-online-source-1",
            task_id="t_fullchain:42", source_id=42, engine="flink",
        )
        self.assertEqual(result["engine"], "flink")
        self.assertNotIn("address", result)
        query, params = cursor.execute.await_args.args
        self.assertIn("dev_online_source_projection", query)
        self.assertEqual(params, ("dev-online-source-1", "t_fullchain:42", 42))

    async def test_read_rejects_untrusted_database_identifier(self):
        with self.assertRaisesRegex(ValueError, "database"):
            await read_derived_projection(
                AsyncMock(), environment="development", run_id="dev-online-source-1",
                task_id="t_fullchain:42", source_id=42, engine="flink",
                database="bad-name;drop",
            )


if __name__ == "__main__":
    unittest.main()
