import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { Login } from "./Login";
import { strings } from "./lib/strings";
import { api } from "./api/client";

vi.mock("./api/client", () => ({ api: vi.fn() }));
vi.mock("./lib/auth", () => ({ useAuth: () => ({ refresh: vi.fn() }) }));

it.each(["teacher", "student", "admin", "unknown"])(
  "prefills only a public presentation account for %s without signing in automatically",
  (role) => {
    vi.mocked(api).mockClear();
    render(
      <MemoryRouter initialEntries={[`/?showcase=${role}`]}>
        <Login />
      </MemoryRouter>,
    );
    const publicRole = role === "teacher" || role === "student";
    expect(screen.getByLabelText(strings.login)).toHaveValue(
      publicRole ? `demo.${role}` : "",
    );
    expect(screen.getByLabelText(strings.password)).toHaveValue(
      publicRole ? "Showcase112!" : "",
    );
    expect(api).not.toHaveBeenCalled();
  },
);

it("uses edited credentials through normal authentication and displays its failure", async () => {
  vi.mocked(api).mockRejectedValueOnce(new Error("Пароль изменён"));
  render(
    <MemoryRouter initialEntries={["/?showcase=teacher"]}>
      <Login />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText(strings.password), {
    target: { value: "Edited-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: strings.signIn }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Пароль изменён");
  expect(api).toHaveBeenCalledWith("/auth/login", {
    method: "POST",
    body: JSON.stringify({
      login: "demo.teacher",
      password: "Edited-password",
      totp: null,
    }),
  });
  expect(screen.getByRole("button", { name: strings.signIn })).toBeEnabled();
});
