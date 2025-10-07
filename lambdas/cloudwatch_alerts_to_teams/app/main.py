import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.data_classes import SNSEvent, event_source
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools.utilities.parameters import SecretsProvider

logger = Logger()

DEFAULT_TIMEOUT_SECONDS = 10
_webhook_url_cache: Optional[str] = None
secrets_provider = SecretsProvider()


def _load_timeout_seconds(default: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    """Load timeout configuration while enforcing sane defaults."""
    raw_timeout = os.getenv("TIMEOUT_SECONDS", str(default))

    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid TIMEOUT_SECONDS value provided; falling back to default",
            raw_value=raw_timeout,
            default_value=default,
        )
        return default

    if timeout <= 0:
        logger.warning(
            "TIMEOUT_SECONDS must be a positive integer; falling back to default",
            raw_value=raw_timeout,
            default_value=default,
        )
        return default

    return timeout


WEBHOOK_URL_SECRET_NAME = os.getenv("WEBHOOK_URL_SECRET_NAME") or ""
AWS_REGION = os.getenv("AWS_REGION") or ""
TIMEOUT_SECONDS = _load_timeout_seconds()


class AlarmProcessingError(Exception):
    """Base exception for alarm processing failures."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidAlarmPayloadError(AlarmProcessingError):
    """Raised when the SNS message cannot be parsed into a valid alarm."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message, status_code=status_code)


class WebhookDeliveryError(AlarmProcessingError):
    """Raised when delivering the adaptive card to Teams fails."""


class ConfigurationError(AlarmProcessingError):
    """Raised when the function configuration is invalid."""


DEFAULT_STATE_STYLE: Dict[str, str] = {
    "icon": "❓",
    "colour": "Default",
    "title": "Alarm State Changed",
}


def _is_valid_webhook_url(candidate: Any) -> bool:
    """Ensure the retrieved webhook URL looks sane before attempting to use it."""
    if not isinstance(candidate, str):
        return False

    parsed = urlparse(candidate)
    return parsed.scheme == "https" and bool(parsed.netloc)


def create_requests_session() -> requests.Session:
    """Create a requests session with retry strategy"""
    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"POST"},
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)

    return session


session = create_requests_session()


def get_webhook_url(force_refresh: bool = False) -> str:
    """Get webhook URL, decrypting if necessary (with caching)."""
    global _webhook_url_cache

    if not WEBHOOK_URL_SECRET_NAME:
        raise ConfigurationError("WEBHOOK_URL_SECRET_NAME variable is required")

    if not force_refresh and _webhook_url_cache:
        return _webhook_url_cache

    try:
        secret_value: Any = secrets_provider.get(WEBHOOK_URL_SECRET_NAME)
    except Exception as exc:  # pragma: no cover - safety net
        message = f"Unexpected error retrieving secret '{WEBHOOK_URL_SECRET_NAME}' for webhook"
        logger.exception(message)
        raise AlarmProcessingError(message) from exc

    if isinstance(secret_value, dict):
        webhook_url = (
            secret_value.get("webhook_url")
            or secret_value.get("url")
            or secret_value.get("value")
        )
    else:
        webhook_url = str(secret_value).strip() if secret_value else ""

    if not webhook_url:
        raise ConfigurationError(
            f"Secret '{WEBHOOK_URL_SECRET_NAME}' does not contain a webhook URL"
        )

    if not _is_valid_webhook_url(webhook_url):
        raise ConfigurationError("Webhook URL must be an https URL with a hostname")

    _webhook_url_cache = webhook_url
    return webhook_url


def validate_environment() -> Tuple[bool, str]:
    """Validate required environment variables"""
    if not WEBHOOK_URL_SECRET_NAME:
        return False, "WEBHOOK_URL_SECRET_NAME variable is required"

    if not AWS_REGION:
        return False, "AWS_REGION environment variable is required"

    if TIMEOUT_SECONDS <= 0:
        return False, "TIMEOUT_SECONDS must be a positive integer"

    return True, ""


def sanitize_text(text: str, max_length: int = 1000) -> str:
    """Sanitize text for Teams adaptive cards"""
    if not isinstance(text, str):
        text = str(text)

    # Escape markdown characters, normalise whitespace, and limit length
    sanitized = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .strip()
    )
    return sanitized[:max_length]


