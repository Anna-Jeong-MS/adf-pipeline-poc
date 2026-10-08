# Scenario 01 - 메타데이터 기반 공통 Pipeline 설계 및 전체 자동화

이 문서는 Oracle Control Table의 메타데이터로 동일한 ADF Copy 구조를 반복
적용하고, Azure Function이 테이블별 Pipeline 생성과 실행을 자동화하는 방법을
설명한다. 가능 여부를 판정하는 문서가 아니라 고객이 직접 구성하는 단계별
가이드다.

## 1.1 Pipeline 설계 아키텍처

### 1.1.1 권장 구조

```text
Oracle ADF_CONTROL_TABLE
  |
  +-- Pipeline Sync Timer Function
  |     +-- 활성 행을 Cursor 배치로 조회
  |     +-- 공통 Pipeline 템플릿에 테이블 설정 적용
  |     +-- ADF Pipeline Create/Update API
  |
  +-- Pipeline Run Timer Function
        +-- 실행 예정 행을 SKIP LOCKED로 Claim
        +-- ADF Create Run API
              |
              +-- GetSourceCount
              +-- CopyToAdls
              +-- ValidateRowCount
              +-- FailRowCountMismatch
```

여기서 **공통 Pipeline**은 하나의 Pipeline이 모든 대량 테이블을 동시에 처리한다는
뜻이 아니라 모든 테이블에 적용할 활동·파라미터·오류 처리의 표준 템플릿이다.
Control Table 한 행마다 이 템플릿으로 독립 Pipeline을 만들어 테이블별 재실행,
동시성 제한, 모니터링을 분리한다.

### 1.1.2 Lookup 적용 기준

| 방식 | 적용 기준 | 메타데이터 읽기 |
|---|---|---|
| ADF Lookup + ForEach | 활성 행이 항상 5,000개 미만이고 출력이 4MB 미만인 소규모 환경 | `Lookup.output.value` |
| Function Cursor 배치 | 수백~수천 테이블, 행 증가 가능성, API 호출 제어 필요 | Oracle `fetchmany` |

ADF Lookup은 최대 5,000행만 반환하고 출력은 4MB를 넘을 수 없다. 따라서 운영
가이드의 기본은 Function Cursor 배치이며, Pipeline 안의 `GetSourceCount` Lookup은
`COUNT(*)` 한 행만 반환하므로 이 제한과 무관하다.

Lookup + ForEach로 구성하는 경우:

1. Oracle Control Table Dataset을 만든다.
2. Lookup Activity의 **First row only**를 해제한다.
3. Query에 `WHERE IS_ACTIVE = 'Y'`를 적용한다.
4. ForEach **Items**에 `@activity('LookupControl').output.value`를 설정한다.
5. ForEach **Batch count**를 Oracle 허용 세션 수 이하로 설정한다.
6. 행 수와 직렬화된 출력 크기를 운영 임계치로 감시한다.

제한에 가까워지면 `ROW_NUMBER()` 또는 `CONTROL_ID` 범위로 페이지를 나누는 상위
Until Pipeline을 만들 수 있지만, 페이지 상태·재시도·중복 방지가 복잡해지므로
이 샘플처럼 Function Cursor로 전환하는 것을 권장한다.

### 1.1.3 사전 준비

- Azure Data Factory
- Oracle DB와 `ADF_CONTROL_TABLE`
- ADLS Gen2 대상 Container
- Azure Key Vault
- Python Azure Function App
- ADF와 Function에서 Oracle까지의 사설 네트워크 경로
- [Control Table 확장 SQL](../../database/control-table-extension.sql)

### 1.1.4 Portal에서 Linked Service 구성

#### Oracle Linked Service

1. Azure Portal에서 Data Factory를 열고 **Launch Studio**를 선택한다.
2. ADF Studio의 **Manage > Linked services > + New**를 선택한다.
3. **Oracle**을 검색하고 선택한다.
4. 이름을 `ls_oracle_poc`로 입력한다.
5. Integration Runtime과 Oracle Host, Port, Service Name을 설정한다.
6. 인증 정보는 Key Vault Linked Service의 Secret을 참조한다.
7. **Test connection** 후 **Create**를 선택한다.

Self-hosted Integration Runtime을 사용하면 Oracle 네트워크에 접근 가능한 Host에
설치하고 방화벽에서 필요한 방향의 1521 연결만 허용한다.

