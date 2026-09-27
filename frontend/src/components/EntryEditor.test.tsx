import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { EntryEditor } from "./EntryEditor";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: vi.fn().mockResolvedValue({ groups: [], services: [], modifiers: [] }),
}));

it("keeps accessible names on prefilled teacher fields and enforces the scenario text limit", async () => {
  vi.mocked(api).mockResolvedValue({ groups: [], services: [], modifiers: [] });
  const change = vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EntryEditor
        descriptionLimit={400}
        onChange={change}
        card={{
          applicant: { name: "Учебный", phone: "112" },
          address: { raw: "Дубнинская улица, д. 28", clarification: "Двор" },
          description: "Горит мусор во дворе, пострадавших нет.",
          incident_type_id: null,
          modifiers: [],
          notified_services: [],
        }}
      />
    </QueryClientProvider>,
  );
  const description = screen.getByRole("textbox", {
    name: "Описание",
  });
  expect(description).toHaveValue("Горит мусор во дворе, пострадавших нет.");
  expect(description).toHaveAttribute("maxlength", "400");
  expect(screen.getByRole("textbox", { name: "Адрес" })).toHaveValue(
    "Дубнинская улица, д. 28",
  );
  fireEvent.change(description, { target: { value: "Уточнённое описание" } });
  expect(change).toHaveBeenCalledWith(
    expect.objectContaining({ description: "Уточнённое описание" }),
  );
  await waitFor(() => expect(client.isFetching()).toBe(0));
});
