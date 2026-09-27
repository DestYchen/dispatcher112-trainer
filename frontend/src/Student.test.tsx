import { act, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { api, RequestError } from "./api/client";
import { enqueue, readActions } from "./lib/offline";
import type { RealtimeEvent } from "./lib/realtime";
import { Student } from "./Student";

const handlers = vi.hoisted(() => ({
  event: undefined as ((event: RealtimeEvent) => void) | undefined,
}));
vi.mock("./lib/auth", () => ({
  useAuth: () => ({
    user: {
      id: "student",
      full_name: "Участник",
      service: null,
    },
  }),
}));
vi.mock("./lib/realtime", () => ({
  synchronize: vi.fn(),
  useServerNow: () => 0,
  useRealtime: (_role: string, onEvent: (event: RealtimeEvent) => void) => {
    handlers.event = onEvent;
    return { connected: true, retryIn: 1, reconnect: vi.fn() };
  },
}));
vi.mock("./api/client", async (original) => ({
  ...(await original<typeof import("./api/client")>()),
  api: vi.fn(),
}));

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api).mockReset();
});

async function prepare(failure: Error) {
  const action = enqueue("student", {
    assignmentId: "card",
    status: "ACCEPTED",
    comment: "",
  });
  const attempts: string[] = [];
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith("/status")) {
      attempts.push(new Headers(options?.headers).get("Idempotency-Key")!);
      if (attempts.length === 1) throw failure;
      return {};
    }
    return { cards: [], lesson: null, server_time: "2026-09-27T12:00:00Z" };
  });
  const view = render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
          },
        })
      }
    >
      <Student />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(api).toHaveBeenCalled());
  return { action, attempts, view };
}

async function heartbeat() {
  await act(async () =>
    handlers.event!({
      type: "HEARTBEAT",
      ts: "2026-09-27T12:00:00Z",
      payload: {},
    }),
  );
}

it("replays HTTP actions after recovery while the WebSocket remains connected", async () => {
  const { action, attempts, view } = await prepare(
    new TypeError("Network unavailable"),
  );
  await heartbeat();
  await waitFor(() => expect(attempts).toHaveLength(1));
  expect(readActions("student")).toHaveLength(1);
  await heartbeat();
  await waitFor(() => expect(readActions("student")).toHaveLength(0));
  expect(attempts).toEqual([action.key, action.key]);
  await heartbeat();
  expect(attempts).toHaveLength(2);
  view.unmount();
});

it("does not automatically retry a rejected business action", async () => {
  const { attempts, view } = await prepare(
    new RequestError(409, "CARD_CLOSED", "Карточка закрыта"),
  );
  await heartbeat();
  await waitFor(() =>
    expect(readActions("student")[0].error).toBe("Карточка закрыта"),
  );
  await heartbeat();
  expect(attempts).toHaveLength(1);
  view.unmount();
});