#### ADLS Gen2 Linked Service

1. **Manage > Linked services > + New**를 선택한다.
2. **Azure Data Lake Storage Gen2**를 선택한다.
3. 이름을 `ls_adls_gen2`로 입력한다.
4. 인증은 **Managed Identity**를 선택한다.
5. Storage Account를 선택하고 **Test connection**을 실행한다.
6. Data Factory Identity에 대상 Container 범위의
   **Storage Blob Data Contributor**를 부여한다.

### 1.1.5 파라미터 Dataset 구성

#### Oracle Dataset `ds_oracle_dynamic`

1. **Author > Datasets > + New dataset > Oracle**을 선택한다.
2. Linked Service는 `ls_oracle_poc`를 선택한다.
3. **Parameters**에서 `schemaName`, `tableName` 문자열을 추가한다.
4. Dataset Connection의 Schema에 `@dataset().schemaName`,
   Table에 `@dataset().tableName`을 설정한다.

#### Parquet Dataset `ds_adls_parquet_dynamic`

1. **Author > Datasets > + New dataset > Azure Data Lake Storage Gen2 >
   Parquet**을 선택한다.
2. Linked Service는 `ls_adls_gen2`를 선택한다.
3. `folderPath`, `fileName` 문자열 Parameter를 추가한다.
4. File path의 Directory와 File에 각각
   `@dataset().folderPath`, `@dataset().fileName`을 설정한다.

### 1.1.6 Control Table 구성

기존 테이블을 먼저 백업하고 확장 SQL을 개발 DB에 적용한다.

```sql
create table ADF_CONTROL_TABLE_BAK_20261008 as
select * from ADF_CONTROL_TABLE;
```

주요 열:

| 열 | 용도 |
|---|---|
| `SOURCE_SCHEMA`, `SOURCE_TABLE` | 원천 객체 |
| `TARGET_FOLDER` | ADLS 대상 경로 |
| `PIPELINE_NAME` | 자동 생성할 Pipeline 이름 |
| `PARTITION_OPTION` | `None`, `DynamicRange`, `PhysicalPartitionsOfTable` |
| `PARTITION_COLUMN` | Dynamic Range 정수형 분할 열 |
| `PARTITION_LOWER_BOUND`, `PARTITION_UPPER_BOUND` | 복사할 키 범위 |
| `PARALLEL_COPIES` | 테이블 내부 Copy 병렬 수 |
| `RUN_INTERVAL_MINUTES`, `NEXT_RUN_AT_UTC` | 실행 일정 |
| `FAILURE_COUNT`, `LAST_ERROR` | 제출 실패 추적 |

일반 테이블 예:

```sql
insert into ADF_CONTROL_TABLE (
  CONTROL_ID, SOURCE_SCHEMA, SOURCE_TABLE, TARGET_FOLDER,
  IS_ACTIVE, LOAD_ORDER, PARTITION_OPTION, PARALLEL_COPIES,
  RUN_INTERVAL_MINUTES
)
values (
  1001, 'GS_POC', 'GS_PROJECTS', 'gs_projects',
  'Y', 10, 'None', 1, 1440
);
commit;
```

대량 테이블은 먼저 실제 Bound를 구하고 분할 설정을 저장한다.

```sql
select min(COST_ID), max(COST_ID)
from GS_POC.GS_COST_ACTUALS;

update ADF_CONTROL_TABLE
set PARTITION_OPTION = 'DynamicRange',
    PARTITION_COLUMN = 'COST_ID',
    PARTITION_LOWER_BOUND = 1,
    PARTITION_UPPER_BOUND = 10000000,
    PARALLEL_COPIES = 8
where SOURCE_SCHEMA = 'GS_POC'
  and SOURCE_TABLE = 'GS_COST_ACTUALS';
commit;
```

Bound는 Copy 범위를 제한하므로 신규 키가 Upper Bound를 넘기 전에 갱신한다.
`PARALLEL_COPIES`는 4부터 시작해 Oracle Session, CPU, I/O를 확인하며 조정한다.

### 1.1.7 공통 Pipeline 템플릿

[Function 샘플](../../function-app/function_app.py)의 `pipeline_resource`가 다음
공통 구조를 ADF Pipeline JSON으로 만든다.

