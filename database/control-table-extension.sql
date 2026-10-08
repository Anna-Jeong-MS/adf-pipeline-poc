whenever sqlerror exit sql.sqlcode rollback

alter table ADF_CONTROL_TABLE add (
  PIPELINE_NAME varchar2(260),
  PARTITION_OPTION varchar2(30) default 'None' not null,
  PARTITION_COLUMN varchar2(128),
  PARTITION_LOWER_BOUND number,
  PARTITION_UPPER_BOUND number,
  PARALLEL_COPIES number(2) default 1 not null,
  RUN_INTERVAL_MINUTES number(8) default 1440 not null,
  NEXT_RUN_AT_UTC timestamp with time zone default systimestamp not null,
  LAST_DISPATCH_AT_UTC timestamp with time zone,
  LAST_RUN_ID varchar2(64),
  FAILURE_COUNT number(8) default 0 not null,
  LAST_ERROR varchar2(2000)
);

update ADF_CONTROL_TABLE
set
  PIPELINE_NAME =
    substr(lower('pl_copy_' || source_schema || '_' || source_table), 1, 250)
    || '_'
    || lower(substr(
      standard_hash(source_schema || '.' || source_table, 'SHA256'),
      1,
      8
    )),
  PARALLEL_COPIES = 1;

alter table ADF_CONTROL_TABLE modify PIPELINE_NAME not null;

alter table ADF_CONTROL_TABLE add constraint UK_ADF_PIPELINE_NAME
  unique (PIPELINE_NAME);

alter table ADF_CONTROL_TABLE add constraint CK_ADF_PARTITION_OPTION
  check (PARTITION_OPTION in ('None', 'DynamicRange', 'PhysicalPartitionsOfTable'));

alter table ADF_CONTROL_TABLE add constraint CK_ADF_PARALLEL_COPIES
  check (PARALLEL_COPIES between 1 and 50);

alter table ADF_CONTROL_TABLE add constraint CK_ADF_RUN_INTERVAL
  check (RUN_INTERVAL_MINUTES >= 1);

create index IX_ADF_CONTROL_DUE
  on ADF_CONTROL_TABLE (
    IS_ACTIVE,
    NEXT_RUN_AT_UTC,
    LOAD_ORDER
  );

create or replace trigger TRG_ADF_CONTROL_PIPELINE_NAME
before insert or update of source_schema, source_table on ADF_CONTROL_TABLE
for each row
begin
  if :new.pipeline_name is null
    or updating('SOURCE_SCHEMA')
    or updating('SOURCE_TABLE')
  then
    :new.pipeline_name :=
      substr(
        lower('pl_copy_' || :new.source_schema || '_' || :new.source_table),
        1,
        250
      )
      || '_'
      || lower(substr(
        standard_hash(
          :new.source_schema || '.' || :new.source_table,
          'SHA256'
        ),
        1,
        8
      ));
  end if;
end;
/

commit;

-- Example for a large table:
update ADF_CONTROL_TABLE
set
  PARTITION_OPTION = 'DynamicRange',
  PARTITION_COLUMN = 'COST_ID',
  PARTITION_LOWER_BOUND = 1,
  PARTITION_UPPER_BOUND = 10000000,
  PARALLEL_COPIES = 8
where SOURCE_SCHEMA = 'GS_POC'
  and SOURCE_TABLE = 'GS_COST_ACTUALS';

commit;
