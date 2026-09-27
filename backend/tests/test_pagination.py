from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lesson
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


async def test_teacher_lists_page_without_duplicates_or_missing_rows(
    client: AsyncClient, db: AsyncSession
) -> None:
    lesson, _ = await lesson_fixture(db, 1)
    db.add_all(
        [
            Lesson(
                title=f"Страница {index}", teacher_id=lesson.teacher_id, settings=lesson.settings
            )
            for index in range(3)
        ]
    )
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    for path in ("/teacher/lessons", "/teacher/scenarios?status=APPROVED"):
        separator = "&" if "?" in path else "?"
        expected = (await client.get(f"/api/v1{path}{separator}limit=200")).json()["items"]
        items = []
        cursor = None
        while True:
            response = await client.get(
                f"/api/v1{path}{separator}limit=1" + (f"&cursor={cursor}" if cursor else "")
            )
            assert response.status_code == 200
            items.extend(response.json()["items"])
            cursor = response.json()["next_cursor"]
            if not cursor:
                break
        assert items == expected
        assert len({item["id"] for item in items}) == len(items)
        assert (await client.get(f"/api/v1{path}{separator}cursor=invalid!")).status_code == 400
