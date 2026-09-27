from enum import StrEnum


class ResponseStatus(StrEnum):
    ADDED = "ADDED"
    RECEIVED = "RECEIVED"
    ACCEPTED = "ACCEPTED"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    RESPONSE_STARTED = "RESPONSE_STARTED"
    ARRIVED = "ARRIVED"
    WORK_IN_PROGRESS = "WORK_IN_PROGRESS"
    WORK_COMPLETED = "WORK_COMPLETED"
    WORK_REFUSED = "WORK_REFUSED"


class AssignmentState(StrEnum):
    QUEUED = "QUEUED"
    DELIVERED = "DELIVERED"
    OPENED = "OPENED"
    PRIMARY_SET = "PRIMARY_SET"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class ModifierCode(StrEnum):
    THREAT_TO_PEOPLE = "THREAT_TO_PEOPLE"
    VICTIMS = "VICTIMS"
    FATALITIES = "FATALITIES"
    NO_ACCESS = "NO_ACCESS"
    ROAD_BLOCKED = "ROAD_BLOCKED"
    CHILD_INVOLVED = "CHILD_INVOLVED"


STATUS_LABELS = {
    ResponseStatus.ADDED: "Добавлена",
    ResponseStatus.RECEIVED: "Получена",
    ResponseStatus.ACCEPTED: "Принята",
    ResponseStatus.NOT_ACCEPTED: "Не принята",
    ResponseStatus.RESPONSE_STARTED: "Реагирование начато",
    ResponseStatus.ARRIVED: "Прибыли",
    ResponseStatus.WORK_IN_PROGRESS: "Работы ведутся",
    ResponseStatus.WORK_COMPLETED: "Работы завершены",
    ResponseStatus.WORK_REFUSED: "Отказ от работ",
}
MODIFIER_LABELS = {
    "THREAT_TO_PEOPLE": "Угроза людям",
    "VICTIMS": "Есть пострадавшие",
    "FATALITIES": "Есть погибшие",
    "NO_ACCESS": "Нет доступа",
    "ROAD_BLOCKED": "Движение перекрыто",
    "CHILD_INVOLVED": "Участвует ребёнок",
}
