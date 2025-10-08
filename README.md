## PHE Alarms
A Python Lambda function that subscribes an SNS topic. When an alarm notification is received on the SNS topic, the Lambda forwards the alert to Teams.

# Overview
When CloudWatch alarms trigger in any participating AWS account, they can publish alarm notifications to a local SNS topic. A lightweight relay Lambda that is subscribed to that local topic then forwards the notification to a central SNS topic in the monitoring account. This Lambda receives notifications from that centralised SNS topic and sends formatted alerts to Teams via a webhook URL stored in AWS Secrets Manager. This avoids needing to deploy complex code to multiple accounts and having to manage the Teams webhook in multiple places.

This setup works around the limitation that CloudWatch alarms cannot include PrincipalOrgID in their SNS permissions — avoiding the need to maintain a list of individual AWS account IDs allowed to publish directly.

Infrastructure as Code is stored in [devops-phe-alarms-iac](https://github.com/ukhsa-collaboration/devops-phe-alarms-iac).
StackSet deploying the relay Lambdas is stored in [ohid-aws-landing-zone](https://github.com/ukhsa-collaboration/ohid-aws-landing-zone).

## Architecture Diagram

```mermaid
flowchart TD
    subgraph AWS_Account["PHE Organization AWS Accounts"]
        A1[CloudWatch Alarm 1]
        A2[CloudWatch Alarm 2]
        A3[CloudWatch Alarm 3]

        subgraph Local_Forwarding
            SNS_LOCAL[(Local SNS Topic)]
            LAMBDA_FORWARD[Relay Lambda]
        end

        A1 -->|Alarm Notification| SNS_LOCAL
        A2 -->|Alarm Notification| SNS_LOCAL
        A3 -->|Alarm Notification| SNS_LOCAL
        SNS_LOCAL -->|Invoke| LAMBDA_FORWARD
        LAMBDA_FORWARD -->|Publish| SNS_CENTRAL
    end

    subgraph Monitoring_Account["Central Monitoring Account"]
        SNS_CENTRAL[(Central SNS Topic)]
        LAMBDA_MAIN[Main PHE Alarms Lambda]
        SECRETS[AWS Secrets Manager]
        TEAMS[Microsoft Teams Channel]

        SNS_CENTRAL -->|Invoke| LAMBDA_MAIN
        LAMBDA_MAIN -->|Retrieve Webhook| SECRETS
        LAMBDA_MAIN -->|Send Alert| TEAMS
    end

    style SNS_LOCAL fill:#fdf6b2,stroke:#d97706,stroke-width:2px
    style LAMBDA_FORWARD fill:#fef3c7,stroke:#92400e,stroke-width:2px
    style SNS_CENTRAL fill:#fde68a,stroke:#b45309,stroke-width:2px
    style LAMBDA_MAIN fill:#bfdbfe,stroke:#1e3a8a,stroke-width:2px
    style SECRETS fill:#d1fae5,stroke:#047857,stroke-width:2px
    style TEAMS fill:#c7d2fe,stroke:#4338ca,stroke-width:2px
```
