from typing import Any

TECHNICAL_PREFIXES = (
    "USER_",
    "PASSWORD_",
    "LOGIN",
    "LOGOUT",
    "WORKSTATION_",
    "CLASSIFIER_",
    "STREETS_",
    "ACCESS_POLICY_",
    "MAINTENANCE_",
    "TECHNICAL_OPERATION_",
    "TECHNICAL_REPORT_",
    "SYSTEM_HTTP_ERROR",
    "SYSTEM_RECOVERY_",
    "SOFTWARE_UPDATE_",
    "RUNTIME_CONFIGURATION_",
)


def technical_audit_payload(action: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None or action.startswith(TECHNICAL_PREFIXES):
        return payload
    visible = {
        key: value
        for key, value in payload.items()
        if key in {"status", "state", "revision", "task_mode", "error_code"}
    }
    if len(visible) != len(payload):
        visible["content_hidden"] = True
    return visible
