from app.domain.audit_visibility import technical_audit_payload


def test_technical_audit_hides_teaching_content_without_mutating_original() -> None:
    original = {"comment": "Персональные сведения", "status": "ACCEPTED", "score": {"total": 42}}
    assert technical_audit_payload("STATUS_CHANGED", original) == {
        "status": "ACCEPTED",
        "content_hidden": True,
    }
    assert original["comment"] == "Персональные сведения" and original["score"] == {"total": 42}
    assert technical_audit_payload(
        "CARD_DRAFT_SAVED", {"revision": 2, "card": {"name": "Заявитель"}}
    ) == {"revision": 2, "content_hidden": True}
    assert technical_audit_payload("SCORE_OVERRIDE", {"score": 99, "reason": "Комментарий"}) == {
        "content_hidden": True
    }


def test_technical_events_keep_required_security_evidence() -> None:
    value = {"previous": {"role": "TEACHER"}, "current": {"role": "STUDENT"}}
    assert technical_audit_payload("USER_UPDATED", value) == value
    assert technical_audit_payload("LOGIN_FAILED", {"login": "student"}) == {"login": "student"}
    assert technical_audit_payload("LOGIN", None) is None
    assert technical_audit_payload("SYSTEM_RECOVERY_PREPARED", {"snapshot": "backup-id"}) == {
        "snapshot": "backup-id"
    }
