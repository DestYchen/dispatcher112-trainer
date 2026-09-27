import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSip } from "../lib/sip";
import { api } from "../api/client";
import type { CardDetail, EntryCard, LessonSettings } from "../lib/cardTypes";
import { countdown, remaining } from "../lib/cardTypes";
import {
  readEntryDraft,
  writeEntryDraft,
  type EntryDraft,
} from "../lib/entryDraft";
import { enqueue, readActions } from "../lib/offline";
import { strings } from "../lib/strings";
import { DeadlineBar } from "./CardView";
import { EntryEditor } from "./EntryEditor";
import { EntryComparison } from "./EntryComparison";
import { TerminalDialog } from "./TerminalDialog";
import { CallRecording } from "./CallRecording";
import styles from "./Workspace.module.css";
import common from "./Common.module.css";

export function CardEntry({
  detail,
  userId,
  now,
  settings,
  flush,
}: {
  detail: CardDetail;
  userId: string;
  now: number;
  settings: LessonSettings;
  flush: () => Promise<void>;
}) {
  const entry = detail.entry!;
  const fallback = {
    card: entry.draft,
    revision: entry.revision,
    dirty: false,
  };
  const [draft, setDraft] = useState<EntryDraft>(() =>
    readEntryDraft(userId, detail.assignment_id, fallback),
  );
  const [error, setError] = useState<Error | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [pending, setPending] = useState(() =>
    readActions(userId).some(
      (row) => row.assignmentId === detail.assignment_id,
    ),
  );
  const closed = detail.state === "CLOSED";
  const phone = useSip();
  const queryClient = useQueryClient();
  const voice = entry.incoming_channel === "VOICE";
  const accepted = entry.accepted_delay_ms !== null;
  const [calling, setCalling] = useState(false);
  const calls = useQuery({
    queryKey: ["sip-calls", userId, detail.assignment_id],
    queryFn: () =>
      api<{
        calls: {
          id: string;
          state: string;
          answered_at: string | null;
          failure_reason: string | null;
          recording_available: boolean;
        }[];
      }>(`/sip/assignments/${detail.assignment_id}/calls`),
    enabled: voice,
    refetchInterval: voice && !closed ? 2000 : false,
  });
  useEffect(() => {
    if (
      voice &&
      !accepted &&
      calls.data?.calls.some((call) => call.answered_at)
    )
      void queryClient.invalidateQueries({
        queryKey: ["card", userId, detail.assignment_id],
      });
  }, [voice, accepted, calls.data, queryClient, userId, detail.assignment_id]);
  async function requestCall() {
    setCalling(true);
    setError(null);
    try {
      await api(`/sip/assignments/${detail.assignment_id}/calls`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ direction: "INBOUND" }),
      });
      await calls.refetch();
    } catch (cause) {
      setError(cause as Error);
    } finally {
      setCalling(false);
    }
  }
  useEffect(() => {
    const update = () => {
      setPending(
        readActions(userId).some(
          (row) => row.assignmentId === detail.assignment_id,
        ),
      );
      setDraft((previous) =>
        readEntryDraft(userId, detail.assignment_id, previous),
      );
    };
    window.addEventListener("dispatcher-actions", update);
    window.addEventListener("dispatcher-entry-draft", update);
    return () => {
      window.removeEventListener("dispatcher-actions", update);
      window.removeEventListener("dispatcher-entry-draft", update);
    };
  }, [userId, detail.assignment_id]);
  function change(card: EntryCard) {
    const next = { ...draft, card, dirty: true };
    setDraft(next);
    try {
      writeEntryDraft(userId, detail.assignment_id, next);
      setError(null);
    } catch (cause) {
      setError(cause as Error);
    }
  }
  function send(submit: boolean) {
    try {
      enqueue(userId, {
        kind: "draft",
        assignmentId: detail.assignment_id,
        card: draft.card,
        revision: draft.revision,
      });
      if (submit)
        enqueue(userId, {
          kind: "submit-card",
          assignmentId: detail.assignment_id,
          revision: draft.revision + 1,
        });
      setConfirm(false);
      setError(null);
      void flush();
    } catch (cause) {
      setError(cause as Error);
    }
  }
  async function reloadDraft() {
    try {
      const current = await api<CardDetail>(
        `/student/assignments/${detail.assignment_id}`,
      );
      const next = {
        card: current.entry!.draft,
        revision: current.entry!.revision,
        dirty: false,
      };
      writeEntryDraft(userId, detail.assignment_id, next);
      setDraft(next);
      setError(null);
    } catch (cause) {
      setError(cause as Error);
    }
  }
  const seconds = remaining(detail.timers.processing_deadline_at, now);
  return (
    <article className={styles.card} aria-label={detail.card.card_number}>
      <header className={styles.cardHeader}>
        <div>
          <p>{strings.incomingMessage}</p>
          <h1 className={styles.number}>{detail.card.card_number}</h1>
        </div>
      </header>
      <div className={`${styles.timerArea} ${styles.entryTimer}`}>
        <div>
          <div
            className={`${styles.timer} ${(entry.accepted_delay_ms ?? 0) > settings.primary_status_deadline_sec * 1000 ? styles.alarm : styles.ok}`}
            data-testid="primary-timer"
          >
            {entry.accepted_delay_ms === null
              ? countdown(remaining(detail.timers.primary_deadline_at, now))
              : countdown(Math.ceil(entry.accepted_delay_ms / 1000))}
          </div>
          <p>
            {entry.accepted_delay_ms === null
              ? strings.incomingNotAccepted
              : strings.incomingAccepted}
            {(entry.accepted_delay_ms ?? 0) >
              settings.primary_status_deadline_sec * 1000 &&
              ` · ${strings.overdue}`}
          </p>
        </div>
        <div>
          <div
            className={`${styles.timer} ${(seconds ?? 1) < 0 && !closed ? styles.alarm : ""}`}
            data-testid="processing-timer"
          >
            {closed ? "✓" : countdown(seconds)}
          </div>
          <p>{closed ? strings.cardClosed : strings.entryDeadline}</p>
          {!closed && (
            <DeadlineBar
              seconds={seconds}
              total={settings.card_processing_deadline_sec}
              label={strings.entryDeadline}
            />
          )}
        </div>
      </div>
      <section className={styles.section}>
        <h2>{strings.incomingMessage}</h2>
        <p className={styles.description}>{entry.incoming_message}</p>
        {voice && (
          <div>
            <p>
              Голосовое обращение принимается через учебный телефон. Разговор
              записывается для разбора с преподавателем.
            </p>
            {calls.isPending && <p role="status">Проверка вызова…</p>}
            {calls.error && (
              <p role="alert">
                {calls.error.message}
                <button onClick={() => void calls.refetch()}>Повторить</button>
              </p>
            )}
            {calls.data?.calls.length === 0 && (
              <p>Ожидание входящего вызова.</p>
            )}
            {calls.data?.calls[0]?.failure_reason && (
              <p role="status">{calls.data.calls[0].failure_reason}</p>
            )}
            {!closed && (
              <button
                disabled={
                  calling ||
                  phone.connection !== "READY" ||
                  phone.call !== "IDLE"
                }
                onClick={() => void requestCall()}
              >
                {calling
                  ? "Подготовка вызова…"
                  : accepted
                    ? "Повторить обращение"
                    : "Получить учебный вызов"}
              </button>
            )}
            {calls.data?.calls
              .filter((call) => call.recording_available)
              .map((call) => (
                <CallRecording
                  key={call.id}
                  callId={call.id}
                />
              ))}
          </div>
        )}
      </section>
      {closed && entry.score ? (
        <EntryComparison score={entry.score} />
      ) : (
        <>
          <EntryEditor
            card={draft.card}
            onChange={change}
            disabled={pending || closed || (voice && !accepted)}
            assignmentId={detail.assignment_id}
          />
          <section className={styles.section} id="student-actions">
            <p>{strings.entryInstructions}</p>
            <p role="status">
              {pending
                ? strings.entryQueued
                : draft.dirty
                  ? strings.entryLocal
                  : draft.revision === 0
                    ? strings.entryEmpty
                    : strings.entrySaved}
            </p>
            {error && (
              <p role="alert" className={styles.alarm}>
                {error.message}
              </p>
            )}
            <div className={common.toolbar}>
              <button
                disabled={pending || closed || (voice && !accepted)}
                onClick={() => send(false)}
              >
                {strings.saveDraft}
              </button>
              <button
                className={common.primary}
                disabled={pending || closed || (voice && !accepted)}
                onClick={() => setConfirm(true)}
              >
                {strings.submitCard}
              </button>
              <button disabled={pending} onClick={() => void reloadDraft()}>
                {strings.entryConflict}
              </button>
            </div>
          </section>
        </>
      )}
      {confirm && (
        <TerminalDialog
          entryMode
          number={detail.card.card_number}
          status={strings.submitCard}
          comment={draft.card.description}
          cancel={() => setConfirm(false)}
          confirm={() => send(true)}
        />
      )}
    </article>
  );
}
