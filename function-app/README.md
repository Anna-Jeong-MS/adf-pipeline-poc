# ADF Pipeline Manager Function

> **Legacy PoC 구현:** 이 코드는 Timer와 Oracle Cursor를 사용하는 이전 구조다.
> 현재 목표인 공통 Schedule Trigger, Lookup Keyset Paging, Self-hosted IR,
> Watermark Threshold Dispatcher는 아직 구현되지 않았다. 배포 전에
> [목표 아키텍처](../docs/target-architecture.md)와
> [Scenario 01](../scenarios/01-metadata-pipeline/README.md)을 검토한다.

Python v2 Azure Function으로 Oracle Control Table에서 테이블 설정을 읽어 ADF
Pipeline을 생성/갱신하고 실행을 요청한다.

## Functions

| Function | Trigger | 역할 |
|---|---|---|
| `sync_table_pipelines` | Timer | 테이블별 Pipeline Create/Update |
| `run_table_pipelines` | Timer | 테이블별 Pipeline Create Run 요청 |

Pipeline 동기화는 ADF Lookup 대신 Oracle Cursor `fetchmany`를 사용하므로 Lookup의
5,000행/4MB 제한을 받지 않는다. 실행 Timer는 예정된 행만 `SKIP LOCKED`로 Claim한다.

## 인증

`DefaultAzureCredential`을 사용한다. Azure 배포 환경에서는 Function System-assigned
Managed Identity가 사용된다. Identity에는 대상 Factory 범위의 최소 권한을
부여한다.

## Oracle 연결

`python-oracledb` Thin mode를 사용하므로 Oracle Client 설치가 필요 없다.
`ORACLE_PASSWORD`는 Azure App Setting의 Key Vault Reference로 구성한다.

## 로컬 테스트

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest
pytest
```

## 주의 사항

샘플 `run_table_pipelines`는 `NEXT_RUN_AT_UTC`가 지난 행을 최대 설정 개수만큼
Claim해 Create Run을 요청한다. 기록하는 Run ID는 요청 접수 결과이며 Pipeline
최종 성공을 의미하지 않는다. Pipeline 완료 기반 재처리와 전역 동시성 제어가
필요하면 Durable Functions 및 Dispatch 상태/Lease를 추가한다.
