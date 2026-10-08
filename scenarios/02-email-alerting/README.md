# Scenario 02 - Azure Portal Pipeline 이메일 알림 구성

이 문서는 ADF Pipeline의 성공/실패를 Azure Monitor가 감지해 이메일로 전달하도록
고객이 Portal에서 직접 구성하는 단계별 가이드다. Factory Metric Alert는
성공·실패 실행 건수 발생을 감지하고, Pipeline 이름·Run ID·상세 오류는 Log
Analytics에서 조회한다. 이 구성만으로 전체 배치의 성공이나 실행마다 한 통의
이메일이 보장되지는 않는다.

## 1. 구성 구조

```text
ADF Pipeline Run
  +-- Factory Metric -------------------+
  |                                     |
  +-- Diagnostic Settings -> Log Analytics -> Alert Rule
                                        |
                                        v
                                  Action Group
                                        |
                                        v
                                  Email Receiver
```

## 2. 사전 조건

- Azure Data Factory와 실행 가능한 Pipeline
- Azure Monitor Alert Rule을 만들 권한
- 알림을 받을 운영 이메일 또는 Distribution List
- 테이블별 Log Alert용 Log Analytics Workspace
- ADF Diagnostic Settings를 변경할 권한

리소스 생성 직후에는 Metric Signal이나 Diagnostic Log가 표시되기까지 시간이
필요할 수 있다. 이메일 주소와 구독 ID는 화면 캡처에서 마스킹한다.

## 3. Portal에서 Action Group 구성

### 3.1 Action Group 만들기

