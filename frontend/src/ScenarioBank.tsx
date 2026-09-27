import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { allItems, api } from "./api/client";
import { Review, type Scenario } from "./Preparation";
import { AsyncView } from "./components/AsyncView";
import { learningStrings as s, strings } from "./lib/strings";
import common from "./components/Common.module.css";
import styles from "./Learning.module.css";

type BankScenario = Scenario & {
  status: keyof typeof s.scenarioStates;
  editable: boolean;
  archived: boolean;
};
export function ScenarioBank() {
  const [status, setStatus] =
    useState<keyof typeof s.scenarioStates>("APPROVED");
  const [archived, setArchived] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["scenario-bank", status, archived],
    queryFn: () =>
      allItems<BankScenario>(
        `/teacher/scenarios?status=${status}&include_archived=${archived}`,
      ),
  });
  const job = useQuery({
    queryKey: ["scenario-bank-job", jobId],
    enabled: !!jobId,
    queryFn: () =>
      api<{ status: string; error: string | null }>(`/teacher/jobs/${jobId}`),
    refetchInterval: (query) =>
      ["DONE", "FAILED"].includes(query.state.data?.status ?? "")
        ? false
        : 1000,
  });
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["scenario-bank"] }),
      client.invalidateQueries({ queryKey: ["approved"] }),
      client.invalidateQueries({ queryKey: ["reviews"] }),
    ]);
  };
  const mutation = useMutation({
    mutationFn: async ({
      item,
      action,
    }: {
      item: BankScenario;
      action: "copy" | "archive" | "delete";
    }) => {
      const result = await api<{ id: string } | undefined>(
        `/teacher/scenarios/${item.id}${action === "delete" ? "" : `/${action}`}`,
        {
          method:
            action === "delete"
              ? "DELETE"
              : action === "archive"
                ? "PATCH"
                : "POST",
          ...(action === "archive"
            ? { body: JSON.stringify({ archived: !item.archived }) }
            : {}),
        },
      );
      if (action === "copy" && result) {
        setStatus("PENDING_REVIEW");
        setSelected(result.id);
      }
      setDeleting(null);
      await refresh();
    },
  });
  const current = query.data?.items.find((item) => item.id === selected);
  return (
    <section>
      <h2>{s.scenarioBank}</h2>
      <div className={common.form}>
        <label>
          {s.scenarioBank}
          <select
            aria-label={s.scenarioBank}
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as typeof status);
              setSelected(null);
            }}
          >
            {Object.entries(s.scenarioStates).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={archived}
            onChange={(event) => setArchived(event.target.checked)}
          />
          {s.includeArchived}
        </label>
      </div>
      {mutation.error && (
        <p role="alert" className={common.error}>
          {mutation.error.message}
        </p>
      )}
      {jobId && (
        <AsyncView
          loading={job.isPending}
          error={job.error}
          retry={() => void job.refetch()}
          empty={!job.data && strings.noScenarios}
        >
          <p role="status">{job.data?.error ?? job.data?.status}</p>
        </AsyncView>
      )}
      <AsyncView
        loading={query.isPending}
        error={query.error}
        retry={() => void query.refetch()}
        empty={!query.data?.items.length && strings.noScenarios}
      >
        {current && (
          <Review
            key={current.id}
            scenario={current}
            refresh={async () => {
              setSelected(null);
              await refresh();
            }}
            onJob={setJobId}
          />
        )}
        <div className={styles.list}>
          {query.data?.items.map((item) => (
            <article
              key={item.id}
              data-scenario-id={item.id}
              className={styles.item}
            >
              <h3>{item.title}</h3>
              <p>
                {s.difficulty}: {item.difficulty} ·{" "}
                {s.scenarioStates[item.status]}
              </p>
              <div className={common.toolbar}>
                <button
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate({ item, action: "copy" })}
                >
                  {s.revisedCopy}
                </button>
                {item.editable && item.status === "PENDING_REVIEW" && (
                  <button onClick={() => setSelected(item.id)}>
                    {s.reviewScenario}
                  </button>
                )}
                {item.editable && (
                  <>
                    <button
                      disabled={mutation.isPending}
                      onClick={() =>
                        mutation.mutate({ item, action: "archive" })
                      }
                    >
                      {item.archived ? s.restoreScenario : s.archiveScenario}
                    </button>
                    <button onClick={() => setDeleting(item.id)}>
                      {s.deleteScenario}
                    </button>
                  </>
                )}
              </div>
              {deleting === item.id && (
                <div className={styles.notice}>
                  <p>{s.deletionHelp}</p>
                  <button
                    disabled={mutation.isPending}
                    onClick={() => mutation.mutate({ item, action: "delete" })}
                  >
                    {s.confirmDelete}
                  </button>
                  <button onClick={() => setDeleting(null)}>{s.cancel}</button>
                </div>
              )}
            </article>
          ))}
        </div>
      </AsyncView>
    </section>
  );
}
