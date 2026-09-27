"""Single local SIP controller. Media stays in Asterisk; timestamps come from ARI."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from websockets.asyncio.client import connect

from app import runtime_configuration
from app.config import settings
from app.db.base import session_factory
from app.db.models import (
    Assignment,
    AuditLog,
    DirectoryEntry,
    InteractionEvent,
    Lesson,
    Scenario,
    SipCall,
    StatusEvent,
    User,
)
from app.domain.card_entry import incoming_message
from app.domain.cards import own_assignment
from app.domain.sip import (
    ACTIVE_STATES,
    ROOT,
    control_password,
    endpoint_config,
    endpoint_name,
    endpoint_password,
    prepare_speech,
)
from app.logging import configure_logging
from app.transport_security import http_verify, redis_tls, websocket_tls

logger = logging.getLogger(__name__)
ARI = settings.sip_ari_url.rstrip("/")


async def provision() -> None:
    async with session_factory() as db:
        users = list(
            await db.scalars(
                select(User)
                .where(User.is_active.is_(True), User.role.in_(["STUDENT", "TEACHER"]))
                .order_by(User.id)
            )
        )
        content = "".join(
            endpoint_config(
                user.id, endpoint_password(settings.jwt_secret, user.id, user.password_hash)
            )
            for user in users
        )
        ROOT.mkdir(parents=True, exist_ok=True)
        target = ROOT / "accounts.conf"
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            temporary = ROOT / "accounts.pending"
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
        scenarios = await db.scalars(
            select(Scenario)
            .join(Assignment)
            .join(Lesson)
            .where(
                Lesson.status.in_(["PLANNED", "RUNNING"]),
                Assignment.task_mode == "CARD_ENTRY",
            )
            .distinct()
        )
        for scenario in scenarios:
            prepare_speech(incoming_message(scenario.card_payload))
        for entry in await db.scalars(select(DirectoryEntry)):
            prepare_speech(entry.greeting)


async def ari(client: httpx.AsyncClient, method: str, path: str, **params: Any) -> Any:
    response = await client.request(method, ARI + path, params=params)
    if method == "DELETE" and response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json() if response.content else None


async def notify(teacher_id: UUID, user_id: UUID, assignment_id: UUID, kind: str) -> None:
    async with Redis.from_url(settings.redis_url, **redis_tls()) as cache:
        await cache.publish(
            "dispatcher:events",
            json.dumps(
                {
                    "room": f"user:{teacher_id}",
                    "type": "STUDENT_ACTION",
                    "payload": {
                        "student_id": str(user_id),
                        "assignment_id": str(assignment_id),
                        "kind": kind,
                    },
                }
            ),
        )
        await cache.publish(
            "dispatcher:events",
            json.dumps(
                {
                    "room": f"user:{user_id}",
                    "type": "CARD_UPDATED",
                    "payload": {"assignment_id": str(assignment_id)},
                }
            ),
        )


async def connected(client: httpx.AsyncClient, event: dict[str, Any]) -> None:
    channel = event["channel"]
    channel_id = channel["id"]
    if event.get("args") == ["recording"] and channel["name"].startswith("Snoop/"):
        return
    try:
        call_id = UUID(event["args"][0])
    except (ValueError, IndexError):
        await ari(client, "DELETE", f"/channels/{channel_id}")
        return
    async with session_factory() as db:
        original = await db.get(SipCall, call_id)
        if original is None:
            await ari(client, "DELETE", f"/channels/{channel_id}")
            return
        assignment, lesson = await own_assignment(
            db, original.assignment_id, original.user_id, lock=True
        )
        row = await db.scalar(select(SipCall).where(SipCall.id == call_id).with_for_update())
        if row is None or row.state not in ACTIVE_STATES:
            await ari(client, "DELETE", f"/channels/{channel_id}")
            return
        user = await db.get(User, row.user_id)
        if (
            not user
            or not user.is_active
            or not lesson
            or lesson.status != "RUNNING"
            or assignment.state == "CLOSED"
            or not channel["name"].startswith(f"PJSIP/{endpoint_name(row.user_id)}-")
            or (
                row.answered_at is None
                and (datetime.now(UTC) - row.created_at).total_seconds() > 60
            )
        ):
            await ari(client, "DELETE", f"/channels/{channel_id}")
            return
        if row.answered_at is not None:
            # A token is single use; replay cannot replace an established channel.
            if row.channel_id != channel_id:
                await ari(client, "DELETE", f"/channels/{channel_id}")
            return
        await ari(client, "POST", f"/channels/{channel_id}/answer")
        now = datetime.now(UTC)
        row.channel_id, row.state, row.answered_at = channel_id, "CONNECTED", now
        if row.direction == "INBOUND" and assignment.opened_at is None:
            assignment.opened_at = assignment.primary_status_at = now
            assignment.state = "PRIMARY_SET"
            assert assignment.delivered_at is not None
            db.add(
                StatusEvent(
                    assignment_id=assignment.id,
                    status="RECEIVED",
                    is_automatic=True,
                    elapsed_ms=max(
                        0, round((now - assignment.delivered_at).total_seconds() * 1000)
                    ),
                    created_at=now,
                )
            )
            for action in ("CARD_OPENED", "INCOMING_ACCEPTED"):
                db.add(InteractionEvent(assignment_id=assignment.id, kind=action, created_at=now))
                db.add(
                    AuditLog(
                        user_id=user.id,
                        action=action,
                        entity_type="assignment",
                        entity_id=assignment.id,
                        created_at=now,
                    )
                )
        db.add(
            AuditLog(
                user_id=user.id,
                action="SIP_CALL_CONNECTED",
                entity_type="sip_call",
                entity_id=row.id,
                created_at=now,
            )
        )
        await db.commit()
        recorder_id = f"record-{row.id}"
        await ari(
            client,
            "POST",
            f"/channels/{channel_id}/snoop",
            app="dispatcher",
            appArgs="recording",
            spy="both",
            snoopId=recorder_id,
        )
        await ari(
            client,
            "POST",
            f"/channels/{recorder_id}/record",
            name=row.id.hex,
            format="wav",
            maxDurationSeconds=3600,
            beep="false",
            ifExists="fail",
        )
        await ari(
            client,
            "POST",
            f"/channels/{channel_id}/play",
            media=f"sound:{ROOT}/speech/{row.media_name}",
        )
        await notify(lesson.teacher_id, user.id, assignment.id, "SIP_CALL_CONNECTED")


async def ended(event: dict[str, Any]) -> None:
    async with session_factory() as db:
        row = await db.scalar(
            select(SipCall)
            .where(SipCall.channel_id == event["channel"]["id"], SipCall.state.in_(ACTIVE_STATES))
            .with_for_update()
        )
        if row:
            row.ended_at = datetime.now(UTC)
            row.state = "ENDED" if row.answered_at else "FAILED"
            row.failure_reason = None if row.answered_at else "Вызов не принят. Можно повторить."
            db.add(
                AuditLog(
                    user_id=row.user_id,
                    action="SIP_CALL_ENDED",
                    entity_type="sip_call",
                    entity_id=row.id,
                    payload={"state": row.state},
                    created_at=row.ended_at,
                )
            )
            await db.commit()

            assignment = await db.get(Assignment, row.assignment_id)
            assert assignment is not None
            lesson = await db.get(Lesson, assignment.lesson_id)
            assert lesson is not None
            await notify(lesson.teacher_id, row.user_id, row.assignment_id, "SIP_CALL_ENDED")


async def reconcile(client: httpx.AsyncClient) -> None:
    channels = {item["id"] for item in await ari(client, "GET", "/channels")}
    async with session_factory() as db:
        rows = list(
            await db.scalars(
                select(SipCall)
                .where(SipCall.state.in_(ACTIVE_STATES))
                .with_for_update(skip_locked=True)
            )
        )
        now = datetime.now(UTC)
        for row in rows:
            assignment = await db.get(Assignment, row.assignment_id)
            assert assignment is not None
            lesson = await db.get(Lesson, assignment.lesson_id)
            user = await db.get(User, row.user_id)
            age = (now - row.created_at).total_seconds()
            invalid = (
                not user
                or not user.is_active
                or not lesson
                or lesson.status != "RUNNING"
                or assignment.state == "CLOSED"
                or age > runtime_configuration.current.sip.max_call_seconds
            )
            missing = row.channel_id and row.channel_id not in channels and age > 5
            timeout = (
                row.answered_at is None
                and age > runtime_configuration.current.sip.unanswered_seconds
            )
            if invalid or missing or timeout:
                if row.channel_id in channels:
                    await ari(client, "DELETE", f"/channels/{row.channel_id}")
                row.state = "ENDED" if row.answered_at else "FAILED"
                row.ended_at = now
                row.failure_reason = "Связь завершена. Заполненная карточка сохранена."
                db.add(
                    AuditLog(
                        user_id=row.user_id,
                        action="SIP_CALL_ENDED",
                        entity_type="sip_call",
                        entity_id=row.id,
                        payload={"state": row.state},
                        created_at=now,
                    )
                )
            elif row.state == "REQUESTED" and row.direction == "INBOUND":
                row.channel_id = str(row.id)
                row.state = "RINGING"
                try:
                    await ari(
                        client,
                        "POST",
                        "/channels",
                        endpoint=f"PJSIP/{endpoint_name(row.user_id)}",
                        app="dispatcher",
                        appArgs=row.id.hex,
                        channelId=row.channel_id,
                        callerId="Учебный заявитель <112>",
                        timeout=runtime_configuration.current.sip.inbound_ring_seconds,
                    )
                except httpx.HTTPStatusError:
                    row.state, row.ended_at = "FAILED", now
                    row.failure_reason = (
                        "Гарнитура не зарегистрирована. Включите телефон и повторите."
                    )
        await db.commit()


async def maintain(client: httpx.AsyncClient) -> None:
    while True:
        try:
            await provision()
            await reconcile(client)
            await deliver_incoming(client)
        except Exception:
            logger.exception("SIP: подготовка или синхронизация недоступна")
        await asyncio.sleep(2)


async def deliver_incoming(client: httpx.AsyncClient) -> None:
    online = {
        row["resource"]
        for row in await ari(client, "GET", "/endpoints")
        if row["technology"] == "PJSIP" and row["state"] == "online"
    }
    async with session_factory() as db:
        candidates = list(
            await db.scalars(
                select(Assignment)
                .join(Lesson)
                .where(
                    Lesson.status == "RUNNING",
                    Lesson.settings["incoming_channel"].astext == "VOICE",
                    Assignment.task_mode == "CARD_ENTRY",
                    Assignment.opened_at.is_(None),
                    Assignment.delivered_at.is_not(None),
                    Assignment.state != "CLOSED",
                    ~select(SipCall.id).where(SipCall.assignment_id == Assignment.id).exists(),
                )
                .order_by(Assignment.delivered_at, Assignment.id)
                .with_for_update(of=Assignment, skip_locked=True)
            )
        )
        for assignment in candidates:
            if endpoint_name(assignment.student_id) not in online:
                continue
            user = await db.scalar(
                select(User).where(User.id == assignment.student_id).with_for_update()
            )
            if (
                not user
                or not user.is_active
                or await db.scalar(
                    select(SipCall.id)
                    .where(SipCall.user_id == user.id, SipCall.state.in_(ACTIVE_STATES))
                    .limit(1)
                )
            ):
                continue
            scenario = await db.get(Scenario, assignment.scenario_id)
            assert scenario is not None
            media_name = prepare_speech(incoming_message(scenario.card_payload))
            if not (ROOT / "speech" / f"{media_name}.wav").is_file():
                continue
            row = SipCall(
                user_id=user.id,
                assignment_id=assignment.id,
                direction="INBOUND",
                media_name=media_name,
                created_at=datetime.now(UTC),
            )
            db.add(row)
            await db.flush()
            db.add(
                AuditLog(
                    user_id=user.id,
                    action="SIP_CALL_REQUESTED",
                    entity_type="sip_call",
                    entity_id=row.id,
                    payload={"direction": "INBOUND", "automatic": True},
                )
            )
        await db.commit()


async def run() -> None:
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    configuration = await runtime_configuration.start("sip_worker")
    auth = httpx.BasicAuth("dispatcher", control_password(settings.jwt_secret))
    async with httpx.AsyncClient(
        auth=auth, timeout=3, trust_env=False, verify=http_verify()
    ) as client:
        task = asyncio.create_task(maintain(client))
        try:
            while True:
                try:
                    authorization = auth.auth_flow(httpx.Request("GET", ARI))
                    header = next(authorization).headers["Authorization"]
                    async with connect(
                        ARI.replace("https://", "wss://").replace("http://", "ws://")
                        + "/events?app=dispatcher",
                        additional_headers={"Authorization": header},
                        **websocket_tls(ARI),
                    ) as socket:
                        async for message in socket:
                            event = json.loads(message)
                            try:
                                if event["type"] == "StasisStart":
                                    await connected(client, event)
                                elif event["type"] in ("StasisEnd", "ChannelDestroyed"):
                                    await ended(event)
                            except Exception:
                                logger.exception("SIP: обработка события не завершена")
                except Exception:
                    logger.exception("SIP: соединение с локальной АТС потеряно")
                    await asyncio.sleep(1)
        finally:
            task.cancel()
            configuration.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(configuration, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(run())
