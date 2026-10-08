# Scenario 01 - Lookup Paging 기반 Threshold Dispatcher

## 1. 목적

현재 ADF Lookup Activity로 Control Table 전체를 읽는 방식을, Control Table이
5,000행 또는 4MB를 넘어도 처리할 수 있는 페이지 방식으로 변경한다.

최종 실행 흐름은 다음과 같다.

```text
공통 Schedule Trigger 하나
  -> Threshold Dispatcher
      -> Control Table 페이지 조회
      -> 테이블별 신규 행 수 확인
      -> 임계치 충족 테이블만 Claim
      -> Function이 해당 테이블 Pipeline Create/Update
      -> 해당 테이블 Pipeline만 Create Run
```

이 문서는 배포 전 구성·검증 시나리오다. 현재 Function 코드와 SQL은 아직 이
구조로 변경되지 않았으므로, 먼저 [목표 아키텍처](../../docs/target-architecture.md)를
검토하고 승인한다.

## 2. 설계 원칙

1. Schedule Trigger는 공통 Dispatcher용 한 개만 사용한다.
2. 테이블별 Trigger는 만들지 않는다.
3. Control Table은 전체 조회하지 않고 `CONTROL_ID` Keyset Paging을 사용한다.
4. Oracle 연결은 Self-hosted Integration Runtime만 사용한다.
5. Azure Function은 Oracle을 조회하지 않고 ADF Pipeline 관리 API만 호출한다.
6. 테이블별 Pipeline은 처음 실행되거나 정의가 바뀔 때 Create/Update한다.
7. 임계치 미달 행은 계속 대기한다.
8. 임계치를 충족하면 Claim 시점까지 대기 중인 신규 행 전체를 적재한다.
9. Watermark는 Pipeline 최종 성공 후에만 전진한다.

## 3. 사전 준비

- Azure Data Factory
- Oracle에 접근 가능한 Self-hosted IR Host
- 온프레미스 Oracle Source와 Control Schema
- ADLS Gen2
- Azure Function App
- Azure Key Vault
- 개발 환경에서 사용할 일반/대량/실패 테스트 테이블

배포 전에 다음 값을 합의한다.

| 설정 | 시작 권장값 | 조정 기준 |
|---|---:|---|
| Dispatcher 주기 | 5분 | 허용 지연과 Oracle 부하 |
| Lookup page size | 200 | 직렬화 크기와 Activity Payload |
| Page Worker batchCount | 10 | Oracle Session과 SHIR 처리량 |
| Claim lease | 30분 | 가장 느린 Table Pipeline 시간보다 길게 |
| 테이블 Pipeline concurrency | 1 | 동일 테이블 중복 실행 방지 보조 |

## 4. Portal에서 Self-hosted IR 구성

1. ADF Studio에서 **Manage > Integration runtimes > + New**를 선택한다.
2. **Azure, Self-Hosted**를 선택하고 **Self-Hosted**를 선택한다.
3. 이름을 `ir_onprem_oracle`로 입력한다.
4. Oracle에 네트워크로 접근 가능한 Windows Host에 Runtime을 설치한다.
5. Portal에 표시된 인증 키로 Runtime을 등록한다.
6. **Nodes**에서 상태가 **Running**인지 확인한다.
7. Host에서 Oracle 1521과 DNS 해석을 확인한다.
8. 운영에서는 최소 두 노드와 장애 전환을 검토한다.

## 5. Portal에서 Linked Service 구성

### 5.1 Oracle

1. **Manage > Linked services > + New > Oracle**을 선택한다.
2. 이름을 `ls_oracle_poc`로 입력한다.
3. **Connect via integration runtime**에서 `ir_onprem_oracle`을 선택한다.
4. Oracle Host, Port, Service Name을 입력한다.
5. 암호는 Key Vault Secret으로 참조한다.
6. Control 계정에 Control Table/Package 권한, Source 계정에 대상 테이블 조회
   권한만 부여한다.
7. **Test connection**이 성공하는지 확인한다.

### 5.2 ADLS Gen2

1. **Manage > Linked services > + New > Azure Data Lake Storage Gen2**를 선택한다.
2. 이름을 `ls_adls_gen2`로 입력한다.
3. 인증은 **Managed Identity**를 사용한다.
4. Data Factory Identity에 Container 범위의
   **Storage Blob Data Contributor**를 부여한다.
