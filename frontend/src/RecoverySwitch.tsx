import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { recoveryStrings as t } from "./lib/strings";

export function RecoverySwitch() {
  const query = useQuery({
    queryKey: ["recovery-switch"],
    queryFn: () =>
      api<{
        actor_id: string;
        switch: {
          id: string;
          phase: string;
          reason: string;
          at: string;
          imported?: number;
        } | null;
        recovery: { instance: string; snapshot: string } | null;
      }>("/admin/operations/recovery"),
    retry: false,
    refetchInterval: 10000,
  });
  return (
    <section aria-label={t.label}>
      <h3>{t.title}</h3>
      <p>{t.explanation}</p>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        retry={() => void query.refetch()}
      >
        {query.data && (
          <>
            {query.data.recovery && (
              <p>
                {t.instance}: {query.data.recovery.instance}. {t.snapshot}:{" "}
                {query.data.recovery.snapshot}.
              </p>
            )}
            {query.data.switch ? (
              <>
                <p role="status">
                  {t.phases[query.data.switch.phase as keyof typeof t.phases] ??
                    query.data.switch.phase}
                </p>
                <p>
                  {query.data.switch.reason} ·{" "}
                  {new Date(query.data.switch.at).toLocaleString("ru-RU")}
                </p>
                <p>
                  {t.imported}: {query.data.switch.imported ?? 0}.
                </p>
              </>
            ) : (
              <p>{t.empty}</p>
            )}
            <details>
              <summary>{t.commands}</summary>
              <p>{t.commandHelp}</p>
              <p>
                <code>
                  {`python scripts/switch_recovery.py promote --target .recovery/restored --actor ${query.data.actor_id} --reason "${t.reason}"`}
                </code>
              </p>
              <p>
                <code>
                  python scripts/switch_recovery.py rollback --target
                  .recovery/restored
                </code>
              </p>
              <p>{t.preserved}</p>
            </details>
          </>
        )}
      </AsyncView>
    </section>
  );
}
