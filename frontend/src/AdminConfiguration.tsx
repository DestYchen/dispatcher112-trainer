import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { TableScroll } from "./components/TableScroll";
import common from "./components/Common.module.css";
import layout from "./Learning.module.css";
import { configurationStrings as t } from "./lib/configurationStrings";

export interface RuntimeConfiguration {
  database: {
    api_pool_size: number;
    worker_pool_size: number;
    sip_pool_size: number;
    max_overflow: number;
    pool_timeout_seconds: number;
    statement_timeout_ms: number;
    lock_timeout_ms: number;
  };
  sip: {
    inbound_ring_seconds: number;
    unanswered_seconds: number;
    max_call_seconds: number;
  };
  logging: { level: "INFO" | "WARNING" | "ERROR"; http_access: boolean };
}
type Role = keyof typeof t.roles;
export interface ConfigurationIndex {
  snapshot: {
    revision: number;
    configuration: RuntimeConfiguration;
    reason: string;
    changed_at: string | null;
  };
  configured: boolean;
  maintenance: {
    enabled: boolean;
    job_id?: string | null;
    update_id?: string | null;
    switch_id?: string | null;
    topology_id?: string | null;
  };
  database_connection_budget: number;
  actor_id?: string;
  backend_replicas?: number;
  topology?: { phase: string } | null;
  applied: boolean;
  missing: Role[];
  processes: {
    id: string;
    role: Role;
    revision: number | null;
    error: string | null;
    at: string;
    pool_size: number;
    checked_out: number;
    statement_timeout_ms: number;
    lock_timeout_ms: number;
  }[];
}
interface SaveResult extends ConfigurationIndex {
  saved_revision: number;
  superseded: boolean;
  replayed: boolean;
}
const queryKey = ["admin-configuration"];
const path = "/admin/operations/configuration";
const databaseFields = [
  ["api_pool_size", 5, 50],
  ["worker_pool_size", 1, 20],
  ["sip_pool_size", 1, 10],
  ["max_overflow", 0, 10],
  ["pool_timeout_seconds", 1, 60],
  ["statement_timeout_ms", 500, 30000],
  ["lock_timeout_ms", 100, 10000],
] as const;
const sipFields = [
  ["inbound_ring_seconds", 5, 120],
  ["unanswered_seconds", 5, 120],
  ["max_call_seconds", 60, 3600],
] as const;

