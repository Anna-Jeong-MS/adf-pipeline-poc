# Azure Function 기반 테이블별 Pipeline 생성·실행 요청 자동화

> **Legacy 옵션 문서:** 아래 내용은 Function이 Oracle에 직접 연결하는 이전
> Timer/Cursor 방식을 설명한다. 현재 목표 구조는 Function의 Oracle 연결을
> 제거하고 ADF Dispatcher와 Self-hosted IR을 사용한다.
> [목표 아키텍처](target-architecture.md)를 우선 적용한다.

## 요구사항

- 테이블마다 독립 ADF Pipeline 리소스를 생성한다.
- 한 테이블의 대량 행은 Oracle 병렬 읽기로 처리한다.
- Azure Function Timer가 Pipeline 생성·갱신과 Create Run 요청을 자동화한다.
- 한 Timer 호출에서 제출할 Pipeline 수를 제한한다.

## 공통 전제

ADF에는 다음 리소스가 사전에 존재해야 한다.

- `ls_oracle_poc`
- `ls_adls_gen2`
- `ds_oracle_dynamic`
- `ds_adls_parquet_dynamic`

Function Managed Identity에는 대상 Data Factory 범위의 Pipeline 생성/갱신 및
실행 권한이 필요하다. Oracle 암호는 Function App Setting에 Key Vault Reference로
설정하고 소스 코드나 `local.settings.json`에 저장하지 않는다.

최소 Custom Role의 `Actions`에는 환경에 따라 다음 관리 작업이 필요하다.

```text
Microsoft.DataFactory/factories/pipelines/read
Microsoft.DataFactory/factories/pipelines/write
Microsoft.DataFactory/factories/pipelines/createRun/action
```

Function이 Oracle Control Table을 직접 읽으므로 Oracle까지 네트워크 경로도
필요하다. 운영에서는 Function VNet Integration과 사설 Oracle 연결을 사용하고,
공인 1521 포트 개방을 기본 설계로 사용하지 않는다.

샘플 SQL은 Pipeline 이름의 앞 250자와 테이블 정규 이름의 SHA-256 앞 8자를
조합해 ADF 이름 길이 제한 안에서 충돌 가능성을 낮춘다.

## 1안: Function이 Pipeline 생성/갱신과 실행 담당

### 흐름

```text
Oracle ADF_CONTROL_TABLE
  -> sync_table_pipelines Timer Function
  -> 테이블별 Pipeline PUT

Oracle ADF_CONTROL_TABLE
  -> run_table_pipelines Timer Function
  -> 테이블별 Pipeline createRun
```

`function-app/function_app.py`에 실행 가능한 Python v2 샘플이 있다.

### 장점

- 신규 테이블이 Control Table에 등록되면 다음 Sync 주기에 Pipeline이 자동 생성된다.
- 테이블별 `partitionOptions`, `partitionColumnName`, `parallelCopies`를 적용할 수 있다.
- 수백 Pipeline을 사람이 만들지 않아도 된다.
- 런타임 생성 방식에서는 ADF Studio Publish나 Trigger Start가 필요 없다.

### 단점

- Function Managed Identity에 ADF 리소스 쓰기 권한이 필요하다.
- 런타임이 ADF 정의를 변경하므로 변경 승인과 감사 정책이 필요하다.
- 삭제된 Control Table 행에 대응하는 오래된 Pipeline 삭제 정책을 별도로 정해야 한다.

### 권장 용도

- 테이블 추가와 설정 변경이 잦다.
- Control Table을 ADF Pipeline 정의의 실질적인 Source of Truth로 사용한다.
- 런타임 자동 생성에 대한 조직 승인이 있다.

## 2안: Bicep이 Pipeline 생성, Function은 실행만 담당

### 흐름

```text
tables.json
  -> Bicep for-loop
  -> 테이블별 Pipeline 배포

Function Timer
  -> tables.json 또는 Control Table 조회
  -> 테이블별 Pipeline createRun
```

### 중요한 제약

Bicep은 배포 시 Oracle Control Table을 직접 조회할 수 없다. 따라서 Pipeline 생성
목록은 Git의 `tables.json`과 같은 정적 매니페스트로 제공해야 한다. Control Table과
매니페스트를 맞추려면 별도의 생성 Job 또는 Pull Request 자동화가 필요하다.

