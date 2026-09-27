import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import common from "./components/Common.module.css";
import { technicalStrings as t } from "./lib/strings";

interface Policy {
  session_minutes: number;
  login_attempts: number;
  lockout_minutes: number;
  min_password_length: number;
  require_admin_totp: boolean;
}

function PolicyForm({
  initial,
  refresh,
}: {
  initial: Policy;
  refresh: () => Promise<unknown>;
}) {
  const [value, setValue] = useState(initial);
  const save = useMutation({
    mutationFn: async () => {
      await api("/admin/policies", {
        method: "PATCH",
        body: JSON.stringify(value),
      });
      await refresh();
    },
  });
  const fields = [
    ["session_minutes", 15, 480],
    ["login_attempts", 3, 5],
    ["lockout_minutes", 15, 60],
    ["min_password_length", 8, 128],
  ] as const;
  return (
    <form
      className={common.form}
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      {fields.map(([key, min, max]) => (
        <label key={key}>
          {t[key]}
          <input
            aria-label={t[key]}
            type="number"
            min={min}
            max={max}
            required
            value={value[key]}
            onChange={(event) =>
              setValue({ ...value, [key]: Number(event.target.value) })
            }
          />
        </label>
      ))}
      <label>
        <input
          type="checkbox"
          checked={value.require_admin_totp}
          onChange={(event) =>
            setValue({ ...value, require_admin_totp: event.target.checked })
          }
        />
        {t.require_admin_totp}
      </label>
      <p>{t.policyScope}</p>
      <p>{t.auditPolicy}</p>
      <button disabled={save.isPending}>{t.save}</button>
      {save.error && <p role="alert">{save.error.message}</p>}
      {save.isSuccess && <p role="status">{t.saved}</p>}
    </form>
  );
}

export function AdminPolicy() {
  const query = useQuery({
    queryKey: ["admin-policies"],
    queryFn: () => api<{ access: Policy }>("/admin/policies"),
  });
  return (
    <AsyncView
      loading={query.isPending}
      error={query.error}
      retry={() => void query.refetch()}
      empty={!query.isPending && !query.error && !query.data && t.noPolicy}
    >
      {query.data && (
        <PolicyForm
          initial={query.data.access}
          refresh={() => query.refetch()}
        />
      )}
    </AsyncView>
  );
}
