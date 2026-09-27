import { useState } from "react";
import { strings } from "../lib/strings";
import { TableScroll } from "./TableScroll";
import common from "./Common.module.css";
import styles from "./Teacher.module.css";

export type LessonSummary = {
  id: string;
  title: string;
  status: keyof typeof strings.lessonStatuses;
  settings?: { training_mode?: keyof typeof strings.trainingModes };
};

export function LessonList({
  lessons,
  pending,
  onLive,
  onReport,
  onPrepare,
  onAction,
}: {
  lessons: LessonSummary[];
  pending: boolean;
  onLive: (lesson: LessonSummary) => void;
  onReport: (id: string) => void;
  onPrepare: (id: string) => void;
  onAction: (id: string, command: "start" | "finish") => void;
}) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("ALL");
  const order = { RUNNING: 0, PLANNED: 1, FINISHED: 2 };
  const rows = lessons
    .filter(
      (row) =>
        (status === "ALL" || row.status === status) &&
        row.title
          .toLocaleLowerCase("ru")
          .includes(search.trim().toLocaleLowerCase("ru")),
    )
    .sort((a, b) => order[a.status] - order[b.status]);
  return (
    <section aria-label={strings.allLessons}>
      <dl className={styles.summary}>
        {(["PLANNED", "RUNNING", "FINISHED"] as const).map((item) => (
          <div key={item}>
            <dt>{strings.lessonStatuses[item]}</dt>
            <dd>{lessons.filter((lesson) => lesson.status === item).length}</dd>
          </div>
        ))}
      </dl>
      <div className={`${common.form} ${styles.filters}`}>
        <label>
          {strings.lessonSearch}
          <input
            aria-label={strings.lessonSearch}
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <label>
          {strings.lessonFilter}
          <select
            aria-label={strings.lessonFilter}
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="ALL">{strings.allLessons}</option>
            {Object.entries(strings.lessonStatuses).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {rows.length === 0 ? (
        <div className={common.empty}>
          <p>{strings.noMatchingLessons}</p>
          <button
            onClick={() => {
              setSearch("");
              setStatus("ALL");
            }}
          >
            {strings.clearLessonFilters}
          </button>
        </div>
      ) : (
        <TableScroll>
          <table className={`${common.table} ${styles.lessons}`}>
            <thead>
              <tr>
                <th>{strings.title}</th>
                <th>{strings.status}</th>
                <th>{strings.actions}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} data-lesson-id={row.id}>
                  <td>
                    <strong>{row.title}</strong>
                    {row.settings?.training_mode && (
                      <p className={styles.meta}>
                        {strings.trainingModes[row.settings.training_mode]}
                      </p>
                    )}
                    <p className={styles.meta}>
                      {strings.lessonNextStep[row.status]}
                    </p>
                  </td>
                  <td>
                    <span className={styles.badge} data-state={row.status}>
                      {strings.lessonStatuses[row.status]}
                    </span>
                  </td>
                  <td>
                    <div className={styles.lessonActions}>
                      <button
                        className={
                          row.status === "RUNNING" ? common.primary : undefined
                        }
                        onClick={() => onLive(row)}
                      >
                        {strings.liveConsole}
                      </button>
                      <button
                        className={
                          row.status === "FINISHED" ? common.primary : undefined
                        }
                        onClick={() => onReport(row.id)}
                      >
                        {strings.lessonReport}
                      </button>
                      {row.status === "PLANNED" && (
                        <button onClick={() => onPrepare(row.id)}>
                          {strings.prepare}
                        </button>
                      )}
                      {row.status !== "FINISHED" && (
                        <button
                          disabled={pending}
                          onClick={() =>
                            onAction(
                              row.id,
                              row.status === "PLANNED" ? "start" : "finish",
                            )
                          }
                        >
                          {row.status === "PLANNED"
                            ? strings.startLesson
                            : strings.finishLesson}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </section>
  );
}