개념 예:

```bicep
param tables array

resource tablePipelines 'Microsoft.DataFactory/factories/pipelines@2018-06-01' = [
  for table in tables: {
    parent: dataFactory
    name: table.pipelineName
    properties: buildTablePipeline(table)
  }
]
```

### 장점

- Pipeline 정의 변경이 Git Pull Request와 배포 이력에 남는다.
- Function은 ADF Pipeline 쓰기 권한 없이 실행 권한만 가질 수 있다.
- 운영 환경의 변경 통제가 명확하다.

### 단점

- Oracle Control Table과 `tables.json` 동기화가 필요하다.
- 신규 테이블은 Git 변경과 Bicep 배포가 완료돼야 실행할 수 있다.
- 수백 Pipeline ARM 리소스로 인해 배포 시간이 길어진다.

### 권장 용도

- 운영 환경에서 런타임 리소스 생성을 허용하지 않는다.
- 모든 Pipeline 변경에 PR 승인과 IaC 배포가 필요하다.
- 테이블 목록 변경 빈도가 낮다.

## 대용량 테이블 처리

Pipeline을 테이블별로 분리하는 것만으로 한 테이블의 Copy 속도가 증가하지 않는다.
Oracle Source에서 다음 설정이 필요하다.

| Control Table 설정 | ADF Copy 설정 |
|---|---|
| `PARTITION_OPTION` | `None`, `DynamicRange`, `PhysicalPartitionsOfTable` |
| `PARTITION_COLUMN` | Dynamic Range의 정수형 분할 열 |
| `PARTITION_LOWER_BOUND` | 분할 최솟값 |
| `PARTITION_UPPER_BOUND` | 분할 최댓값 |
| `PARALLEL_COPIES` | 한 테이블 내부 병렬 Copy 수 |

우선순위:

1. Oracle 파티션 테이블이면 `PhysicalPartitionsOfTable`
2. 정수형 PK/인덱스 열이 있으면 `DynamicRange`
3. 적절한 분할 열이 없으면 `None`

`PARALLEL_COPIES`를 높이면 Oracle 세션과 I/O도 증가한다. 4부터 시작해 DBA 부하
시험 후 8, 16 순으로 조정한다.

Dynamic Range의 Lower/Upper Bound는 실제 복사 범위를 제한한다. 신규 행의 분할
키가 Upper Bound를 넘으면 해당 행은 복사되지 않고 Row Count 검증이 실패한다.
실행 전에 `MIN(partition_column)`/`MAX(partition_column)`으로 Bound를 갱신하거나
Control Table 동기화 과정에서 자동 계산해야 한다.

## 스케줄과 동시 실행

샘플은 두 Timer를 사용한다.

- `PIPELINE_SYNC_SCHEDULE`: Pipeline 정의 동기화
- `PIPELINE_RUN_SCHEDULE`: Pipeline 실행

`MAX_PIPELINES_PER_SCHEDULE`로 한 번에 시작할 Pipeline 수를 제한한다. Timer는
`NEXT_RUN_AT_UTC`가 지난 행을 `FOR UPDATE SKIP LOCKED`로 Claim하고 다음 실행
시각을 먼저 갱신한다. 따라서 수백 개 중 앞의 N개만 반복 실행되지 않고 다음
Timer 호출에서 나머지 예정 행을 계속 처리한다. Create Run이 실패하면 `FAILURE_COUNT`를 증가시키고 최대 60분의 지수 Backoff를
적용한다. ARM 429와 일시적인 5xx 응답은 `Retry-After` 또는 제한된 지수 Backoff로
최대 5회 재시도한다.

단순 Timer는 Pipeline Run 제출까지만 제어하고 실제 완료까지 기다리지 않는다.
동시에 실행 중인 Pipeline 수를 엄격하게 제한하려면 Durable Functions로 Batch
완료 후 다음 Batch를 실행하는 오케스트레이션을 추가한다.

각 생성 Pipeline은 `concurrency: 1`로 설정되어 동일 테이블의 동시 실행을
제한한다. 중복 Create Run 요청을 제거하지는 않으며 후속 Run이 대기할 수 있다.

