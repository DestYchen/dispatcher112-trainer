import { afterEach, expect, it, vi } from "vitest";
import { cancelAction, enqueue, flushActions, readActions } from "./offline";
import { readEntryDraft, writeEntryDraft } from "./entryDraft";
import type { EntryCard } from "./cardTypes";

const card: EntryCard = {
  applicant: { name: "Учебный заявитель", phone: "" },
  address: { raw: "Дубнинская улица, д. 28", clarification: "" },
  description: "Пожар",
  incident_type_id: null,
  modifiers: [],
  notified_services: [],
};
afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});
it("keeps the draft during a network failure, then saves before submitting with original keys", async () => {
  writeEntryDraft("student", "card", { card, revision: 0, dirty: true });
  const save = enqueue("student", {
    kind: "draft",
    assignmentId: "card",
    card,
    revision: 0,
  });
  enqueue("student", {
    kind: "submit-card",
    assignmentId: "card",
    revision: 1,
  });
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 1 })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ state: "CLOSED" })));
  vi.stubGlobal("fetch", fetcher);
  await expect(flushActions("student")).rejects.toThrow("offline");
  expect(readActions("student")).toHaveLength(2);
  expect(readActions("another-student")).toHaveLength(0);
  await flushActions("student");
  expect(fetcher.mock.calls[1][0]).toContain("/card/draft");
  expect(fetcher.mock.calls[2][0]).toContain("/card/submit-card");
  expect(
    new Headers(fetcher.mock.calls[1][1].headers).get("Idempotency-Key"),
  ).toBe(save.key);
  expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ revision: 1 });
  expect(
    readEntryDraft("student", "card", { card, revision: 0, dirty: true }),
  ).toEqual({ card, revision: 1, dirty: false });
  expect(readActions("student")).toEqual([]);
});
it("does not submit after a conflicting draft and preserves the local answer", async () => {
  writeEntryDraft("student", "card", { card, revision: 0, dirty: true });
  enqueue("student", {
    kind: "draft",
    assignmentId: "card",
    card,
    revision: 0,
  });
  enqueue("student", {
    kind: "submit-card",
    assignmentId: "card",
    revision: 1,
  });
  const fetcher = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        error: { code: "DRAFT_CONFLICT", message: "Конфликт черновика" },
      }),
      { status: 409 },
    ),
  );
  vi.stubGlobal("fetch", fetcher);
  await expect(flushActions("student")).rejects.toThrow("Конфликт черновика");
  await flushActions("student");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(readActions("student")).toHaveLength(2);
  expect(
    readEntryDraft("student", "card", { card, revision: 9, dirty: false }).card
      .address.raw,
  ).toBe(card.address.raw);
  cancelAction("student", readActions("student")[0].key);
  await flushActions("student");
  expect(readActions("student")).toEqual([]);
  expect(fetcher).toHaveBeenCalledTimes(1);
});
