export class RequestError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

let csrf = "";
export async function allItems<T>(path: string): Promise<{ items: T[] }> {
  const items: T[] = [];
  let cursor: string | null = null;
  do {
    const page: { items: T[]; next_cursor?: string | null } = await api(
      `${path}${path.includes("?") ? "&" : "?"}limit=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    );
    items.push(...page.items);
    cursor = page.next_cursor ?? null;
  } while (cursor);
  return { items };
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (
    init.body &&
    !(init.body instanceof FormData) &&
    !headers.has("Content-Type")
  )
    headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET") headers.set("X-CSRF-Token", csrf);
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers,
    credentials: "include",
    signal: init.signal ?? AbortSignal.timeout(10000),
  });
  if (response.status === 204) return undefined as T;
  const data = await response.json();
  if (data.csrf_token) csrf = data.csrf_token;
  if (data.error?.details?.csrf_token) csrf = data.error.details.csrf_token;
  if (!response.ok)
    throw new RequestError(
      response.status,
      data.error?.code ?? "INTERNAL_ERROR",
      data.error?.message ?? response.statusText,
      data.error?.details,
    );
  return data as T;
}
