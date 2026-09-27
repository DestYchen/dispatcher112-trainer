import { TableScroll } from "./components/TableScroll";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { strings } from "./lib/strings";
import common from "./components/Common.module.css";

export interface ManagedUser {
  id: string;
  login: string;
  last_name: string;
  first_name: string;
  middle_name: string | null;
  role: "ADMIN" | "TEACHER" | "STUDENT";
  service_id: string | null;
  is_active: boolean;
  totp_enabled: boolean;
}
const blank = {
  login: "",
  last_name: "",
  first_name: "",
  middle_name: "",
  role: "STUDENT" as ManagedUser["role"],
  service_id: "",
  password: "",
  totp_secret: "",
};

export function AdminUsers() {
  const [editing, setEditing] = useState<ManagedUser | null>(null);
  const [form, setForm] = useState(blank);
  const [password, setPassword] = useState("");
  const [disableTotp, setDisableTotp] = useState(false);
  const [cursor, setCursor] = useState("");
  const users = useQuery({
    queryKey: ["admin-users", cursor],
    queryFn: () =>
      api<{ items: ManagedUser[]; next_cursor: string | null }>(
        `/admin/users?cursor=${cursor}`,
      ),
  });
  const services = useQuery({
    queryKey: ["admin-services"],
    queryFn: () =>
      api<{ items: { id: string; name: string }[] }>("/admin/services"),
  });
  const save = useMutation({
    mutationFn: async () => {
      const fields = {
        login: form.login,
        last_name: form.last_name,
        first_name: form.first_name,
        middle_name: form.middle_name || null,
        role: form.role,
        service_id: form.service_id || null,
        ...(disableTotp
          ? { totp_secret: null }
          : form.totp_secret
            ? { totp_secret: form.totp_secret }
            : {}),
      };
      await api("/admin/users", {
        method: editing ? "PATCH" : "POST",
        body: JSON.stringify(
          editing
            ? { ...fields, id: editing.id }
            : { ...fields, password: form.password },
        ),
      });
      setEditing(null);
      setDisableTotp(false);
      setForm(blank);
      await users.refetch();
    },
  });
  const block = useMutation({
    mutationFn: async (user: ManagedUser) => {
      await api(
        user.is_active ? `/admin/users/${user.id}/block` : "/admin/users",
        {
          method: user.is_active ? "POST" : "PATCH",
          ...(user.is_active
            ? {}
            : { body: JSON.stringify({ id: user.id, is_active: true }) }),
        },
      );
      await users.refetch();
    },
  });
  const reset = useMutation({
    mutationFn: async () => {
      await api(`/admin/users/${editing!.id}/reset-password`, {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setPassword("");
    },
  });
  return (
    <>
      <AsyncView
        loading={users.isPending}
        error={users.error}
        empty={users.data?.items.length === 0 && strings.noUsers}
        retry={() => void users.refetch()}
      >
        <TableScroll>
          <table className={common.table}>
            <thead>
              <tr>
                {[
                  strings.login,
                  strings.lastName,
                  strings.role,
                  strings.status,
                  strings.actions,
                ].map((text) => (
                  <th key={text}>{text}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {users.data?.items.map((user) => (
                <tr key={user.id}>
                  <td>{user.login}</td>
                  <td>
                    {user.last_name} {user.first_name}
                  </td>
                  <td>{strings.roleLabels[user.role]}</td>
                  <td>
                    {user.is_active ? strings.active : strings.blocked}
                    {user.totp_enabled && ` · ${strings.totpEnabled}`}
                  </td>
                  <td>
                    <button
                      onClick={() => {
                        setEditing(user);
                        setDisableTotp(false);
                        setForm({
                          ...blank,
                          ...user,
                          middle_name: user.middle_name ?? "",
                          service_id: user.service_id ?? "",
                        });
                        reset.reset();
                      }}
                    >
                      {strings.editUser}
                    </button>
                    <button
                      disabled={block.isPending}
                      onClick={() => block.mutate(user)}
                    >
                      {user.is_active ? strings.blockUser : strings.unblockUser}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </AsyncView>
      {block.error && <p role="alert">{block.error.message}</p>}
      <div className={common.toolbar}>
        <button disabled={!cursor} onClick={() => setCursor("")}>
          {strings.firstPage}
        </button>
        <button
          disabled={!users.data?.next_cursor}
          onClick={() => setCursor(users.data!.next_cursor!)}
        >
          {strings.nextPage}
        </button>
      </div>
      <AsyncView
        loading={services.isPending}
        error={services.error}
        empty={services.data?.items.length === 0 && strings.noGroups}
        retry={() => void services.refetch()}
      >
        <form
          className={common.form}
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <h2>{editing ? strings.editUser : strings.createUser}</h2>
          {(["login", "last_name", "first_name", "middle_name"] as const).map(
            (key) => (
              <label key={key}>
                {
                  {
                    login: strings.login,
                    last_name: strings.lastName,
                    first_name: strings.firstName,
                    middle_name: strings.middleName,
                  }[key]
                }
                <input
                  required={key !== "middle_name"}
                  maxLength={key === "login" ? 64 : 100}
                  value={form[key]}
                  onChange={(event) =>
                    setForm({ ...form, [key]: event.target.value })
                  }
                />
              </label>
            ),
          )}
          <label>
            {strings.role}
            <select
              aria-label={strings.role}
              value={form.role}
              onChange={(event) =>
                setForm({
                  ...form,
                  role: event.target.value as ManagedUser["role"],
                })
              }
            >
              {Object.entries(strings.roleLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            {strings.service}
            <select
              aria-label={strings.service}
              required={form.role === "STUDENT"}
              value={form.service_id}
              onChange={(event) =>
                setForm({ ...form, service_id: event.target.value })
              }
            >
              <option value="">{strings.noService}</option>
              {services.data?.items.map((service) => (
                <option key={service.id} value={service.id}>
                  {service.name}
                </option>
              ))}
            </select>
          </label>
          {!editing && (
            <label>
              {strings.newPassword}
              <input
                type="password"
                required
                minLength={8}
                maxLength={256}
                value={form.password}
                onChange={(event) =>
                  setForm({ ...form, password: event.target.value })
                }
              />
            </label>
          )}
          <label>
            {strings.totpSecret}
            <input
              autoComplete="off"
              value={form.totp_secret}
              pattern="[A-Z2-7]{16,64}"
              onChange={(event) =>
                setForm({
                  ...form,
                  totp_secret: event.target.value.toUpperCase(),
                })
              }
            />
          </label>
          {editing?.totp_enabled && (
            <label>
              <input
                type="checkbox"
                checked={disableTotp}
                onChange={(event) => setDisableTotp(event.target.checked)}
              />
              {strings.disableTotp}
            </label>
          )}
          <button disabled={save.isPending}>{strings.saveUser}</button>
          {editing && (
            <button
              type="button"
              onClick={() => {
                setEditing(null);
                setForm(blank);
              }}
            >
              {strings.cancelEdit}
            </button>
          )}
          {save.error && <p role="alert">{save.error.message}</p>}
        </form>
      </AsyncView>
      {editing && (
        <form
          className={common.form}
          onSubmit={(event) => {
            event.preventDefault();
            reset.mutate();
          }}
        >
          <label>
            {strings.newPassword}
            <input
              type="password"
              required
              minLength={8}
              maxLength={256}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button disabled={reset.isPending}>{strings.resetPassword}</button>
          {reset.error && <p role="alert">{reset.error.message}</p>}
          {reset.isSuccess && <p role="status">{strings.passwordReset}</p>}
        </form>
      )}
    </>
  );
}
