import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { TableScroll } from "./components/TableScroll";
import common from "./components/Common.module.css";
import layout from "./Learning.module.css";
import { integrityStrings as t } from "./lib/integrityStrings";
import type { Operation } from "./TechnicalConsole";

export interface IntegrityResult {
  status: keyof typeof t.states;
  code?: string;
  checked_at?: string;
  started_at?: string;
  files?: number;
  expected_files?: number;
  issues_total?: number;
  truncated?: boolean;
  issues?: {
    scope: keyof typeof t.scopes;
    path: string;
    kind: keyof typeof t.kinds;
  }[];
  baseline?: {
    snapshot: string;
    selected_at: string;
    id: string;
    actor_id: string;
    reason: string;
  } | null;
}

export function IntegrityOutcome({ result }: { result: IntegrityResult }) {
  return (
    <div className={layout.item}>
      <p
        role={
          result.status === "FAIL" || result.status === "UNAVAILABLE"
            ? "alert"
            : "status"
        }
      >
        {t.states[result.status]}
      </p>
      {result.code === "INVALID_BASELINE" && <p>{t.baselineInvalid}</p>}
      {result.baseline && (
        <>
          <p className={layout.text}>
            {t.baseline}: {result.baseline.snapshot}
          </p>
          <p>
            {t.selected}:{" "}
            {new Date(result.baseline.selected_at).toLocaleString("ru-RU")}
          </p>
        </>
      )}
      {result.checked_at && (
        <p>
          {t.checked}: {new Date(result.checked_at).toLocaleString("ru-RU")}
        </p>
      )}
      {result.status === "RUNNING" && result.started_at && (
        <p>
          {t.started}: {new Date(result.started_at).toLocaleString("ru-RU")}
        </p>
      )}
      {result.files !== undefined && (
        <p>
          {t.files}: {result.files} / {result.expected_files}
        </p>
      )}
      {result.issues_total !== undefined && (
        <p>
          {t.violations}: {result.issues_total}
        </p>
      )}
      {!!result.issues?.length && (
        <TableScroll>
          <table className={common.table}>
            <thead>
              <tr>
                <th>{t.path}</th>
                <th>{t.problem}</th>
              </tr>
            </thead>
            <tbody>
              {result.issues.map((issue, index) => (
                <tr key={index}>
                  <td className={layout.text}>
                    {t.scopes[issue.scope]}: {issue.path || "—"}
                  </td>
                  <td>{t.kinds[issue.kind]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
      {result.truncated && <p>{t.truncated}</p>}
    </div>
  );
}

export function FileIntegrity({
  controls,
}: {
  controls?: {
    disabled: boolean;
    snapshots: { name: string; created_at: string | null }[];
    submit: (operation: Operation) => void;
  };
}) {
  const query = useQuery({
    queryKey: ["file-integrity"],
    queryFn: () => api<IntegrityResult>("/admin/diagnostics/files"),
    refetchInterval: 10000,
    retry: false,
  });
  const [snapshot, setSnapshot] = useState("");
  const [reason, setReason] = useState("");
  const [identity, setIdentity] = useState(() => crypto.randomUUID());
  const selectable = !!controls?.snapshots.some(
    (item) => item.name === snapshot,
  );
  return (
    <section className={layout.list} aria-label={t.title}>
      <h3>{t.title}</h3>
      <p>{t.explanation}</p>
      <p>{t.scope}</p>
      <p>{t.limits}</p>
      <button onClick={() => void query.refetch()} disabled={query.isFetching}>
        {t.refresh}
      </button>
      {query.error && <p role="alert">{t.unavailable}</p>}
      <AsyncView
        loading={query.isPending}
        error={!query.data ? query.error : null}
        retry={() => void query.refetch()}
      >
        {query.data && (
          <IntegrityOutcome
            result={
              query.error
                ? { ...query.data, status: "UNAVAILABLE" }
                : query.data
            }
          />
        )}
      </AsyncView>
      {controls && (
        <>
          <button
            disabled={controls.disabled}
            onClick={() =>
              controls.submit({
                id: crypto.randomUUID(),
                kind: "backup_integrity_check",
                service: "backup",
              })
            }
          >
            {t.check}
          </button>
          <form
            className={common.form}
            onSubmit={(event) => {
              event.preventDefault();
              if (!controls.disabled && selectable && reason.trim().length >= 5)
                controls.submit({
                  id: identity,
                  kind: "backup_integrity_baseline",
                  service: "backup",
                  snapshot,
                  reason: reason.trim(),
                });
            }}
          >
            <h4>{t.approval}</h4>
            <p>{t.approvalHelp}</p>
            {controls.snapshots.length === 0 && (
              <p className={common.empty}>{t.noSnapshots}</p>
            )}
            <label>
              {t.snapshot}
              <select
                aria-label={t.snapshot}
                required
                value={snapshot}
                disabled={controls.disabled}
                onChange={(event) => {
                  setSnapshot(event.target.value);
                  setIdentity(crypto.randomUUID());
                }}
              >
                <option value="">{t.choose}</option>
                {controls.snapshots.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.created_at
                      ? new Date(item.created_at).toLocaleString("ru-RU")
                      : item.name}{" "}
                    · {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t.reason}
              <textarea
                aria-label={t.reason}
                required
                minLength={5}
                maxLength={1000}
                value={reason}
                disabled={controls.disabled}
                onChange={(event) => {
                  setReason(event.target.value);
                  setIdentity(crypto.randomUUID());
                }}
              />
            </label>
            <button
              disabled={
                controls.disabled || !selectable || reason.trim().length < 5
              }
            >
              {t.approve}
            </button>
          </form>
        </>
      )}
    </section>
  );
}
