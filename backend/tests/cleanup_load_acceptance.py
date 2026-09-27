"""Deactivate only accounts proven to be created by the load fixture, preserving audit."""

import argparse
import asyncio

from sqlalchemy import or_, select

from app.db.base import session_factory
from app.db.models import AuditLog, Lesson, LessonParticipant, User


async def main(include_browser: bool = False) -> None:
    async with session_factory() as db, db.begin():
        active_students = (
            select(LessonParticipant.student_id).join(Lesson).where(Lesson.status == "RUNNING")
        )
        active_teachers = select(Lesson.teacher_id).where(Lesson.status == "RUNNING")
        actions = ["LOAD_USER_CREATED"]
        if include_browser:
            actions.append("BROWSER_ACCEPTANCE_USER_CREATED")
        created = select(AuditLog.entity_id).where(
            AuditLog.action.in_(actions),
            AuditLog.entity_type == "users",
        )
        rows = list(
            await db.scalars(
                select(User).where(
                    User.id.in_(created),
                    User.is_active.is_(True),
                    ~or_(User.id.in_(active_students), User.id.in_(active_teachers)),
                )
            )
        )
        for user in rows:
            user.is_active = False
            db.add(AuditLog(action="TEST_USER_DISABLED", entity_type="users", entity_id=user.id))
    print(f"Deactivated {len(rows)} finished load-test accounts; history preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.browser))
