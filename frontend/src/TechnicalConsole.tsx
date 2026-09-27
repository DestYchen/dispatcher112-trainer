import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import common from "./components/Common.module.css";
import layout from "./Learning.module.css";
import { technicalStrings as t } from "./lib/strings";
import { LocalOperation } from "./LocalOperation";
import { RecoverySwitch } from "./RecoverySwitch";
import {
  BackupControls,
  BackupResult,
  type BackupOutcome,
} from "./BackupControls";

type Action = "start" | "stop" | "restart";
interface Service {
  id: string;
  service: string;
  state: string;
  health: string | null;
  cpu_percent: number | null;
  memory_bytes: number | null;
  memory_limit_bytes: number;
  cpu_limit: number;
  actions: Action[];
  minimum_memory_mb: number;
  image_id: string;
  image_matches?: boolean | null;
  readonly_root?: boolean;
}
export interface Operation {
  id: string;
  kind:
    | "service"
    | "resources"
    | "backup_create"
    | "backup_verify"
    | "backup_configure"
    | "backup_integrity_check"
    | "backup_integrity_baseline";
  service: string;
  action?: Action;
  cpus?: number;
  memory_mb?: number;
  snapshot?: string;
  reason?: string;
  schedule?: {
    enabled: boolean;
    hour: number;
    minute: number;
    retention: number;
  };
}
interface Job {
  id: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  error?: string;
  result?: BackupOutcome;
  execution?: "LOCAL_COMMAND";
}
interface Maintenance {
  enabled: boolean;
  reason: string;
  job_id?: string;
  update_id?: string;
  switch_id?: string;
  topology_id?: string;
}

function Logs({ service }: { service: string }) {
  const query = useQuery({
    queryKey: ["service-logs", service],
    queryFn: () =>
      api<{ items: { instance: string; text: string }[]; stale?: boolean }>(
        `/admin/operations/services/${service}/logs`,
      ),
    refetchInterval: 5000,
  });
  return (
    <AsyncView
      loading={query.isPending}
      error={query.error}
      retry={() => void query.refetch()}
      empty={query.data?.items.every((item) => !item.text) && t.noLogs}
    >
      {query.data?.stale && <p role="status">{t.staleLogs}</p>}
      {query.data?.items.map((item) => (
        <pre className={layout.log} key={item.instance}>
          {item.text}
        </pre>
      ))}
      <button onClick={() => void query.refetch()}>{t.refresh}</button>
    </AsyncView>
  );
}

