import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { CommentInput } from "./CommentInput";
import { strings } from "../lib/strings";

afterEach(() => vi.unstubAllGlobals());
it("debounces checks, underlines critical addresses, and shows suggestions without blocking input", async () => {
  const fetcher = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        issues: [
          {
            kind: "ADDRESS_TYPO",
            severity: "CRITICAL",
            offset: 0,
            length: 11,
            text: "Дубининская",
            message: "Проверьте улицу",
            suggestions: ["Дубнинская улица"],
          },
        ],
        grammar_available: true,
        notice: null,
      }),
    ),
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CommentInput
        assignmentId="id"
        text="Дубининская улица"
        setText={vi.fn()}
        inputRef={createRef()}
        required={false}
      />
    </QueryClientProvider>,
  );
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.getByText(strings.checkingText)).toBeInTheDocument();
  const issue = await screen.findByRole("button", {
    name: "Дубининская: Проверьте улицу",
  });
  expect(fetcher).toHaveBeenCalledTimes(1);
  fireEvent.click(issue);
  expect(screen.getByText("Дубнинская улица")).toBeInTheDocument();
  expect(screen.getByRole("textbox")).not.toBeDisabled();
});
it("handles failed checks and an empty successful result with a working retry", async () => {
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("network"))
    .mockResolvedValue(
      new Response(
        JSON.stringify({ issues: [], grammar_available: true, notice: null }),
      ),
    );
  vi.stubGlobal("fetch", fetcher);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CommentInput
        assignmentId="id"
        text="Текст"
        setText={vi.fn()}
        inputRef={createRef()}
        required={false}
      />
    </QueryClientProvider>,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: strings.retryNow }),
  );
  await waitFor(() =>
    expect(screen.getByText(strings.noTextIssues)).toBeInTheDocument(),
  );
  expect(fetcher).toHaveBeenCalledTimes(2);
});
