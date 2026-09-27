import { useMutation, useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { TableScroll } from "./components/TableScroll";
import { updateStrings as t } from "./lib/updateStrings";
import common from "./components/Common.module.css";
import layout from "./Learning.module.css";

type Action = keyof typeof t.actions;
interface Package {
  id: string;
  filename: string;
  version: string;
  description: string;
  size: number;
  uploaded_at: string;
  program_files: number;
  sha256: string;
  publisher_sha256: string;
}
interface Update {
  id: string;
  version: string;
  reason: string;
  phase: string;
  at: string;
}
export interface UpdateIndex {
  trust: {
    configured: boolean;
    fingerprint: string | null;
    error: string | null;
  };
  limits: { archive_bytes: number; packages: number; storage_bytes: number };
  packages: Package[];
  updates: Update[];
  current: { id: string; phase: string; version: string } | null;
  maintenance: {
    enabled: boolean;
    reason: string;
    update_id?: string;
    job_id?: string;
  };
  execution: "LOCAL_COMMAND";
}
interface RequestResult {
  filename: string;
  execution: "LOCAL_COMMAND";
  request: {
    signature: string;
    value: {
      id: string;
      action: Action;
      update_id: string;
      version: string;
      expires_at: string;
      reason: string;
    };
  };
}
const path = "/admin/operations/updates";
const rollbackPhases = [
  "STARTED",
  "BACKED_UP",
  "MIGRATED",
  "READY",
  "ROLLING_BACK",
  "FAILED",
];
const phase = (value: string) =>
  t.phases[value as keyof typeof t.phases] ?? value;
const date = (value: string) => new Date(value).toLocaleString("ru-RU");
const size = (value: number) =>
  `${(value / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МиБ`;

export function AdminUpdates() {
  const query = useQuery({
    queryKey: ["software-updates"],
    queryFn: () => api<UpdateIndex>(path),
    refetchInterval: 5000,
    retry: false,
  });
  const data = query.data;
  const [file, setFile] = useState<File | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [packageId, setPackageId] = useState("");
  const [action, setAction] = useState<Action>("apply");
  const [reason, setReason] = useState("");
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [removeId, setRemoveId] = useState<string | null>(null);
  const download = useMutation({
    mutationFn: async (item: Package) => {
      const response = await fetch(`/api/v1${path}/packages/${item.id}`, {
        credentials: "include",
        signal: AbortSignal.timeout(180000),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => null);
        throw new Error(error?.error?.message ?? t.downloadFailed);
      }
      const blob = await response.blob();
      if (blob.size !== item.size) throw new Error(t.downloadFailed);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `release-${item.version}.zip`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    },
  });
  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error(t.selectFile);
      if (file.size > (data?.limits.archive_bytes ?? 0))
        throw new Error(t.tooLarge);
      return api<Package>(
        `${path}/packages?filename=${encodeURIComponent(file.name)}`,
        {
          method: "POST",
          body: file,
          headers: { "Content-Type": "application/octet-stream" },
          signal: AbortSignal.timeout(180000),
        },
      );
    },
    onSuccess: async (value) => {
      setPackageId(value.id);
      resetRequest();
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await query.refetch();
    },
  });
  const request = useMutation({
    mutationFn: () =>
      api<RequestResult>(`${path}/requests`, {
        method: "POST",
        signal: AbortSignal.timeout(180000),
        body: JSON.stringify({
          id: requestId,
          action,
          package_id: action === "apply" ? packageId : null,
          update_id: action === "apply" ? null : data?.current?.id,
          reason: reason.trim(),
        }),
      }),
    onSuccess: () => query.refetch(),
  });
  const remove = useMutation({
    mutationFn: (identity: string) =>
      api(`${path}/packages/${identity}`, { method: "DELETE" }),
    onSuccess: async () => {
      if (packageId === removeId) setPackageId("");
      setRemoveId(null);
      await query.refetch();
    },
  });
  const allowed =
    action === "apply" ||
    (action === "activate"
      ? data?.current?.phase === "READY"
      : rollbackPhases.includes(data?.current?.phase ?? ""));
  const busy = Boolean(data?.maintenance.update_id || data?.maintenance.job_id);
  function resetRequest() {
    request.reset();
    setRequestId(crypto.randomUUID());
  }
  function downloadRequest() {
    if (!request.data) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(request.data.request, null, 2)], {
        type: "application/json",
      }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = request.data.filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }
  const result = request.data;
  return (
    <div className={layout.list}>
      <p>{t.explanation}</p>
      <button onClick={() => void query.refetch()} disabled={query.isFetching}>
        {t.refresh}
      </button>
      <AsyncView
        loading={query.isPending}
        error={query.error ? new Error(t.unavailable) : null}
        retry={() => void query.refetch()}
      >
        {data && (
          <>
            <section className={layout.notice} aria-label={t.trust}>
              <h3>{t.trust}</h3>
              {data.trust.configured ? (
                <p className={layout.text}>
                  {t.fingerprint}: <code>{data.trust.fingerprint}</code>
                </p>
              ) : (
                <p>{data.trust.error ?? t.noKey}</p>
              )}
              <p>
                {t.limits}: {size(data.limits.archive_bytes)}
              </p>
            </section>
            {data.maintenance.enabled && (
              <p role="status" className={layout.notice}>
                {t.maintenance}: {data.maintenance.reason}
              </p>
            )}
            {data.current && (
              <p className={layout.text}>
                {t.activeOperation}: {data.current.version} —{" "}
                {phase(data.current.phase)}
              </p>
            )}
            {data.current?.phase === "READY" && (
              <p className={layout.notice}>{t.readyHelp}</p>
            )}
          </>
        )}
      </AsyncView>
      <form
        className={common.form}
        onSubmit={(event) => {
          event.preventDefault();
          upload.mutate();
        }}
      >
        <label>
          {t.uploadFile}
          <input
            ref={fileInput}
            aria-label={t.uploadFile}
            type="file"
            accept=".zip,application/zip"
            required
            disabled={upload.isPending}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              upload.reset();
            }}
          />
        </label>
        <button
          disabled={
            !data?.trust.configured ||
            !file ||
            upload.isPending ||
            busy ||
            query.isError
          }
        >
          {t.upload}
        </button>
        {upload.isPending && (
          <AsyncView loading>
            <span>{t.uploading}</span>
          </AsyncView>
        )}
        {upload.error && (
          <p className={common.error} role="alert">
            {upload.error.message}
          </p>
        )}
        {upload.isSuccess && <p role="status">{t.uploaded}</p>}
      </form>
      <section aria-label={t.packages}>
        <h3>{t.packages}</h3>
        <AsyncView
          loading={query.isPending}
          error={query.error}
          retry={() => void query.refetch()}
          empty={data?.packages.length === 0 && t.noPackages}
        >
          <TableScroll>
            <table className={common.table}>
              <thead>
                <tr>
                  <th>{t.version}</th>
                  <th>{t.description}</th>
                  <th>{t.size}</th>
                  <th>{t.uploadedAt}</th>
                  <th>{t.action}</th>
                </tr>
              </thead>
              <tbody>
                {data?.packages.map((item) => (
                  <tr key={item.id}>
                    <td>{item.version}</td>
                    <td>
                      {item.description}
                      <p>
                        {t.files}: {item.program_files}
                      </p>
                    </td>
                    <td>{size(item.size)}</td>
                    <td>{date(item.uploaded_at)}</td>
                    <td>
                      <button
                        disabled={download.isPending}
                        onClick={() => download.mutate(item)}
                      >
                        {t.downloadPackage}
                      </button>
                      <button
                        disabled={busy || remove.isPending}
                        onClick={() => {
                          setRemoveId(item.id);
                          remove.reset();
                        }}
                      >
                        {t.remove}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </AsyncView>
        {download.isPending && <p role="status">{t.downloading}</p>}
        {download.error && (
          <p className={common.error} role="alert">
            {download.error.message}
          </p>
        )}
        {download.isSuccess && <p role="status">{t.downloaded}</p>}
        {removeId && (
          <div className={layout.notice}>
            <p>{t.removeHelp}</p>
            <div className={common.toolbar}>
              <button
                disabled={remove.isPending}
                onClick={() => remove.mutate(removeId)}
              >
                {t.confirmRemove}
              </button>
              <button
                disabled={remove.isPending}
                onClick={() => {
                  setRemoveId(null);
                  remove.reset();
                }}
              >
                {t.cancel}
              </button>
            </div>
            {remove.error && <p role="alert">{remove.error.message}</p>}
          </div>
        )}
      </section>
      <form
        className={common.form}
        onSubmit={(event) => {
          event.preventDefault();
          request.mutate();
        }}
      >
        <h3>{t.prepare}</h3>
        <label>
          {t.action}
          <select
            value={action}
            aria-label={t.action}
            disabled={request.isPending}
            onChange={(event) => {
              setAction(event.target.value as Action);
              resetRequest();
            }}
          >
            {Object.entries(t.actions).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {action === "apply" && (
          <label>
            {t.package}
            <select
              required
              value={packageId}
              aria-label={t.package}
              disabled={request.isPending}
              onChange={(event) => {
                setPackageId(event.target.value);
                resetRequest();
              }}
            >
              <option value="">{t.choosePackage}</option>
              {data?.packages.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.version} — {item.description}
                </option>
              ))}
            </select>
          </label>
        )}
        {!allowed && <p role="status">{t.staleAction}</p>}
        <label>
          {t.reason}
          <textarea
            minLength={5}
            aria-label={t.reason}
            maxLength={1000}
            required
            value={reason}
            disabled={request.isPending}
            onChange={(event) => {
              setReason(event.target.value);
              resetRequest();
            }}
          />
        </label>
        <p>{t.reasonHelp}</p>
        <button
          disabled={
            !data?.trust.configured ||
            query.isError ||
            request.isPending ||
            reason.trim().length < 5 ||
            !allowed ||
            (action === "apply" && (!packageId || busy))
          }
        >
          {t.create}
        </button>
      </form>
      <AsyncView
        loading={request.isPending}
        error={request.error}
        empty={!result && t.noRequest}
        retry={() => request.mutate()}
      >
        {result && (
          <section className={layout.notice} aria-label={t.requestReady}>
            <h3>{t.requestReady}</h3>
            <p>
              {t.actions[result.request.value.action]} ·{" "}
              {result.request.value.version}
            </p>
            <p>
              {t.expires}: {date(result.request.value.expires_at)}
            </p>
            <button onClick={downloadRequest}>{t.downloadRequest}</button>
            <p>{t.localHelp}</p>
            <pre
              className={layout.log}
            >{`python scripts/install_update.py execute-request --request ${result.filename}${result.request.value.action === "apply" ? ` --package release-${result.request.value.version}.zip` : ""}`}</pre>
            <button onClick={resetRequest}>{t.newRequest}</button>
          </section>
        )}
      </AsyncView>
      <section aria-label={t.history}>
        <h3>{t.history}</h3>
        <AsyncView
          loading={query.isPending}
          error={query.error}
          retry={() => void query.refetch()}
          empty={data?.updates.length === 0 && t.noHistory}
        >
          <TableScroll>
            <table className={common.table}>
              <thead>
                <tr>
                  <th>{t.version}</th>
                  <th>{t.phase}</th>
                  <th>{t.at}</th>
                  <th>{t.reason}</th>
                </tr>
              </thead>
              <tbody>
                {data?.updates.map((item) => (
                  <tr key={item.id}>
                    <td>{item.version}</td>
                    <td>{phase(item.phase)}</td>
                    <td>{date(item.at)}</td>
                    <td>{item.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </AsyncView>
      </section>
    </div>
  );
}
