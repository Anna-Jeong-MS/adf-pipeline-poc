# Oracle-ADF Threshold Dispatcher PoC 범위

## 현재 상태

이 문서는 배포 전 합의할 목표 범위다. 현재 저장소의 Function Timer/Oracle Cursor
코드는 이전 PoC 구현이며, 아래 구조로 아직 변경되지 않았다.

## 목표

공통 ADF Schedule Trigger 하나가 Threshold Dispatcher를 실행하고, Dispatcher가
온프레미스 Oracle Control Table을 페이지 단위로 조회한다. 테이블별로 마지막 성공
Watermark 이후 신규 행 수를 확인해 임계치를 충족한 테이블만 Claim하고, Azure
Function을 통해 해당 테이블 Pipeline을 Create/Update한 뒤 Create Run한다.

상세 설계는 [목표 아키텍처](docs/target-architecture.md)를 기준으로 한다.

## 시나리오

### 1. Lookup Paging 기반 Threshold Dispatcher

- Self-hosted IR을 통한 Oracle Control Table 접근
- Lookup 5,000행/4MB 제한을 피하는 `CONTROL_ID` Keyset Paging
- 페이지 자식 Pipeline의 테이블별 신규 행 수 확인
- Watermark 범위와 Claim Lease의 원자적 고정
- 임계치 충족 테이블만 Function으로 Pipeline Create/Update 및 Create Run
- 성공 시에만 Watermark 확정

[Scenario 01 단계별 가이드](scenarios/01-metadata-pipeline/README.md)

### 2. Pipeline 성공/실패 이메일 알림

- Azure Monitor Action Group과 이메일 수신자
- Dispatcher와 테이블 Pipeline 실패 Alert
- Pipeline 이름, Run ID, 오류 상세 조회

[Scenario 02 단계별 가이드](scenarios/02-email-alerting/README.md)

## 포함 범위

1. 목표 아키텍처와 현재 Lookup 방식의 변경점
2. Control Table Paging, Watermark, Threshold, Claim/Lease 설계
3. 공통 Schedule Trigger와 Dispatcher/Page Worker Pipeline 템플릿
4. 테이블별 Pipeline Create/Update 및 Create Run Function
5. Self-hosted IR 기반 Oracle Count, Copy, 상태 갱신
6. NUMBER 및 TIMESTAMP + NUMBER Watermark
7. 임계치 미달, 충족, 중복, 실패 복구 검증
8. Azure Monitor 이메일 알림 구성

## 제외 범위

- Function App, ADF, Oracle VM 등의 기반 인프라 프로비저닝 코드
- 테이블별 Schedule Trigger 생성
- Function의 Oracle 직접 연결
- 임계치 미달 데이터의 최대 대기시간 강제 실행
- Oracle Delete 전파와 범용 CDC
- 실제 수백 개 대량 테이블 부하 시험
- Snowflake/Fabric, Power BI, 비정형 데이터 연계 구현

## 구현 순서

1. 아키텍처와 시나리오 검토 및 승인
2. Control Table/Package SQL 작성
3. Function을 HTTP 관리 API 역할로 변경
4. Dispatcher, Page Worker, Schedule Trigger JSON 작성
5. 테이블 Pipeline Watermark 템플릿 작성
6. 단위/정적 테스트
7. 개발 Azure/Oracle 환경 통합 테스트
8. 결과와 운영 전환 조건 문서화

현재 단계는 1번이며, 승인 전에는 배포를 진행하지 않는다.