Claim은 Run 제출 전에 다음 예정 시각을 갱신하는 at-most-once 방식이다. Function
Host가 Claim 커밋 직후 종료되면 해당 실행이 다음 주기까지 지연될 수 있다. 반대로
Create Run 성공 직후 추적 업데이트가 실패해도 Claim을 즉시 해제하지 않아 이미
시작된 Run을 바로 중복 제출하지 않는다. 엄격한 전달 보장이 필요하면 Durable
Functions와 별도 Dispatch 상태/Lease를 사용한다.

`LAST_RUN_ID`와 `FAILURE_COUNT` 갱신은 Create Run API 요청의 접수 성공/실패를
기록한다. ADF Pipeline의 최종 성공/실패를 조회하거나 실행 후 실패를 재처리하는
기능은 이 샘플에 포함되지 않는다.

## Function 설정

| 설정 | 설명 |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | ADF 구독 |
| `ADF_RESOURCE_GROUP` | ADF 리소스 그룹 |
| `ADF_FACTORY_NAME` | Factory 이름 |
| `ORACLE_DSN` | Oracle Easy Connect 문자열 |
| `ORACLE_USER` | Control Table 조회 사용자 |
| `ORACLE_PASSWORD` | Key Vault Reference 사용 |
| `PIPELINE_SYNC_SCHEDULE` | NCRONTAB Pipeline 동기화 일정 |
| `PIPELINE_RUN_SCHEDULE` | NCRONTAB Pipeline 실행 일정 |
| `MANAGEMENT_API_CONCURRENCY` | ADF 관리 API 병렬 호출 수 |
| `PIPELINE_SYNC_BATCH_SIZE` | Oracle Cursor에서 한 번에 읽을 Pipeline 설정 수 |
| `MAX_PIPELINES_PER_SCHEDULE` | 한 실행에서 시작할 Pipeline 최대 수 |

## Control Table 조회 결과가 ADF Lookup 제한보다 큰 경우

Lookup 제한은 원천 테이블 데이터량이 아니라 Control Table 조회 결과에 적용된다.
조회 결과가 5,000행/4MB 이내이고 ADF 내부 실행 제어로 충분하면 Lookup + ForEach도
사용할 수 있다. 이 샘플은 제한 초과 가능성과 Claim/Backoff 요구 때문에 Oracle
Cursor를 직접 읽는다.

Pipeline 동기화 Function은 다음 방식으로 처리한다.

```text
Oracle Cursor
  -> fetchmany(PIPELINE_SYNC_BATCH_SIZE)
  -> 배치 내 Pipeline Create/Update
  -> 다음 Cursor 배치
```

기본 배치 크기는 100이고 최대 1,000으로 제한한다. Control Table 전체를 Function
메모리에 올리거나 ADF Activity Output으로 전달하지 않는다.

Pipeline 실행 Function은 전체 Control Table을 읽지 않는다.

```text
NEXT_RUN_AT_UTC <= SYSTIMESTAMP
  -> ORDER BY NEXT_RUN_AT_UTC, LOAD_ORDER
  -> FOR UPDATE SKIP LOCKED
  -> MAX_PIPELINES_PER_SCHEDULE만 Claim
```

따라서 Control Table이 수만 행이어도 각 Timer 실행은 예정된 일부 행만 조회한다.
다음 인덱스를 운영 환경에 추가하는 것이 좋다.

```sql
create index IX_ADF_CONTROL_DUE
  on ADF_CONTROL_TABLE (
    IS_ACTIVE,
    NEXT_RUN_AT_UTC,
    LOAD_ORDER
  );
```

Control Table 행 수가 매우 커지면 Pipeline 정의를 매 Sync마다 전부 PUT하지 말고
`DEFINITION_HASH` 또는 `UPDATED_AT_UTC`를 추가해 변경된 행만 동기화해야 한다.

## 공식 참고자료

- [Azure Functions Timer trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-timer)
- [ADF Pipeline Create or Update API](https://learn.microsoft.com/rest/api/datafactory/pipelines/create-or-update)
- [ADF Pipeline Create Run API](https://learn.microsoft.com/rest/api/datafactory/pipelines/create-run)
- [ADF Oracle 병렬 Copy](https://learn.microsoft.com/azure/data-factory/connector-oracle#parallel-copy-from-oracle)
- [ADF ARM Trigger 매개변수화](https://learn.microsoft.com/azure/data-factory/continuous-integration-delivery-resource-manager-custom-parameters)
