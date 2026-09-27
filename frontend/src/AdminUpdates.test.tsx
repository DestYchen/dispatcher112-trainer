import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AdminUpdates, type UpdateIndex } from "./AdminUpdates";
import { updateStrings as t } from "./lib/updateStrings";
import { strings } from "./lib/strings";

const index = (): UpdateIndex => ({
  trust: { configured: true, fingerprint: "a".repeat(64), error: null },
  limits: {
    archive_bytes: 264 * 1024 * 1024,
    packages: 20,
    storage_bytes: 2 ** 31,
  },
  packages: [],
  updates: [],
  current: null,
  maintenance: { enabled: false, reason: "" },
  execution: "LOCAL_COMMAND",
});
function open() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AdminUpdates />
    </QueryClientProvider>,
  );
}
function respond(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it.each(["server", "truncated"])(
  "preserves the form after a %s download failure and retries the actual archive",
  async (failure) => {
    const value = index();
    value.packages = [
      {
        id: "package-id",
        filename: "uploaded.zip",
        version: "1.2.3",
        description: "Пакет для проверки скачивания",
        size: 5,
        uploaded_at: "2026-09-26T10:00:00Z",
        program_files: 100,
        sha256: "a".repeat(64),
        publisher_sha256: "b".repeat(64),
      },
    ];
    let attempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (url.endsWith("/packages/package-id")) {
          attempts += 1;
          if (attempts === 1)
            return Promise.resolve(
              failure === "server"
                ? respond(
                    { error: { message: "Проверка архива недоступна" } },
                    503,
                  )
                : new Response("zip"),
            );
          return Promise.resolve(new Response("12345"));
        }
        return Promise.resolve(respond(value));
      }),
    );
    const createObjectURL = vi.fn(() => "blob:verified-package");
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.download).toBe("release-1.2.3.zip");
      });
    open();
    await screen.findByText(value.packages[0].description);
    fireEvent.change(screen.getByLabelText(t.reason), {
      target: { value: "Подготовка обновления на сервере" },
    });
    fireEvent.click(screen.getByRole("button", { name: t.downloadPackage }));
    await screen.findByText(
      failure === "server" ? "Проверка архива недоступна" : t.downloadFailed,
    );
    expect(click).not.toHaveBeenCalled();
    expect(screen.getByLabelText(t.reason)).toHaveValue(
      "Подготовка обновления на сервере",
    );
    fireEvent.click(screen.getByRole("button", { name: t.downloadPackage }));
    await screen.findByText(t.downloaded);
    expect(attempts).toBe(2);
    expect(createObjectURL.mock.calls).toHaveLength(1);
    expect(click).toHaveBeenCalledOnce();
  },
);

it("explains both empty catalogs and blocks upload without a trusted publisher", async () => {
  const value = index();
  value.trust.configured = false;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => Promise.resolve(respond(value))),
  );
  open();
  expect(await screen.findByText(t.noPackages)).toBeInTheDocument();
  expect(screen.getByText(t.noHistory)).toBeInTheDocument();
  expect(screen.getByText(t.noKey)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(t.uploadFile), {
    target: { files: [new File(["zip"], "release.zip")] },
  });
  expect(screen.getByRole("button", { name: t.upload })).toBeDisabled();
});

it("uses skeletons while loading and offers a real retry after an error", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(
      respond({ error: { message: "Отключение сервера" } }, 503),
    )
    .mockImplementation(() => Promise.resolve(respond(index())));
  vi.stubGlobal("fetch", fetcher);
  open();
  expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
  expect(await screen.findByText(t.unavailable)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: t.refresh }));
  expect(await screen.findByText(t.noPackages)).toBeInTheDocument();
});

