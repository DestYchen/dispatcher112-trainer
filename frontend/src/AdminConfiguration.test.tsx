import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  AdminConfiguration,
  type ConfigurationIndex,
} from "./AdminConfiguration";
import { configurationStrings as t } from "./lib/configurationStrings";

const index = (): ConfigurationIndex => ({
  snapshot: {
    revision: 0,
    reason: "",
    changed_at: null,
    configuration: {
      database: {
        api_pool_size: 20,
        worker_pool_size: 5,
        sip_pool_size: 5,
        max_overflow: 5,
        pool_timeout_seconds: 10,
        statement_timeout_ms: 30000,
        lock_timeout_ms: 3000,
      },
      sip: {
        inbound_ring_seconds: 40,
        unanswered_seconds: 60,
        max_call_seconds: 3600,
      },
      logging: { level: "INFO", http_access: true },
    },
  },
  configured: false,
  maintenance: { enabled: true },
  database_connection_budget: 87,
  applied: false,
  missing: ["backend", "worker", "sip_worker"],
  processes: [],
});
function open() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AdminConfiguration />
    </QueryClientProvider>,
  );
  return client;
}
function respond(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("shows loading, error with retry, and explains defaults and absent processes", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(respond({ error: { message: "Нет связи" } }, 503))
    .mockImplementation(() => Promise.resolve(respond(index())));
  vi.stubGlobal("fetch", fetcher);
  open();
  expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
  await screen.findByText(t.unavailable);
  fireEvent.click(screen.getByRole("button", { name: t.refresh }));
  await screen.findByText(t.defaults);
  expect(screen.getByText(t.noProcesses)).toBeInTheDocument();
  expect(screen.queryByText(t.applied)).not.toBeInTheDocument();
});

it.each(["maintenance", "job", "update"])(
  "blocks saving while %s is unavailable",
  async (mode) => {
    const value = index();
    value.maintenance =
      mode === "maintenance"
        ? { enabled: false }
        : { enabled: true, [mode + "_id"]: "operation" };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(respond(value))),
    );
    open();
    await screen.findByText(t.defaults);
    fireEvent.change(screen.getByLabelText(t.reason), {
      target: { value: "Настройка комплекса" },
    });
    expect(screen.getByRole("button", { name: t.save })).toBeDisabled();
    expect(
      screen.getByText(mode === "maintenance" ? t.maintenanceRequired : t.busy),
    ).toBeInTheDocument();
  },
);

it("preserves the draft and request identity across failed saves and waits for real acknowledgements", async () => {
  const value = index();
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        requests.push(body);
        if (requests.length === 1)
          return Promise.resolve(
            respond({ error: { message: "Повторите сохранение" } }, 503),
          );
        value.snapshot = {
          ...value.snapshot,
          revision: 1,
          configuration: body.configuration,
        };
        value.configured = true;
        return Promise.resolve(
          respond({
            ...value,
            saved_revision: 1,
            replayed: true,
            superseded: false,
          }),
        );
      }
      return Promise.resolve(respond(value));
    }),
  );
  open();
  await screen.findByText(t.defaults);
  fireEvent.change(screen.getByLabelText(t.inbound_ring_seconds), {
    target: { value: "25" },
  });
  fireEvent.change(screen.getByLabelText(t.reason), {
    target: { value: "Настройка учебного звонка" },
  });
  fireEvent.click(screen.getByRole("button", { name: t.save }));
  await screen.findByText("Повторите сохранение");
  expect(screen.getByLabelText(t.inbound_ring_seconds)).toHaveValue(25);
  fireEvent.click(screen.getByRole("button", { name: t.save }));
  await screen.findByText(t.saved);
  expect(requests[0]).toEqual(requests[1]);
  expect(screen.getByText(t.pending)).toBeInTheDocument();
  expect(screen.queryByText(t.applied)).not.toBeInTheDocument();
  value.applied = true;
  value.missing = [];
  value.processes = (["backend", "worker", "sip_worker"] as const).map(
    (role) => ({
      id: role + "-id",
      role,
      revision: 1,
      error: null,
      at: new Date().toISOString(),
      pool_size: 5,
      checked_out: 0,
      statement_timeout_ms: 30000,
      lock_timeout_ms: 3000,
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: t.refresh }));
  await screen.findByText(t.applied);
  expect(screen.getAllByText(t.ready)).toHaveLength(3);
});

it("keeps edits on polling failure and never displays a cached success as confirmed", async () => {
  const value = index();
  value.applied = true;
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(respond(value))
    .mockImplementation(() =>
      Promise.resolve(respond({ error: { message: "Нет связи" } }, 503)),
    );
  vi.stubGlobal("fetch", fetcher);
  open();
  await screen.findByText(t.applied);
  fireEvent.change(screen.getByLabelText(t.api_pool_size), {
    target: { value: "15" },
  });
  fireEvent.change(screen.getByLabelText(t.reason), {
    target: { value: "Сохраняемый черновик" },
  });
  fireEvent.click(screen.getByRole("button", { name: t.refresh }));
  await screen.findByText(t.unavailable);
  expect(screen.getByLabelText(t.api_pool_size)).toHaveValue(15);
  expect(screen.getByLabelText(t.reason)).toHaveValue("Сохраняемый черновик");
  expect(screen.getByRole("button", { name: t.save })).toBeDisabled();
  expect(screen.queryByText(t.applied)).not.toBeInTheDocument();
});

it("requires explicit draft replacement after another administrator changes the revision", async () => {
  let value = index();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => Promise.resolve(respond(value))),
  );
  open();
  await screen.findByText(t.defaults);
  fireEvent.change(screen.getByLabelText(t.api_pool_size), {
    target: { value: "15" },
  });
  value = structuredClone(value);
  value.snapshot.revision = 1;
  value.snapshot.configuration.database.api_pool_size = 30;
  fireEvent.click(screen.getByRole("button", { name: t.refresh }));
  await screen.findByText(t.conflict);
  expect(screen.getByLabelText(t.api_pool_size)).toHaveValue(15);
  expect(screen.getByRole("button", { name: t.save })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: t.reload }));
  await waitFor(() =>
    expect(screen.getByLabelText(t.api_pool_size)).toHaveValue(30),
  );
  expect(screen.queryByText(t.conflict)).not.toBeInTheDocument();
});
