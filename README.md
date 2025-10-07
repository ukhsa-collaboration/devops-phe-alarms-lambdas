## PHE Alarms
A Python Lambda function that subscribes to the SNS topic. When an alarm notification is received on the SNS topic, the Lambda forwards the alert to Teams.

Infrastructure as Code is stored in [devops-phe-alarms-iac](https://github.com/ukhsa-collaboration/devops-phe-alarms-iac).

## Architecture Diagram

```mermaid
flowchart TD
    subgraph AWS_Account["PHE Organization AWS Accounts"]
        A1[CloudWatch Alarm 1]
        A2[CloudWatch Alarm 2]
        A3[CloudWatch Alarm 3]
    end

    A1 -->|Publish Alarm Notification| SNS[(Shared SNS Topic)]
    A2 -->|Publish Alarm Notification| SNS
    A3 -->|Publish Alarm Notification| SNS

    SNS -->|Invoke| LAMBDA[Lambda Function Python]

    subgraph Lambda_Processing["Lambda Processing"]
        LAMBDA -->|Retrieve Webhook from| SECRETS[AWS Secrets Manager]
        LAMBDA -->|Send Alert| TEAMS[Microsoft Teams Channel]
    end

    style SNS fill:#fdf6b2,stroke:#d97706,stroke-width:2px
    style LAMBDA fill:#bfdbfe,stroke:#1e3a8a,stroke-width:2px
    style TEAMS fill:#c7d2fe,stroke:#4338ca,stroke-width:2px
    style SECRETS fill:#d1fae5,stroke:#047857,stroke-width:2px
```
