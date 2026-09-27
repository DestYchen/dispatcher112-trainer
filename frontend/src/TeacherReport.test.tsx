import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { TeacherReport } from "./TeacherReport";
import { strings } from "./lib/strings";

function openReport() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <TeacherReport lessonId="lesson" />
    </QueryClientProvider>,
  );
}
it("renders exactly seven report columns, signed time and manual correction", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          lesson: { title: "Занятие", started_at: null, finished_at: null },
          cards_total: 1,
          columns: [
            "Фамилия",
            "АРМ",
            "Время",
            "Ошибки",
            "Орфогр.",
            "Уровень",
            "Балл",
          ],
          students: [
            {
              student_id: "student",
              short_name: "Иванов И.",
              workstation: "07",
              time_deviation_pct: 12,
              errors: 2,
              spelling_errors: 1,
              level: "базовый",
              total: 76,
              manually_corrected: true,
            },
          ],
          reaction_distribution: [{ label: "До 15 с", count: 1 }],
          heatmap: { violations: [], rows: [] },
        }),
      ),
    ),
  );
  openReport();
  const table = await screen.findByRole("table", {
    name: strings.lessonReport,
  });
  expect(within(table).getAllByRole("columnheader")).toHaveLength(7);
  expect(within(table).getByText("+12 %")).toBeInTheDocument();
  expect(
    within(table).getByTitle(strings.manualCorrection),
  ).toBeInTheDocument();
  expect(screen.getByText(strings.noViolations)).toBeInTheDocument();
});
it("shows a recoverable report error", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ error: { message: "Отчёт недоступен" } }),
          { status: 503 },
        ),
      ),
  );
  openReport();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Отчёт недоступен",
  );
  expect(screen.getByRole("button", { name: strings.retry })).toBeEnabled();
});
