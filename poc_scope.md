# Oracle-ADF 메타데이터 Pipeline 생성·실행 요청 자동화 PoC 범위

## 목표

Oracle Control Table을 Source of Truth로 사용해 공통 Copy/검증 템플릿을 테이블별
독립 ADF Pipeline으로 생성·갱신하고, Azure Function Timer로 실행을 요청한다.
Azure Monitor를 이용한 Pipeline 성공/실패 이메일 알림 구성도 안내한다.

## 시나리오

### 1. 메타데이터 기반 Pipeline 생성·실행 요청 자동화

- Control Table과 공통 Pipeline 활동 구조 설계
- Lookup 제한을 고려한 Function Cursor 배치 조회
- 테이블별 Pipeline Create/Update 및 Create Run 요청 자동화
- 대량 테이블 Partition Copy와 Timer 호출당 제출량 제한

[Scenario 01 단계별 가이드](scenarios/01-metadata-pipeline/README.md)

### 2. Pipeline 성공/실패 이메일 알림

- Azure Monitor Action Group과 이메일 수신자
- Factory 성공/실패 Metric Alert
- 수백 Pipeline 식별을 위한 Diagnostic Settings와 Log Search Alert

[Scenario 02 단계별 가이드](scenarios/02-email-alerting/README.md)

## 포함 범위

1. Control Table 확장 SQL과 샘플 행
2. Oracle/ADLS Linked Service와 Parameter Dataset 구성 절차
3. 공통 Pipeline JSON 구조와 Function 생성 코드
4. Function Managed Identity, 환경 변수, Timer 구성
5. Pipeline 생성·실행 및 배치/재시도 확인 절차
6. Action Group, Metric Alert, Log Alert Portal 구성 절차
7. KQL과 Alert 설정값 샘플

## 제외 범위

- Function App, ADF, Oracle VM 등의 기반 인프라 프로비저닝 코드
- 실제 수백 개 대량 테이블 부하 시험
- Durable Functions 기반 전역 동시성 제어 구현
- Snowflake/Fabric 비용 비교
- Power BI Semantic Model 재활용 구현
- SharePoint/AI Search/Foundry 비정형 데이터 연계 구현

추가 검토사항 8개에 대한 답변은 공개 저장소에 포함하지 않는 내부 미팅 로그에
별도로 정리한다.
