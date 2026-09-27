import { afterEach, expect, it, vi } from "vitest";
import { allItems } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("loads each cursor page and preserves filters without dropping items", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ items: [{ id: "first" }], next_cursor: "Mg==" }),
      ),
    )
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ items: [{ id: "last" }], next_cursor: null }),
      ),
    );
  vi.stubGlobal("fetch", fetcher);
  expect(await allItems("/teacher/scenarios?status=APPROVED")).toEqual({
    items: [{ id: "first" }, { id: "last" }],
  });
  expect(fetcher.mock.calls[1][0]).toBe(
    "/api/v1/teacher/scenarios?status=APPROVED&limit=200&cursor=Mg%3D%3D",
  );
});

it("keeps a later page failure visible instead of returning an incomplete library", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ items: [{ id: "first" }], next_cursor: "Mg==" }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: { code: "INTERNAL_ERROR", message: "Сбой" },
          }),
          { status: 500 },
        ),
      ),
  );
  await expect(allItems("/teacher/lessons")).rejects.toThrow("Сбой");
});
