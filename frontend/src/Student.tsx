import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Results } from "./Results";
import { CardEntry } from "./components/CardEntry";
import { api, RequestError } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { ActionPanel } from "./components/ActionPanel";
import { CardView, DeadlineBar } from "./components/CardView";
import { Connection } from "./components/Connection";
import { strings } from "./lib/strings";
import { synchronize, useRealtime, useServerNow } from "./lib/realtime";
import {
  activeDeadline,
  countdown,
  remaining,
  sortCards,
  type CardDetail,
  type StudentState,
} from "./lib/cardTypes";
import {
  enqueue,
  cancelAction,
  flushActions,
  readActions,
  writeActions,
} from "./lib/offline";
import { useAuth } from "./lib/auth";
import { playSound } from "./lib/sound";
import styles from "./components/Workspace.module.css";

export function Student() {
  const { user } = useAuth();
  const userId = user!.id;
  const [selected, setSelected] = useState<string | null>(null);
  const [actions, setActions] = useState(() => readActions(userId));
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<Error | null>(null);
  const [help, setHelp] = useState(false);
  const [newCard, setNewCard] = useState<string | null>(null);
  const announced = useRef(new Set<string>());
  const query = useQuery({
    queryKey: ["student-state", userId],
    queryFn: async () => {
      const result = await api<StudentState>("/student/state");
      synchronize(result.server_time);
      return result;
    },
  });
  const detail = useQuery({
    queryKey: ["card", userId, selected],
    enabled: !!selected,
    queryFn: async () => {
      const result = await api<CardDetail>(`/student/assignments/${selected}`);
      synchronize(result.timers.server_time);
      return result;
    },
  });
  const refresh = async () => {
    await query.refetch({ throwOnError: true });
    if (selected) await detail.refetch({ throwOnError: true });
  };
  const flush = async () => {
    setSending(true);
    try {
      await flushActions(userId);
      await refresh();
      setSendError(null);
    } catch (error) {
      setSendError(error instanceof Error ? error : new Error(strings.error));
    } finally {
      setSending(false);
    }
  };
  const realtime = useRealtime(
    "student",
    (event) => {
      if (event.type === "HEARTBEAT") {
        // HTTP may recover independently of an already-open WebSocket (including
        // browsers that do not emit online/offline events). Retry preserved actions.
        if (!sending && actions[0] && !actions[0].error) void flush();
        return;
      }
      if (event.type === "CARD_DELIVERED") {
        playSound("new");
        setNewCard(String(event.payload.assignment_id));
      }
      if (event.type === "CARD_EXPIRED") playSound("expired");
      if (event.type === "LESSON_FINISHED") setSelected(null);
      void refresh().catch((error) => setSendError(error as Error));
    },
    async () => {
      await refresh();
      await flush();
    },
  );
  const now = useServerNow();
  const cards = sortCards(query.data?.cards ?? [], now);
  useEffect(() => {
    const update = () => setActions(readActions(userId));
    window.addEventListener("dispatcher-actions", update);
    window.addEventListener("storage", update);
    return () => {
      window.removeEventListener("dispatcher-actions", update);
      window.removeEventListener("storage", update);
    };
  }, [userId]);
  useEffect(() => {
    if (!newCard) return;
    const timeout = setTimeout(() => setNewCard(null), 600);
    return () => clearTimeout(timeout);
  }, [newCard]);
  useEffect(() => {
    for (const card of query.data?.cards ?? []) {
      const seconds = remaining(card.primary_deadline_at, now);
      if (
        card.state !== "CLOSED" &&
        !card.current_status &&
        !(card.task_mode === "CARD_ENTRY" && card.processing_deadline_at) &&
        seconds !== null &&
        seconds > 0 &&
        seconds <= 10 &&
        !announced.current.has(card.assignment_id)
      ) {
        announced.current.add(card.assignment_id);
        playSound("warning");
      }
    }
  }, [now, query.data]);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      const input =
        event.target instanceof HTMLInputElement ||
        event.target instanceof HTMLTextAreaElement;
      if (event.key === "?" && !input) {
        event.preventDefault();
        setHelp((value) => !value);
      }
      if (event.key === "Escape") setHelp(false);
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
  const active = cards.filter((card) => card.state !== "CLOSED");
  const closed = cards.filter((card) => card.state === "CLOSED");
  const pending = actions.find((action) => action.assignmentId === selected);
  const lesson = query.data?.lesson;
  return (
    <>
      <div className={styles.statusbar}>
        <strong>{lesson?.title ?? strings.studentWorkspace}</strong>
        <span>{query.data?.workstation?.number}</span>
        <button onClick={() => setHelp((value) => !value)}>
          {strings.shortcuts} ?
        </button>
        <Connection {...realtime} />
      </div>
      {help && <p className={styles.hint}>{strings.shortcutHelp}</p>}
      {sendError && (
        <div role="alert" className={styles.connection}>
          <p>
            {sendError instanceof RequestError
              ? sendError.message
              : strings.queuedAction}
          </p>
          <button onClick={() => void flush()}>{strings.retryNow}</button>
        </div>
      )}
      {actions
        .filter((action) => action.error)
        .map((action) => (
          <div className={styles.connection} key={action.key}>
            <p>{action.error}</p>
            <button
              onClick={() => {
                cancelAction(userId, action.key);
                setSendError(null);
              }}
            >
              {strings.cancelQueued}
            </button>
          </div>
        ))}
      <AsyncView
        loading={query.isPending}
        error={!query.data ? query.error : null}
        retry={() => void query.refetch()}
      >
        {!lesson ? (
          <>
            <section className={styles.waiting}>
              <h1>{strings.studentWorkspace}</h1>
              <p>
                {user?.full_name} · {user?.service?.name}
              </p>
              <p>{strings.waitingLesson}</p>
              <div className={styles.readyChecklist}>
                <h2>{strings.workspaceReady}</h2>
                <ul>
                  {strings.waitingChecklist.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            </section>
            <Results />
          </>
        ) : (
          <>
            <nav
              className={styles.compactNav}
              aria-label={strings.workspaceNavigation}
            >
              <a href="#student-queue">
                {strings.queue} · {active.length}
              </a>
              <a href="#student-card">{strings.card}</a>
              {selected && <a href="#student-actions">{strings.actions}</a>}
            </nav>
            <div
              className={styles.workspace}
              data-entry={detail.data?.task_mode === "CARD_ENTRY"}
            >
              <nav
                id="student-queue"
                className={styles.queue}
                aria-label={strings.activeCards}
              >
                {[
                  [strings.activeCards, active],
                  [strings.closedCards, closed],
                ].map(([title, rows]) => (
                  <section key={title as string}>
                    <h2 className={styles.sectionTitle}>
                      {title as string} · {(rows as typeof cards).length}
                    </h2>
                    {(rows as typeof cards).length === 0 && (
                      <p className={styles.section}>
                        {title === strings.activeCards
                          ? strings.waitingCards
                          : strings.noClosedCards}
                      </p>
                    )}
                    {(rows as typeof cards).map((card) => {
                      const seconds = remaining(activeDeadline(card), now);
                      const overdue =
                        card.is_overdue ||
                        (card.state !== "CLOSED" && (seconds ?? 1) < 0);
                      return (
                        <button
                          key={card.assignment_id}
                          className={`${styles.queueItem} ${selected === card.assignment_id ? styles.selected : ""} ${newCard === card.assignment_id ? styles.new : ""}`}
                          aria-current={
                            selected === card.assignment_id ? "true" : undefined
                          }
                          onClick={() => setSelected(card.assignment_id)}
                          onKeyDown={(event) => {
                            if (
                              event.key !== "ArrowDown" &&
                              event.key !== "ArrowUp"
                            )
                              return;
                            event.preventDefault();
                            const buttons = Array.from(
                              event.currentTarget
                                .closest("nav")!
                                .querySelectorAll("button"),
                            );
                            const index = buttons.indexOf(event.currentTarget);
                            buttons[
                              Math.max(
                                0,
                                Math.min(
                                  buttons.length - 1,
                                  index + (event.key === "ArrowDown" ? 1 : -1),
                                ),
                              )
                            ].focus();
                          }}
                        >
                          {card.state !== "CLOSED" && (
                            <DeadlineBar
                              seconds={seconds}
                              total={
                                card.current_status ||
                                (card.task_mode === "CARD_ENTRY" &&
                                  card.processing_deadline_at)
                                  ? lesson.settings.card_processing_deadline_sec
                                  : lesson.settings.primary_status_deadline_sec
                              }
                            />
                          )}
                          {overdue && (
                            <strong className={styles.alarm}>
                              {strings.overdue}
                            </strong>
                          )}
                          <span className={styles.queueLine}>
                            <span>
                              {card.state === "CLOSED" && "✓ "}
                              {card.card_number}
                            </span>
                            <span>
                              {card.state === "CLOSED"
                                ? ""
                                : countdown(seconds)}
                            </span>
                          </span>
                          <span>{card.incident_type_name}</span>
                          <span className={styles.metadata}>
                            {card.address_short}
                          </span>
                        </button>
                      );
                    })}
                  </section>
                ))}
              </nav>
              <div id="student-card" className={styles.cardPlaceholder}>
                {!selected ? (
                  <div className={styles.section}>{strings.selectCard}</div>
                ) : (
                  <AsyncView
                    loading={detail.isPending}
                    error={!detail.data ? detail.error : null}
                    retry={() => void detail.refetch()}
                  >
                    {detail.data && (
                      <>
                        {detail.data.task_mode === "CARD_ENTRY" ? (
                          <CardEntry
                            key={selected}
                            detail={detail.data}
                            userId={userId}
                            now={now}
                            settings={lesson.settings}
                            flush={flush}
                          />
                        ) : (
                          <CardView
                            detail={detail.data}
                            settings={lesson.settings}
                            now={now}
                          />
                        )}
                      </>
                    )}
                  </AsyncView>
                )}
              </div>
              {selected &&
                detail.data &&
                detail.data.task_mode !== "CARD_ENTRY" && (
                  <ActionPanel
                    key={selected}
                    detail={detail.data}
                    hints={lesson.settings.hints_enabled}
                    pending={pending}
                    sending={sending}
                    onCancelPending={() => {
                      writeActions(
                        userId,
                        actions.filter((action) => action.key !== pending?.key),
                      );
                      setSendError(null);
                    }}
                    onSend={(status, comment) => {
                      try {
                        enqueue(userId, {
                          assignmentId: selected,
                          status,
                          comment: comment.trim() || null,
                        });
                        if (realtime.connected) void flush();
                        return true;
                      } catch (error) {
                        setSendError(
                          error instanceof Error
                            ? error
                            : new Error(strings.error),
                        );
                        return false;
                      }
                    }}
                  />
                )}
            </div>
          </>
        )}
      </AsyncView>
    </>
  );
}
