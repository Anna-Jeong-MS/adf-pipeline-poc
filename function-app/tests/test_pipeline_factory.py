import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1]))

os.environ.setdefault("AzureWebJobsStorage", "UseDevelopmentStorage=true")
os.environ.setdefault("PIPELINE_SYNC_SCHEDULE", "0 0 2 * * *")
os.environ.setdefault("PIPELINE_RUN_SCHEDULE", "0 * * * * *")

import pytest

import function_app
from function_app import TableConfig, pipeline_resource


def test_builds_table_specific_pipeline_with_dynamic_range() -> None:
    config = TableConfig(
        control_id=1,
        source_schema="GS_POC",
        source_table="GS_COST_ACTUALS",
        target_folder="gs_cost_actuals",
        pipeline_name="pl_copy_gs_cost_actuals",
        partition_option="DynamicRange",
        partition_column="COST_ID",
        partition_lower_bound=1,
        partition_upper_bound=10_000_000,
        parallel_copies=8,
    )

    resource = pipeline_resource(config)

    assert resource["properties"]["concurrency"] == 1
    copy_activity = resource["properties"]["activities"][1]
    assert copy_activity["typeProperties"]["parallelCopies"] == 8
    assert (
        copy_activity["typeProperties"]["source"]["partitionSettings"][
            "partitionColumnName"
        ]
        == "COST_ID"
    )


def test_rejects_unsafe_oracle_identifier() -> None:
    config = TableConfig(
        control_id=2,
        source_schema="GS_POC",
        source_table="GS_COST_ACTUALS; DROP TABLE X",
        target_folder="invalid",
        pipeline_name="invalid",
        partition_option="None",
        partition_column=None,
        partition_lower_bound=None,
        partition_upper_bound=None,
        parallel_copies=1,
    )

    with pytest.raises(ValueError, match="Invalid Oracle identifier"):
        pipeline_resource(config)


def test_tracking_failure_does_not_release_successful_run(monkeypatch) -> None:
    config = TableConfig(
        control_id=3,
        source_schema="GS_POC",
        source_table="GS_PROJECTS",
        target_folder="gs_projects",
        pipeline_name="pl_copy_gs_projects",
        partition_option="None",
        partition_column=None,
        partition_lower_bound=None,
        partition_upper_bound=None,
        parallel_copies=1,
    )
    tracking_calls = []

    monkeypatch.setattr(
        function_app,
        "management_request",
        lambda method, url, body: {"runId": "run-123"},
    )
    monkeypatch.setattr(
        function_app,
        "pipeline_url",
        lambda pipeline_name, action="": (
            f"https://management.example/{pipeline_name}{action}"
        ),
    )

    def fail_tracking(control_id: int, run_id: str) -> None:
        tracking_calls.append((control_id, run_id))
        raise RuntimeError("Oracle tracking unavailable")

    monkeypatch.setattr(function_app, "record_run_success", fail_tracking)
    monkeypatch.setattr(
        function_app,
        "record_run_failure",
        lambda control_id, error: pytest.fail(
            "A submitted run must not release or back off its claim"
        ),
    )

    assert function_app.start_pipeline(config) == (
        "pl_copy_gs_projects",
        "run-123",
    )
    assert tracking_calls == [(3, "run-123")]