function ServiceControl({
  service,
  disabled,
  submit,
}: {
  service: Service;
  disabled: boolean;
  submit: (body: Operation) => void;
}) {
  const [logs, setLogs] = useState(false);
  const [cpus, setCpus] = useState(service.cpu_limit || 1);
  const [memory, setMemory] = useState(
    Math.max(
      service.minimum_memory_mb,
      Math.ceil(service.memory_limit_bytes / 1024 / 1024),
    ),
  );
  const label =
    t.services[service.service as keyof typeof t.services] ?? service.service;
  return (
    <article className={layout.item} aria-label={label}>
      <h3>
        {label} · {service.id}
      </h3>
      <p>
        {t.state}:{" "}
        {t.states[service.state as keyof typeof t.states] ?? service.state}
        {service.health &&
          ` / ${t.states[service.health as keyof typeof t.states] ?? service.health}`}
      </p>
      <p>
        {t.cpuUsage}:{" "}
        {service.cpu_percent === null
          ? t.unknown
          : `${service.cpu_percent.toFixed(1)} %`}{" "}
        · {t.memoryUsage}:{" "}
        {service.memory_bytes === null
          ? t.unknown
          : `${Math.round(service.memory_bytes / 1024 / 1024)} ${t.mb}`}
      </p>
      <p>
        {t.image}:{" "}
        {service.image_matches === true
          ? t.imageMatch
          : service.image_matches === false
            ? t.imageMismatch
            : t.imageUnknown}
        .
        {service.readonly_root !== undefined &&
          ` ${t.rootFilesystem}: ${service.readonly_root ? t.readonly : t.writable}.`}
      </p>
      {service.image_matches === false && <p role="alert">{t.imageCheck}</p>}
      <div className={common.toolbar}>
        {service.actions.map((action) => (
          <button
            key={action}
            disabled={disabled}
            onClick={() =>
              submit({
                id: crypto.randomUUID(),
                kind: "service",
                service: service.service,
                action,
              })
            }
          >
            {t.actions[action]}
          </button>
        ))}
      </div>
      <details>
        <summary>{t.resources}</summary>
        <form
          className={common.form}
          onSubmit={(event) => {
            event.preventDefault();
            submit({
              id: crypto.randomUUID(),
              kind: "resources",
              service: service.service,
              cpus,
              memory_mb: memory,
            });
          }}
        >
          <label>
            {t.cpuLimit}
            <input
              aria-label={`${label}: ${t.cpuLimit}`}
              type="number"
              min={0.5}
              max={64}
              step={0.5}
              required
              value={cpus}
              onChange={(event) => setCpus(Number(event.target.value))}
            />
          </label>
          <label>
            {t.memoryLimit}
            <input
              aria-label={`${label}: ${t.memoryLimit}`}
              type="number"
              min={service.minimum_memory_mb}
              max={65536}
              required
              value={memory}
              onChange={(event) => setMemory(Number(event.target.value))}
            />
          </label>
          <button disabled={disabled}>{t.applyResources}</button>
        </form>
      </details>
      <button aria-expanded={logs} onClick={() => setLogs(!logs)}>
        {t.logs}
      </button>
      {logs && <Logs service={service.service} />}
    </article>
  );
}

