import { afterEach, expect, it, vi } from "vitest";
import { remaining, sortCards, type CardSummary } from "./cardTypes";
import { serverNow, synchronize } from "./realtime";
import { enqueue, flushActions, readActions } from "./offline";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});
it("uses monotonic server time even when the workstation clock changes", () => {
  vi.spyOn(performance, "now").mockReturnValue(100);
  synchronize("2026-09-19T12:00:00Z");
  vi.spyOn(Date, "now").mockReturnValue(0);
  vi.spyOn(performance, "now").mockReturnValue(25100);
  expect(remaining("2026-09-19T12:00:30Z", serverNow())).toBe(5);
});
it("sorts nearest deadlines first, overdue below active, closed last without mutation", () => {
  const card = (
    id: string,
    seconds: number,
    state = "DELIVERED",
    is_overdue = false,
  ): CardSummary => ({
    assignment_id: id,
    card_number: id,
    state,
    is_overdue,
    current_status: null,
    primary_deadline_at: new Date(seconds * 1000).toISOString(),
    processing_deadline_at: null,
    origin: "OPERATOR_112",
    incident_type_name: "",
    address_short: "",
    registered_at: "",
    delivered_at: "",
    has_unread_description: false,
  });
  const cards = [
    card("late", -5),
    card("later", 50),
    card("first", 5),
    card("done", 1, "CLOSED"),
    card("was-late", 20, "PRIMARY_SET", true),
  ];
  expect(sortCards(cards, 0).map((row) => row.assignment_id)).toEqual([
    "first",
    "later",
    "late",
    "was-late",
    "done",
  ]);
  expect(cards[0].assignment_id).toBe("late");
});
it("retains the same action key after network loss and sends it exactly once on retry", async () => {
  const queued = enqueue("user", {
    assignmentId: "card",
    status: "ACCEPTED",
    comment: null,
  });
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue(new Response("{}"));
  vi.stubGlobal("fetch", fetcher);
  await expect(flushActions("user")).rejects.toThrow("offline");
  expect(readActions("user")[0].key).toBe(queued.key);
  await Promise.all([flushActions("user"), flushActions("user")]);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(readActions("user")).toEqual([]);
  expect(
    new Headers(fetcher.mock.calls[1][1].headers).get("Idempotency-Key"),
  ).toBe(queued.key);
  expect(readActions("different-user")).toEqual([]);
});
it("keeps a rejected action with the server explanation instead of endlessly retrying", async () => {
  enqueue("user", { assignmentId: "card", status: "ACCEPTED", comment: null });
  const fetcher = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        error: { code: "CARD_CLOSED", message: "Карточка закрыта" },
      }),
      { status: 409 },
    ),
  );
  vi.stubGlobal("fetch", fetcher);
  await expect(flushActions("user")).rejects.toThrow("Карточка закрыта");
  expect(readActions("user")[0].error).toBe("Карточка закрыта");
  await flushActions("user");
  expect(fetcher).toHaveBeenCalledTimes(1);
});
it("replays an offline report once with its original key, then clears its draft", async () => {
  const draftKey = "dispatcher112-phone:user:card";
  localStorage.setItem(draftKey, "draft");
  const action = enqueue("user", {
    kind: "report",
    assignmentId: "card",
    call_id: "call",
    transcript: "Пожар, пострадавших нет",
    duration_ms: 24000,
  });
  const saved = vi.fn();
  window.addEventListener("dispatcher-report-saved", saved, { once: true });
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue(
      new Response(JSON.stringify({ confirmation_text: "Принято" })),
    );
  vi.stubGlobal("fetch", fetcher);
  await expect(flushActions("user")).rejects.toThrow("offline");
  expect(localStorage.getItem(draftKey)).toBe("draft");
  await Promise.all([flushActions("user"), flushActions("user")]);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls[1][0]).toContain("/card/report");
  expect(
    new Headers(fetcher.mock.calls[1][1].headers).get("Idempotency-Key"),
  ).toBe(action.key);
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({
    call_id: "call",
    transcript: "Пожар, пострадавших нет",
    duration_ms: 24000,
  });
  expect(saved).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem(draftKey)).toBeNull();
  expect(readActions("user")).toEqual([]);
});
