import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { learningStrings as s, strings } from "./lib/strings";
import common from "./components/Common.module.css";
import styles from "./Learning.module.css";

type HistoryItem = {
  assignment_id: string;
  lesson_title: string;
  closed_at: string;
  scenario_title: string;
  score: {
    total: number;
    axes: Record<string, { score: number | null }>;
    violations: { message: string; hint: string }[];
    criteria?: {
      passed: boolean | null;
      errors: string[];
      unavailable?: string[];
    };
  };
  teacher_override: { total: number; comment: string } | null;
  feedback: { id: string; body: string; created_at: string }[];
};
export function StudentPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const query = useQuery({
    queryKey: ["learning-students"],
    queryFn: () =>
      api<{ items: { id: string; name: string }[] }>(
        "/teacher/progress/students",
      ),
  });
  return (
    <AsyncView
      loading={query.isPending}
      error={query.error}
      retry={() => void query.refetch()}
      empty={!query.data?.items.length && s.noStudents}
    >
      <label className={common.form}>
        {s.selectStudent}
        <select
          aria-label={s.selectStudent}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">{s.selectStudent}</option>
          {query.data?.items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>
    </AsyncView>
  );
}
function FeedbackForm({
  id,
  refresh,
}: {
  id: string;
  refresh: () => Promise<unknown>;
}) {
  const [body, setBody] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      api(`/teacher/assignments/${id}/feedback`, {
        method: "POST",
        body: JSON.stringify({ body }),
      }),
    onSuccess: async () => {
      setBody("");
      await refresh();
    },
  });
  return (
    <form
      className={common.form}
      onSubmit={(event) => {
        event.preventDefault();
        mutation.mutate();
      }}
    >
      <label>
        {s.feedback}
        <textarea
          required
          minLength={5}
          maxLength={5000}
          value={body}
          placeholder={s.feedbackPlaceholder}
          onChange={(event) => setBody(event.target.value)}
        />
      </label>
      <button disabled={mutation.isPending}>{s.addFeedback}</button>
      {mutation.error && <p role="alert">{mutation.error.message}</p>}
    </form>
  );
}
export function LearningHistory({ teacher }: { teacher: boolean }) {
  const [student, setStudent] = useState("");
  const query = useInfiniteQuery({
    queryKey: ["learning-history", student],
    enabled: !teacher || !!student,
    initialPageParam: "",
    getNextPageParam: (last: {
      items: HistoryItem[];
      next_cursor: string | null;
    }) => last.next_cursor ?? undefined,
    queryFn: ({ pageParam }) =>
      api<{ items: HistoryItem[]; next_cursor: string | null }>(
        `/learning/history?limit=30${student ? `&student_id=${student}` : ""}${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`,
      ),
  });
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <>
      <h2>{s.history}</h2>
      {teacher && <StudentPicker value={student} onChange={setStudent} />}
      {(!teacher || student) && (
        <AsyncView
          loading={query.isPending}
          error={query.error}
          retry={() => void query.refetch()}
          empty={!items.length && s.noHistory}
        >
          <div className={styles.list}>
            {items.map((item) => (
              <article key={item.assignment_id} className={styles.item}>
                <h3>
                  {item.lesson_title} · {item.scenario_title}
                </h3>
                <p>{new Date(item.closed_at).toLocaleString("ru-RU")}</p>
                <p>
                  <strong>
                    {s.original}: {item.score.total}
                  </strong>
                </p>
                {item.teacher_override && (
                  <p>
                    {s.adjusted}: {item.teacher_override.total} ·{" "}
                    {item.teacher_override.comment}
                  </p>
                )}
                <div className={styles.axes}>
                  {Object.entries(item.score.axes).map(([axis, value]) => (
                    <label key={axis}>
                      {strings.axes[axis as keyof typeof strings.axes]}:{" "}
                      {value.score ?? "—"}
                      <progress max={100} value={value.score ?? 0} />
                    </label>
                  ))}
                </div>
                {item.score.criteria && (
                  <div className={styles.notice}>
                    <strong>
                      {item.score.criteria.passed === null
                        ? s.incompleteCriteria
                        : item.score.criteria.passed
                          ? s.passed
                          : s.failed}
                    </strong>
                    {item.score.criteria.errors.map((error) => (
                      <p key={error}>{error}</p>
                    ))}
                    {item.score.criteria.unavailable?.map((message) => (
                      <p key={message}>{message}</p>
                    ))}
                  </div>
                )}
                {item.score.violations.length ? (
                  item.score.violations.map((violation, index) => (
                    <p key={index}>
                      ▲ {violation.message} {violation.hint}
                    </p>
                  ))
                ) : (
                  <p>✓ {s.noErrors}</p>
                )}
                {item.feedback.map((note) => (
                  <div key={note.id} className={styles.notice}>
                    <strong>
                      {s.feedback} ·{" "}
                      {new Date(note.created_at).toLocaleString("ru-RU")}
                    </strong>
                    <p className={styles.text}>{note.body}</p>
                  </div>
                ))}
                {teacher && (
                  <FeedbackForm
                    id={item.assignment_id}
                    refresh={() => query.refetch()}
                  />
                )}
              </article>
            ))}
          </div>
          {query.hasNextPage && (
            <button
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              {s.more}
            </button>
          )}
        </AsyncView>
      )}
    </>
  );
}