def extract_alarm_data(alarm: Dict[str, Any]) -> Dict[str, str]:
    """Extract and sanitize alarm data with fallbacks"""
    raw_alarm_name = alarm.get("AlarmName", "Unknown")
    if not isinstance(raw_alarm_name, str):
        raw_alarm_name = str(raw_alarm_name)

    raw_state_change_time = alarm.get("StateChangeTime")
    if isinstance(raw_state_change_time, str) and raw_state_change_time:
        alarm_time = raw_state_change_time
    else:
        alarm_time = datetime.now(timezone.utc).isoformat()

    return {
        "account_id": str(alarm.get("AWSAccountId", "000000000000")),
        "alarm_name": sanitize_text(raw_alarm_name),
        "alarm_name_raw": raw_alarm_name,
        "alarm_desc": sanitize_text(
            alarm.get("AlarmDescription", "No description provided")
        ),
        "alarm_reason": sanitize_text(
            alarm.get("NewStateReason", "No reason provided")
        ),
        "alarm_time": alarm_time,
        "alarm_state": alarm.get("NewStateValue", "UNKNOWN").upper(),
        "namespace": sanitize_text(alarm.get("Namespace", "N/A")),
        "threshold": str(alarm.get("Threshold", "N/A")),
        "region": str(alarm.get("Region", AWS_REGION or "unknown")),
    }


def get_state_style(alarm_state: str) -> Optional[Dict[str, str]]:
    """Get styling configuration for alarm state"""
    state_styles = {
        "ALARM": {"icon": "🚨", "colour": "Attention", "title": "Alarm Triggered"},
        "OK": {"icon": "✅", "colour": "Good", "title": "Alarm Resolved"},
        "INSUFFICIENT_DATA": {
            "icon": "⚠️",
            "colour": "Warning",
            "title": "Alarm State Uncertain",
        },
    }
    return state_styles.get(alarm_state)


def build_cloudwatch_url(alarm_name: str, region: Optional[str] = None) -> str:
    """Build CloudWatch console URL for the provided alarm."""
    region_name = (region or AWS_REGION or "").strip() or "us-east-1"
    encoded_name = quote(alarm_name, safe="")

    return (
        "https://"
        f"{region_name}.console.aws.amazon.com/cloudwatch/home?region={region_name}"
        f"#alarmsV2:alarm/{encoded_name}"
    )


def create_adaptive_card(
    alarm_data: Dict[str, str], state_style: Dict[str, str]
) -> Dict[str, Any]:
    """Create Teams adaptive card payload"""
    raw_alarm_name = alarm_data.get("alarm_name_raw", alarm_data["alarm_name"])
    cloudwatch_url = build_cloudwatch_url(raw_alarm_name, alarm_data.get("region"))

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"{state_style['icon']} **{state_style['title']}: {alarm_data['alarm_name']}**",
                            "weight": "Bolder",
                            "size": "Large",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": f"**State:** {alarm_data['alarm_state']}",
                            "weight": "Bolder",
                            "color": state_style["colour"],
                            "spacing": "Small",
                        },
                        {
                            "type": "TextBlock",
                            "text": f"**Description:** {alarm_data['alarm_desc']}",
                            "wrap": True,
                            "spacing": "Small",
                        },
                        {
                            "type": "TextBlock",
                            "text": f"**Reason:** {alarm_data['alarm_reason']}",
                            "wrap": True,
                            "spacing": "Small",
                        },
                        {
                            "type": "FactSet",
                            "spacing": "Medium",
                            "facts": [
                                {
                                    "title": "AWS Account ID",
                                    "value": alarm_data["account_id"],
                                },
                                {
                                    "title": "Namespace",
                                    "value": alarm_data["namespace"],
                                },
                                {
                                    "title": "Threshold",
                                    "value": alarm_data["threshold"],
                                },
                                {"title": "Region", "value": alarm_data["region"]},
                            ],
                        },
                        {
                            "type": "TextBlock",
                            "text": f"**Time:** {alarm_data['alarm_time']}",
                            "isSubtle": True,
                            "wrap": True,
                            "spacing": "Small",
                        },
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "View Alarm in CloudWatch",
                            "url": cloudwatch_url,
                        }
                    ],
                },
            }
        ],
    }


