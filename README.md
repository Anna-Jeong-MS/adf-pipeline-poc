# Table-specific Oracle to ADLS ADF Pipeline Automation

Oracle Control Table을 기준으로 공통 Pipeline 템플릿을 **테이블마다 독립적인
Azure Data Factory Pipeline으로 자동 생성하고 실행**하는 Azure Function
샘플입니다.

대량 테이블은 Pipeline을 분리하는 것에 더해 Oracle `DynamicRange` 또는 물리
파티션 병렬 Copy와 `parallelCopies`를 테이블별로 설정합니다.

## 목표 아키텍처

```text
Oracle ADF_CONTROL_TABLE
        |
        +--> Pipeline Sync Timer Function
        |      +--> PUT pl_copy_<schema>_<table>
        |
        +--> Pipeline Run Timer Function
               +--> POST <pipeline>/createRun
                       |
                       +--> GetSourceCount
                       +--> partitioned CopyToAdls
                       +--> ValidateRowCount
```

PowerShell이나 ADF Schedule Trigger는 실행 스케줄에 사용하지 않습니다. Azure
Function Timer Trigger가 Pipeline 정의 동기화와 실행을 담당합니다.

## 자동화 옵션

두 방안을 모두 문서화했습니다.

1. **Function이 Pipeline 생성/갱신과 실행을 모두 담당**
   - 실행 샘플: [function-app/](function-app/)
   - 테이블 변경이 잦을 때 권장
2. **Bicep이 Pipeline을 생성하고 Function은 실행만 담당**
   - 개념 및 제약: [Function 자동화 옵션](docs/function-automation-options.md)
   - Git 승인과 IaC 변경 통제가 필수일 때 권장

이 저장소에는 인프라 배포 코드를 포함하지 않습니다. Function App, Data Factory,
Linked Service, Dataset, Key Vault 및 네트워크는 고객 환경의 표준 방식으로
준비해야 합니다.

## 저장소 구성

```text
function-app/            Python v2 Azure Function 샘플과 단위 테스트
database/                Control Table 확장 SQL
docs/                    자동화 옵션 및 PoC 검증 범위
scenarios/               고객이 따라 할 수 있는 시나리오별 구성 가이드
```

## PoC 시나리오

1. [메타데이터 기반 공통 Pipeline 설계 및 전체 자동화](scenarios/01-metadata-pipeline/README.md)
2. [Azure Portal Pipeline 이메일 알림 구성](scenarios/02-email-alerting/README.md)

미팅의 추가 검토사항 8개 답변과 이번 범위는
[PoC 검토사항 답변 및 범위](docs/poc-scope-validation.md)에 정리했습니다.

## Function 샘플 실행

### 필수 조건

- Python 3.10 이상
- Azure Functions Core Tools v4
- 기존 Azure Data Factory
- `ds_oracle_dynamic`, `ds_adls_parquet_dynamic`
- Oracle Control Table 접속 정보
- Function Managed Identity의 ADF Pipeline 관리 및 실행 권한

### Control Table 확장

[control-table-extension.sql](database/control-table-extension.sql)을 검토한 뒤 Oracle에
적용합니다. 기존 테이블은 백업하고 개발 환경에서 먼저 검증하십시오.

### 로컬 테스트

```powershell
cd function-app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest
pytest
```

로컬 실행 설정은 `local.settings.example.json`을 복사해 사용하되
`local.settings.json`은 커밋하지 않습니다.

```powershell
Copy-Item local.settings.example.json local.settings.json
func start
```

Azure에서는 `ORACLE_PASSWORD` App Setting에 Key Vault Reference를 사용합니다.

## 대용량 테이블 설정

| Control Table 열 | 역할 |
|---|---|
| `PIPELINE_NAME` | 테이블별 ADF Pipeline 이름 |
| `PARTITION_OPTION` | `None`, `DynamicRange`, `PhysicalPartitionsOfTable` |
| `PARTITION_COLUMN` | Dynamic Range 정수형 분할 열 |
| `PARTITION_LOWER_BOUND` | 분할 최솟값 |
| `PARTITION_UPPER_BOUND` | 분할 최댓값 |
| `PARALLEL_COPIES` | 한 테이블 내부 병렬 Copy 수 |

Pipeline마다 `concurrency: 1`을 적용해 동일 테이블의 중복 실행을 막습니다.
수백 개 Pipeline을 동시에 시작하지 않도록 실행 그룹 또는 Durable Functions
배치 오케스트레이션을 운영 설계에 추가해야 합니다.

Control Table이 ADF Lookup의 5,000행/4MB 제한을 넘더라도 Function은 ADF Lookup을
사용하지 않습니다. Oracle Cursor를 기본 100행씩 읽어 Pipeline을 동기화하고,
실행 시에는 예정된 행만 제한적으로 Claim합니다.

## 제공 범위

기존 PoC에서 확인한 결과:

- Oracle Control Table 기반 6개 테이블 적재
- 원천 Count와 `rowsCopied` 일치
- 불일치 시 `ROW_COUNT_MISMATCH`
- Azure Monitor 성공/실패 Alert 발화

이 저장소는 Portal, SQL, Pipeline 구조, Function 설정, KQL 샘플을 포함한 고객
구성 가이드를 제공합니다. 새로운 Function 기반 테이블별 Pipeline 생성과 대용량
Partition Copy는 코드 및 단위 테스트까지 제공하며, 고객 환경에서는 시나리오의
확인 절차에 따라 통합 실행과 증적을 별도로 남겨야 합니다.
