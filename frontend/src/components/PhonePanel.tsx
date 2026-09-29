import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { dialSip, hangupSip, useSip } from "../lib/sip";
import { useAuth } from "../lib/auth";
import { countdown } from "../lib/cardTypes";
import { enqueue, flushActions, readActions } from "../lib/offline";
import { useServerNow } from "../lib/realtime";
import { strings } from "../lib/strings";
import { AsyncView } from "./AsyncView";
import { BossDialogue } from "./BossDialogue";
import { CommentInput } from "./CommentInput";
import common from "./Common.module.css";
import styles from "./Phone.module.css";

interface Call {
  dial_uri?: string;
  call_id: string;
  callee: { code: string; title: string };
  started_at: string;
  greeting_audio_url: string;
  greeting_text: string;
}
interface Draft {
  call: Call | null;
  text: string;
}
export function PhonePanel({
  assignmentId,
  close,
  pending,
}: {
  assignmentId: string;
  close: () => void;
  pending: boolean;
}) {
  const userId = useAuth().user!.id;
  const phone = useSip();
  const key = `dispatcher112-phone:${userId}:${assignmentId}`;
  const [draft, setDraft] = useState<Draft>(() => {
    try {
      return JSON.parse(
        localStorage.getItem(key) ?? '{"call":null,"text":""}',
      ) as Draft;
    } catch {
      return { call: null, text: "" };
    }
  });
  const [number, setNumber] = useState("");
  const sipCall = useQuery({
    queryKey: ["sip-call", userId, draft.call?.call_id],
    queryFn: () =>
      api<{
        state: string;
        answered_at: string | null;
        ended_at: string | null;
        failure_reason: string | null;
      }>(`/sip/calls/${draft.call!.call_id}`),
    enabled: !!draft.call?.dial_uri,
    refetchInterval: draft.call?.dial_uri ? 1000 : false,
  });
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const [audioError, setAudioError] = useState(false);
  const [replay, setReplay] = useState<string | null>(
    draft.call?.greeting_audio_url ?? null,
  );
  const numberRef = useRef<HTMLInputElement>(null);
  const reportRef = useRef<HTMLTextAreaElement>(null);
  const audioCache = useRef(new Map<string, HTMLAudioElement>());
  const now = useServerNow();
  const persist = (next: Draft) => {
    setDraft(next);
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {
      setError(strings.phoneStorageFailed);
    }
  };
  const play = (url: string) => {
    const audio = audioCache.current.get(url) ?? new Audio(url);
    audioCache.current.set(url, audio);
    setReplay(url);
    setAudioError(false);
    try {
      const settings = JSON.parse(
        localStorage.getItem("dispatcher112-sound") ??
          '{"volume":0.4,"muted":false}',
      ) as { volume: number; muted: boolean };
      audio.volume = Math.max(0, Math.min(1, settings.volume));
      audio.muted = settings.muted;
    } catch {
      audio.volume = 0.4;
    }
    audio.currentTime = 0;
    void audio.play().catch(() => setAudioError(true));
  };
  const directory = useQuery({
    queryKey: ["directory", userId],
    queryFn: () =>
      api<{
        entries: {
          code: string;
          number: string;
          title: string;
          greeting_audio_url: string;
        }[];
      }>("/student/directory"),
  });
  useEffect(() => {
    for (const entry of directory.data?.entries ?? []) {
      for (const url of [
        entry.greeting_audio_url,
        entry.greeting_audio_url.replace("greeting.wav", "confirm.wav"),
      ]) {
        if (!audioCache.current.has(url)) {
          const audio = new Audio(url);
          audio.preload = "auto";
          audio.load();
          audioCache.current.set(url, audio);
        }
      }
    }
  }, [directory.data]);
  useEffect(() => {
    if (draft.call) reportRef.current?.focus();
    else numberRef.current?.focus();
  }, [draft.call]);
  useEffect(() => {
    const cache = audioCache.current;
    return () => {
      for (const audio of cache.values()) audio.pause();
    };
  }, []);
  useEffect(() => {
    const saved = (event: Event) => {
      const data = (
        event as CustomEvent<{
          assignmentId: string;
          confirmation_audio_url: string;
          confirmation_text: string;
        }>
      ).detail;
      if (data.assignmentId !== assignmentId) return;
      persist({ call: null, text: "" });
      setConfirmation(data.confirmation_text);
      play(data.confirmation_audio_url);
    };
    window.addEventListener("dispatcher-report-saved", saved);
    return () => window.removeEventListener("dispatcher-report-saved", saved);
  });
  const call = useMutation({
    networkMode: "always",
    mutationFn: async () => {
      setError(null);
      setConfirmation(null);
      const result = await api<Call>(
        phone.connection === "READY"
          ? `/sip/assignments/${assignmentId}/calls`
          : `/student/assignments/${assignmentId}/call`,
        {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify(
            phone.connection === "READY"
              ? { number, direction: "OUTBOUND" }
              : { number },
          ),
        },
      );
      persist({ call: result, text: draft.text });
      if (result.dial_uri) await dialSip(result.dial_uri);
      else play(result.greeting_audio_url);
    },
  });
  const report = useMutation({
    networkMode: "always",
    mutationFn: async () => {
      if (!draft.call || !draft.text.trim()) return;
      if (draft.call.dial_uri) await hangupSip();
      setError(null);
      const existing = readActions(userId).find(
        (action) =>
          action.assignmentId === assignmentId &&
          action.kind === "report" &&
          action.call_id === draft.call!.call_id,
      );
      if (!existing)
        enqueue(userId, {
          assignmentId,
          kind: "report",
          call_id: draft.call.call_id,
          transcript: draft.text.trim(),
          duration_ms: Math.max(
            0,
            Math.round(now - Date.parse(draft.call.started_at)),
          ),
        });
      await flushActions(userId);
    },
  });
  return (
    <section className={styles.panel} aria-label={strings.phone}>
      <header className={styles.header}>
        <h2>{strings.phone}</h2>
        <button aria-label={strings.closePhone} onClick={close}>
          ✕
        </button>
      </header>
      <p>
        {phone.connection === "READY"
          ? "IP-вызов через гарнитуру. Аудиозапись доступна для учебного разбора."
          : "Аудиосимуляция доклада. Для IP-вызова включите гарнитуру вверху страницы."}
      </p>
      {confirmation && (
        <p role="status" className={styles.connected}>
          {confirmation}
        </p>
      )}
      {audioError && <p role="alert">{strings.phoneAudioFailed}</p>}
      {replay && (
        <button onClick={() => play(replay)}>{strings.replayPhrase}</button>
      )}
      {error && <p role="alert">{error}</p>}
      {draft.call ? (
        <>
          {draft.call.dial_uri && (
            <AsyncView
              loading={sipCall.isPending}
              error={sipCall.error}
              retry={() => void sipCall.refetch()}
            >
              {sipCall.data?.failure_reason && (
                <p role="status">{sipCall.data.failure_reason}</p>
              )}
            </AsyncView>
          )}
          <div className={styles.connected}>
            <span className={styles.dot}>●</span>{" "}
            {draft.call.dial_uri
              ? sipCall.data?.answered_at
                ? sipCall.data.ended_at
                  ? "Вызов завершён"
                  : strings.callConnected
                : "Ожидание соединения"
              : strings.callConnected}{" "}
            · {draft.call.callee.title}
            <div>
              {countdown(
                Math.max(
                  0,
                  Math.floor(
                    ((sipCall.data?.ended_at
                      ? Date.parse(sipCall.data.ended_at)
                      : now) -
                      Date.parse(
                        sipCall.data?.answered_at ?? draft.call.started_at,
                      )) /
                      1000,
                  ),
                ),
              )}
            </div>
          </div>
          {draft.call.dial_uri && !sipCall.data?.answered_at && (
            <button
              onClick={() =>
                void (async () => {
                  try {
                    await api(`/sip/calls/${draft.call!.call_id}/cancel`, {
                      method: "POST",
                    });
                    await hangupSip();
                    persist({ call: null, text: draft.text });
                  } catch (error) {
                    setError(
                      error instanceof Error ? error.message : strings.error,
                    );
                  }
                })()
              }
            >
              Отменить неотвеченный вызов
            </button>
          )}
          {!draft.call.dial_uri ? (
            <BossDialogue
              assignmentId={assignmentId}
              callId={draft.call.call_id}
              calleeTitle={draft.call.callee.title}
              greeting={draft.call.greeting_text}
              play={play}
              onFinished={(text) => {
                persist({ call: null, text: "" });
                setConfirmation(text);
              }}
            />
          ) : (
          <>
          <p className={styles.phrase}>«{draft.call.greeting_text}»</p>
          <form
            className={common.form}
            onSubmit={(event) => {
              event.preventDefault();
              report.mutate();
            }}
          >
            <label htmlFor="phone-report">{strings.phoneReport}</label>
            <CommentInput
              assignmentId={assignmentId}
              text={draft.text}
              setText={(text) => persist({ ...draft, text })}
              inputRef={reportRef}
              required
              field="report"
            />
            <button
              disabled={
                report.isPending ||
                pending ||
                !draft.text.trim() ||
                (!!draft.call.dial_uri && !sipCall.data?.answered_at)
              }
            >
              {strings.finishReport}
            </button>
            {(report.error || pending) && (
              <p role="status">
                {report.error?.message ?? strings.queuedAction}
              </p>
            )}
            {pending && (
              <button
                type="button"
                onClick={() =>
                  void flushActions(userId).catch((reason) =>
                    setError(
                      reason instanceof Error ? reason.message : strings.error,
                    ),
                  )
                }
              >
                {strings.retryNow}
              </button>
            )}
          </form>
          </>
          )}
        </>
      ) : (
        <>
          <form
            className={common.form}
            onSubmit={(event) => {
              event.preventDefault();
              call.mutate();
            }}
          >
            <label>
              {strings.dialedNumber}
              <input
                ref={numberRef}
                className={styles.number}
                aria-label={strings.dialedNumber}
                value={number}
                maxLength={16}
                pattern="[0-9*#]+"
                required
                onChange={(event) =>
                  setNumber(event.target.value.replace(/[^0-9*#]/g, ""))
                }
              />
            </label>
            <div className={styles.keypad}>
              {["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"].map(
                (digit) => (
                  <button
                    type="button"
                    key={digit}
                    onClick={() =>
                      setNumber((value) => (value + digit).slice(0, 16))
                    }
                  >
                    {digit}
                  </button>
                ),
              )}
            </div>
            <button
              type="button"
              onClick={() => setNumber((value) => value.slice(0, -1))}
            >
              {strings.eraseDigit}
            </button>
            <button
              className={styles.call}
              disabled={call.isPending || !number || pending}
            >
              {call.isPending ? strings.callConnecting : strings.startCall}
            </button>
            {call.error && <p role="status">{call.error.message}</p>}
          </form>
          <h2>{strings.directory}</h2>
          <AsyncView
            loading={directory.isPending}
            error={directory.error}
            empty={
              directory.data?.entries.length === 0 && strings.emptyDirectory
            }
            retry={() => void directory.refetch()}
          >
            <ul className={styles.directory}>
              {directory.data?.entries.map((entry) => (
                <li key={entry.code}>
                  <button
                    onClick={() => {
                      setNumber(entry.number);
                      numberRef.current?.focus();
                    }}
                  >
                    {entry.number} · {entry.title}
                  </button>
                </li>
              ))}
            </ul>
          </AsyncView>
        </>
      )}
    </section>
  );
}