function ConfigurationForm({
  data,
  stale,
}: {
  data: ConfigurationIndex;
  stale: boolean;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useState(data.snapshot.configuration);
  const [revision, setRevision] = useState(data.snapshot.revision);
  const [reason, setReason] = useState("");
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const save = useMutation({
    mutationFn: () =>
      api<SaveResult>(path, {
        method: "POST",
        body: JSON.stringify({
          id: requestId,
          expected_revision: revision,
          reason,
          configuration: draft,
        }),
      }),
    onSuccess: (result) => {
      client.setQueryData(queryKey, result);
      setDraft(result.snapshot.configuration);
      setRevision(result.snapshot.revision);
      setRequestId(crypto.randomUUID());
    },
  });
  function edit(value: RuntimeConfiguration) {
    setDraft(value);
    setRequestId(crypto.randomUUID());
    save.reset();
  }
  const importFile = useMutation({
    mutationFn: (file: File) =>
      api<{ configuration: RuntimeConfiguration; revision: number }>(
        `${path}/import.xml`,
        {
          method: "POST",
          headers: { "Content-Type": "application/xml" },
          body: file,
        },
      ),
    onSuccess: (result) => {
      edit(result.configuration);
      setRevision(result.revision);
    },
  });
  const exportFile = useMutation({
    mutationFn: async () => {
      const response = await fetch(`/api/v1${path}/export.xml`, {
        credentials: "include",
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error(t.exportFailed);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "runtime-configuration.xml";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  });
  const conflict = revision !== data.snapshot.revision;
  const busy =
    !!data.maintenance.job_id ||
    !!data.maintenance.update_id ||
    !!data.maintenance.switch_id ||
    !!data.maintenance.topology_id ||
    importFile.isPending;
  const limit =
    (data.backend_replicas ?? 1) *
      (draft.database.api_pool_size + draft.database.max_overflow) +
    draft.database.worker_pool_size +
    draft.database.sip_pool_size +
    2 * draft.database.max_overflow;
  return (
    <form
      className={common.form}
      onSubmit={(event) => {
        event.preventDefault();
        if (
          !stale &&
          !busy &&
          !conflict &&
          data.maintenance.enabled &&
          !save.isPending
        )
          save.mutate();
      }}
    >
      {conflict && <p role="alert">{t.conflict}</p>}
      <div className={common.toolbar}>
        <button
          type="button"
          disabled={exportFile.isPending || stale}
          onClick={() => exportFile.mutate()}
        >
          {t.exportXml}
        </button>
        <label>
          {t.importXml}
          <input
            type="file"
            accept=".xml,application/xml"
            disabled={save.isPending || importFile.isPending}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) importFile.mutate(file);
              event.target.value = "";
            }}
          />
        </label>
      </div>
      {importFile.isPending && <p role="status">{t.importing}</p>}
      {importFile.isSuccess && <p role="status">{t.imported}</p>}
      {importFile.error && <p role="alert">{importFile.error.message}</p>}
      {exportFile.error && <p role="alert">{exportFile.error.message}</p>}
      <button
        type="button"
        disabled={stale || save.isPending || importFile.isPending}
        onClick={() => {
          setDraft(data.snapshot.configuration);
          setRevision(data.snapshot.revision);
          setRequestId(crypto.randomUUID());
          save.reset();
        }}
      >
        {t.reload}
      </button>
      <fieldset disabled={save.isPending || importFile.isPending}>
        <legend>{t.database}</legend>
        {databaseFields.map(([key, min, max]) => (
          <label key={key}>
            {t[key]}
            <input
              type="number"
              required
              min={min}
              max={max}
              step={1}
              value={
                Number.isFinite(draft.database[key]) ? draft.database[key] : ""
              }
              onChange={(event) =>
                edit({
                  ...draft,
                  database: {
                    ...draft.database,
                    [key]: event.target.valueAsNumber,
                  },
                })
              }
            />
          </label>
        ))}
      </fieldset>
      <p>{t.databaseHelp}</p>
      <p>
        {t.requested}: {Number.isFinite(limit) ? limit : "—"} / {t.budget}:{" "}
        {data.database_connection_budget}
      </p>
      <fieldset disabled={save.isPending || importFile.isPending}>
        <legend>{t.sip}</legend>
        {sipFields.map(([key, min, max]) => (
          <label key={key}>
            {t[key]}
            <input
              type="number"
              required
              min={min}
              max={max}
              step={1}
              value={Number.isFinite(draft.sip[key]) ? draft.sip[key] : ""}
              onChange={(event) =>
                edit({
                  ...draft,
                  sip: { ...draft.sip, [key]: event.target.valueAsNumber },
                })
              }
            />
          </label>
        ))}
      </fieldset>
      <p>{t.sipHelp}</p>
      <fieldset disabled={save.isPending || importFile.isPending}>
        <legend>{t.logging}</legend>
        <label>
          {t.level}
          <select
            value={draft.logging.level}
            onChange={(event) =>
              edit({
                ...draft,
                logging: {
                  ...draft.logging,
                  level: event.target
                    .value as RuntimeConfiguration["logging"]["level"],
                },
              })
            }
          >
            {Object.entries(t.levels).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={draft.logging.http_access}
            onChange={(event) =>
              edit({
                ...draft,
                logging: {
                  ...draft.logging,
                  http_access: event.target.checked,
                },
              })
            }
          />
          {t.http_access}
        </label>
      </fieldset>
      <p>{t.loggingHelp}</p>
      <label>
        {t.reason}
        <textarea
          required
          minLength={5}
          maxLength={1000}
          value={reason}
          disabled={save.isPending}
          onChange={(event) => {
            setReason(event.target.value);
            setRequestId(crypto.randomUUID());
            save.reset();
          }}
        />
      </label>
      {!data.maintenance.enabled && <p>{t.maintenanceRequired}</p>}
      {busy && <p>{t.busy}</p>}
      <button
        className={common.primary}
        disabled={
          stale ||
          busy ||
          conflict ||
          !data.maintenance.enabled ||
          save.isPending ||
          reason.trim().length < 5
        }
      >
        {save.isPending ? t.saving : t.save}
      </button>
      {save.error && <p role="alert">{save.error.message}</p>}
      {save.isSuccess && (
        <p role="status">{save.data.superseded ? t.superseded : t.saved}</p>
      )}
    </form>
  );
}

export function AdminConfiguration() {
  const query = useQuery({
    queryKey,
    queryFn: () => api<ConfigurationIndex>(path),
    refetchInterval: 2000,
    retry: false,
  });
  const data = query.data;
  return (
    <div className={layout.list}>
      <p>{t.explanation}</p>
      <button onClick={() => void query.refetch()} disabled={query.isFetching}>
        {t.refresh}
      </button>
      {query.error && <p role="alert">{t.unavailable}</p>}
      <AsyncView
        loading={query.isPending}
        error={!data ? query.error : null}
        retry={() => void query.refetch()}
      >
        {data && (
          <>
            {!data.configured && <p className={common.empty}>{t.defaults}</p>}
            <p>
              {t.revision}: {data.snapshot.revision}
            </p>
            <section className={layout.item} aria-label={t.processes}>
              <h3>{t.processes}</h3>
              <p role="status">
                {!query.error && data.applied ? t.applied : t.pending}
              </p>
              {data.missing.length > 0 && (
                <p>
                  {t.missing}:{" "}
                  {data.missing.map((role) => t.roles[role]).join(", ")}
                </p>
              )}
              <AsyncView empty={data.processes.length === 0 && t.noProcesses}>
                <TableScroll>
                  <table className={common.table}>
                    <thead>
                      <tr>
                        {[
                          t.process,
                          t.revision,
                          t.state,
                          t.pool,
                          t.timeouts,
                          t.at,
                        ].map((label) => (
                          <th key={label}>{label}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {data.processes.map((item) => (
                        <tr key={item.role + item.id}>
                          <th scope="row">
                            {t.roles[item.role]}
                            <br />
                            <small>{item.id}</small>
                          </th>
                          <td>{item.revision ?? "—"}</td>
                          <td>
                            {item.error
                              ? t.failed
                              : item.revision === data.snapshot.revision
                                ? t.ready
                                : t.waiting}
                          </td>
                          <td>
                            {item.pool_size} / {item.checked_out}
                          </td>
                          <td>
                            {item.statement_timeout_ms} / {item.lock_timeout_ms}
                          </td>
                          <td>{new Date(item.at).toLocaleString("ru-RU")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableScroll>
              </AsyncView>
            </section>
            <ConfigurationForm data={data} stale={!!query.error} />
            <section className={layout.item} aria-label={t.servers}>
              <h3>{t.serverCount}</h3>
              <p>
                {t.countedServers}: {data.backend_replicas ?? 1}.{" "}
                {t.poolSummary}
              </p>
              {data.topology?.phase === "APPLYING" && (
                <p role="status">{t.scaling}</p>
              )}
              {data.actor_id && (
                <details>
                  <summary>{t.changeServers}</summary>
                  <p>{t.scalingHelp}</p>
                  <p>
                    <code>
                      python scripts/scale_backends.py --replicas 2 --actor{" "}
                      {data.actor_id}
                    </code>
                  </p>
                  <p>{t.scalingComplete}</p>
                </details>
              )}
            </section>
          </>
        )}
      </AsyncView>
    </div>
  );
}
