import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { LessonList, type LessonSummary } from "./LessonList";
import { strings } from "../lib/strings";

const lessons: LessonSummary[] = [
  { id: "old", title: "Архивное", status: "FINISHED" },
  {
    id: "next",
    title: "Пожар",
    status: "PLANNED",
    settings: { training_mode: "CARD_ACTIONS" },
  },
  { id: "now", title: "Текущее", status: "RUNNING" },
];

function show(pending = false) {
  const callbacks = {
    onLive: vi.fn(),
    onReport: vi.fn(),
    onPrepare: vi.fn(),
    onAction: vi.fn(),
  };
  render(<LessonList lessons={lessons} pending={pending} {...callbacks} />);
  return callbacks;
}

it("places live lessons first without changing supplied data and filters by title/status", () => {
  show();
  expect(
    screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.querySelector("strong")?.textContent),
  ).toEqual(["Текущее", "Пожар", "Архивное"]);
  expect(lessons[0].id).toBe("old");
  fireEvent.change(screen.getByLabelText(strings.lessonSearch), {
    target: { value: "  ПОЖАР  " },
  });
  expect(screen.getAllByRole("row")).toHaveLength(2);
  fireEvent.change(screen.getByLabelText(strings.lessonFilter), {
    target: { value: "FINISHED" },
  });
  expect(screen.getByText(strings.noMatchingLessons)).toBeVisible();
  fireEvent.click(
    screen.getByRole("button", { name: strings.clearLessonFilters }),
  );
  expect(screen.getAllByRole("row")).toHaveLength(4);
});

it("sends actions for the selected lesson and hides invalid lifecycle actions", () => {
  const calls = show();
  const planned = within(screen.getByText("Пожар").closest("tr")!);
  fireEvent.click(planned.getByRole("button", { name: strings.prepare }));
  fireEvent.click(planned.getByRole("button", { name: strings.startLesson }));
  fireEvent.click(planned.getByRole("button", { name: strings.liveConsole }));
  expect(calls.onPrepare).toHaveBeenCalledWith("next");
  expect(calls.onAction).toHaveBeenCalledWith("next", "start");
  expect(calls.onLive).toHaveBeenCalledWith(lessons[1]);
  const active = within(screen.getByText("Текущее").closest("tr")!);
  fireEvent.click(active.getByRole("button", { name: strings.finishLesson }));
  expect(calls.onAction).toHaveBeenCalledWith("now", "finish");
  const finished = within(screen.getByText("Архивное").closest("tr")!);
  expect(
    finished.queryByRole("button", { name: strings.startLesson }),
  ).toBeNull();
  expect(
    finished.queryByRole("button", { name: strings.finishLesson }),
  ).toBeNull();
  fireEvent.click(finished.getByRole("button", { name: strings.lessonReport }));
  expect(calls.onReport).toHaveBeenCalledWith("old");
});

it("prevents repeated lifecycle submissions while pending", () => {
  show(true);
  expect(
    screen.getByRole("button", { name: strings.startLesson }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: strings.finishLesson }),
  ).toBeDisabled();
});
