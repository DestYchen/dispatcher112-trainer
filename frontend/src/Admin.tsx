import { TableScroll } from "./components/TableScroll";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { allItems, api } from "./api/client";
import { AdminUsers, type ManagedUser } from "./AdminUsers";
import { AdminPolicy } from "./AdminPolicy";
import { AdminDiagnostics } from "./AdminDiagnostics";
import { AdminUpdates } from "./AdminUpdates";
import { AdminConfiguration } from "./AdminConfiguration";
import { TechnicalConsole } from "./TechnicalConsole";
import { AsyncView } from "./components/AsyncView";
import { DownloadButton } from "./components/DownloadButton";
import { strings, exchangeStrings } from "./lib/strings";
import common from "./components/Common.module.css";
import styles from "./components/Reports.module.css";

function Workstations() {
  const [number, setNumber] = useState("");
  const [room, setRoom] = useState("");
  const query = useQuery({
    queryKey: ["admin-workstations"],
    queryFn: () =>
      api<{
        items: {
          id: string;
          number: string;
          room: string | null;
          is_active: boolean;
        }[];
      }>("/admin/workstations"),
  });
  const create = useMutation({
    mutationFn: async () => {
      await api("/admin/workstations", {
        method: "POST",
        body: JSON.stringify({ number, room }),
      });
      setNumber("");
      await query.refetch();
    },
  });
  return (
    <>
      <DownloadButton
        path="/admin/workstations.xml"
        filename="workstations.xml"
      >
        {exchangeStrings.workstations}
      </DownloadButton>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        empty={query.data?.items.length === 0 && strings.noWorkstations}
        retry={() => void query.refetch()}
      >
        <TableScroll>
          <table className={common.table}>
            <thead>
              <tr>
                <th>{strings.workstationNumber}</th>
                <th>{strings.room}</th>
                <th>{strings.status}</th>
              </tr>
            </thead>
            <tbody>
              {query.data?.items.map((row) => (
                <tr key={row.id}>
                  <td>{row.number}</td>
                  <td>{row.room}</td>
                  <td>{row.is_active ? strings.active : strings.blocked}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </AsyncView>
      <form
        className={common.form}
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <label>
          {strings.workstationNumber}
          <input
            required
            maxLength={16}
            value={number}
            onChange={(event) => setNumber(event.target.value)}
          />
        </label>
        <label>
          {strings.room}
          <input
            maxLength={64}
            value={room}
            onChange={(event) => setRoom(event.target.value)}
          />
        </label>
        <button disabled={create.isPending}>{strings.createWorkstation}</button>
        {create.error && <p role="alert">{create.error.message}</p>}
      </form>
    </>
  );
}
function ImportFile({
  kind,
  refresh,
}: {
  kind: "classifier" | "streets";
  refresh: () => Promise<unknown>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: async () => {
      const body = new FormData();
      body.append("file", file!);
      await api(`/admin/${kind}/import`, { method: "POST", body });
      await refresh();
    },
  });
  return (
    <form
      className={common.form}
      onSubmit={(event) => {
        event.preventDefault();
        upload.mutate();
      }}
    >
      <p>
        {kind === "classifier"
          ? strings.classifierFormat
          : strings.streetsFormat}
      </p>
      <label>
        {strings.importFile}
        <input
          type="file"
          required
          accept={kind === "classifier" ? ".xlsx" : ".csv"}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
      </label>
      <button disabled={!file || upload.isPending}>{strings.importNow}</button>
      {upload.error && <p role="alert">{upload.error.message}</p>}
      {upload.isSuccess && <p role="status">{strings.importDone}</p>}
    </form>
  );
}
function Classifier() {
  const query = useQuery({
    queryKey: ["classifier"],
    queryFn: () =>
      api<{ groups: number; types: number; services: number }>(
        "/admin/classifier/stats",
      ),
  });
  return (
    <>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        empty={query.data?.types === 0 && strings.noGroups}
        retry={() => void query.refetch()}
      >
        <TableScroll>
          <table className={common.table}>
            <tbody>
              {(["groups", "types", "services"] as const).map((key) => (
                <tr key={key}>
                  <th>{strings[key]}</th>
                  <td>{query.data?.[key]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </AsyncView>
      <ImportFile kind="classifier" refresh={() => query.refetch()} />
    </>
  );
}
function Streets() {
  const [cursor, setCursor] = useState("");
  const query = useQuery({
    queryKey: ["admin-streets", cursor],
    queryFn: () =>
      api<{
        items: { id: string; name: string; district: string | null }[];
        next_cursor: string | null;
      }>(`/admin/streets${cursor ? `?cursor=${cursor}` : ""}`),
  });
  return (
    <>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        empty={query.data?.items.length === 0 && strings.emptyStreets}
        retry={() => void query.refetch()}
      >
        <TableScroll>
          <table className={common.table}>
            <thead>
              <tr>
                <th>{strings.streetName}</th>
                <th>{strings.district}</th>
              </tr>
            </thead>
            <tbody>
              {query.data?.items.map((row) => (
                <tr key={row.id}>
                  <td>{row.name}</td>
                  <td>{row.district}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </AsyncView>
      <div className={common.toolbar}>
        <button disabled={!cursor} onClick={() => setCursor("")}>
          {strings.firstPage}
        </button>
        <button
          disabled={!query.data?.next_cursor}
          onClick={() => setCursor(query.data!.next_cursor!)}
        >
          {strings.nextPage}
        </button>
      </div>
      <ImportFile kind="streets" refresh={() => query.refetch()} />
    </>
  );
}
function Audit() {
  const [form, setForm] = useState({ user_id: "", from: "", to: "" });
  const [filter, setFilter] = useState("");
  const [cursor, setCursor] = useState("");
  const users = useQuery({
    queryKey: ["audit-users"],
    queryFn: () => allItems<ManagedUser>("/admin/users"),
  });
  const query = useQuery({
    queryKey: ["admin-audit", filter, cursor],
    queryFn: () =>
      api<{
        items: {
          id: number;
          user_id: string | null;
          action: string;
          entity_type: string | null;
          entity_id: string | null;
          created_at: string;
          payload: unknown;
        }[];
        next_cursor: string | null;
      }>(`/admin/audit?${filter}${cursor ? `&cursor=${cursor}` : ""}`),
  });
  return (
    <>
      <AsyncView
        loading={users.isPending}
        error={users.error}
        retry={() => void users.refetch()}
      >
        <form
          className={common.form}
          onSubmit={(event) => {
            event.preventDefault();
            const params = new URLSearchParams();
            for (const [key, value] of Object.entries(form))
              if (value)
                params.set(
                  key,
                  key === "user_id" ? value : new Date(value).toISOString(),
                );
            setCursor("");
            setFilter(params.toString());
          }}
        >
          <label>
            {strings.auditUser}
            <select
              aria-label={strings.auditUser}
              value={form.user_id}
              onChange={(event) =>
                setForm({ ...form, user_id: event.target.value })
              }
            >
              <option value="">{strings.allUsers}</option>
              {users.data?.items.map((user) => (
                <option value={user.id} key={user.id}>
                  {user.login} · {user.last_name}
                </option>
              ))}
            </select>
          </label>
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
          <button>{strings.applyFilters}</button>
        </form>
      </AsyncView>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        empty={query.data?.items.length === 0 && strings.noAudit}
        retry={() => void query.refetch()}
      >
        <TableScroll>
          <table className={common.table}>
            <thead>
              <tr>
                {[
                  strings.date,
                  strings.auditUser,
                  strings.auditAction,
                  strings.auditObject,
                  strings.details,
                ].map((text) => (
                  <th key={text}>{text}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {query.data?.items.map((row) => (
                <tr key={row.id}>
                  <td>{new Date(row.created_at).toLocaleString("ru-RU")}</td>
                  <td>
                    {users.data?.items.find((user) => user.id === row.user_id)
                      ?.login ??
                      row.user_id ??
                      "—"}
                  </td>
                  <td>{row.action}</td>
                  <td>
                    {row.entity_type} {row.entity_id}
                  </td>
                  <td>
                    {row.payload !== null && (
                      <details>
                        <summary>{strings.details}</summary>
                        <pre>{JSON.stringify(row.payload, null, 2)}</pre>
                      </details>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </AsyncView>
      <div className={common.toolbar}>
        <button disabled={!cursor} onClick={() => setCursor("")}>
          {strings.firstPage}
        </button>
        <button
          disabled={!query.data?.next_cursor}
          onClick={() => setCursor(query.data!.next_cursor!)}
        >
          {strings.nextPage}
        </button>
      </div>
    </>
  );
}
function SystemHealth() {
  type Component = keyof typeof strings.healthComponents;
  type Health = Record<
    Component,
    { ok: boolean; latency_ms?: number; queue_depth?: number }
  > & { last_backup_at: string | null };
  const query = useQuery({
    queryKey: ["admin-health"],
    queryFn: () => api<Health>("/admin/health"),
    refetchInterval: 3000,
    retry: false,
  });
  return (
    <AsyncView
      loading={query.isPending}
      error={query.error}
      retry={() => void query.refetch()}
    >
      <TableScroll>
        <table className={common.table}>
          <thead>
            <tr>
              <th>{strings.component}</th>
              <th>{strings.status}</th>
              <th>{strings.latency}</th>
            </tr>
          </thead>
          <tbody>
            {(Object.keys(strings.healthComponents) as Component[]).map(
              (key) => (
                <tr key={key}>
                  <th>{strings.healthComponents[key]}</th>
                  <td
                    className={query.data?.[key].ok ? styles.ok : styles.alarm}
                  >
                    {query.data?.[key].ok
                      ? `● ${strings.componentOk}`
                      : `▲ ${strings.componentDown}`}
                  </td>
                  <td>{query.data?.[key].latency_ms ?? "—"}</td>
                </tr>
              ),
            )}
          </tbody>
        </table>
      </TableScroll>
      <p>
        {strings.queueDepth}: {query.data?.worker.queue_depth}
      </p>
      <p>
        {strings.lastBackup}:{" "}
        {query.data?.last_backup_at
          ? new Date(query.data.last_backup_at).toLocaleString("ru-RU")
          : strings.noBackup}
      </p>
    </AsyncView>
  );
}
export function AdminHome() {
  const [section, setSection] =
    useState<keyof typeof strings.adminSections>("users");
  return (
    <section className={styles.report}>
      <h1>{strings.adminWorkspace}</h1>
      <nav
        className={`${common.toolbar} ${styles.adminTabs}`}
        aria-label={strings.adminWorkspace}
      >
        {Object.entries(strings.adminSections).map(([key, title]) => (
          <button
            key={key}
            aria-pressed={section === key}
            onClick={() => setSection(key as typeof section)}
          >
            {title}
          </button>
        ))}
      </nav>
      <div className={`${common.form} ${styles.adminSelect}`}>
        <label>
          {strings.adminSectionSelect}
          <select
            aria-label={strings.adminSectionSelect}
            value={section}
            onChange={(event) =>
              setSection(event.target.value as typeof section)
            }
          >
            {Object.entries(strings.adminSections).map(([key, title]) => (
              <option key={key} value={key}>
                {title}
              </option>
            ))}
          </select>
        </label>
      </div>
      <h2>{strings.adminSections[section]}</h2>
      {section === "users" ? (
        <AdminUsers />
      ) : section === "workstations" ? (
        <Workstations />
      ) : section === "classifier" ? (
        <Classifier />
      ) : section === "streets" ? (
        <Streets />
      ) : section === "policies" ? (
        <AdminPolicy />
      ) : section === "configuration" ? (
        <AdminConfiguration />
      ) : section === "operations" ? (
        <TechnicalConsole />
      ) : section === "backups" ? (
        <TechnicalConsole mode="backups" />
      ) : section === "audit" ? (
        <Audit />
      ) : section === "diagnostics" ? (
        <AdminDiagnostics />
      ) : section === "updates" ? (
        <AdminUpdates />
      ) : (
        <SystemHealth />
      )}
    </section>
  );
}
