import { TableScroll } from "./components/TableScroll";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { Observation } from "./TeacherLive";
import { strings, exchangeStrings as t } from "./lib/strings";
import common from "./components/Common.module.css";
import styles from "./components/Reports.module.css";

interface Report {
  lesson: {
    status: string;
    title: string;
    started_at: string | null;
    finished_at: string | null;
  };
  columns: string[];
  cards_total: number;
  students: {
    student_id: string;
    short_name: string;
    workstation: string | null;
    time_deviation_pct: number | null;
    errors: number;
    spelling_errors: number;
    level: string | null;
    total: number | null;
    manually_corrected: boolean;
  }[];
  reaction_distribution: { label: string; count: number }[];
  heatmap: {
    violations: { code: string; message: string }[];
    rows: { incident_type_name: string; counts: Record<string, number> }[];
  };
}
export function TeacherReport({ lessonId }: { lessonId: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [certificateStudent, setCertificateStudent] = useState("");
  const query = useQuery({
    queryKey: ["lesson-report", lessonId],
    queryFn: () => api<Report>(`/teacher/lessons/${lessonId}/report`),
  });
  const download = useMutation({
    mutationFn: async (
      format: "pdf" | "csv" | "xlsx" | "xml" | { studentId: string } = "pdf",
    ) => {
      const target =
        typeof format === "string"
          ? `report.${format}`
          : `students/${format.studentId}/certificate.pdf`;
      const response = await fetch(
        `/api/v1/teacher/lessons/${lessonId}/${target}`,
        { credentials: "include", signal: AbortSignal.timeout(30000) },
      );
      if (!response.ok) throw new Error(t.failed);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download =
        typeof format === "string"
          ? `lesson-${lessonId}.${format}`
          : `certificate-${format.studentId}.pdf`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  });
  const data = query.data;
  const maximum = Math.max(
    1,
    ...(data?.reaction_distribution.map((row) => row.count) ?? []),
  );
  return (
    <section className={styles.report}>
      <div className={common.toolbar}>
        <h1>{strings.lessonReport}</h1>
        <button
          disabled={!data || download.isPending}
          onClick={() => download.mutate("pdf")}
        >
          {download.isPending ? t.preparing : strings.downloadPdf}
        </button>
        <button
          disabled={!data || download.isPending}
          onClick={() => download.mutate("xlsx")}
        >
          {t.xlsx}
        </button>
        <button
          disabled={!data || download.isPending}
          onClick={() => download.mutate("csv")}
        >
          {t.csv}
        </button>
        <button
          disabled={!data || download.isPending}
          onClick={() => download.mutate("xml")}
        >
          {t.xml}
        </button>
        <button onClick={() => void query.refetch()}>{strings.retryNow}</button>
      </div>
      {download.error && <p role="alert">{download.error.message}</p>}
      <AsyncView
        loading={query.isPending}
        error={query.error}
        empty={data?.students.length === 0 && strings.noStudents}
        retry={() => void query.refetch()}
      >
        {data && (
          <>
            <h2>{data.lesson.title}</h2>
            <div className={common.toolbar}>
              <label>
                {t.certificateStudent}
                <select
                  value={certificateStudent}
                  onChange={(event) =>
                    setCertificateStudent(event.target.value)
                  }
                >
                  <option value="">{t.selectStudent}</option>
                  {data.students.map((student) => (
                    <option key={student.student_id} value={student.student_id}>
                      {student.short_name}
                    </option>
                  ))}
                </select>
              </label>
              <button
                disabled={
                  !certificateStudent ||
                  data.lesson.status !== "FINISHED" ||
                  download.isPending
                }
                onClick={() => {
                  if (certificateStudent)
                    download.mutate({ studentId: certificateStudent });
                }}
              >
                {t.certificate}
              </button>
            </div>
            {data.lesson.status !== "FINISHED" && (
              <p>{t.certificateUnavailable}</p>
            )}
            <p>
              {[data.lesson.started_at, data.lesson.finished_at]
                .filter(Boolean)
                .map((value) => new Date(value!).toLocaleString("ru-RU"))
                .join(" — ")}
            </p>
            <TableScroll>
              <table className={common.table} aria-label={strings.lessonReport}>
                <thead>
                  <tr>
                    {data.columns.map((column) => (
                      <th scope="col" key={column}>
                        {column}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.students.map((row) => (
                    <tr
                      key={row.student_id}
                      onClick={() => setSelected(row.student_id)}
                    >
                      <td>
                        <button onClick={() => setSelected(row.student_id)}>
                          {row.short_name}
                        </button>
                      </td>
                      <td>{row.workstation ?? "—"}</td>
                      <td
                        className={
                          (row.time_deviation_pct ?? 0) <= 0
                            ? styles.ok
                            : styles.alarm
                        }
                      >
                        {row.time_deviation_pct === null
                          ? "—"
                          : `${row.time_deviation_pct > 0 ? "+" : ""}${row.time_deviation_pct} %`}
                      </td>
                      <td>{row.errors}</td>
                      <td>{row.spelling_errors}</td>
                      <td>{row.level ?? "—"}</td>
                      <td>
                        {row.total ?? "—"}
                        {row.manually_corrected && (
                          <span title={strings.manualCorrection}> *</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
            <p>{strings.reportExplanation}</p>
            {data.cards_total === 0 ? (
              <p>{strings.noResultCards}</p>
            ) : (
              <>
                <h2>{strings.reactionDistribution}</h2>
                <div className={styles.distribution}>
                  {data.reaction_distribution.map((row) => (
                    <div key={row.label}>
                      <span>{row.label}</span>
                      <meter
                        value={row.count}
                        max={maximum}
                        aria-label={row.label}
                      />
                      <strong>{row.count}</strong>
                    </div>
                  ))}
                </div>
                <h2>{strings.errorsByType}</h2>
                {data.heatmap.rows.length === 0 ? (
                  <p>{strings.noViolations}</p>
                ) : (
                  <div className={styles.scroll}>
                    <TableScroll>
                      <table className={common.table}>
                        <thead>
                          <tr>
                            <th>{strings.incidentType}</th>
                            {data.heatmap.violations.map((v) => (
                              <th key={v.code}>{v.message}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {data.heatmap.rows.map((row) => (
                            <tr key={row.incident_type_name}>
                              <th>{row.incident_type_name}</th>
                              {data.heatmap.violations.map((v) => (
                                <td
                                  key={v.code}
                                  className={
                                    row.counts[v.code] ? styles.hot : undefined
                                  }
                                >
                                  {row.counts[v.code] ?? 0}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </TableScroll>
                  </div>
                )}
              </>
            )}
          </>
        )}
      </AsyncView>
      {selected && (
        <Observation
          key={selected}
          lessonId={lessonId}
          studentId={selected}
          close={() => setSelected(null)}
        />
      )}
    </section>
  );
}
