# Target Architecture - Lookup Paging Threshold Dispatcher

## 문서 상태

- 상태: 배포 전 설계안
- 대상: 온프레미스 Oracle 수백 개 테이블
- 현재 단계: 아키텍처와 검증 시나리오 검토
- 아직 수행하지 않은 작업: SQL, ADF JSON, Function 코드 변경과 Azure 배포

현재 저장소의 Function Timer/Oracle Cursor 구현은 이전 PoC 기준이다. 이 문서의
설계가 승인되기 전에는 해당 구현을 목표 환경에 배포하지 않는다.

## 1. 요구사항

1. 공통 ADF Schedule Trigger 하나만 사용한다.
2. Trigger는 Threshold Dispatcher Pipeline을 실행한다.
3. Dispatcher는 Oracle Control Table을 기준으로 테이블별 신규 행 수를 확인한다.
4. 마지막 성공 Watermark 이후 신규 행 수가 테이블별 임계치 이상인 경우에만
   해당 테이블을 Claim한다.
5. Claim된 테이블의 독립 Pipeline만 Create Run한다.
6. 임계치 미달 데이터는 최대 대기시간 없이 다음 Schedule까지 계속 대기한다.
7. 임계치를 충족하면 Claim 시점까지 대기 중인 신규 행 전체를 적재한다.
8. Oracle Control Table과 Source Table 접근은 Self-hosted Integration Runtime을
   사용한다.
9. 테이블별 Pipeline Create/Update와 Create Run 요청은 Azure Function이 담당한다.

## 2. 현재 방식과 변경 이유

### 현재 방식

```text
ADF Pipeline
  -> Lookup Control Table 전체 조회
  -> ForEach
  -> 테이블별 처리
```

Lookup은 한 번에 최대 5,000행, 출력 4MB까지 반환한다. 현재 Control Table이 이
범위 안이면 문제가 없지만, 향후 테이블 수와 메타데이터 열이 증가하면 일부 행만
반환되거나 Activity가 실패할 수 있다.

### 목표 방식

Lookup을 제거하지 않고, 한 Activity가 전체 Control Table을 읽지 않도록
`CONTROL_ID` 기반 Keyset Paging을 적용한다. 각 페이지는 제한보다 충분히 작은
크기로 조회하고, 페이지 처리용 자식 Pipeline에서 테이블별 임계치를 확인한다.

Offset Paging은 앞 페이지의 행 추가·삭제에 따라 중복 또는 누락될 수 있으므로
사용하지 않는다. Dispatcher 시작 시점의 상한 `MAX(CONTROL_ID)`를 고정해 실행
도중 추가된 Control 행은 다음 Schedule에서 처리한다.

## 3. 목표 아키텍처

```text
ADF Schedule Trigger (1개)
  |
  v
pl_threshold_dispatcher
  |
  +-- Initialize scanUpperControlId, lastControlId, pageSize
  |
  +-- Until: 현재 페이지가 비거나 상한에 도달할 때까지
        |
        +-- LookupControlPage
        |     Oracle Control Table via Self-hosted IR
        |     CONTROL_ID > lastControlId
        |     CONTROL_ID <= scanUpperControlId
        |     ORDER BY CONTROL_ID
        |     FETCH NEXT pageSize
        |
        +-- Execute pl_check_threshold_page
        |     |
        |     +-- ForEach(table metadata, limited batchCount)
        |           |
        |           +-- CheckNewRows
        |           |     last successful Watermark 이후
        |           |     임계치까지만 존재 여부 확인
        |           |
        |           +-- If threshold reached
        |                 |
        |                 +-- ClaimAndFreezeWindow
        |                 |     Lease 획득
        |                 |     old/new Watermark 고정
        |                 |     현재 대기 중 신규 행 전체 범위 고정
        |                 |
        |                 +-- Web Activity with Managed Identity
        |                 |     -> Azure Function HTTP endpoint
        |                 |          -> Pipeline Create/Update
        |                 |          -> Pipeline Create Run
        |                 |
        |                 +-- MarkSubmitted(runId)
        |
        +-- Set lastControlId to page last CONTROL_ID

Generated table Pipeline
  |
  +-- GetIncrementalSourceCount
  +-- CopyIncrementalRows via Self-hosted IR
  +-- Validate source count, expected count, rowsCopied
  +-- Success: Commit Watermark
  +-- Failure: Keep old Watermark and record failure
```

