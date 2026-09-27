import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import common from "./components/Common.module.css";
import layout from "./Learning.module.css";
import { backupStrings as t } from "./lib/strings";
import type { Operation } from "./TechnicalConsole";
import {
  FileIntegrity,
  IntegrityOutcome,
  type IntegrityResult,
} from "./FileIntegrity";

interface Schedule {
  enabled: boolean;
  hour: number;
  minute: number;
  retention: number;
}
export interface BackupOutcome {
  status?: IntegrityResult["status"];
  name?: string;
  snapshot?: string;
  files?: number;
  program_files?: number;
  bytes?: number;
  verified_at?: string;
  restored_users?: number;
  restored_types?: number;
  restored_audit?: number;
  source_unchanged?: boolean;
  schedule?: Schedule;
}
interface Snapshot {
  name: string;
  created_at: string | null;
  files: number | null;
  bytes: number | null;
  valid_manifest: boolean;
  program_files?: number | null;
  verification: BackupOutcome | null;
}

export function BackupResult({ result }: { result: BackupOutcome }) {
  if (result.status)
    return <IntegrityOutcome result={result as IntegrityResult} />;
  return (
    <div className={layout.notice}>
      {(result.name || result.snapshot) && (
        <p className={layout.text}>{result.name ?? result.snapshot}</p>
      )}
      <dl className={layout.metrics}>
        {result.files !== undefined && (
          <div>
            <dt>{t.files}</dt>
            <dd>{result.files}</dd>
          </div>
        )}
        {result.bytes !== undefined && (
          <div>
            <dt>{t.size}</dt>
            <dd>
              {Math.ceil(result.bytes / 1024 / 1024)} {t.mb}
            </dd>
          </div>
        )}
        {result.program_files !== undefined && (
          <div>
            <dt>{t.programFiles}</dt>
            <dd>{result.program_files}</dd>
          </div>
        )}
        {result.restored_users !== undefined && (
          <div>
            <dt>{t.users}</dt>
            <dd>{result.restored_users}</dd>
          </div>
        )}
        {result.restored_types !== undefined && (
          <div>
            <dt>{t.types}</dt>
            <dd>{result.restored_types}</dd>
          </div>
        )}
        {result.restored_audit !== undefined && (
          <div>
            <dt>{t.audit}</dt>
            <dd>{result.restored_audit}</dd>
          </div>
        )}
      </dl>
      {result.source_unchanged && <p>{t.sourceUnchanged}</p>}
      {result.schedule && <p>{t.scheduleSaved}</p>}
    </div>
  );
}

function ScheduleForm({
  current,
  disabled,
  submit,
}: {
  current: Schedule;
  disabled: boolean;
  submit: (body: Operation) => void;
}) {
  const [draft, setDraft] = useState<Schedule | null>(null);
  const value = draft ?? current;
  return (
    <form
      className={common.form}
      onSubmit={(event) => {
        event.preventDefault();
        submit({
          id: crypto.randomUUID(),
          service: "backup",
          kind: "backup_configure",
          schedule: value,
        });
      }}
    >
      <label>
        <input
          type="checkbox"
          checked={value.enabled}
          onChange={(event) =>
            setDraft({ ...value, enabled: event.target.checked })
          }
        />
        {t.enabled}
      </label>
      <label>
        {t.time}
        <input
          type="time"
          required
          value={`${String(value.hour).padStart(2, "0")}:${String(value.minute).padStart(2, "0")}`}
          onChange={(event) => {
            const [hour, minute] = event.target.value.split(":").map(Number);
            setDraft({ ...value, hour, minute });
          }}
        />
      </label>
      <label>
        {t.retention}
        <input
          type="number"
          min={14}
          max={90}
          step={1}
          required
          value={value.retention}
          onChange={(event) =>
            setDraft({ ...value, retention: Number(event.target.value) })
          }
        />
      </label>
      <p>{t.retentionHelp}</p>
      <div className={common.toolbar}>
        <button disabled={disabled}>{t.saveSchedule}</button>
        {draft && (
          <button type="button" onClick={() => setDraft(null)}>
            {t.cancelChanges}
          </button>
        )}
      </div>
    </form>
  );
}

