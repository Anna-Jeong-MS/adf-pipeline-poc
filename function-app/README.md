# ADF Pipeline Manager Function

Python v2 Azure Function으로 Oracle Control Table에서 테이블 설정을 읽어 ADF
Pipeline을 생성/갱신하고 실행한다.

## Functions

| Function | Trigger | 역할 |
|---|---|---|
| `sync_table_pipelines` | Timer | 테이블별 Pipeline Create/Update |
| `run_table_pipelines` | Timer | 테이블별 Pipeline Create Run |

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
Claim해 여러 Timer 주기에 걸쳐 빠짐없이 제출한다. 다만 Pipeline 완료까지 기다려
전역 동시성을 제어하지는 않는다. 수백 개 대용량 테이블 운영에서는 Durable
Functions로 Batch 완료를 기다린 후 다음 Batch를 시작하도록 확장한다.
