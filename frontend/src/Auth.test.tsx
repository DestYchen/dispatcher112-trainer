import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { App } from "./App";
import { AuthProvider } from "./lib/auth";
import { strings } from "./lib/strings";

it.each(["STUDENT", "TEACHER", "ADMIN"] as const)(
  "signs in and opens the protected %s route",
  async (role) => {
    let loggedIn = false;
    const user = {
      id: "user-1",
      login: "user",
      full_name: "Иванов Иван",
      short_name: "Иванов И.",
      role,
      service: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        if (url.endsWith("/auth/login")) {
          expect(new Headers(options.headers).get("X-CSRF-Token")).toBe(
            "pre-csrf",
          );
          loggedIn = true;
          return new Response(
            JSON.stringify({ user, csrf_token: "session-csrf" }),
          );
        }
        if (url.endsWith("/auth/me"))
          return new Response(
            JSON.stringify(
              loggedIn
                ? { ...user, csrf_token: "session-csrf" }
                : {
                    error: {
                      code: "UNAUTHENTICATED",
                      message: "Вход",
                      details: { csrf_token: "pre-csrf" },
                    },
                  },
            ),
            { status: loggedIn ? 200 : 401 },
          );
        if (url.includes("/student/state"))
          return new Response(
            JSON.stringify({
              server_time: new Date().toISOString(),
              lesson: null,
              cards: [],
            }),
          );
        if (url.includes("/participants"))
          return new Response(JSON.stringify({ students: [] }));
        if (url.includes("/scenarios"))
          return new Response(JSON.stringify({ items: [] }));
        if (url.includes("/admin/users") || url.includes("/admin/services"))
          return new Response(JSON.stringify({ items: [], next_cursor: null }));
        return new Response(
          JSON.stringify(
            url.includes("/lessons")
              ? { items: [] }
              : { groups: 24, types: 1200, services: 58 },
          ),
        );
      }),
    );
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <MemoryRouter initialEntries={["/admin"]}>
          <AuthProvider>
            <App />
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.change(await screen.findByLabelText(strings.login), {
      target: { value: "user" },
    });
    fireEvent.change(screen.getByLabelText(strings.password), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: strings.signIn }));
    const heading =
      role === "STUDENT"
        ? strings.studentWorkspace
        : role === "TEACHER"
          ? strings.teacherWorkspace
          : strings.adminWorkspace;
    expect(
      await screen.findByRole("heading", { name: heading }),
    ).toBeInTheDocument();
  },
);
