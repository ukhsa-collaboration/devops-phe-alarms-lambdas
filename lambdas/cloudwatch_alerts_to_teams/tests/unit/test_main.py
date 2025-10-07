import importlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def main_module(monkeypatch):
    """Reload the Lambda module with controlled environment variables."""
    monkeypatch.setenv("WEBHOOK_URL_SECRET_NAME", "secret-name")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("TIMEOUT_SECONDS", "15")

    module = importlib.reload(
        importlib.import_module("cloudwatch_alerts_to_teams.app.main")
    )
    module._webhook_url_cache = None

    yield module

    module._webhook_url_cache = None


def _build_sns_event_payload(overrides_list=None):
    base_alarm = {
        "AlarmName": "HighCPU",
        "AlarmDescription": "CPU usage exceeded threshold",
        "NewStateReason": "Threshold crossed: 1 datapoint",
        "NewStateValue": "ALARM",
        "AWSAccountId": "123456789012",
        "Namespace": "AWS/EC2",
        "Threshold": 80,
        "Region": "eu-west-1",
    }

    overrides_list = overrides_list or [{}]
    records = []

    for index, overrides in enumerate(overrides_list, start=1):
        alarm_payload = {**base_alarm, **overrides}
        records.append(
            {
                "EventSource": "aws:sns",
                "EventVersion": "1.0",
                "EventSubscriptionArn": f"arn:aws:sns:eu-west-1:123456789012:topic:{index}",
                "Sns": {
                    "Type": "Notification",
                    "MessageId": f"msg-{index}",
                    "TopicArn": "arn:aws:sns:eu-west-1:123456789012:topic",
                    "Subject": "ALARM: Test",
                    "Message": json.dumps(alarm_payload),
                    "Timestamp": "2023-01-01T00:00:00.000Z",
                    "SignatureVersion": "1",
                    "Signature": "signature",
                    "SigningCertUrl": "https://example.com/cert",
                    "UnsubscribeUrl": "https://example.com/unsub",
                    "MessageAttributes": {},
                },
            }
        )

    return {"Records": records}


def test_extract_alarm_data_includes_defaults(main_module):
    alarm_payload = {
        "AlarmName": "Critical_Alarm*",
        "NewStateValue": "ok",
        "AlarmDescription": None,
    }

    data = main_module.extract_alarm_data(alarm_payload)

    assert data["alarm_name"] == "Critical\\_Alarm\\*"
    assert data["alarm_name_raw"] == "Critical_Alarm*"
    assert data["alarm_state"] == "OK"
    assert data["region"] == "eu-west-1"
    assert data["threshold"] == "N/A"
    assert data["alarm_time"]


def test_process_sns_record_success(main_module, monkeypatch):
    event_payload = _build_sns_event_payload()
    sns_event = main_module.SNSEvent(event_payload)
    sns_record = list(sns_event.records)[0]

    secret_mock = MagicMock(return_value="https://example.com/webhook")
    monkeypatch.setattr(main_module.secrets_provider, "get", secret_mock)

    send_mock = MagicMock(return_value=(True, "Success", 200))
    monkeypatch.setattr(main_module, "send_to_teams", send_mock)

    result = main_module.process_sns_record(sns_record)

    assert result["status_code"] == 200
    assert result["alarm_name"] == "HighCPU"
    send_mock.assert_called_once()
    secret_mock.assert_called_once_with("secret-name")
    assert main_module._webhook_url_cache == "https://example.com/webhook"


def test_lambda_handler_partial_failure_returns_aggregate(main_module, monkeypatch):
    event_payload = _build_sns_event_payload(
        overrides_list=[
            {},
            {"AlarmName": "DiskFull", "NewStateValue": "ALARM"},
        ]
    )

    secret_mock = MagicMock(return_value="https://example.com/webhook")
    monkeypatch.setattr(main_module.secrets_provider, "get", secret_mock)

    send_mock = MagicMock(side_effect=[(True, "Success", 200), (False, "boom", 500)])
    monkeypatch.setattr(main_module, "send_to_teams", send_mock)
    main_module._webhook_url_cache = None

    response = main_module.lambda_handler(
        event_payload, SimpleNamespace(aws_request_id="req-1")
    )

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert len(body["successes"]) == 1
    assert len(body["failures"]) == 1
    assert body["failures"][0]["status_code"] == 500
    assert secret_mock.call_count == 1


def test_lambda_handler_with_no_records_returns_400(main_module):
    response = main_module.lambda_handler({"Records": []}, SimpleNamespace())

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["error"] == "SNS event did not contain any records"


def test_lambda_handler_handles_missing_sns_payload(main_module):
    response = main_module.lambda_handler(
        {
            "Records": [
                {
                    "EventSource": "aws:sns",
                    "EventVersion": "1.0",
                    "EventSubscriptionArn": "arn:aws:sns:eu-west-1:123456789012:topic:1",
                    # 'Sns' key intentionally missing
                }
            ]
        },
        SimpleNamespace(),
    )

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert len(body["failures"]) == 1
    assert body["failures"][0]["message"] == "SNS record missing required 'Sns' payload"
