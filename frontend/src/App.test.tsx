import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HealthScreen as App } from "./HealthScreen";
import { strings } from "./lib/strings";

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

describe("connection screen", () => {
  it("shows server state and keeps training label visible", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "ok" }), {
          headers: { "x-request-id": "request-test" },
        }),
      ),
    );
    renderApp();
    expect(screen.getByText(strings.checking)).toBeInTheDocument();
    expect(await screen.findByText(`✓ ${strings.ready}`)).toBeInTheDocument();
    expect(screen.getByText(strings.training)).toBeInTheDocument();
    expect(
      screen.getByText(`${strings.requestId}: request-test`),
    ).toBeInTheDocument();
  });

  it("offers a working retry after a connection failure", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Network"))
      .mockResolvedValue(new Response(JSON.stringify({ status: "ok" })));
    vi.stubGlobal("fetch", fetchMock);
    renderApp();
    expect(await screen.findByText(`× ${strings.error}`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: strings.retry }));
    expect(await screen.findByText(`✓ ${strings.ready}`)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not display a malformed response as success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}")));
    renderApp();
    expect(await screen.findByText(`× ${strings.error}`)).toBeInTheDocument();
  });
});
