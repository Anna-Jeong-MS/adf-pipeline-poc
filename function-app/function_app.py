import concurrent.futures
import dataclasses
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import azure.functions as func
import oracledb
from azure.identity import DefaultAzureCredential


app = func.FunctionApp()
credential = DefaultAzureCredential()


@dataclasses.dataclass(frozen=True)
class TableConfig:
    control_id: int
    source_schema: str
    source_table: str
    target_folder: str
    pipeline_name: str
    partition_option: str
    partition_column: str | None
    partition_lower_bound: int | None
    partition_upper_bound: int | None
    parallel_copies: int


def required_setting(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required application setting is missing: {name}")
    return value


def row_to_config(row) -> TableConfig:
    return TableConfig(
        control_id=row[0],
        source_schema=row[1],
        source_table=row[2],
        target_folder=row[3],
        pipeline_name=row[4],
        partition_option=row[5] or "None",
        partition_column=row[6],
        partition_lower_bound=row[7],
        partition_upper_bound=row[8],
        parallel_copies=row[9] or 1,
    )


def iter_table_config_batches(batch_size: int):
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("PIPELINE_SYNC_BATCH_SIZE must be between 1 and 1000")
    query = """
        select
          control_id,
          source_schema,
          source_table,
          target_folder,
          pipeline_name,
          partition_option,
          partition_column,
          partition_lower_bound,
          partition_upper_bound,
          parallel_copies
        from adf_control_table
        where is_active = 'Y'
        order by load_order
    """
    with oracledb.connect(
        user=required_setting("ORACLE_USER"),
        password=required_setting("ORACLE_PASSWORD"),
        dsn=required_setting("ORACLE_DSN"),
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            while rows := cursor.fetchmany(batch_size):
                yield [row_to_config(row) for row in rows]


def claim_due_table_configs(limit: int) -> list[TableConfig]:
    if limit < 1 or limit > 1000:
        raise ValueError("MAX_PIPELINES_PER_SCHEDULE must be between 1 and 1000")
    candidate_limit = limit * 4
    query = f"""
        select
          control_id,
          source_schema,
          source_table,
          target_folder,
          pipeline_name,
          partition_option,
          partition_column,
          partition_lower_bound,
          partition_upper_bound,
          parallel_copies
        from adf_control_table
        where control_id in (
          select control_id
          from adf_control_table
          where is_active = 'Y'
            and next_run_at_utc <= systimestamp
          order by next_run_at_utc, load_order
          fetch first {candidate_limit} rows only
        )
        order by next_run_at_utc, load_order
        for update skip locked
    """
    with oracledb.connect(
        user=required_setting("ORACLE_USER"),
        password=required_setting("ORACLE_PASSWORD"),
        dsn=required_setting("ORACLE_DSN"),
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            configs = [row_to_config(row) for row in cursor.fetchmany(limit)]
            if configs:
                cursor.executemany(
                    """
                    update adf_control_table
                    set
                      last_dispatch_at_utc = systimestamp,
                      next_run_at_utc = systimestamp
                        + numtodsinterval(run_interval_minutes, 'MINUTE')
                    where control_id = :1
                    """,
                    [(config.control_id,) for config in configs],
                )
            connection.commit()
            return configs


def record_run_success(control_id: int, run_id: str) -> None:
    with oracledb.connect(
        user=required_setting("ORACLE_USER"),
        password=required_setting("ORACLE_PASSWORD"),
        dsn=required_setting("ORACLE_DSN"),
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update adf_control_table
                set
                  last_run_id = :run_id,
                  failure_count = 0,
                  last_error = null
                where control_id = :control_id
                """,
                run_id=run_id,
                control_id=control_id,
            )
        connection.commit()


def record_run_failure(control_id: int, error: Exception) -> None:
    with oracledb.connect(
        user=required_setting("ORACLE_USER"),
        password=required_setting("ORACLE_PASSWORD"),
        dsn=required_setting("ORACLE_DSN"),
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update adf_control_table
                set
                  failure_count = failure_count + 1,
                  last_error = :last_error,
                  next_run_at_utc = systimestamp + numtodsinterval(
                    least(power(2, failure_count + 1), 60),
                    'MINUTE'
                  )
                where control_id = :control_id
                """,
                last_error=str(error)[:2000],
                control_id=control_id,
            )
        connection.commit()


def validate_identifier(value: str, field_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]{0,127}", value):
        raise ValueError(f"Invalid Oracle identifier in {field_name}: {value}")
    return value


def pipeline_resource(config: TableConfig) -> dict:
    schema = validate_identifier(config.source_schema, "SOURCE_SCHEMA")
    table = validate_identifier(config.source_table, "SOURCE_TABLE")
    if config.partition_option not in {
        "None",
        "DynamicRange",
        "PhysicalPartitionsOfTable",
    }:
        raise ValueError(
            f"Unsupported PARTITION_OPTION: {config.partition_option}"
        )
    if config.parallel_copies < 1 or config.parallel_copies > 50:
        raise ValueError(
            f"PARALLEL_COPIES must be between 1 and 50: {config.pipeline_name}"
        )
    if not re.fullmatch(r"[A-Za-z0-9/_=-]+", config.target_folder):
        raise ValueError(f"Invalid TARGET_FOLDER: {config.target_folder}")

    source = {
        "type": "OracleSource",
        "queryTimeout": "02:00:00",
        "partitionOption": config.partition_option,
    }
    copy_properties = {
        "source": source,
        "sink": {
            "type": "ParquetSink",
            "storeSettings": {"type": "AzureBlobFSWriteSettings"},
            "formatSettings": {"type": "ParquetWriteSettings"},
        },
        "enableStaging": False,
        "parallelCopies": config.parallel_copies,
    }

    if config.partition_option == "DynamicRange":
        if not config.partition_column:
            raise ValueError(
                f"DynamicRange requires PARTITION_COLUMN: {schema}.{table}"
            )
        source["partitionSettings"] = {
            "partitionColumnName": validate_identifier(
                config.partition_column, "PARTITION_COLUMN"
            )
        }
        if config.partition_lower_bound is not None:
            source["partitionSettings"]["partitionLowerBound"] = str(
                config.partition_lower_bound
            )
        if config.partition_upper_bound is not None:
            source["partitionSettings"]["partitionUpperBound"] = str(
                config.partition_upper_bound
            )

    dataset_parameters = {
        "schemaName": schema,
        "tableName": table,
    }
    return {
        "properties": {
            "description": f"Generated pipeline for {schema}.{table}",
            "concurrency": 1,
            "folder": {"name": "generated/table-pipelines"},
            "annotations": ["generated-by-adf-pipeline-manager"],
            "activities": [
                {
                    "name": "GetSourceCount",
                    "type": "Lookup",
                    "dependsOn": [],
                    "policy": {
                        "timeout": "0.02:00:00",
                        "retry": 2,
                        "retryIntervalInSeconds": 60,
                    },
                    "typeProperties": {
                        "source": {
                            "type": "OracleSource",
                            "oracleReaderQuery": (
                                f"SELECT COUNT(*) AS SOURCE_COUNT FROM "
                                f"{schema}.{table}"
                            ),
                            "queryTimeout": "02:00:00",
                        },
                        "dataset": {
                            "referenceName": "ds_oracle_dynamic",
                            "type": "DatasetReference",
                            "parameters": dataset_parameters,
                        },
                        "firstRowOnly": True,
                    },
                },
                {
                    "name": "CopyToAdls",
                    "type": "Copy",
                    "dependsOn": [
                        {
                            "activity": "GetSourceCount",
                            "dependencyConditions": ["Succeeded"],
                        }
                    ],
                    "policy": {
                        "timeout": "0.12:00:00",
                        "retry": 2,
                        "retryIntervalInSeconds": 60,
                    },
                    "typeProperties": copy_properties,
                    "inputs": [
                        {
                            "referenceName": "ds_oracle_dynamic",
                            "type": "DatasetReference",
                            "parameters": dataset_parameters,
                        }
                    ],
                    "outputs": [
                        {
                            "referenceName": "ds_adls_parquet_dynamic",
                            "type": "DatasetReference",
                            "parameters": {
                                "folderPath": {
                                    "value": (
                                        f"@concat('{config.target_folder}/"
                                        "load_date=', formatDateTime(utcNow(), "
                                        "'yyyy-MM-dd'))"
                                    ),
                                    "type": "Expression",
                                },
                                "fileName": {
                                    "value": (
                                        f"@concat('{table}_', "
                                        "formatDateTime(utcNow(), "
                                        "'yyyyMMdd_HHmmss'), '.parquet')"
                                    ),
                                    "type": "Expression",
                                },
                            },
                        }
                    ],
                },
                {
                    "name": "ValidateRowCount",
                    "type": "IfCondition",
                    "dependsOn": [
                        {
                            "activity": "CopyToAdls",
                            "dependencyConditions": ["Succeeded"],
                        }
                    ],
                    "typeProperties": {
                        "expression": {
                            "value": (
                                "@equals("
                                "int(activity('GetSourceCount').output."
                                "firstRow.SOURCE_COUNT),"
                                "int(activity('CopyToAdls').output.rowsCopied))"
                            ),
                            "type": "Expression",
                        },
                        "ifTrueActivities": [],
                        "ifFalseActivities": [
                            {
                                "name": "FailRowCountMismatch",
                                "type": "Fail",
                                "dependsOn": [],
                                "typeProperties": {
                                    "message": {
                                        "value": (
                                            f"@concat('Row count mismatch for "
                                            f"{schema}.{table}: source=', "
                                            "activity('GetSourceCount').output."
                                            "firstRow.SOURCE_COUNT, ', copied=', "
                                            "activity('CopyToAdls').output."
                                            "rowsCopied, "
                                            f"', partitionOption="
                                            f"{config.partition_option}, "
                                            f"lower={config.partition_lower_bound}, "
                                            f"upper={config.partition_upper_bound}')"
                                        ),
                                        "type": "Expression",
                                    },
                                    "errorCode": "ROW_COUNT_MISMATCH",
                                },
                            }
                        ],
                    },
                },
            ],
        }
    }


def management_request(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    retryable_statuses = {429, 500, 502, 503, 504}
    maximum_attempts = 5
    for attempt in range(maximum_attempts):
        token = credential.get_token(
            "https://management.azure.com/.default"
        ).token
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if (
                error.code not in retryable_statuses
                or attempt == maximum_attempts - 1
            ):
                raise RuntimeError(
                    f"Azure management request failed ({error.code}): {detail}"
                ) from error
            retry_after = error.headers.get("Retry-After")
            delay = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else min(2**attempt, 30)
            )
            logging.warning(
                "Azure management request returned %s; retrying in %ss",
                error.code,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("Azure management request retry loop ended unexpectedly")


def pipeline_url(pipeline_name: str, action: str = "") -> str:
    subscription_id = required_setting("AZURE_SUBSCRIPTION_ID")
    resource_group = required_setting("ADF_RESOURCE_GROUP")
    factory_name = required_setting("ADF_FACTORY_NAME")
    encoded_pipeline = urllib.parse.quote(pipeline_name, safe="")
    return (
        "https://management.azure.com/subscriptions/"
        f"{subscription_id}/resourceGroups/{resource_group}/providers/"
        f"Microsoft.DataFactory/factories/{factory_name}/pipelines/"
        f"{encoded_pipeline}{action}?api-version=2018-06-01"
    )


def sync_pipeline(config: TableConfig) -> str:
    management_request(
        "PUT",
        pipeline_url(config.pipeline_name),
        pipeline_resource(config),
    )
    return config.pipeline_name


def start_pipeline(config: TableConfig) -> tuple[str, str]:
    try:
        response = management_request(
            "POST",
            pipeline_url(config.pipeline_name, "/createRun"),
            {},
        )
        run_id = response.get("runId")
        if not run_id:
            raise RuntimeError(
                f"ADF did not return a runId for pipeline {config.pipeline_name}"
            )
    except Exception as error:
        try:
            record_run_failure(config.control_id, error)
        except Exception:
            logging.exception(
                "Failed to record dispatch failure for %s",
                config.pipeline_name,
            )
        raise
    try:
        record_run_success(config.control_id, run_id)
    except Exception:
        logging.exception(
            "Pipeline %s started as run %s, but tracking update failed",
            config.pipeline_name,
            run_id,
        )
    return config.pipeline_name, run_id


def run_parallel(operation, configs: list[TableConfig]) -> list:
    max_workers = bounded_int_setting(
        "MANAGEMENT_API_CONCURRENCY", default=8, minimum=1, maximum=32
    )
    failures = []
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(operation, config): config for config in configs
        }
        for future in concurrent.futures.as_completed(future_map):
            config = future_map[future]
            try:
                results.append(future.result())
            except Exception as error:
                logging.exception(
                    "Operation failed for pipeline %s", config.pipeline_name
                )
                failures.append(f"{config.pipeline_name}: {error}")
    if failures:
        raise RuntimeError("; ".join(failures))
    return results


def bounded_int_setting(
    name: str, default: int, minimum: int, maximum: int
) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


@app.timer_trigger(
    schedule="%PIPELINE_SYNC_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def sync_table_pipelines(timer: func.TimerRequest) -> None:
    batch_size = bounded_int_setting(
        "PIPELINE_SYNC_BATCH_SIZE", default=100, minimum=1, maximum=1000
    )
    synchronized_count = 0
    for configs in iter_table_config_batches(batch_size):
        pipelines = run_parallel(sync_pipeline, configs)
        synchronized_count += len(pipelines)
    logging.info("Synchronized %d table pipelines", synchronized_count)


@app.timer_trigger(
    schedule="%PIPELINE_RUN_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def run_table_pipelines(timer: func.TimerRequest) -> None:
    maximum_runs = bounded_int_setting(
        "MAX_PIPELINES_PER_SCHEDULE", default=20, minimum=1, maximum=1000
    )
    configs = claim_due_table_configs(maximum_runs)
    runs = run_parallel(start_pipeline, configs)
    logging.info("Started %d table pipelines: %s", len(runs), runs)