```json
{
  "properties": {
    "concurrency": 1,
    "folder": { "name": "generated/table-pipelines" },
    "activities": [
      { "name": "GetSourceCount", "type": "Lookup" },
      { "name": "CopyToAdls", "type": "Copy" },
      { "name": "ValidateRowCount", "type": "IfCondition" }
    ],
    "annotations": ["generated-by-adf-pipeline-manager"]
  }
}
```

- `GetSourceCount`: `SELECT COUNT(*)` 한 행 조회
- `CopyToAdls`: Dataset Parameter와 Control Table의 Partition 설정 적용
- `ValidateRowCount`: Lookup Count와 `CopyToAdls.output.rowsCopied` 비교
- 불일치: `ROW_COUNT_MISMATCH` 코드로 Fail Activity 실행
- `concurrency: 1`: 같은 테이블 Pipeline의 중복 동시 실행 방지

정확한 생성 로직과 Dynamic Range JSON은
[function_app.py](../../function-app/function_app.py)의 `pipeline_resource`를
사용한다. 테이블/Schema/Partition 열은 식별자 검증 후 JSON에 반영된다.

## 1.2 전체 Pipeline 자동화

### 1.2.1 자동화 방식

두 Timer Function이 역할을 나눈다.

| Function | 기본 역할 |
|---|---|
| `sync_table_pipelines` | Control Table을 배치 조회해 Pipeline Create/Update |
| `run_table_pipelines` | 예정 행을 Claim하고 Pipeline Create Run |

ADF Schedule Trigger나 배포 PowerShell은 사용하지 않는다. 신규 Control Table 행은
다음 Sync 주기에 Pipeline으로 생성되고, `NEXT_RUN_AT_UTC`가 지난 행은 Run Timer가
제한된 개수만 제출한다.

### 1.2.2 Function App 배포

1. 이 저장소의 [function-app](../../function-app/)을 준비한다.
2. `requirements.txt`에 있는 Package를 설치하고 `pytest`를 실행한다.
3. VS Code Azure Functions Extension 또는 조직 CI/CD로 Function App에 배포한다.
4. Portal에서 Function App의 **Functions**에
   `sync_table_pipelines`, `run_table_pipelines`가 표시되는지 확인한다.
5. **Overview > Application Insights**가 연결됐는지 확인한다.

로컬 검증:

```powershell
cd function-app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest
pytest
```

### 1.2.3 Managed Identity와 권한

1. Function App의 **Settings > Identity > System assigned**를 **On**으로 저장한다.
2. Data Factory의 **Access control (IAM) > Add role assignment**를 연다.
3. 다음 Action만 포함한 조직 Custom Role을 Factory Scope에 부여한다.

```text
Microsoft.DataFactory/factories/pipelines/read
Microsoft.DataFactory/factories/pipelines/write
Microsoft.DataFactory/factories/pipelines/createRun/action
```

4. Key Vault에서 Function Identity에 Secret Read 권한을 부여한다.
5. Oracle 계정에는 Control Table 조회·갱신과 대상 테이블 조회 권한만 부여한다.

### 1.2.4 Function 환경 변수

Function App의 **Settings > Environment variables > App settings**에서 설정한다.

| 이름 | 예시/설명 |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | 대상 구독 ID |
| `ADF_RESOURCE_GROUP` | Factory Resource Group |
| `ADF_FACTORY_NAME` | Factory 이름 |
| `ORACLE_DSN` | `<PRIVATE_HOST>:1521/<SERVICE>` |
| `ORACLE_USER` | Control Table 사용자 |
| `ORACLE_PASSWORD` | Key Vault Reference |
| `PIPELINE_SYNC_SCHEDULE` | `0 0 2 * * *` |
| `PIPELINE_RUN_SCHEDULE` | `0 * * * * *` |
| `PIPELINE_SYNC_BATCH_SIZE` | `100` |
| `MANAGEMENT_API_CONCURRENCY` | `8` |
| `MAX_PIPELINES_PER_SCHEDULE` | `20` |

설정 저장 후 Function App을 재시작한다. Oracle 연결은 VNet Integration, Private
DNS, NSG/방화벽을 통해 확인하며 공인 DSN과 암호를 문서나 화면 캡처에 남기지 않는다.

### 1.2.5 Pipeline 생성 자동화 확인

