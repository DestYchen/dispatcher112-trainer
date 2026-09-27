import { TableScroll } from "./components/TableScroll";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { allItems, api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { CardView } from "./components/CardView";
import { EntryComparison } from "./components/EntryComparison";
import { Connection } from "./components/Connection";
import { CallRecording } from "./components/CallRecording";
import type { CardDetail, CardSummary, LessonSettings } from "./lib/cardTypes";
import { countdown } from "./lib/cardTypes";
import { useRealtime, useServerNow } from "./lib/realtime";
import { strings } from "./lib/strings";
import common from "./components/Common.module.css";
import styles from "./components/Teacher.module.css";

interface LiveData {
  server_time: string;
  lesson_status: "PLANNED" | "RUNNING" | "FINISHED";
  elapsed_sec: number;
  students: {
    student_id: string;
    short_name: string;
    workstation: string | null;
    online: boolean;
    active_cards: number;
    closed: number;
    expired: number;
    current_score: number | null;
    last_action: { kind: string; status: string | null; at: string } | null;
    alert: "IDLE" | "OVERDUE" | null;
  }[];
  aggregate: {
    cards_delivered: number;
    cards_closed: number;
    cards_expired: number;
    avg_primary_delay_ms: number | null;
    top_violations: { code: string; message: string; count: number }[];
  };
}
interface Score {
  total: number;
  axes: Record<string, { score: number | null; weight: number }>;
}
interface ObservedCard extends CardDetail {
  sip_calls?: {
    id: string;
    direction: string;
    created_at: string;
    state: string;
    recording_available: boolean;
  }[];
  score: Score | null;
  teacher_override: {
    axes?: Record<string, number>;
    total?: number;
    comment: string;
  } | null;
  effective_score: Score | null;
  events: { id: string; kind: string; at: string }[];
}
function activity(kind: string) {
  return (
    strings.eventKinds[kind as keyof typeof strings.eventKinds] ??
    strings.activity
  );
}
function Changed({ value }: { value: string | number }) {
  return (
    <span key={value} className={styles.changed}>
      {value}
    </span>
  );
}
function Correction({
  card,
  refresh,
}: {
  card: ObservedCard;
  refresh: () => Promise<unknown>;
}) {
  const [axis, setAxis] = useState("total");
  const [value, setValue] = useState(String(card.effective_score?.total ?? 0));
  const [comment, setComment] = useState("");
  const reuse = useMutation({
    mutationFn: () =>
      api(`/teacher/assignments/${card.assignment_id}/reuse-card`, {
        method: "POST",
      }),
  });
  const mutation = useMutation({
    mutationFn: async () => {
      await api(`/teacher/assignments/${card.assignment_id}/override`, {
        method: "POST",
        body: JSON.stringify({
          ...(axis === "total"
            ? { total: Number(value), axes: card.teacher_override?.axes ?? {} }
            : {
                axes: { ...card.teacher_override?.axes, [axis]: Number(value) },
              }),
          comment,
        }),
      });
      await refresh();
    },
  });
  return (
    <section className={styles.section}>
      {card.entry?.score && <EntryComparison score={card.entry.score} />}
      {card.entry?.submitted_at && (
        <div>
          <button
            disabled={reuse.isPending || reuse.isSuccess}
            onClick={() => reuse.mutate()}
          >
            {strings.reuseCard}
          </button>
          {reuse.isSuccess && <p>{strings.reusedCard}</p>}
          {reuse.error && <p role="alert">{reuse.error.message}</p>}
        </div>
      )}
      <h2>{strings.scoreCorrection}</h2>
      <p>
        {strings.originalScore}: <strong>{card.score?.total}</strong> ·{" "}
        {strings.teacherScore}: <strong>{card.effective_score?.total}</strong>
      </p>
      {card.teacher_override && (
        <p>
          {strings.manualCorrection}: {card.teacher_override.comment}
        </p>
      )}
      <TableScroll>
        <table className={common.table}>
          <thead>
            <tr>
              <th>{strings.axis}</th>
              <th>{strings.originalScore}</th>
              <th>{strings.teacherScore}</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(card.score?.axes ?? {}).map(([name, original]) => (
              <tr key={name}>
                <th>{strings.axes[name as keyof typeof strings.axes]}</th>
                <td>{original.score ?? strings.skippedAxis}</td>
                <td>
                  {card.effective_score?.axes[name]?.score ??
                    strings.skippedAxis}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      <form
        className={common.form}
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate();
        }}
      >
        <label>
          {strings.axis}
          <select
            aria-label={strings.axis}
            value={axis}
            onChange={(event) => {
              const chosen = event.target.value;
              setAxis(chosen);
              setValue(
                String(
                  chosen === "total"
                    ? (card.effective_score?.total ?? 0)
                    : (card.effective_score?.axes[chosen]?.score ?? 0),
                ),
              );
            }}
          >
            <option value="total">{strings.totalScore}</option>
            {Object.entries(strings.axes).map(([key, text]) => (
              <option key={key} value={key}>
                {text}
              </option>
            ))}
          </select>
        </label>
        <label>
          {strings.newScore}
          <input
            required
            type="number"
            min={0}
            max={100}
            step="0.01"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
        <label>
          {strings.correctionReason}
          <textarea
            aria-label={strings.correctionReason}
            required
            maxLength={2000}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
          />
        </label>
        <button disabled={mutation.isPending || !comment.trim()}>
          {strings.saveCorrection}
        </button>
        {mutation.error && <p role="alert">{mutation.error.message}</p>}
        {mutation.isSuccess && (
          <p className={styles.online}>{strings.correctionSaved}</p>
        )}
      </form>
    </section>
  );
}
export function Observation({
  lessonId,
  studentId,
  close,
}: {
  lessonId: string;
  studentId: string;
  close: () => void;
}) {
  const client = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const now = useServerNow();
  const query = useQuery({
    queryKey: ["observation", lessonId, studentId],
    queryFn: () =>
      api<{ lesson: { settings: LessonSettings }; cards: CardSummary[] }>(
        `/teacher/lessons/${lessonId}/students/${studentId}/observe`,
      ),
  });
  const selectedId = selected ?? query.data?.cards[0]?.assignment_id;
  const detail = useQuery({
    queryKey: ["observed-card", selectedId],
    enabled: !!selectedId,
    queryFn: () => api<ObservedCard>(`/teacher/assignments/${selectedId}`),
  });
  return (
    <section className={styles.section}>
      <div className={common.toolbar}>
        <h2>{strings.observation}</h2>
        <button onClick={close}>{strings.closeObservation}</button>
      </div>
      <p>{strings.observationReadOnly}</p>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        empty={query.data?.cards.length === 0 && strings.noObservedCards}
        retry={() => void query.refetch()}
      >
        <div className={styles.observation}>
          <nav aria-label={strings.observedCards} className={styles.queue}>
            {query.data?.cards.map((card) => (
              <button
                key={card.assignment_id}
                aria-pressed={selectedId === card.assignment_id}
                onClick={() => setSelected(card.assignment_id)}
              >
                {card.card_number}
                <br />
                {card.incident_type_name}
                <br />
                {card.current_status
                  ? strings.statusLabels[
                      card.current_status as keyof typeof strings.statusLabels
                    ]
                  : strings.noStatus}
              </button>
            ))}
          </nav>
          <AsyncView
            loading={detail.isPending}
            error={detail.error}
            retry={() => void detail.refetch()}
          >
            {detail.data && query.data && (
              <div>
                <CardView
                  detail={detail.data}
                  settings={query.data.lesson.settings}
                  now={now}
                />
                <section className={styles.section}>
                  <h2>{strings.history}</h2>
                  {(detail.data.sip_calls ?? []).map((call) => (
                    <div key={call.id}>
                      <p>
                        {call.direction === "INBOUND"
                          ? "Входящий вызов"
                          : "Исходящий вызов"}{" "}
                        ·{" "}
                        {new Date(call.created_at).toLocaleTimeString("ru-RU")}
                      </p>
                      {call.recording_available ? (
                        <CallRecording callId={call.id} />
                      ) : (
                        <p>
                          {["ENDED", "FAILED"].includes(call.state)
                            ? "Аудиозапись отсутствует."
                            : "Запись станет доступна после разговора."}
                        </p>
                      )}
                    </div>
                  ))}
                  {detail.data.my_block.history.map((event) => (
                    <p key={event.id}>
                      {new Date(event.at).toLocaleTimeString("ru-RU")} ·{" "}
                      {event.label} · {event.comment ?? strings.noComment}
                    </p>
                  ))}
                  {detail.data.events.map((event) => (
                    <p key={event.id}>
                      {new Date(event.at).toLocaleTimeString("ru-RU")} ·{" "}
                      {activity(event.kind)}
                    </p>
                  ))}
                </section>
                {detail.data.score && (
                  <Correction
                    key={detail.data.assignment_id}
                    card={detail.data}
                    refresh={async () => {
                      await detail.refetch();
                      await client.invalidateQueries({
                        queryKey: ["teacher-live", lessonId],
                      });
                      await client.invalidateQueries({
                        queryKey: ["lesson-report", lessonId],
                      });
                    }}
                  />
                )}
              </div>
            )}
          </AsyncView>
        </div>
      </AsyncView>
    </section>
  );
}
export function TeacherLive({
  lessonId,
  title,
  refreshLessons,
}: {
  lessonId: string;
  title: string;
  refreshLessons: () => Promise<unknown>;
}) {
  const client = useQueryClient();
  const [observed, setObserved] = useState<string | null>(null);
  const [student, setStudent] = useState("");
  const [scenario, setScenario] = useState("");
  const live = useQuery({
    queryKey: ["teacher-live", lessonId],
    queryFn: () => api<LiveData>(`/teacher/lessons/${lessonId}/live`),
  });
  const scenarios = useQuery({
    queryKey: ["approved"],
    queryFn: () =>
      allItems<{ id: string; title: string }>(
        "/teacher/scenarios?status=APPROVED",
      ),
  });
  const refresh = async () => {
    await Promise.all([
      live.refetch(),
      client.invalidateQueries({ queryKey: ["observation", lessonId] }),
      client.invalidateQueries({ queryKey: ["observed-card"] }),
    ]);
  };
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (refreshTimer.current !== null) clearTimeout(refreshTimer.current);
    },
    [],
  );
  const connection = useRealtime(
    "teacher",
    () => {
      // One snapshot includes every committed action in a burst from the class.
      if (refreshTimer.current === null) {
        refreshTimer.current = setTimeout(() => {
          refreshTimer.current = null;
          void refresh();
        }, 1000);
      }
    },
    refresh,
  );
  const lifecycle = useMutation({
    mutationFn: async (command: "start" | "finish") => {
      await api(`/teacher/lessons/${lessonId}/${command}`, { method: "POST" });
      await refresh();
      await refreshLessons();
    },
  });
  const assignment = useMutation({
    mutationFn: async () => {
      await api(`/teacher/lessons/${lessonId}/assign`, {
        method: "POST",
        body: JSON.stringify({
          assignments: [{ student_id: student, scenario_id: scenario }],
        }),
      });
      await refresh();
    },
  });
  const data = live.data;
  return (
    <section className={styles.console}>
      <div className={common.toolbar}>
        <h1>{title}</h1>
        <span>{countdown(data?.elapsed_sec ?? 0)}</span>
        {data && data.lesson_status !== "FINISHED" && (
          <button
            className={
              data.lesson_status === "PLANNED" ? common.primary : undefined
            }
            disabled={lifecycle.isPending}
            onClick={() =>
              lifecycle.mutate(
                data.lesson_status === "PLANNED" ? "start" : "finish",
              )
            }
          >
            {data.lesson_status === "PLANNED"
              ? strings.startLesson
              : strings.finishLesson}
          </button>
        )}
      </div>
      <Connection {...connection} />
      {lifecycle.error && <p role="alert">{lifecycle.error.message}</p>}
      <AsyncView
        loading={live.isPending}
        error={live.error}
        empty={data?.students.length === 0 && strings.noStudents}
        retry={() => void live.refetch()}
      >
        {data && (
          <>
            <p className={styles.section}>
              {strings.delivered}: {data.aggregate.cards_delivered} ·{" "}
              {strings.closedCards}: {data.aggregate.cards_closed} ·{" "}
              {strings.expiredCards}: {data.aggregate.cards_expired} ·{" "}
              {strings.averageReaction}:{" "}
              {data.aggregate.avg_primary_delay_ms === null
                ? "—"
                : (data.aggregate.avg_primary_delay_ms / 1000).toFixed(1)}{" "}
              {strings.secondsShort}
            </p>
            <TableScroll>
              <table className={common.table} aria-label={strings.liveStudents}>
                <thead>
                  <tr>
                    <th>{strings.workstation}</th>
                    <th>{strings.student}</th>
                    <th>{strings.activeCards}</th>
                    <th>{strings.closedCards}</th>
                    <th>{strings.expiredCards}</th>
                    <th>{strings.totalScore}</th>
                    <th>{strings.activity}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.students.map((row) => (
                    <tr
                      key={row.student_id}
                      className={row.alert ? styles.alert : undefined}
                      onClick={() => setObserved(row.student_id)}
                    >
                      <td>
                        <span
                          className={
                            row.online ? styles.online : styles.offline
                          }
                          title={row.online ? strings.online : strings.offline}
                          aria-label={
                            row.online ? strings.online : strings.offline
                          }
                        >
                          {row.online ? "●" : "○"}
                        </span>{" "}
                        {row.workstation ?? "—"}
                      </td>
                      <td>
                        <button onClick={() => setObserved(row.student_id)}>
                          {row.short_name}
                        </button>
                      </td>
                      <td>
                        <Changed value={row.active_cards} />
                      </td>
                      <td>
                        <Changed value={row.closed} />
                      </td>
                      <td>
                        <Changed value={row.expired} />
                      </td>
                      <td>
                        <Changed value={row.current_score ?? "—"} />
                      </td>
                      <td>
                        <Changed
                          value={
                            row.alert
                              ? strings.alerts[row.alert]
                              : row.last_action
                                ? `${row.last_action.status ? strings.statusLabels[row.last_action.status as keyof typeof strings.statusLabels] : activity(row.last_action.kind)} ${new Date(row.last_action.at).toLocaleTimeString("ru-RU")}`
                                : "—"
                          }
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
            <section className={styles.section}>
              <h2>{strings.topViolations}</h2>
              {data.aggregate.top_violations.length ? (
                <ul>
                  {data.aggregate.top_violations.map((item) => (
                    <li key={item.code}>
                      {item.message} — {item.count}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>{strings.noViolationsYet}</p>
              )}
            </section>
            {data.lesson_status !== "FINISHED" && (
              <AsyncView
                loading={scenarios.isPending}
                error={scenarios.error}
                empty={
                  scenarios.data?.items.length === 0 && strings.noScenarios
                }
                retry={() => void scenarios.refetch()}
              >
                <form
                  className={common.form}
                  onSubmit={(event) => {
                    event.preventDefault();
                    assignment.mutate();
                  }}
                >
                  <h2>{strings.manualAssignment}</h2>
                  <label>
                    {strings.assignmentStudent}
                    <select
                      required
                      aria-label={strings.assignmentStudent}
                      value={student}
                      onChange={(event) => setStudent(event.target.value)}
                    >
                      <option value="">{strings.chooseStudent}</option>
                      {data.students.map((row) => (
                        <option key={row.student_id} value={row.student_id}>
                          {row.short_name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    {strings.assignmentScenario}
                    <select
                      required
                      aria-label={strings.assignmentScenario}
                      value={scenario}
                      onChange={(event) => setScenario(event.target.value)}
                    >
                      <option value="">{strings.chooseScenario}</option>
                      {scenarios.data?.items.map((row) => (
                        <option key={row.id} value={row.id}>
                          {row.title}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button disabled={assignment.isPending}>
                    {strings.sendAssignment}
                  </button>
                  {assignment.error && (
                    <p role="alert">{assignment.error.message}</p>
                  )}
                  {assignment.isSuccess && (
                    <p className={styles.online}>{strings.assignmentSent}</p>
                  )}
                </form>
              </AsyncView>
            )}
          </>
        )}
      </AsyncView>
      {observed && (
        <Observation
          key={observed}
          lessonId={lessonId}
          studentId={observed}
          close={() => setObserved(null)}
        />
      )}
    </section>
  );
}
