# Table-specific Oracle to ADLS ADF Pipeline Automation

온프레미스 Oracle Control Table을 기준으로 테이블별 신규 행 수를 확인하고,
임계치를 충족한 테이블의 독립 ADF Pipeline만 생성·실행하는 PoC 저장소입니다.

현재는 **배포 전 목표 아키텍처와 시나리오를 검토하는 단계**입니다. 저장소의
Function Timer/Oracle Cursor 코드는 이전 PoC 구현이며 목표 구조로 아직 변경되지
않았습니다. 설계 승인 전에는 기존 코드를 목표 환경에 배포하지 않습니다.

대량 테이블은 Pipeline을 분리하는 것에 더해 Oracle `DynamicRange` 또는 물리
파티션 병렬 Copy와 `parallelCopies`를 테이블별로 설정합니다.

## 목표 아키텍처

```text
공통 ADF Schedule Trigger
  -> Threshold Dispatcher
      -> Control Table Keyset Paging via Self-hosted IR
      -> 테이블별 신규 행 수 확인
      -> 임계치 충족 테이블만 Claim
      -> Azure Function
          -> 테이블 Pipeline Create/Update
          -> 해당 Pipeline Create Run
              -> 고정 Watermark 범위 Count/Copy/검증
              -> 성공 시 Watermark 확정
```

Lookup은 한 번에 최대 5,000행/4MB까지만 반환하므로 Control Table 전체를 한 번에
읽지 않습니다. 공통 Schedule Trigger 하나가 Dispatcher를 시작하고,
`CONTROL_ID` Keyset Paging으로 제한 이하의 Metadata Page를 순회합니다.

상세 설계: [Lookup Paging Threshold Dispatcher 목표 아키텍처](docs/target-architecture.md)

## 변경되는 자동화 경계

1. ADF와 Self-hosted IR이 Oracle Control/Source Data Plane을 담당합니다.
2. Function은 Oracle에 직접 연결하지 않고 ADF Pipeline 관리 API만 호출합니다.
3. 테이블별 Trigger 대신 공통 Schedule Trigger 하나를 사용합니다.
4. Watermark와 Claim Lease로 임계치 판단, 중복 방지, 실패 복구를 제어합니다.

이 저장소에는 인프라 배포 코드를 포함하지 않습니다. Function App, Data Factory,
Linked Service, Dataset, Key Vault 및 네트워크는 고객 환경의 표준 방식으로
준비해야 합니다.

## 저장소 구성

```text
function-app/            Python v2 Azure Function 샘플과 단위 테스트
database/                Control Table 확장 SQL
docs/                    자동화 옵션과 운영 고려사항
scenarios/               고객이 따라 할 수 있는 시나리오별 구성 가이드
```

## PoC 시나리오

1. [메타데이터 기반 Pipeline 생성·실행 요청 자동화](scenarios/01-metadata-pipeline/README.md)
2. [Azure Portal Pipeline 이메일 알림 구성](scenarios/02-email-alerting/README.md)

## 현재 구현 주의사항

현재 [Function 샘플](function-app/)과
[Control Table 확장 SQL](database/control-table-extension.sql)은 이전 Timer/Cursor
구조입니다. 목표 구조의 SQL, HTTP Function, Dispatcher JSON, Schedule Trigger
JSON은 아키텍처 승인 후 변경합니다.

## 대용량 테이블 설정

| Control Table 열 | 역할 |
|---|---|
| `PIPELINE_NAME` | 테이블별 ADF Pipeline 이름 |
| `PARTITION_OPTION` | `None`, `DynamicRange`, `PhysicalPartitionsOfTable` |
| `PARTITION_COLUMN` | Dynamic Range 정수형 분할 열 |
| `PARTITION_LOWER_BOUND` | 분할 최솟값 |
| `PARTITION_UPPER_BOUND` | 분할 최댓값 |
| `PARALLEL_COPIES` | 한 테이블 내부 병렬 Copy 수 |

Pipeline마다 `concurrency: 1`을 적용해 동일 테이블의 동시 실행을 제한합니다.
중복 Create Run 요청 자체를 제거하는 것은 아니며 추가 요청은 대기할 수 있습니다.
수백 개 Pipeline을 동시에 시작하지 않도록 실행 그룹 또는 Durable Functions
배치 오케스트레이션을 운영 설계에 추가해야 합니다.

Control Table이 Lookup 제한을 넘을 수 있으므로 목표 구조에서는 Lookup을
`CONTROL_ID` 기준으로 페이지 처리합니다. 한 Page는 기본 200행이며, 페이지
처리는 자식 Pipeline으로 분리해 ADF의 Until/ForEach 중첩 제한을 피합니다.

## 제공 범위

기존 PoC에서 확인한 결과:

- Oracle Control Table 기반 6개 테이블 적재
- 원천 Count와 `rowsCopied` 일치
- 불일치 시 `ROW_COUNT_MISMATCH`
- Azure Monitor 성공/실패 Alert 발화

이 저장소는 먼저 목표 아키텍처와 Portal 구성·검증 시나리오를 제공합니다.
설계 승인 후 코드와 템플릿을 구현하고, 로컬 단위/정적 테스트와 고객 개발 환경의
Self-hosted IR/Oracle/ADF 통합 검증을 구분해 결과를 기록합니다.
