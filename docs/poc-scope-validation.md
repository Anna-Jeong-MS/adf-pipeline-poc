# PoC 검토사항 답변 및 범위

미팅에서 정리한 추가 검토사항 8개에 대한 답변과 이번 PoC에서 고객이 직접
구성할 범위를 정리한다. 이번 문서는 가능/불가능 판정보다 권장 설계와 적용 범위를
명확히 하는 데 목적이 있다.

## 추가 검토사항 8개 답변

### 1. 메타데이터 기반 공통 파이프라인 설계 여부

가능하다. Oracle Control Table을 Source of Truth로 두고 공통 Copy/검증 활동
템플릿을 모든 테이블에 적용한다. 이 PoC는 대량 테이블의 독립 실행 요구를 위해
공통 템플릿으로 테이블별 Pipeline 리소스를 자동 생성하는 구조를 사용한다.

### 2. ADF Lookup 제한을 고려한 페이징/배치 처리 구조

Lookup은 최대 5,000행, 출력 4MB 제한이 있으므로 전체 Control Table을 한 번에
읽는 운영 구조에는 적합하지 않다. 소규모는 Lookup + ForEach를 사용할 수 있고,
운영 규모는 Function이 Oracle Cursor `fetchmany`로 나눠 읽고 실행 대상만
`FOR UPDATE SKIP LOCKED`로 Claim한다.

### 3. 대량 테이블 Row Count 조회 및 Oracle 통계정보 갱신 정책

`COUNT(*)`는 정확하지만 대량 테이블의 Full Scan과 일관성 시점 차이로 부하와
오탐 가능성이 있다. PoC에서는 정확 Count와 ADF `rowsCopied`를 비교하되, 운영은
Partition/Watermark 단위 Audit Count를 우선하고 `DBMS_STATS` 갱신 주기는 DBA
정책과 적재 패턴에 맞춰 별도로 정한다.

### 4. Git Mode, Live Mode, Publish, Trigger 배포 운영 기준

사람이 수정하는 ADF 리소스는 개발 Factory의 Git Mode에서 Branch/PR/Publish 후
환경별 ARM 배포로 승격한다. SDK나 Function이 만든 Live 리소스는 Git에 자동
반영되지 않으므로, 이 PoC의 자동 생성 Pipeline은 Control Table과 Function 로그를
변경 이력으로 관리하고 Timer Function을 실행 스케줄의 단일 주체로 사용한다.

### 5. Snowflake와 Fabric 간 역할 분담 및 비용 비교

기존 Snowflake는 중앙 저장·Compute 역할을 유지하고 Fabric은 Power BI, OneLake,
Data Factory 등 Microsoft 분석 워크로드가 주는 통합 이점이 있을 때 후보가 된다.
기능 중복, 데이터 이동, Capacity와 Snowflake Credit을 동일 워크로드로 측정하기
전에는 전환 결론을 내리지 않으며 이번 ADF PoC 범위에서는 제외한다.

### 6. Power BI Semantic Model/대시보드 데이터 재활용 방식

시각화 화면을 스크래핑하지 말고 원천 Curated Data를 재사용하는 것이 우선이다.
Semantic Model 결과가 꼭 필요하면 XMLA Endpoint나 Execute Queries REST API를
사용하고, Export Data는 제한적인 사용자 추출 용도로만 사용한다.

### 7. SharePoint/문서 비정형 데이터의 AI Search 또는 Foundry 연계

SharePoint List 같은 구조화 데이터는 ADF/Fabric Connector로 적재할 수 있다.
문서·이미지는 Document Intelligence로 추출한 뒤 AI Search에서 인덱싱·벡터화하고,
Foundry Agent가 해당 Search Index를 Grounding Source로 사용하는 구조가 적합하다.

### 8. Managed Identity, Key Vault, 네트워크, 방화벽, 권한 모델

Function과 ADF는 Managed Identity를 사용하고 암호는 Key Vault Reference로
주입한다. Oracle은 Private IP/VNet 경로로 연결하며 공인 1521 개방을 피하고,
Factory Pipeline Read/Write/Create Run과 Oracle Control Table 권한만 최소 부여한다.

## 이번 PoC 시나리오

### 시나리오 1. 메타데이터 기반 공통 Pipeline 설계 및 전체 자동화

- 1.1 Control Table, 공통 Pipeline 템플릿, 대량 테이블 Partition Copy 설계
- 1.2 Function Timer를 이용한 테이블별 Pipeline 생성·갱신·실행 자동화
- Lookup을 사용하는 소규모 방식과 제한을 회피하는 운영 방식을 함께 설명
- Portal 설정, SQL, Pipeline JSON 구조, Function 설정과 확인 절차 제공

상세 가이드: [Scenario 01](../scenarios/01-metadata-pipeline/README.md)

### 시나리오 2. Pipeline 성공/실패 이메일 알림

- Azure Monitor Action Group과 이메일 수신자 구성
- Factory 전체 성공/실패 Metric Alert 구성
- 수백 개 테이블별 Pipeline을 위한 Diagnostic Settings와 Log Alert 구성
- Portal 단계, KQL 샘플, 테스트 및 수집할 증적 제공

상세 가이드: [Scenario 02](../scenarios/02-email-alerting/README.md)

## 이번 PoC에서 직접 구성하지 않는 항목

검토사항 3의 운영 통계 정책, 5의 Snowflake/Fabric 비용 비교, 6의 Power BI 재활용,
7의 비정형 AI 연계는 위에 권장 방향을 제시하지만 이번 두 시나리오에서는 실제
리소스를 구성하지 않는다. 별도 PoC에서는 대표 데이터와 예상 처리량, 보존 기간,
동시 사용자 수를 먼저 확정한 뒤 비용과 성능을 측정해야 한다.

## 공식 참고자료

- [ADF Lookup Activity](https://learn.microsoft.com/azure/data-factory/control-flow-lookup-activity)
- [Metadata-driven Copy Pipeline](https://learn.microsoft.com/azure/data-factory/copy-data-tool-metadata-driven)
- [ADF Source Control](https://learn.microsoft.com/azure/data-factory/source-control)
- [ADF CI/CD](https://learn.microsoft.com/azure/data-factory/continuous-integration-delivery)
- [Azure Monitor Action Groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups)
