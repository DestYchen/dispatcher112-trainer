import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ActionPanel } from "./ActionPanel";
import type { CardDetail } from "../lib/cardTypes";
import { strings } from "../lib/strings";

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      {children}
    </QueryClientProvider>
  );
}

const detail: CardDetail = {
  assignment_id: "id",
  state: "OPENED",
  card: {
    card_number: "2026-0919-000001",
    origin: "OPERATOR_112",
    registered_at: "",
    operator_workstation: null,
    applicant: { name: "", phone: "" },
    address: { raw: "", clarification: null },
    attributes: [],
    incident_type_name: "",
    modifiers: [],
    description: "",
    notification_list: [],
  },
  my_block: {
    service_code: "DDS",
    current_status: null,
    available_statuses: [
      { code: "ACCEPTED", label: "Принята", comment_required: false },
      { code: "NOT_ACCEPTED", label: "Не принята", comment_required: true },
    ],
    history: [],
  },
  timers: {
    server_time: "",
    primary_deadline_at: "",
    processing_deadline_at: null,
  },
};
it("renders only server statuses and requires fifteen trimmed characters for refusal", () => {
  const send = vi.fn().mockReturnValue(true);
  render(
    <ActionPanel
      detail={detail}
      hints={false}
      pending={undefined}
      sending={false}
      onSend={send}
      onCancelPending={vi.fn()}
    />,
    { wrapper: Wrapper },
  );
  expect(screen.queryByText("Прибыли")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Не принята" }));
  const comment = screen.getByRole("textbox");
  expect(comment).toHaveFocus();
  expect(
    screen.getByRole("button", { name: strings.saveStatus }),
  ).toBeDisabled();
  fireEvent.change(comment, { target: { value: " " + "а".repeat(14) + " " } });
  expect(
    screen.getByRole("button", { name: strings.saveStatus }),
  ).toBeDisabled();
  fireEvent.change(comment, { target: { value: "Отказ: передано в МЧС." } });
  fireEvent.click(screen.getByRole("button", { name: strings.saveStatus }));
  expect(send).toHaveBeenCalledWith("NOT_ACCEPTED", "Отказ: передано в МЧС.");
});
it("supports keyboard selection and blocks a second action while a queued one is pending", () => {
  const send = vi.fn().mockReturnValue(true);
  const { rerender } = render(
    <ActionPanel
      detail={detail}
      hints={false}
      pending={undefined}
      sending={false}
      onSend={send}
      onCancelPending={vi.fn()}
    />,
    { wrapper: Wrapper },
  );
  fireEvent.keyDown(window, { altKey: true, code: "Digit1" });
  expect(screen.getByRole("button", { name: "Принята" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  rerender(
    <ActionPanel
      detail={detail}
      hints={false}
      pending={{
        key: "key",
        assignmentId: "id",
        status: "ACCEPTED",
        comment: null,
      }}
      sending={false}
      onSend={send}
      onCancelPending={vi.fn()}
    />,
  );
  expect(
    screen.getByRole("button", { name: strings.saveStatus }),
  ).toBeDisabled();
  expect(screen.getByText(strings.queuedAction)).toBeInTheDocument();
});
