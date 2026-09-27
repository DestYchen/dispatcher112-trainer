from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeliness: float = Field(default=0.30, ge=0, le=1)
    correctness: float = Field(default=0.40, ge=0, le=1)
    completeness: float = Field(default=0.20, ge=0, le=1)
    literacy: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def valid_sum(self) -> Self:
        if abs(sum(self.model_dump().values()) - 1) > 0.000001:
            raise ValueError("Сумма весов должна быть равна единице.")
        return self


class SuccessCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_total: float = Field(default=70, ge=0, le=100)
    max_errors: int = Field(default=5, ge=0, le=1000)
    max_spelling_errors: int = Field(default=3, ge=0, le=1000)
    min_words: int = Field(default=0, ge=0, le=100)
    sentence_end_required: bool = False
    required_terms: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_terms(self) -> Self:
        if any(not term.isalpha() or len(term) > 50 for term in self.required_terms):
            raise ValueError(
                "Требования к словам: до 20 слов, только буквы, не более 50 букв в слове."
            )
        if len(set(term.casefold().replace("ё", "е") for term in self.required_terms)) != len(
            self.required_terms
        ):
            raise ValueError("Обязательные слова не должны повторяться.")
        return self


class LessonSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_status_deadline_sec: int = Field(default=30, ge=1, le=3600)
    card_processing_deadline_sec: int = Field(default=180, ge=1, le=86400)
    max_concurrent_cards: int = Field(default=3, ge=1, le=20)
    card_interval_sec: int = Field(default=45, ge=1, le=3600)
    difficulty_range: tuple[int, int] = (1, 6)
    incident_group_ids: list[UUID] = Field(default_factory=list)
    scenario_mode: Literal["GENERATED", "TICKETS", "MIXED"] = "GENERATED"
    training_mode: Literal["CARD_ACTIONS", "CARD_ENTRY", "MIXED"] = "CARD_ACTIONS"
    incoming_channel: Literal["TEXT", "VOICE"] = "TEXT"
    grammar_check_enabled: bool = True
    hints_enabled: bool = False
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    success_criteria: SuccessCriteria | None = None

    @model_validator(mode="after")
    def valid_difficulty(self) -> Self:
        if not 1 <= self.difficulty_range[0] <= self.difficulty_range[1] <= 10:
            raise ValueError("Сложность должна быть от 1 до 10.")
        return self