5. **Test connection**을 실행한다.

## 6. Control Table 목표 열

구현 단계에서 기존 Control Table에 다음 개념을 추가한다.

| 그룹 | 열 | 역할 |
|---|---|---|
| 식별 | `CONTROL_ID` | Paging과 상태 갱신의 불변 키 |
| Source | `SOURCE_SCHEMA`, `SOURCE_TABLE` | 원천 객체 |
| Pipeline | `PIPELINE_NAME`, `IS_ACTIVE` | 생성 이름과 활성 상태 |
| Watermark | `WATERMARK_COLUMN`, `WATERMARK_TYPE` | NUMBER 또는 TIMESTAMP |
| Tie-breaker | `TIE_BREAKER_COLUMN` | TIMESTAMP 동률 행의 숫자 PK |
| 성공 지점 | `LAST_SUCCESS_*` | 마지막 성공 적재 상한 |
| 임계치 | `MIN_NEW_ROWS` | 실행에 필요한 최소 신규 행 |
| Pending | `PENDING_*` | Claim된 고정 적재 범위 |
| Claim | `DISPATCH_STATUS`, `DISPATCH_BATCH_ID` | 상태와 중복 방지 키 |
| Lease | `LEASE_EXPIRES_AT_UTC` | 중단된 Claim 복구 |
| ADF | `LAST_ADF_RUN_ID` | 제출된 실행 추적 |
| 오류 | `FAILURE_COUNT`, `LAST_ERROR` | Backoff와 진단 |

Oracle 식별자는 허용 문자와 실제 Dictionary 존재 여부를 검증한다. Control Table에
임의 SQL 조건을 저장해 Function이나 Pipeline에서 연결하지 않는다.

## 7. ADF Pipeline 구성

### 7.1 `pl_threshold_dispatcher`

#### Parameters

- `pageSize`: 기본 200
- `maxPages`: 안전한 최대 반복 수

#### Variables

- `scanUpperControlId`
- `lastControlId`
- `currentPageCount`
- `hasMorePages`

#### Activity 순서

1. **LookupScanUpperControlId**
   - 실행 시작 시 활성 Control 행의 최대 `CONTROL_ID`를 고정한다.
2. **UntilControlPages**
   - `currentPageCount == 0` 또는 상한 도달까지 반복한다.
3. **LookupControlPage**
   - `lastControlId < CONTROL_ID <= scanUpperControlId`
   - `ORDER BY CONTROL_ID`
   - `pageSize`개만 반환한다.
4. **IfPageHasRows**
   - 결과가 있으면 자식 Pipeline을 실행한다.
5. **ExecuteCheckThresholdPage**
   - Page Array를 `pl_check_threshold_page` Parameter로 전달한다.
   - **Wait on completion**을 선택한다.
6. **SetLastControlId**
   - 페이지 마지막 행의 `CONTROL_ID`로 갱신한다.

ADF는 Until 내부에 ForEach를 직접 중첩할 수 없으므로 테이블 반복은 자식
Pipeline에 둔다.

### 7.2 `pl_check_threshold_page`

#### Parameter

- `controlRows`: 한 페이지의 Control Metadata Array

#### Activity 순서

1. **ForEachControlRow**
   - Items: `controlRows`
   - `isSequential`: false
   - `batchCount`: 기본 10
2. **CheckNewRows**
   - 마지막 성공 Watermark 이후 행을 임계치까지만 읽는다.
3. **IfThresholdReached**
   - Count가 `MIN_NEW_ROWS` 이상일 때만 다음 단계로 진행한다.
4. **ClaimAndFreezeWindow**
   - Oracle Package를 호출한다.
   - 다른 Dispatcher가 Claim했는지 다시 확인한다.
   - 현재 대기 행 전체의 상한 Watermark와 Expected Rows를 고정한다.
5. **RequestFunctionDispatch**
   - Managed Identity Web Activity로 Function을 호출한다.
   - Pipeline 정의와 Run Parameter를 전달한다.
6. **MarkSubmitted**
   - Function 응답의 ADF Run ID를 Control Table에 기록한다.
7. **MarkDispatchFailure**
   - Function 요청이 실패하면 오류를 기록하고 Lease 복구 대상으로 남긴다.

### 7.3 공통 Schedule Trigger