ADF는 Until 안에 ForEach를 직접 중첩할 수 없다. 따라서 상위 Dispatcher의 Until은
페이지 조회와 `Execute Pipeline`만 담당하고, 자식 `pl_check_threshold_page`가
ForEach를 담당한다.

## 4. 구성 요소별 책임

| 구성 요소 | 책임 | 하지 않는 일 |
|---|---|---|
| Schedule Trigger | Dispatcher를 일정 주기로 시작 | 테이블별 Trigger 생성 |
| Threshold Dispatcher | Control Table 페이지 순회와 실행 흐름 제어 | 원천 데이터 Copy |
| Page Worker Pipeline | 테이블별 임계치 확인과 Claim | ADF 리소스 관리 API 직접 호출 |
| Self-hosted IR | 온프레미스 Oracle 연결 | ADF Pipeline 생성 |
| Oracle Control Package | 원자적 Claim, Lease, Watermark 상태 변경 | ADF Create Run |
| Azure Function | 테이블 Pipeline Create/Update와 Create Run | Oracle 직접 연결 |
| Function Storage Ledger | Batch ID와 ADF Run ID의 멱등성 기록 | Oracle Watermark 관리 |
| Table Pipeline | 고정된 Watermark 범위 Copy와 검증 | 다음 대상 테이블 선택 |
| Azure Monitor | Pipeline 실패와 실행 상태 알림 | Watermark 변경 |

## 5. Control Table Paging 설계

### 5.1 페이지 조회 조건

개념 쿼리:

```sql
select *
from (
  select
    CONTROL_ID,
    SOURCE_SCHEMA,
    SOURCE_TABLE,
    PIPELINE_NAME,
    WATERMARK_COLUMN,
    WATERMARK_TYPE,
    TIE_BREAKER_COLUMN,
    MIN_NEW_ROWS,
    LAST_SUCCESS_WATERMARK,
    LAST_SUCCESS_TIE_BREAKER,
    PARALLEL_COPIES
  from ADF_CONTROL_TABLE
  where IS_ACTIVE = 'Y'
    and CONTROL_ID > :last_control_id
    and CONTROL_ID <= :scan_upper_control_id
  order by CONTROL_ID
)
where rownum <= :page_size;
```

- 기본 `pageSize`: 200
- 허용 최대값: 1,000
- 페이지별 결과는 5,000행/4MB보다 작아야 한다.
- Execute Pipeline이나 Web Activity에 전달하는 JSON도 Activity Payload 한도를
  넘지 않도록 필요한 열만 조회한다.
- 페이지의 직렬화 크기가 기준을 넘으면 행 수가 아니라 Payload 크기를 기준으로
  `pageSize`를 낮춘다.

### 5.2 페이지 종료 조건

다음 중 하나이면 반복을 종료한다.

1. Lookup 결과가 0행이다.
2. 마지막 `CONTROL_ID`가 `scanUpperControlId` 이상이다.
3. 안전을 위한 최대 페이지 반복 횟수에 도달했다. 이 경우 성공으로 처리하지 않고
   명시적으로 실패시켜 설정 오류를 노출한다.

## 6. 신규 행 수와 Watermark 설계

### 6.1 임계치 확인

임계치 확인 단계에서는 매번 전체 신규 행을 `COUNT(*)`하지 않는다. Watermark
인덱스를 사용해 최대 `MIN_NEW_ROWS`개까지만 읽고, 그 수가 임계치와 같으면
적재 후보로 판단한다.

```sql
select count(*) as THRESHOLD_COUNT
from (
  select 1
  from <validated schema>.<validated table>
  where <watermark predicate after last success>
    and rownum <= :min_new_rows
);
```

