import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { FileIntegrity } from "./FileIntegrity";
import { TableScroll } from "./components/TableScroll";
import { diagnosticsStrings as t, strings } from "./lib/strings";
import common from "./components/Common.module.css";
import layout from "./Learning.module.css";
import styles from "./components/Reports.module.css";

interface TechnicalReport {
  generated_at: string;
  period: { from: string; to: string };
  usage: Record<keyof typeof t.usage, number>;
  inventory: Record<keyof typeof t.inventory, number>;
  http: {
    since: string;
    requests: number;
    server_errors: number;
    mean_ms: number | null;
    over_two_seconds: number;
    journal_write_failures: number;
  };
  integrity: {
    code: keyof typeof t.checks;
    status: "OK" | "FAIL" | "UNAVAILABLE" | "EMPTY";
    last_verified_at?: string | null;
  }[];
  failures: {
    total: number;
    next_cursor: string | null;
    items: {
      at: string;
      source: keyof typeof t.sources;
      reference: string | null;
      request_id?: string | null;
      method?: string;
      route?: string;
      status?: number | null;
      code?: string;
    }[];
  };
}

function date(value: string) {
  return new Date(value).toLocaleString("ru-RU");
}

export function AdminDiagnostics() {
  const [form, setForm] = useState({ from: "", to: "" });
  const [filter, setFilter] = useState("");
  const query = useQuery({
    queryKey: ["technical-report", filter],
    queryFn: () => api<TechnicalReport>(`/admin/diagnostics/report?${filter}`),
    retry: false,
  });
  const report = query.data;
  const download = useMutation({
    mutationFn: async () => {
      if (!report) throw new Error(t.noReport);
      const params = new URLSearchParams({ ...report.period, export: "true" });
      const value = await api<TechnicalReport>(
        `/admin/diagnostics/report?${params}`,
        { signal: AbortSignal.timeout(30000) },
      );
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(value, null, 2)], {
          type: "application/json",
        }),
      );
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "technical-report.json";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    },
  });
  function page(cursor: string | null) {
    if (!report) return;
    const params = new URLSearchParams(report.period);
    if (cursor) params.set("cursor", cursor);
    setFilter(params.toString());
  }
  return (
    <div className={layout.list}>
      <FileIntegrity />
      <p>{t.explanation}</p>
      <form
        className={common.form}
        onSubmit={(event) => {
          event.preventDefault();
          const params = new URLSearchParams();
          for (const key of ["from", "to"] as const)
            if (form[key]) params.set(key, new Date(form[key]).toISOString());
          const next = params.toString();
          if (next === filter) void query.refetch();
          else setFilter(next);
          download.reset();
        }}
      >
        <fieldset>
          <legend>{t.period}</legend>
          {(["from", "to"] as const).map((key) => (
            <label key={key}>
              {key === "from" ? strings.dateFrom : strings.dateTo}
              <input
                type="datetime-local"
                value={form[key]}
                onChange={(event) =>
                  setForm({ ...form, [key]: event.target.value })
                }
              />
            </label>
          ))}
        </fieldset>
        <p>{t.periodHelp}</p>
        <button disabled={query.isFetching}>{t.build}</button>
      </form>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        retry={() => void query.refetch()}
      >
        {report && (
          <>
            <div className={common.toolbar}>
              <p>
                {t.appliedPeriod}: {date(report.period.from)} —{" "}
                {date(report.period.to)}
              </p>
              <button
                disabled={download.isPending || query.isFetching}
                onClick={() => download.mutate()}
              >
                {download.isPending ? t.downloading : t.download}
              </button>
            </div>
            {download.error && <p role="alert">{download.error.message}</p>}
            <section aria-label={t.usageTitle}>
              <h3>{t.usageTitle}</h3>
              <AsyncView
                empty={!Object.values(report.usage).some(Boolean) && t.noUsage}
              >
                <dl className={layout.metrics}>
                  {Object.entries(report.usage).map(([key, value]) => (
                    <div key={key}>
                      <dt>{t.usage[key as keyof typeof t.usage]}</dt>
                      <dd>{value.toLocaleString("ru-RU")}</dd>
                    </div>
                  ))}
                </dl>
              </AsyncView>
            </section>
            <section aria-label={t.inventoryTitle}>
              <h3>{t.inventoryTitle}</h3>
              <p>{date(report.generated_at)}</p>
              <dl className={layout.metrics}>
                {Object.entries(report.inventory).map(([key, value]) => (
                  <div key={key}>
                    <dt>{t.inventory[key as keyof typeof t.inventory]}</dt>
                    <dd>{value.toLocaleString("ru-RU")}</dd>
                  </div>
                ))}
              </dl>
            </section>
            <section aria-label={t.httpTitle}>
              <h3>{t.httpTitle}</h3>
              <p>
                {t.httpSince}: {date(report.http.since)}
              </p>
              <p className={layout.notice}>{t.httpHelp}</p>
              <dl className={layout.metrics}>
                {(Object.keys(t.http) as (keyof typeof t.http)[]).map((key) => (
                  <div key={key}>
                    <dt>{t.http[key]}</dt>
                    <dd>{report.http[key]?.toLocaleString("ru-RU") ?? "—"}</dd>
                  </div>
                ))}
              </dl>
              {report.http.journal_write_failures > 0 && (
                <p role="alert" className={common.error}>
                  {t.journalIncomplete}
                </p>
              )}
            </section>
            <section aria-label={t.integrityTitle}>
              <h3>{t.integrityTitle}</h3>
              <p>{t.integrityHelp}</p>
              <dl className={layout.metrics}>
                {report.integrity.map((check) => (
                  <div key={check.code}>
                    <dt>{t.checks[check.code]}</dt>
                    <dd
                      className={
                        check.status === "OK"
                          ? styles.ok
                          : check.status === "FAIL"
                            ? styles.alarm
                            : undefined
                      }
                    >
                      {t.statuses[check.status]}
                      {check.last_verified_at && (
                        <p>
                          {t.lastVerified}: {date(check.last_verified_at)}
                        </p>
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
            <section aria-label={t.failuresTitle}>
              <h3>
                {t.failuresTitle} ·{" "}
                {report.failures.total.toLocaleString("ru-RU")}
              </h3>
              <p>{t.failureHelp}</p>
              <AsyncView empty={!report.failures.items.length && t.noFailures}>
                <TableScroll>
                  <table className={common.table}>
                    <thead>
                      <tr>
                        <th>{strings.date}</th>
                        <th>{t.source}</th>
                        <th>{t.failure}</th>
                        <th>{t.reference}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.failures.items.map((item) => (
                        <tr key={`${item.source}-${item.reference}`}>
                          <td>{date(item.at)}</td>
                          <td>{t.sources[item.source]}</td>
                          <td>
                            {item.source === "HTTP" ? (
                              <>
                                {item.status} · {item.code}
                                <p>
                                  {item.method} {item.route}
                                </p>
                              </>
                            ) : (
                              t.failureDescriptions[item.source]
                            )}
                          </td>
                          <td>{item.request_id ?? item.reference ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableScroll>
              </AsyncView>
              <div className={common.toolbar}>
                <button
                  disabled={
                    !new URLSearchParams(filter).has("cursor") ||
                    query.isFetching
                  }
                  onClick={() => page(null)}
                >
                  {strings.firstPage}
                </button>
                <button
                  disabled={!report.failures.next_cursor || query.isFetching}
                  onClick={() => page(report.failures.next_cursor)}
                >
                  {strings.nextPage}
                </button>
              </div>
            </section>
          </>
        )}
      </AsyncView>
    </div>
  );
}
