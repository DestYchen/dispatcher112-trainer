import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as client from "../api/client";
import { AuthProvider, useAuth } from "./auth";

const user = {
  id: "student-id", login: "student", full_name: "Ученик", short_name: "У.",
  role: "STUDENT", service: null,
};

function Probe() {
  const auth = useAuth();
  return <div>
    <span>{auth.user ? "workspace remains mounted" : "login required"}</span>
    {auth.error && <span role="alert">{auth.error.message}</span>}
  </div>;
}

function mount() {
  const cache = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } });
  render(<QueryClientProvider client={cache}><AuthProvider><Probe /></AuthProvider></QueryClientProvider>);
  return cache;
}

describe("session refresh after a connection interruption", () => {
  it("keeps the existing workspace mounted when the session check has a network error", async () => {
    const api = vi.spyOn(client, "api").mockResolvedValue(user);
    const cache = mount();
    await screen.findByText("workspace remains mounted");
    api.mockRejectedValue(new TypeError("Failed to fetch"));
    await act(async () => { await cache.refetchQueries({ queryKey: ["auth"] }); });
    await waitFor(() => expect(cache.getQueryState(["auth"])?.status).toBe("error"));
    expect(screen.getByText("workspace remains mounted")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    cache.clear();
  });

  it("retries a transient online event failure and refreshes the session", async () => {
    const api = vi.spyOn(client, "api").mockResolvedValue(user);
    const cache = mount();
    await screen.findByText("workspace remains mounted");
    api.mockRejectedValueOnce(new TypeError("Network not ready")).mockResolvedValue(user);
    await act(async () => { await cache.refetchQueries({ queryKey: ["auth"] }); });
    expect(cache.getQueryState(["auth"])?.status).toBe("success");
    expect(screen.getByText("workspace remains mounted")).toBeInTheDocument();
    cache.clear();
  });

  it("removes the authenticated workspace when the server revokes the session", async () => {
    const api = vi.spyOn(client, "api").mockResolvedValue(user);
    const cache = mount();
    await screen.findByText("workspace remains mounted");
    api.mockRejectedValue(new client.RequestError(401, "UNAUTHENTICATED", "Session revoked"));
    await act(async () => { await cache.refetchQueries({ queryKey: ["auth"] }); });
    expect(await screen.findByText("login required")).toBeInTheDocument();
    cache.clear();
  });

  it("does not hide an authoritative access denial behind cached session data", async () => {
    const api = vi.spyOn(client, "api").mockResolvedValue(user);
    const cache = mount();
    await screen.findByText("workspace remains mounted");
    api.mockRejectedValue(new client.RequestError(403, "FORBIDDEN", "Access denied"));
    await act(async () => { await cache.refetchQueries({ queryKey: ["auth"] }); });
    expect(await screen.findByRole("alert")).toHaveTextContent("Access denied");
    cache.clear();
  });
});
