import { useEffect, useRef, useState } from "react";

export interface RealtimeEvent {
  type: string;
  ts: string;
  payload: Record<string, unknown>;
}

// Monotonic elapsed time is unaffected by changing the workstation's wall clock.
let anchorServer = 0;
let anchorLocal = 0;
export function synchronize(serverTime: string) {
  const time = Date.parse(serverTime);
  if (Number.isFinite(time)) {
    anchorServer = time;
    anchorLocal = performance.now();
  }
}
export function serverNow() {
  return anchorServer + performance.now() - anchorLocal;
}
export function useServerNow() {
  const [now, setNow] = useState(serverNow);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(serverNow()), 250);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}
export function useRealtime(
  role: "student" | "teacher",
  onEvent: (event: RealtimeEvent) => void,
  onReconnect: () => Promise<unknown>,
) {
  const handlers = useRef({ onEvent, onReconnect });
  handlers.current = { onEvent, onReconnect };
  const [connected, setConnected] = useState(false);
  const [retryIn, setRetryIn] = useState(1);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | undefined;
    let attempt = 0;
    let timer = 0;
    let heartbeat = performance.now();
    let reconnectAt = 0;
    const retire = () => {
      const previous = socket;
      socket = undefined;
      if (!previous) return;
      previous.onopen =
        previous.onclose =
        previous.onerror =
        previous.onmessage =
          null;
      previous.close();
    };
    const retry = () => {
      if (disposed) return;
      clearTimeout(timer);
      retire();
      setConnected(false);
      const seconds = [1, 2, 4, 8, 10][Math.min(attempt++, 4)];
      reconnectAt = performance.now() + seconds * 1000;
      setRetryIn(seconds);
      timer = window.setTimeout(connect, seconds * 1000);
    };
    const connect = () => {
      if (disposed) return;
      clearTimeout(timer);
      retire();
      heartbeat = performance.now();
      const current = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/ws?role=${role}`,
      );
      socket = current;
      current.onopen = () => {
        if (disposed || socket !== current) return;
        if (!navigator.onLine) {
          retry();
          return;
        }
        heartbeat = performance.now();
        void handlers.current
          .onReconnect()
          .then(() => {
            if (
              disposed ||
              socket !== current ||
              current.readyState !== WebSocket.OPEN
            )
              return;
            attempt = 0;
            reconnectAt = 0;
            setConnected(true);
          })
          .catch(() => {
            if (socket === current) retry();
          });
      };
      current.onmessage = (message) => {
        if (disposed || socket !== current) return;
        try {
          const event = JSON.parse(String(message.data)) as RealtimeEvent;
          // Replayed stream events keep their original timestamp. Only a fresh
          // heartbeat may change the server clock used by the two timers.
          if (event.type === "HEARTBEAT") synchronize(event.ts);
          heartbeat = performance.now();
          handlers.current.onEvent(event);
        } catch {
          retry();
        }
      };
      current.onerror = current.onclose = () => {
        if (socket === current) retry();
      };
    };
    const offline = () => {
      setConnected(false);
      clearTimeout(timer);
      retire();
    };
    const online = () => {
      if (
        socket?.readyState === WebSocket.OPEN ||
        socket?.readyState === WebSocket.CONNECTING
      )
        return;
      clearTimeout(timer);
      connect();
    };
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    connect();
    const watchdog = window.setInterval(() => {
      if (socket && performance.now() - heartbeat > 15000) retry();
      if (reconnectAt)
        setRetryIn(
          Math.max(0, Math.ceil((reconnectAt - performance.now()) / 1000)),
        );
    }, 500);
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearInterval(watchdog);
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
      retire();
    };
  }, [role, version]);
  return {
    connected,
    retryIn,
    reconnect: () => setVersion((value) => value + 1),
  };
}