1. Function App **Functions > sync_table_pipelines > Code + Test**를 연다.
2. **Test/Run**으로 한 번 실행한다.
3. **Monitor**에서 Invocation 상세와 오류가 없는지 확인한다.
4. ADF Studio **Author > Factory Resources > Pipelines**를 연다.
5. `generated/table-pipelines` Folder에서 활성 Control 행과 Pipeline을 대조한다.
6. Sync를 다시 실행해 중복이 생기지 않고 같은 이름이 갱신되는지 확인한다.

Application Insights 예:

```kusto
traces
| where timestamp > ago(30m)
| where message has "Synchronized"
| order by timestamp desc
```

### 1.2.6 Pipeline 실행 자동화 확인

테스트 행의 실행 시각을 현재로 바꾼다.

```sql
update ADF_CONTROL_TABLE
set NEXT_RUN_AT_UTC = systimestamp
where SOURCE_SCHEMA = 'GS_POC'
  and SOURCE_TABLE = 'GS_PROJECTS';
commit;
```

1. **Functions > run_table_pipelines > Code + Test > Test/Run**을 실행한다.
2. Function **Monitor**에서 Pipeline 이름과 Run ID를 기록한다.
3. ADF Studio **Monitor > Pipeline runs**에서 같은 Run ID를 검색한다.
4. 각 Activity의 Input/Output에서 Dataset Parameter, `rowsCopied`, Count를 확인한다.
5. Control Table의 `LAST_RUN_ID`, `LAST_DISPATCH_AT_UTC`,
   `NEXT_RUN_AT_UTC`, `FAILURE_COUNT`, `LAST_ERROR`를 확인한다.

### 1.2.7 수백 테이블 배치와 실패 처리

- Run Timer 한 번에는 `MAX_PIPELINES_PER_SCHEDULE`개만 Claim한다.
- 다음 Timer 호출이 남은 예정 행을 이어서 처리한다.
- ARM 429/일시적 5xx는 `Retry-After` 또는 제한된 Backoff로 재시도한다.
- 제출 실패는 `FAILURE_COUNT`, `LAST_ERROR`에 기록하고 최대 60분 Backoff한다.
- 전체 동시 실행 수를 완료 기준으로 엄격히 제한하려면 Durable Functions에서
  Batch 완료를 기다린 후 다음 Batch를 제출하도록 확장한다.

### 1.2.8 Git Mode와 Live Mode 운영

1. 사람이 작성하는 Linked Service/Dataset 등은 개발 Factory의 Git Mode에서
   Feature Branch와 Pull Request로 관리한다.
2. 승인 후 Collaboration Branch에 Merge하고 **Publish**하여 개발 Live Factory에
   반영한다.
3. Test/Production은 Publish 산출 ARM Template을 CI/CD로 배포한다.
4. Function이 관리 API로 만든 Pipeline은 Live Factory에 바로 생성되고 Git에
   자동 기록되지 않는다.
5. 자동 생성 Pipeline의 Source of Truth는 Control Table, Function 버전,
   Application Insights Audit로 정하고 사람이 직접 편집하지 않는다.

모든 Pipeline을 반드시 Git 승인 대상으로 관리해야 한다면
[Function 자동화 옵션](../../docs/function-automation-options.md)의 Bicep 정적
Manifest 방식을 선택한다. 이 경우 Oracle Control Table 변경만으로 즉시 Pipeline이
생성되지는 않는다.

## 구성 후 확인 자료

다음 자료를 고객 환경의 구축 기록으로 남긴다.

1. Control Table 열과 샘플 행(접속 정보 제외)
2. Oracle/ADLS Linked Service Test Connection
3. 두 Parameter Dataset 설정
4. 일반/대량 테이블 Pipeline Activity와 Partition 설정
5. Function Identity와 ADF Custom Role
6. 마스킹한 Function 환경 변수 목록
7. Sync/Run Invocation 및 Application Insights 로그
8. ADF Pipeline Run ID와 Activity Output
9. Control Table의 Dispatch/Backoff 추적값

## 공식 참고자료

- [Lookup Activity 제한](https://learn.microsoft.com/azure/data-factory/control-flow-lookup-activity)
- [Metadata-driven Copy](https://learn.microsoft.com/azure/data-factory/copy-data-tool-metadata-driven)
- [ADF Source Control](https://learn.microsoft.com/azure/data-factory/source-control)
- [ADF CI/CD](https://learn.microsoft.com/azure/data-factory/continuous-integration-delivery)
