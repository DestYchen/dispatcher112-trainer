import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { FileIntegrity, type IntegrityResult } from "./FileIntegrity";
import { integrityStrings as t } from "./lib/integrityStrings";

function respond(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function open(controls?: Parameters<typeof FileIntegrity>[0]["controls"]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <FileIntegrity controls={controls} />
    </QueryClientProvider>,
  );
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("explains the missing baseline and only offers verified snapshots", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation(() => Promise.resolve(respond({ status: "EMPTY" }))),
  );
  open({ disabled: false, snapshots: [], submit: vi.fn() });
  expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
  await screen.findByText(t.states.EMPTY);
  expect(screen.getByText(t.noSnapshots)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: t.approve })).toBeDisabled();
});

it("shows detailed violations and truncation without claiming the whole system is intact", async () => {
  const result: IntegrityResult = {
    status: "FAIL",
    checked_at: "2026-09-27T00:00:00Z",
    files: 600,
    expected_files: 601,
    issues_total: 250,
    truncated: true,
    issues: [
      { scope: "program", path: "backend/app/main.py", kind: "CHANGED" },
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => Promise.resolve(respond(result))),
  );
  open();
  await screen.findByText(t.states.FAIL);
  expect(screen.getByText(t.kinds.CHANGED)).toBeInTheDocument();
  expect(screen.getByText(t.truncated)).toBeInTheDocument();
  expect(screen.getByText(t.limits)).toBeInTheDocument();
  expect(screen.queryByText(t.states.OK)).not.toBeInTheDocument();
});

it("retains approval fields after transport failure and invalidates cached success", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(respond({ status: "OK" }))
    .mockImplementation(() =>
      Promise.resolve(respond({ error: { message: "Недоступно" } }, 503)),
    );
  vi.stubGlobal("fetch", fetcher);
  open({
    disabled: false,
    snapshots: [{ name: "snapshot", created_at: null }],
    submit: vi.fn(),
  });
  await screen.findByText(t.states.OK);
  fireEvent.change(screen.getByLabelText(t.snapshot), {
    target: { value: "snapshot" },
  });
  fireEvent.change(screen.getByLabelText(t.reason), {
    target: { value: "Проверенная поставка" },
  });
  fireEvent.click(screen.getByRole("button", { name: t.refresh }));
  await screen.findByText(t.unavailable);
  expect(screen.getByLabelText(t.reason)).toHaveValue("Проверенная поставка");
  expect(screen.getByLabelText(t.snapshot)).toHaveValue("snapshot");
  expect(screen.queryByText(t.states.OK)).not.toBeInTheDocument();
});

it("requires maintenance and a reason, and reuses the request identity on a retry", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation(() => Promise.resolve(respond({ status: "EMPTY" }))),
  );
  const submit = vi.fn();
  open({
    disabled: false,
    snapshots: [{ name: "verified-snapshot", created_at: null }],
    submit,
  });
  await screen.findByText(t.states.EMPTY);
  fireEvent.change(screen.getByLabelText(t.snapshot), {
    target: { value: "verified-snapshot" },
  });
  expect(screen.getByRole("button", { name: t.approve })).toBeDisabled();
  fireEvent.change(screen.getByLabelText(t.reason), {
    target: { value: "Проверенная поставка" },
  });
  fireEvent.click(screen.getByRole("button", { name: t.approve }));
  fireEvent.click(screen.getByRole("button", { name: t.approve }));
  expect(submit).toHaveBeenCalledTimes(2);
  expect(submit.mock.calls[0]).toEqual(submit.mock.calls[1]);
  expect(submit.mock.calls[0][0]).toMatchObject({
    kind: "backup_integrity_baseline",
    service: "backup",
    snapshot: "verified-snapshot",
    reason: "Проверенная поставка",
  });
});

it("blocks mutation controls outside maintenance", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation(() => Promise.resolve(respond({ status: "EMPTY" }))),
  );
  const submit = vi.fn();
  open({
    disabled: true,
    snapshots: [{ name: "snapshot", created_at: null }],
    submit,
  });
  await screen.findByText(t.states.EMPTY);
  expect(screen.getByRole("button", { name: t.check })).toBeDisabled();
  expect(screen.getByRole("button", { name: t.approve })).toBeDisabled();
  expect(submit).not.toHaveBeenCalled();
});