1. ADF Studio **Manage > Triggers > + New**를 선택한다.
2. 이름을 `trg_threshold_dispatcher`로 입력한다.
3. Type은 **Schedule**을 선택한다.
4. Time zone과 5분 시작 주기를 설정한다.
5. `pl_threshold_dispatcher` 하나만 연결한다.
6. 개발 검증 중에는 Started가 아닌 Stopped로 배포한다.
7. 수동 검증 완료 후 **Start**하고 **Publish**한다.

Trigger는 Pipeline 내부 엔터티가 아니라 별도 ADF 리소스지만 Factory ARM
Template으로 Pipeline과 함께 Export/배포할 수 있다. 배포 후에는 명시적으로
Trigger 상태를 Start해야 한다.

## 8. Function 구성 경계

ADF Web Activity는 Function App Authentication의 Entra Audience를 사용해 Managed
Identity로 호출한다. Function Key만 사용하는 Anonymous 공개 Endpoint는 목표
구성이 아니다.

Function은 다음 순서만 수행한다.

1. JSON Schema, 숫자 범위, 식별자 검증
2. 공통 Worker 템플릿으로 테이블 Pipeline Create/Update
3. `DISPATCH_BATCH_ID` 중복 확인
4. Storage Ledger에 기존 ADF Run ID가 있으면 기존 결과 반환
5. 기존 Run이 없으면 고정된 Watermark Parameter로 Create Run
6. Batch ID와 Pipeline Run ID를 Ledger에 기록하고 응답

Function에는 Oracle DSN, 사용자, 암호, Oracle Driver를 두지 않는다.

ADF Create Run과 Ledger 기록 사이에는 분산 Transaction이 없다. Create Run 성공
직후 Function이 종료되면 중복 ADF Run이 생길 수 있으므로, Worker Pipeline의 첫
Activity가 Oracle `BEGIN_BATCH_EXECUTION`을 호출해 같은 Batch ID 중 하나만 실제
Count/Copy를 수행하게 한다.

## 9. 생성되는 테이블 Pipeline

```text
AcquireBatchExecution
  -> GetIncrementalSourceCount
  -> CopyIncrementalRows
  -> ValidateExpectedAndCopiedRows
      -> Success: MarkWatermarkSuccess
      -> Failure: FailRowCountMismatch + MarkRunFailure
```

- Count와 Copy는 같은 `old < watermark <= new` 조건을 사용한다.
- TIMESTAMP는 숫자 PK Tie-breaker를 함께 사용한다.
- Claim 후 유입된 행은 고정된 `new` Watermark보다 크므로 다음 실행으로 남는다.
- Source Count, Claim Expected Rows, `rowsRead`, `rowsCopied`가 모두 일치해야 한다.
- 성공할 때만 `LAST_SUCCESS_*`를 `PENDING_*`로 이동한다.
- 실패하면 기존 성공 Watermark를 유지한다.

## 10. 배포 전 검증 시나리오

### Scenario A - Lookup 제한 이내 회귀

1. Control 행 10개를 준비한다.
2. `pageSize = 200`으로 Dispatcher를 수동 실행한다.
3. Lookup이 한 페이지를 반환하는지 확인한다.
4. 기존 Lookup 방식과 동일한 활성 테이블 목록이 처리되는지 비교한다.

기대 결과: 누락·중복 없이 10개를 한 페이지에서 확인한다.

### Scenario B - Lookup 5,000행 초과

1. 비운영 Control Table에 5,100개 이상의 가상 Metadata 행을 준비한다.
2. `pageSize = 200`으로 Dispatcher를 실행한다.
3. 페이지별 행 수가 200 이하인지 확인한다.
4. 처리된 `CONTROL_ID`의 최소·최대·개수·중복을 검증한다.

기대 결과: 단일 Lookup은 제한 이하이고 전체 행은 페이지 간 누락·중복 없이 한 번씩
스캔된다.

### Scenario C - 임계치 미달

1. 마지막 성공 Watermark 이후 900행을 준비한다.
2. `MIN_NEW_ROWS = 1000`으로 설정한다.
3. Dispatcher를 두 번 실행한다.

기대 결과: Claim, Function 호출, Create Run이 발생하지 않고 성공 Watermark도
변경되지 않는다.

### Scenario D - 임계치 충족

1. 마지막 성공 Watermark 이후 7,500행을 준비한다.
2. `MIN_NEW_ROWS = 1000`으로 설정한다.
3. Dispatcher를 실행한다.