export function TechnicalConsole({
  mode = "services",
}: {
  mode?: "services" | "backups";
}) {
  const [reason, setReason] = useState("");
  const [lastJob, setLastJob] = useState<string | null>(null);
  const maintenance = useQuery({
    queryKey: ["maintenance"],
    queryFn: () => api<Maintenance>("/admin/maintenance"),
    refetchInterval: 3000,
  });
  const services = useQuery({
    queryKey: ["technical-services"],
    queryFn: () =>
      api<{
        items: Service[];
        host?: { cpus: number; memory_bytes: number; engine_version: string };
        execution?: "LOCAL_COMMAND";
        collected_at?: string | null;
        stale?: boolean;
        refresh_interval_sec?: number | null;
      }>("/admin/operations/services"),
    refetchInterval: 5000,
    retry: false,
    enabled: mode === "services",
  });
  const jobId = maintenance.data?.job_id ?? lastJob;
  const job = useQuery({
    queryKey: ["technical-job", jobId],
    queryFn: () => api<Job>(`/admin/operations/jobs/${jobId}`),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data &&
      ["SUCCEEDED", "FAILED"].includes(query.state.data.status)
        ? false
        : 1500,
  });
  const change = useMutation({
    mutationFn: async () => {
      await api("/admin/maintenance", {
        method: "POST",
        body: JSON.stringify({ enabled: !maintenance.data?.enabled, reason }),
      });
      await maintenance.refetch();
    },
  });
  const operation = useMutation({
    mutationFn: async (body: Operation) => {
      try {
        await api("/admin/operations/jobs", {
          method: "POST",
          body: JSON.stringify(body),
        });
        setLastJob(body.id);
      } finally {
        await maintenance.refetch();
      }
    },
  });
  const externallyLocked = !!(
    maintenance.data?.update_id ||
    maintenance.data?.switch_id ||
    maintenance.data?.topology_id
  );
  const busy =
    operation.isPending || !!maintenance.data?.job_id || externallyLocked;
  return (
    <>
      <AsyncView
        loading={maintenance.isPending}
        error={maintenance.error}
        retry={() => void maintenance.refetch()}
      >
        <p role="status">
          {maintenance.data?.enabled ? t.maintenanceOn : t.maintenanceOff}
        </p>
        <p>{t.maintenanceHelp}</p>
        {externallyLocked && <p role="status">{t.operationLocked}</p>}
        <form
          className={common.form}
          onSubmit={(event) => {
            event.preventDefault();
            change.mutate();
          }}
        >
          <label>
            {t.reason}
            <input
              aria-label={t.reason}
              required
              minLength={5}
              maxLength={1000}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <button disabled={busy || change.isPending}>
            {maintenance.data?.enabled
              ? t.leaveMaintenance
              : t.enterMaintenance}
          </button>
          {change.error && <p role="alert">{change.error.message}</p>}
        </form>
      </AsyncView>
      {operation.error && <p role="alert">{operation.error.message}</p>}
      {jobId && (
        <AsyncView
          loading={job.isPending}
          error={job.error}
          retry={() => void job.refetch()}
        >
          <p role="status">
            {t.operation}: {job.data && t.jobs[job.data.status]}
          </p>
          {job.data?.error && <p role="alert">{job.data.error}</p>}
          {job.data?.status === "QUEUED" &&
            job.data.execution === "LOCAL_COMMAND" && (
              <LocalOperation
                key={jobId}
                id={jobId}
                completed={() => {
                  void job.refetch();
                  void maintenance.refetch();
                }}
              />
            )}
          {job.data?.status === "SUCCEEDED" &&
            job.data.result &&
            mode === "backups" && <BackupResult result={job.data.result} />}
        </AsyncView>
      )}
      {mode === "backups" ? (
        <>
          <BackupControls
            disabled={!maintenance.data?.enabled || busy}
            submit={(body) => operation.mutate(body)}
          />
          <RecoverySwitch />
        </>
      ) : (
        <>
          {services.data?.execution === "LOCAL_COMMAND" && (
            <section aria-label={t.serviceInformation}>
              <p>{t.monitorHelp}</p>
              <p>
                <code>python scripts/technical_operations.py watch</code>
              </p>
              <p role="status">
                {services.data.collected_at
                  ? `${t.collected}: ${new Date(services.data.collected_at).toLocaleString("ru-RU")}`
                  : t.monitorEmpty}
                {services.data.stale && services.data.collected_at
                  ? `. ${t.stale}.`
                  : ""}
              </p>
              {services.data.collected_at && (
                <p role="status">
                  {services.error || services.data.stale
                    ? t.monitorStopped
                    : services.data.refresh_interval_sec
                      ? t.monitorLive
                      : t.monitorSnapshot}
                </p>
              )}
              <button
                onClick={() => void services.refetch()}
                disabled={services.isFetching}
              >
                {t.refreshServices}
              </button>
            </section>
          )}
          <AsyncView
            loading={services.isPending}
            error={services.error}
            retry={() => void services.refetch()}
            empty={services.data?.items.length === 0 && t.noServices}
          >
            {services.data?.host && (
              <dl className={layout.metrics}>
                <div>
                  <dt>{t.hostCpus}</dt>
                  <dd>{services.data.host.cpus}</dd>
                </div>
                <div>
                  <dt>{t.hostMemory}</dt>
                  <dd>
                    {Math.round(services.data.host.memory_bytes / 1024 / 1024)}{" "}
                    {t.mb}
                  </dd>
                </div>
                <div>
                  <dt>{t.engine}</dt>
                  <dd>{services.data.host.engine_version}</dd>
                </div>
              </dl>
            )}
            <div className={layout.list}>
              {services.data?.items.map((service) => (
                <ServiceControl
                  key={service.id}
                  service={service}
                  disabled={
                    !maintenance.data?.enabled || busy || !!services.data?.stale
                  }
                  submit={(body) => operation.mutate(body)}
                />
              ))}
            </div>
          </AsyncView>
        </>
      )}
    </>
  );
}
