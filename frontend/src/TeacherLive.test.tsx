import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { RealtimeEvent } from "./lib/realtime";
import { TeacherLive } from "./TeacherLive";

const state = vi.hoisted(() => ({
  requests: 0,
  status: "RUNNING",
  operations: [] as string[],
  listener: undefined as ((event: RealtimeEvent) => void) | undefined,
}));
vi.mock("./lib/realtime", () => ({
  useServerNow: () => 0,
  useRealtime: (_role: string, listener: (event: RealtimeEvent) => void) => {
    state.listener = listener;
    return { connected: true, retryIn: 0, reconnect: vi.fn() };
  },
}));
vi.mock("./api/client", () => ({
  allItems: async () => ({ items: [] }),
  api: async (path: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      state.operations.push(path);
      state.status = path.endsWith("/start") ? "RUNNING" : "FINISHED";
    }
    state.requests += 1;
    return {
      server_time: "2026-09-22T00:00:00Z",
      lesson_status: state.status,
      elapsed_sec: 1,
      aggregate: {
        cards_delivered: 1,
        cards_closed: 0,
        cards_expired: 0,
        avg_primary_delay_ms: null,
        top_violations: [],
      },
      students:
        state.requests === 1
          ? []
          : [
              {
                student_id: "student",
                short_name: "Иванов И.",
                workstation: "07",
                online: true,
                active_cards: 1,
                closed: 0,
                expired: 0,
                current_score: null,
                last_action: null,
                alert: null,
              },
            ],
    };
  },
}));
afterEach(() => vi.useRealTimers());

it("coalesces twenty class events into one fresh snapshot within two seconds", async () => {
  vi.useFakeTimers();
  state.requests = 0;
  state.status = "RUNNING";
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <TeacherLive
        lessonId="lesson"
        title="Занятие"
        refreshLessons={async () => undefined}
      />
    </QueryClientProvider>,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(state.requests).toBe(1);
  act(() => {
    for (let index = 0; index < 20; index += 1)
      state.listener?.({
        type: "STUDENT_ACTION",
        ts: "2026-09-22T00:00:00Z",
        payload: {},
      });
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(999);
  });
  expect(state.requests).toBe(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(state.requests).toBe(2);
  expect(screen.getByText("Иванов И.")).toBeInTheDocument();
});

it("starts and finishes a lesson from the console and refreshes the list", async () => {
  state.requests = 0;
  state.status = "PLANNED";
  state.operations = [];
  const refreshLessons = vi.fn(async () => undefined);
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <TeacherLive
        lessonId="prepared"
        title="Показ"
        refreshLessons={refreshLessons}
      />
    </QueryClientProvider>,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Начать занятие" }),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Завершить занятие" }),
  );
  expect(await screen.findByText("Иванов И.")).toBeInTheDocument();
  await vi.waitFor(() => expect(refreshLessons).toHaveBeenCalledTimes(2));
  expect(state.operations).toEqual([
    "/teacher/lessons/prepared/start",
    "/teacher/lessons/prepared/finish",
  ]);
  expect(screen.queryByRole("button", { name: "Начать занятие" })).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Завершить занятие" }),
  ).toBeNull();
});