it("keeps the chosen archive after a failed verification and sends the bytes unchanged", async () => {
  const fetcher = vi
    .fn()
    .mockImplementation((url: string) =>
      Promise.resolve(
        url.includes("/packages?")
          ? respond({ error: { message: "Подпись не прошла проверку" } }, 400)
          : respond(index()),
      ),
    );
  vi.stubGlobal("fetch", fetcher);
  open();
  await screen.findByText(t.noPackages);
  const file = new File(["fixture archive bytes"], "release.zip", {
    type: "application/zip",
  });
  const input = screen.getByLabelText<HTMLInputElement>(t.uploadFile);
  fireEvent.change(input, { target: { files: [file] } });
  expect(screen.getByRole("button", { name: t.upload })).toBeEnabled();
  fireEvent.submit(input.closest("form")!);
  expect(
    await screen.findByText("Подпись не прошла проверку"),
  ).toBeInTheDocument();
  expect(input.files?.[0]).toBe(file);
  const call = fetcher.mock.calls.find(([url]) => url.includes("/packages?"));
  expect(call?.[1].body).toBe(file);
  expect(call?.[1].headers.get("Content-Type")).toBe(
    "application/octet-stream",
  );
  expect(screen.getByRole("button", { name: t.upload })).toBeEnabled();
});

it("retains the reason and identity for retries and never calls a local request installed", async () => {
  const value = index();
  value.packages = [
    {
      id: "package-id",
      filename: "release.zip",
      version: "1.2.3",
      description: "Проверенный выпуск",
      size: 5000,
      uploaded_at: "2026-09-26T10:00:00Z",
      program_files: 100,
      sha256: "a".repeat(64),
      publisher_sha256: "b".repeat(64),
    },
  ];
  const bodies: Record<string, string>[] = [];
  const fetcher = vi
    .fn()
    .mockImplementation((url: string, options: RequestInit) => {
      if (url.endsWith("/requests")) {
        const body = JSON.parse(String(options.body));
        bodies.push(body);
        if (bodies.length === 1)
          return Promise.resolve(
            respond({ error: { message: "Временная ошибка запроса" } }, 503),
          );
        return Promise.resolve(
          respond({
            filename: `software-update-${body.id}.json`,
            execution: "LOCAL_COMMAND",
            request: {
              signature: "signature",
              value: {
                ...body,
                update_id: body.id,
                version: "1.2.3",
                expires_at: "2026-09-27T10:00:00Z",
              },
            },
          }),
        );
      }
      return Promise.resolve(respond(value));
    });
  vi.stubGlobal("fetch", fetcher);
  open();
  await screen.findByText("Проверенный выпуск");
  fireEvent.change(screen.getByLabelText(t.package), {
    target: { value: "package-id" },
  });
  fireEvent.change(screen.getByLabelText(t.reason), {
    target: { value: "Плановое обновление сервера" },
  });
  fireEvent.click(screen.getByRole("button", { name: t.create }));
  await screen.findByText("Временная ошибка запроса");
  expect(screen.getByLabelText(t.reason)).toHaveValue(
    "Плановое обновление сервера",
  );
  fireEvent.click(screen.getByRole("button", { name: strings.retry }));
  expect(
    await screen.findByRole("heading", { name: t.requestReady }),
  ).toBeInTheDocument();
  expect(bodies[0]).toEqual(bodies[1]);
  expect(screen.queryByText(t.phases.ACTIVE)).not.toBeInTheDocument();
  expect(
    screen.getByText(/python scripts\/install_update.py execute-request/),
  ).toHaveTextContent("--package release-1.2.3.zip");
});

it("shows actual READY and ACTIVE phases and disables a stale activation choice", async () => {
  const value = index();
  value.current = { id: "update-id", phase: "READY", version: "1.2.3" };
  value.maintenance.enabled = true;
  value.maintenance.update_id = "update-id";
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => Promise.resolve(respond(value))),
  );
  open();
  await screen.findByText(t.readyHelp);
  fireEvent.change(screen.getByLabelText(t.action), {
    target: { value: "activate" },
  });
  fireEvent.change(screen.getByLabelText(t.reason), {
    target: { value: "Проверка версии завершена" },
  });
  expect(screen.getByRole("button", { name: t.create })).toBeEnabled();
  value.current.phase = "ACTIVE";
  value.maintenance.enabled = false;
  fireEvent.click(screen.getByRole("button", { name: t.refresh }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: t.create })).toBeDisabled(),
  );
  expect(screen.getByText(t.staleAction)).toBeInTheDocument();
});