def send_to_teams(
    card_payload: Dict[str, Any],
    webhook_url: str,
    http_session: Optional[requests.Session] = None,
    timeout_seconds: Optional[int] = None,
) -> Tuple[bool, str, int]:
    """Send adaptive card to Teams webhook."""
    session_to_use = http_session or session
    timeout = timeout_seconds or TIMEOUT_SECONDS

    if timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS

    try:
        response = session_to_use.post(
            webhook_url,
            json=card_payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

        if response.status_code >= 400:
            error_msg = (
                f"Teams webhook returned {response.status_code}: {response.text[:200]}"
            )
            logger.error(error_msg)
            return False, error_msg, response.status_code

        return True, "Success", response.status_code

    except requests.exceptions.Timeout:
        error_msg = "Timeout sending webhook to Teams"
        logger.error(error_msg)
        return False, error_msg, 408
    except requests.exceptions.RequestException as e:
        error_msg = f"Request error sending to Teams: {str(e)}"
        logger.error(error_msg)
        return False, error_msg, 500
    except Exception as e:
        error_msg = f"Unexpected error sending to Teams: {str(e)}"
        logger.error(error_msg)
        return False, error_msg, 500


def process_sns_record(sns_record: Any) -> Dict[str, Any]:
    """Process a single SNS record and deliver the alarm notification."""
    try:
        sns_message = sns_record.sns
    except KeyError as exc:
        raise InvalidAlarmPayloadError(
            "SNS record missing required 'Sns' payload"
        ) from exc

    message_id = getattr(sns_message, "message_id", "unknown")

    try:
        alarm_payload = json.loads(sns_message.message)
    except json.JSONDecodeError as exc:
        raise InvalidAlarmPayloadError(f"Invalid JSON in SNS message: {exc}") from exc

    if not isinstance(alarm_payload, dict):
        raise InvalidAlarmPayloadError("SNS message must be a JSON object")

    alarm_data = extract_alarm_data(alarm_payload)

    logger.info(
        "Processing alarm notification",
        alarm_name=alarm_data["alarm_name"],
        alarm_state=alarm_data["alarm_state"],
        message_id=message_id,
    )

    state_style = get_state_style(alarm_data["alarm_state"])
    if not state_style:
        logger.warning(
            "Unknown alarm state received; default styling applied",
            alarm_state=alarm_data["alarm_state"],
            message_id=message_id,
        )
        state_style = DEFAULT_STATE_STYLE

    webhook_url = get_webhook_url()
    card_payload = create_adaptive_card(alarm_data, state_style)
    success, message, status_code = send_to_teams(card_payload, webhook_url)

    if not success:
        raise WebhookDeliveryError(message, status_code=status_code)

    logger.info(
        "Alarm notification delivered",
        alarm_name=alarm_data["alarm_name"],
        alarm_state=alarm_data["alarm_state"],
        message_id=message_id,
        status_code=status_code,
    )

    return {
        "alarm_name": alarm_data["alarm_name"],
        "alarm_state": alarm_data["alarm_state"],
        "status_code": status_code,
        "message_id": message_id,
    }


def _safe_message_id(sns_record: Any) -> str:
    """Best-effort retrieval of the SNS message id without raising."""
    try:
        sns_message = sns_record.sns
    except KeyError:
        return "unknown"

    return getattr(sns_message, "message_id", "unknown")


@event_source(data_class=SNSEvent)
def lambda_handler(event: SNSEvent, context: LambdaContext) -> Dict[str, Any]:
    """Main Lambda handler with SNS event validation"""

    env_valid, env_error = validate_environment()
    if not env_valid:
        logger.error(f"Environment validation failed: {env_error}")
        return {"statusCode": 500, "body": json.dumps({"error": env_error})}

    records = list(event.records)
    if not records:
        error_msg = "SNS event did not contain any records"
        logger.error(error_msg)
        return {"statusCode": 400, "body": json.dumps({"error": error_msg})}

    successes: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for sns_record in records:
        message_id = _safe_message_id(sns_record)

        try:
            result = process_sns_record(sns_record)
            successes.append(result)
        except AlarmProcessingError as exc:
            logger.error(
                "Failed to process SNS record",
                error_message=str(exc),
                message_id=message_id,
                status_code=exc.status_code,
            )
            failures.append(
                {
                    "message": str(exc),
                    "status_code": exc.status_code,
                    "message_id": message_id,
                }
            )
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception(
                "Unexpected error processing SNS record",
                message_id=message_id,
                error_message=str(exc),
            )
            failures.append(
                {
                    "message": "Unexpected error processing SNS record",
                    "detail": str(exc),
                    "status_code": 500,
                    "message_id": message_id,
                }
            )

    if failures:
        status_code = max(failure["status_code"] for failure in failures)
        response_body: Dict[str, Any] = {
            "error": "One or more notifications failed",
            "failures": failures,
        }
        if successes:
            response_body["successes"] = successes

        return {"statusCode": status_code, "body": json.dumps(response_body)}

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": f"Processed {len(successes)} alarm notification(s)",
                "successes": successes,
            }
        ),
    }