기대 결과:

- 해당 테이블만 Claim된다.
- Pending 범위는 7,500행 전체를 포함한다.
- Function이 테이블 Pipeline을 Create/Update하고 Create Run한다.
- 성공 후 Watermark가 Claim 상한으로 이동한다.

### Scenario E - Claim 이후 신규 행 유입

1. 7,500행을 Claim한다.
2. Copy 실행 중 300행을 추가한다.
3. 첫 Pipeline 완료 후 Dispatcher를 다시 실행한다.

기대 결과: 첫 실행은 7,500행만 적재하고 300행은 다음 임계치 판단 대상으로 남는다.

### Scenario F - 동일 테이블 중복 Dispatcher

1. 같은 Dispatcher를 짧은 간격으로 두 번 시작한다.
2. 같은 Control 행의 Claim과 Function 호출을 확인한다.

기대 결과: 정상 재시도에서는 Storage Ledger 때문에 하나의 ADF Run만 생성된다.
Create Run 직후 장애를 주입해 중복 Run이 생기더라도 Oracle Batch 실행 잠금 때문에
실제 Count/Copy는 하나만 수행된다.

### Scenario G - Create Run 요청 실패

1. Function의 ADF 권한을 테스트 범위에서 임시 제거한다.
2. 임계치 충족 테이블을 실행한다.
3. 권한 복구 후 Lease/Backoff 정책에 따라 재실행한다.

기대 결과: 성공 Watermark는 이동하지 않으며 오류와 Failure Count가 기록되고,
복구 후 같은 Pending 범위를 재제출한다.

### Scenario H - Copy 또는 Count 불일치

1. 테스트 Pipeline에서 Expected Rows와 다른 결과를 만들도록 구성한다.
2. Pipeline 실패와 Control 상태를 확인한다.

기대 결과: `LAST_SUCCESS_*`가 변경되지 않고 재실행 가능한 Pending 범위가 남는다.

### Scenario I - NUMBER Watermark

1. 증가형 숫자 PK 테이블을 등록한다.
2. 경계값과 그 사이 행을 추가한다.
3. Count와 Copy 조건이 `old < key <= new`인지 확인한다.

기대 결과: 경계 중복·누락이 없다.

### Scenario J - TIMESTAMP + PK Watermark

1. 같은 수정일시를 가진 여러 행을 서로 다른 숫자 PK로 준비한다.
2. 두 번에 걸쳐 적재한다.

기대 결과: Timestamp 동률 행이 PK Tie-breaker로 모두 한 번씩 적재된다.

## 11. 배포 후 수집할 증적

1. Self-hosted IR Node 상태와 Oracle Linked Service Test Connection
2. Dispatcher Run ID와 페이지별 Lookup 행 수
3. 5,000행 초과 테스트의 전체 Control ID 대조 결과
4. 임계치 미달 시 Function 미호출 증적
5. Claim된 old/new Watermark와 Expected Rows
6. Function 요청 Correlation ID와 ADF Run ID
7. Count, rowsRead, rowsCopied 비교
8. 성공/실패 후 Control Table 상태
9. 중복 Dispatcher에서 단일 Run이 생성된 결과
10. Azure Monitor Alert와 이메일 수신 결과

## 12. 다음 단계

이 문서 승인 후 다음 순서로 구현한다.

1. Control Table과 Oracle Dispatch Package
2. Function HTTP Endpoint와 Watermark Pipeline Factory
3. Page Worker와 Dispatcher ADF JSON
4. Schedule Trigger JSON
5. 단위/정적 테스트
6. 개발 환경 통합 테스트
7. [Scenario 02 알림](../02-email-alerting/README.md) 연계 검증

## 공식 참고자료

- [Lookup Activity 제한](https://learn.microsoft.com/azure/data-factory/control-flow-lookup-activity#supported-capabilities)
- [ForEach 제한](https://learn.microsoft.com/azure/data-factory/control-flow-for-each-activity#limitations-and-workarounds)
- [Script Activity](https://learn.microsoft.com/azure/data-factory/transform-data-using-script)
- [Self-hosted Integration Runtime](https://learn.microsoft.com/azure/data-factory/create-self-hosted-integration-runtime)
- [Oracle Connector](https://learn.microsoft.com/azure/data-factory/connector-oracle)
- [Web Activity](https://learn.microsoft.com/azure/data-factory/control-flow-web-activity)