Claim Procedure는 경쟁 상태를 방지하기 위해 같은 조건을 다시 확인한다.

### 6.2 Claim 시 고정할 값

임계치를 충족하면 다음 값을 하나의 Oracle Transaction에서 고정한다.

- `DISPATCH_BATCH_ID`
- `PENDING_WATERMARK_FROM`
- `PENDING_WATERMARK_TO`
- `PENDING_TIE_BREAKER_FROM`
- `PENDING_TIE_BREAKER_TO`
- `PENDING_EXPECTED_ROWS`
- `LEASE_EXPIRES_AT_UTC`
- `DISPATCH_STATUS = 'CLAIMED'`

Claim 시점까지 대기 중인 신규 행이 7,500행이고 임계치가 1,000행이면,
`PENDING_EXPECTED_ROWS`는 7,500행이 되며 한 번의 Pipeline 실행에서 이 범위
전체를 적재한다. Claim 이후 들어온 행은 고정된 상한 Watermark 밖에 있으므로
다음 Schedule에서 처리한다.

### 6.3 지원 Watermark

| 유형 | 조건 | 주의사항 |
|---|---|---|
| NUMBER | `old < key AND key <= new` | 증가형 PK/Sequence 권장 |
| TIMESTAMP + NUMBER | Timestamp와 숫자 PK의 사전식 범위 | 같은 Timestamp 중복 누락 방지 |

NUMBER Watermark는 Insert 탐지에 적합하지만 기존 행 Update와 Delete를 탐지하지
않는다. Update가 요구되면 수정일시 + PK를 사용하고, Delete 전파가 필요하면
Soft Delete 또는 CDC를 별도 설계한다.

## 7. Claim과 중복 방지

1. 같은 `CONTROL_ID`가 `CLAIMED` 또는 `SUBMITTED`이고 Lease가 유효하면 재Claim하지
   않는다.
2. Lease가 만료된 Claim은 ADF Run 존재 여부를 확인한 뒤 복구한다.
3. Function 요청에는 `DISPATCH_BATCH_ID`를 Idempotency Key로 전달한다.
4. Function은 Azure Storage Ledger에서 같은 Batch ID와 ADF Run ID를 찾으면 새
   Run을 만들지 않고 기존 결과를 반환한다.
5. ADF Create Run과 Storage Ledger 기록은 하나의 Transaction으로 묶을 수 없다.
   Create Run 성공 직후 Function이 종료되는 희귀 구간에는 중복 ADF Run이 생길
   수 있으므로, Worker 시작 시 Oracle `BEGIN_BATCH_EXECUTION`을 원자적으로 호출해
   하나의 Run만 실제 Count/Copy를 수행하게 한다.
6. 생성되는 테이블 Pipeline에는 `concurrency: 1`을 적용하지만, 이것만으로 중복
   제출이 제거되지는 않으므로 Batch ID 중복 방지가 별도로 필요하다.

## 8. Function 경계

Function은 ADF Web Activity가 보내는 검증된 JSON만 처리한다.

```json
{
  "controlId": 1001,
  "dispatchBatchId": "00000000-0000-0000-0000-000000000000",
  "pipelineName": "pl_copy_gs_poc_gs_projects",
  "sourceSchema": "GS_POC",
  "sourceTable": "GS_PROJECTS",
  "watermarkType": "NUMBER",
  "watermarkColumn": "PROJECT_ID",
  "watermarkFrom": "1000",
  "watermarkTo": "8500",
  "expectedRows": 7500
}
```

Function의 책임:

1. Payload Schema와 Oracle 식별자를 검증한다.
2. 공통 템플릿으로 테이블 Pipeline을 Create/Update한다.
3. Storage Ledger에서 Batch ID의 기존 Run을 조회한다.
4. 기존 Run이 없으면 고정된 Watermark와 Batch ID로 Create Run한다.
5. Batch ID와 ADF Run ID를 Ledger에 기록하고 응답한다.

Function은 Oracle Driver, Oracle 암호, Oracle 네트워크 경로를 갖지 않는다.
Function App Authentication으로 Entra 인증을 강제하고 ADF Web Activity는
Managed Identity로 호출한다.