export function BackupControls({
  disabled,
  submit,
}: {
  disabled: boolean;
  submit: (body: Operation) => void;
}) {
  const query = useQuery({
    queryKey: ["backup-catalog"],
    queryFn: () =>
      api<{ items: Snapshot[]; schedule: Schedule; worker_ready: boolean }>(
        "/admin/backups",
      ),
    refetchInterval: 3000,
    retry: false,
  });
  return (
    <AsyncView
      loading={query.isPending}
      error={query.data ? null : query.error}
      retry={() => void query.refetch()}
    >
      {query.data && (
        <>
          {query.error && (
            <div role="alert">
              <p>{query.error.message}</p>
              <button onClick={() => void query.refetch()}>{t.refresh}</button>
            </div>
          )}
          <p>{t.scope}</p>
          <p role={query.data.worker_ready ? "status" : "alert"}>
            {query.data.worker_ready ? t.ready : t.unavailable}
          </p>
          <div className={common.toolbar}>
            <button
              disabled={disabled || !query.data.worker_ready || !!query.error}
              onClick={() =>
                submit({
                  id: crypto.randomUUID(),
                  kind: "backup_create",
                  service: "backup",
                })
              }
            >
              {t.create}
            </button>
            <button onClick={() => void query.refetch()}>{t.refresh}</button>
          </div>
          <h3>{t.schedule}</h3>
          <ScheduleForm
            current={query.data.schedule}
            disabled={disabled || !query.data.worker_ready || !!query.error}
            submit={submit}
          />
          <h3>{t.saved}</h3>
          <FileIntegrity
            controls={{
              disabled: disabled || !query.data.worker_ready || !!query.error,
              snapshots: query.data.items.filter(
                (item) =>
                  item.valid_manifest &&
                  !!item.program_files &&
                  !!item.verification?.verified_at,
              ),
              submit,
            }}
          />
          <AsyncView empty={query.data.items.length === 0 && t.empty}>
            <div className={layout.list}>
              {query.data.items.map((item) => (
                <article className={layout.item} key={item.name}>
                  <h3>
                    {item.created_at
                      ? new Date(item.created_at).toLocaleString("ru-RU")
                      : t.unknownDate}
                  </h3>
                  <p>{item.name}</p>
                  {item.valid_manifest ? (
                    <p>
                      {item.files} {t.fileUnit} ·{" "}
                      {Math.ceil((item.bytes ?? 0) / 1024 / 1024)} {t.mb}
                    </p>
                  ) : (
                    <p role="alert">{t.damaged}</p>
                  )}
                  {item.valid_manifest && (
                    <p>
                      {item.program_files
                        ? `${t.programFiles}: ${item.program_files}`
                        : t.legacyScope}
                    </p>
                  )}
                  <p>
                    {t.lastVerification}:{" "}
                    {item.verification?.verified_at
                      ? new Date(item.verification.verified_at).toLocaleString(
                          "ru-RU",
                        )
                      : t.notVerified}
                  </p>
                  {item.verification && (
                    <BackupResult result={item.verification} />
                  )}
                  <button
                    disabled={
                      disabled ||
                      !query.data?.worker_ready ||
                      !!query.error ||
                      !item.valid_manifest
                    }
                    onClick={() =>
                      submit({
                        id: crypto.randomUUID(),
                        kind: "backup_verify",
                        service: "backup",
                        snapshot: item.name,
                      })
                    }
                  >
                    {t.verify}
                  </button>
                </article>
              ))}
            </div>
          </AsyncView>
        </>
      )}
    </AsyncView>
  );
}
