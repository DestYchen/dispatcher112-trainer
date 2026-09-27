import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useRealtime } from "./realtime";

class SlowSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static sockets: SlowSocket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  constructor() {
    SlowSocket.sockets.push(this);
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  // A disconnected network cannot complete the closing handshake immediately.
  close() {
    this.readyState = 2;
  }
}

it("recovers without waiting for a closing handshake and ignores a stale refresh failure", async () => {
  SlowSocket.sockets = [];
  vi.stubGlobal("WebSocket", SlowSocket);
  let rejectOld: (reason: Error) => void = () => {
    throw new Error("Refresh not started");
  };
  const refresh = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectOld = reject;
        }),
    )
    .mockResolvedValue(undefined);
  const hook = renderHook(() => useRealtime("student", vi.fn(), refresh));
  act(() => SlowSocket.sockets[0].open());
  act(() => window.dispatchEvent(new Event("offline")));
  act(() => window.dispatchEvent(new Event("online")));
  act(() => SlowSocket.sockets[1].open());
  await waitFor(() => expect(hook.result.current.connected).toBe(true));
  await act(async () => rejectOld(new Error("Old request timed out")));
  expect(SlowSocket.sockets[1].readyState).toBe(1);
  expect(hook.result.current.connected).toBe(true);
  hook.unmount();
});

it("retries a stalled connecting socket even if the browser sends no close event", async () => {
  vi.useFakeTimers();
  SlowSocket.sockets = [];
  vi.stubGlobal("WebSocket", SlowSocket);
  const hook = renderHook(() =>
    useRealtime("student", vi.fn(), vi.fn().mockResolvedValue(undefined)),
  );
  try {
    await act(() => vi.advanceTimersByTimeAsync(17000));
    expect(SlowSocket.sockets).toHaveLength(2);
    expect(SlowSocket.sockets[0].readyState).toBe(2);
    act(() => SlowSocket.sockets[1].open());
    await act(async () => {
      await Promise.resolve();
    });
    expect(hook.result.current.connected).toBe(true);
  } finally {
    hook.unmount();
    vi.useRealTimers();
  }
});

it("closes a surviving socket on browser offline and restores full state on reconnect", async () => {
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    static sockets: Socket[] = [];
    readyState = 0;
    onopen: (() => void) | null = null;
    onclose: (() => void) | null = null;
    constructor() {
      Socket.sockets.push(this);
    }
    open() {
      this.readyState = 1;
      this.onopen?.();
    }
    close() {
      this.readyState = 3;
      this.onclose?.();
    }
  }
  vi.stubGlobal("WebSocket", Socket);
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
  const refresh = vi.fn().mockResolvedValue(undefined);
  const hook = renderHook(() => useRealtime("teacher", vi.fn(), refresh));
  act(() => Socket.sockets[0].open());
  await waitFor(() => expect(hook.result.current.connected).toBe(true));
  online.mockReturnValue(false);
  act(() => window.dispatchEvent(new Event("offline")));
  expect(hook.result.current.connected).toBe(false);
  expect(Socket.sockets[0].readyState).toBe(3);
  online.mockReturnValue(true);
  act(() => window.dispatchEvent(new Event("online")));
  act(() => Socket.sockets[1].open());
  await waitFor(() => expect(hook.result.current.connected).toBe(true));
  expect(refresh).toHaveBeenCalledTimes(2);
  hook.unmount();
  online.mockRestore();
});