## 9. 생성되는 테이블 Pipeline

테이블 Pipeline은 다음 Parameter를 받는다.

- `controlId`
- `dispatchBatchId`
- `watermarkFrom`, `watermarkTo`
- `tieBreakerFrom`, `tieBreakerTo`
- `expectedRows`

Count와 Copy는 반드시 같은 Watermark 조건을 사용한다. Copy 성공 후 다음 세 값이
모두 일치해야 Watermark를 확정한다.

```text
AcquireBatchExecution == acquired
GetIncrementalSourceCount == expectedRows
CopyIncrementalRows.rowsRead == expectedRows
CopyIncrementalRows.rowsCopied == expectedRows
```

같은 Batch ID의 중복 ADF Run은 `AcquireBatchExecution`에서 실제 작업 권한을 얻지
못하면 Count/Copy를 실행하지 않고 중복 상태로 종료한다.

성공 시 Oracle Script Activity가 Self-hosted IR을 통해 `MARK_SUCCESS`를 호출한다.
실패 시 `LAST_SUCCESS_*`는 변경하지 않고 Pending 범위와 오류를 남겨 재실행할 수
있게 한다.

## 10. Portal 구성 순서

1. Self-hosted IR을 Oracle 접근 가능 Host에 설치하고 ADF에 등록한다.
2. Oracle Linked Service가 Self-hosted IR을 사용하도록 설정한다.
3. Control Table과 Dispatch Package의 최소 권한 계정을 구성한다.
4. ADLS Linked Service와 Parameter Dataset을 만든다.
5. Function App의 Managed Identity와 ADF 관리 권한을 구성한다.
6. Function의 Storage Ledger와 최소 데이터 권한을 구성한다.
7. Function App Authentication에서 Entra 인증을 필수로 설정한다.
8. ADF Managed Identity가 Function을 호출할 수 있도록 App Role을 부여한다.
9. `pl_check_threshold_page`를 먼저 배포한다.
10. `pl_threshold_dispatcher`를 배포한다.
11. 공통 Schedule Trigger 하나를 Dispatcher에 연결하고 명시적으로 Start한다.
12. 소수 테스트 테이블로 임계치 미달·충족·실패 복구를 검증한다.
13. Paging과 병렬 수를 단계적으로 높인다.

## 11. 배포 전 승인 기준

- [ ] 전체 Control Table Lookup을 Paging으로 변경하는 데 동의
- [ ] 기본 `pageSize`와 Page Worker `batchCount` 확정
- [ ] NUMBER 및 TIMESTAMP + NUMBER Watermark 지원 범위 확정
- [ ] 임계치 충족 시 현재 대기 행 전체 적재 정책 승인
- [ ] Claim Lease 시간과 만료 복구 정책 확정
- [ ] Function Storage Ledger와 Worker Batch 실행 잠금 승인
- [ ] Function의 Oracle 직접 연결 제거 승인
- [ ] 공통 Schedule Trigger 한 개 사용 승인
- [ ] 실제 Azure/Oracle 통합 시험 전까지 설계 상태임을 확인

## 공식 참고자료

- [Lookup Activity 제한](https://learn.microsoft.com/azure/data-factory/control-flow-lookup-activity#supported-capabilities)
- [ForEach 제한과 중첩 우회](https://learn.microsoft.com/azure/data-factory/control-flow-for-each-activity#limitations-and-workarounds)
- [Script Activity 출력 제한](https://learn.microsoft.com/azure/data-factory/transform-data-using-script#activity-output)
- [ADF 서비스 제한](https://learn.microsoft.com/azure/azure-resource-manager/management/azure-subscription-service-limits#azure-data-factory-limits)
- [Oracle Connector](https://learn.microsoft.com/azure/data-factory/connector-oracle)
- [Pipeline Create or Update API](https://learn.microsoft.com/rest/api/datafactory/pipelines/create-or-update)
- [Pipeline Create Run API](https://learn.microsoft.com/rest/api/datafactory/pipelines/create-run)