1. [Azure Portal](https://portal.azure.com/)에서 **Monitor**를 검색해 연다.
2. **Alerts > Action groups > + Create**를 선택한다.
3. **Basics**에 다음 값을 입력한다.

| 항목 | 샘플 값 |
|---|---|
| Subscription | 대상 구독 |
| Resource group | 모니터링 Resource Group |
| Region | 조직의 데이터 처리 정책에 맞는 Global/Regional |
| Action group name | `ag-adf-pipeline-notification` |
| Display name | `adfnotify` |

### 3.2 이메일 수신자 추가

1. **Next: Notifications**를 선택한다.
2. **Notification type**에서 **Email/SMS message/Push/Voice**를 선택한다.
3. 이름에 `pipeline-operator`를 입력한다.
4. **Email**을 선택하고 운영 이메일 주소를 입력한다.
5. **OK**를 선택한다.
6. 가능하면 **Enable the common alert schema**를 활성화한다.
7. **Review + create > Create**를 선택한다.
8. 수신자에게 Action Group 추가 안내 메일이 도착하는지 확인한다.

메일이 오지 않으면 스팸/격리함과 조직의 Azure Alert 발신자 허용 정책을 확인한다.

### 3.3 Action Group 자체 테스트

1. 만든 Action Group을 연다.
2. 상단 **Test**를 선택한다.
3. Sample type을 선택한다.
4. `pipeline-operator` Notification을 선택한다.
5. **Test**를 실행한다.
6. Portal의 실행 결과와 실제 받은 편지함 수신을 모두 기록한다.

Action Group Test는 통신 경로만 확인하며 ADF Alert 조건 발화까지 검증하지 않는다.

## 4. Factory 내 실패 실행 건수 Metric Alert

1. Data Factory 리소스의 **Monitoring > Alerts**를 연다.
2. **+ Create > Alert rule**을 선택한다.
3. **Scope**가 대상 Data Factory 하나인지 확인한다.
4. **Condition > See all signals**에서 `PipelineFailedRuns` 또는
   **Failed pipeline runs metrics**를 선택한다.
5. 다음 조건을 입력한다.

| 항목 | 샘플 값 |
|---|---|
| Threshold | Static |
| Aggregation type | Total |
| Operator | Greater than |
| Threshold value | `0` |
| Check every | 1 minute |
| Lookback period | 5 minutes |

6. 특정 Pipeline만 감지하려면 **Split by dimensions** 또는 Filter에서 `Name`
   차원을 선택하고 Pipeline 이름을 지정한다.
7. **Actions > Select action groups**에서
   `ag-adf-pipeline-notification`을 선택한다.
8. **Details**를 다음처럼 설정한다.

| 항목 | 샘플 값 |
|---|---|
| Severity | `1 - Error` |
| Alert rule name | `alert-adf-pipeline-failed` |
| Description | `ADF pipeline failed` |
| Enable upon creation | Enabled |
| Automatically resolve | Enabled |

9. **Review + create > Create**를 선택한다.

이 Alert는 평가 구간에 실패 실행이 한 건 이상 있었음을 알린다. 여러 Pipeline의
성공 여부를 조합해 “전체 배치 성공”을 판정하는 규칙은 아니다.

## 5. Factory 내 성공 실행 건수 Metric Alert

성공 메일까지 필요한 경우 위 절차를 반복해 다음 값만 변경한다.

| 항목 | 샘플 값 |
|---|---|
| Signal | `PipelineSucceededRuns` |
| Aggregation | Total |
| Operator / Threshold | Greater than / `0` |
| Check every / Lookback | 5 minutes / 5 minutes |
| Severity | `3 - Informational` |
| Rule name | `alert-adf-pipeline-succeeded` |

운영에서는 성공 메일이 많아질 수 있으므로 일별 Summary가 목적이면 즉시 성공
Alert 대신 Logic App/Workbook 기반 집계를 검토한다.

Metric Alert에서도 `Name` 차원으로 Pipeline 이름을 구분할 수 있다. Log
Analytics를 추가하는 핵심 이유는 Metric에 없는 Run ID와 상세 오류를 조회하기
위해서다.

## 6. 수백 Pipeline용 Diagnostic Settings

Factory Metric은 Factory 범위의 실행 건수 집계에 적합하지만 실패 Pipeline의 Run
ID와 상세 오류를 식별하려면 Log Analytics를 사용한다.

1. Data Factory의 **Monitoring > Diagnostic settings**를 연다.
2. **+ Add diagnostic setting**을 선택한다.
3. 이름을 `diag-adf-pipeline-runs`로 입력한다.
4. Log Category에서 `PipelineRuns`, `ActivityRuns`를 선택한다.
5. Timer/ADF Trigger도 추적하면 `TriggerRuns`를 추가한다.
6. **Send to Log Analytics workspace**를 선택한다.
7. Subscription과 Workspace를 선택한다.
8. 가능한 경우 **Resource specific** Destination Table을 선택한다.
9. **Save**를 선택한다.
10. 테스트 Pipeline을 실행하고 로그 유입을 기다린다.

## 7. 실패 Pipeline KQL 작성

Log Analytics Workspace의 **Logs**를 열고 먼저 `ADFPipelineRun` Table이 있는지
확인한다. Resource-specific Table을 사용하는 샘플:

```kusto
ADFPipelineRun
| where TimeGenerated > ago(10m)
| where _ResourceId =~ "<DATA_FACTORY_RESOURCE_ID>"
| where Status == "Failed"
| project TimeGenerated, PipelineName, RunId, Status,
          FailureType, ErrorCode, ErrorMessage
| order by TimeGenerated desc
```

Legacy `AzureDiagnostics`를 사용하는 Workspace의 샘플:

```kusto
AzureDiagnostics
| where TimeGenerated > ago(10m)
| where ResourceProvider == "MICROSOFT.DATAFACTORY"
| where Category == "PipelineRuns"
| where status_s == "Failed"
| project TimeGenerated,
          PipelineName = pipelineName_s,
          RunId = runId_g,
          Status = status_s,
          FailureType = failureType_s,
          ErrorMessage = message_s
| order by TimeGenerated desc
```

Workspace의 실제 Schema에 따라 열 이름이 다르면 왼쪽 **Tables**에서 열을 확인해
수정한다. 쿼리는 시간 조건을 가장 먼저 적용하고 실제 실패 한 건만 반환하는지
검증한 후 Alert로 만든다. 여러 Factory가 같은 Workspace를 사용하면
`<DATA_FACTORY_RESOURCE_ID>`를 실제 Factory Resource ID로 바꾸고 필터를 유지한다.

## 8. 알림 목적별 Log Alert 만들기

### 8.1 장애 상태 감지

1. 검증한 실패 KQL 위의 **New alert rule**을 선택한다.
2. **Scope**가 대상 Log Analytics Workspace인지 확인한다.
3. **Condition**에서 Measurement를 **Table rows**로 선택한다.
4. **Operator = Greater than**, **Threshold = 0**으로 설정한다.
5. **Evaluation frequency = 5 minutes**,
   **Lookback period = 10 minutes**로 시작한다.
6. Pipeline별 장애 상태가 필요하면 **Split by dimensions**에서 `PipelineName`을
   선택한다.
7. **Actions**에서 기존 Action Group을 연결한다.
8. **Details**에 다음 값을 입력한다.

| 항목 | 샘플 값 |
|---|---|
| Severity | `1 - Error` |
| Alert rule name | `alert-adf-table-pipeline-failed` |
| Automatically resolve | Enabled |

9. **Review + create > Create**를 선택한다.

수백 Pipeline마다 Metric Alert를 하나씩 만들지 않는다. 단일 Log Alert 또는 업무
그룹별 소수 Alert로 시작한다. `Automatically resolve = Enabled`인 stateful
Alert는 같은 차원의 Alert가 Fired 상태인 동안 추가 위반에 대해 새 Action을
실행하지 않으므로 실행 건별 이메일을 보장하지 않는다.

### 8.2 실패 실행별 알림

실행 한 건마다 별도 알림이 필요하면 다음 중 하나를 선택한다.

1. **Simple log search alert**
   - 위 KQL이 실패 실행 한 행당 한 결과를 반환하게 한다.
   - Simple log search alert로 각 행을 개별 평가한다.
   - 고객 구독과 Portal에서 해당 Alert 유형의 제공 여부를 확인한다.
2. **Log search alert 차원 분할**
   - Query 결과에 `PipelineName`, `RunId`를 문자열로 Projection한다.
   - **Split by dimensions**에 두 열을 추가해 조합별로 평가한다.
   - Run ID는 매번 달라지는 고 Cardinality 값이므로 Alert Instance 수와 비용을
     사전에 검토한다.
3. **Logic App 상세 메일**
   - Alert 또는 주기 실행으로 KQL을 호출한다.
   - 조회 결과를 HTML Table로 변환해 PipelineName, RunId, ErrorCode,
     ErrorMessage를 한 메일에 포함한다.

운영 장애 상태 알림과 실행 건별 감사 메일은 목적이 다르므로 별도 Rule/Workflow로
관리한다.

## 9. 알림 발화 테스트

### 9.1 실패 알림

1. 테스트 Pipeline의 연결 정보 또는 Row Count 조건을 테스트 범위에서만 변경해
   의도적으로 실패시킨다.
2. ADF Studio **Monitor > Pipeline runs**에서 상태가 `Failed`인지 확인한다.
3. Portal **Monitor > Alerts**에서 Alert Instance를 연다.
4. Rule, Fired time, Target resource, Condition을 기록한다.
5. 수신 메일에서 Pipeline 이름, 시간, Resource Link를 확인한다.
6. 실행 건별 알림을 선택했다면 서로 다른 Run ID 두 건을 연속 실패시켜 두 건이
   각각 식별되는지 확인한다.
7. 조건이 사라진 뒤 Resolved 상태도 확인한다.
8. 테스트용 오류 설정은 원상 복구한다.

### 9.2 성공 알림

1. 정상 Pipeline을 실행한다.
2. ADF Run이 `Succeeded`인지 확인한다.
3. 성공 Metric Alert Instance와 메일을 확인한다.
4. 메일 빈도가 운영자에게 과도한지 평가한다.

## 10. 샘플 Alert 구성값

Portal 입력값을 환경별 Parameter로 관리할 때 사용할 최소 샘플:

```json
{
  "actionGroupName": "ag-adf-pipeline-notification",
  "receiverName": "pipeline-operator",
  "receiverEmail": "<MASKED_EMAIL>",
  "failedMetricAlert": {
    "signal": "PipelineFailedRuns",
    "aggregation": "Total",
    "operator": "GreaterThan",
    "threshold": 0,
    "windowSize": "PT5M",
    "evaluationFrequency": "PT1M",
    "severity": 1
  },
  "succeededMetricAlert": {
    "signal": "PipelineSucceededRuns",
    "aggregation": "Total",
    "operator": "GreaterThan",
    "threshold": 0,
    "windowSize": "PT5M",
    "evaluationFrequency": "PT5M",
    "severity": 3
  }
}
```

이 JSON은 값의 기준 샘플이며 그 자체로 배포하는 ARM Template은 아니다. 조직의
IaC Repository에서는 같은 값을 Bicep/ARM Parameter로 옮겨 관리한다.

## 11. 구성 후 확인 자료

1. Action Group Basics와 Notification 설정
2. 마스킹한 Email Receiver
3. Action Group Test 결과와 수신 메일
4. 성공/실패 Metric Alert Condition
5. Diagnostic Settings Category와 Workspace
6. 실패 KQL 결과
7. Log Alert Condition과 연결된 Action Group
8. ADF Run ID, Fired Alert, 이메일의 상호 연결

## 공식 참고자료

- [Action Group 만들기](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups)
- [Metric Alert Rule 만들기](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-create-metric-alert-rule)
- [ADF Monitoring](https://learn.microsoft.com/azure/data-factory/monitor-data-factory)
- [ADF Monitoring Data Reference](https://learn.microsoft.com/azure/data-factory/monitor-data-factory-reference)
- [ADFPipelineRun 열 정의](https://learn.microsoft.com/azure/azure-monitor/reference/tables/adfpipelinerun)
- [Azure Monitor Alert 상태](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-overview)
- [Simple Log Search Alert](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-create-simple-alert)
