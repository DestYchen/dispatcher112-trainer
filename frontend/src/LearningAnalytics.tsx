import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { StudentPicker } from "./LearningHistory";
import { learningStrings as s, strings } from "./lib/strings";
import common from "./components/Common.module.css";
import styles from "./Learning.module.css";

type Model = {
  id: string;
  version: string;
  dataset_kind: string;
  dataset_note: string;
  training_count: number;
  test_count: number;
  mae_overall: number;
  baseline_mae_overall: number;
  beats_baseline: boolean;
  converged: boolean;
  created_at: string;
  comparisons: {
    sample: string;
    actual: Record<string, number>;
    predicted: Record<string, number>;
  }[];
};
type Forecast = {
  id: string;
  created_at: string;
  model_version: string;
  dataset_kind: string;
  predicted_axes: Record<string, number>;
  actual_axes: Record<string, number> | null;
  recommendations: string[];
  beats_baseline: boolean;
  difficulty: number;
  task_mode: keyof typeof strings.trainingModes;
  validation_mae: number;
};

export function LearningAnalytics({ teacher }: { teacher: boolean }) {
  const [student, setStudent] = useState("");
  const models = useQuery({
    queryKey: ["learning-models"],
    enabled: teacher,
    queryFn: () => api<{ items: Model[] }>("/teacher/analytics/models"),
  });
  const forecasts = useQuery({
    queryKey: ["learning-forecasts", student],
    enabled: !teacher || !!student,
    queryFn: () =>
      api<{ items: Forecast[] }>(
        `/learning/forecasts${student ? `?student_id=${student}` : ""}`,
      ),
  });
  const train = useMutation({
    mutationFn: (data: FormData) =>
      api("/teacher/analytics/models", {
        method: "POST",
        signal: AbortSignal.timeout(120000),
        body: JSON.stringify({
          dataset_kind: data.get("dataset_kind"),
          dataset_note: data.get("dataset_note"),
        }),
      }),
    onSuccess: () => models.refetch(),
  });
  const forecast = useMutation({
    mutationFn: (data: FormData) =>
      api("/teacher/analytics/forecast", {
        method: "POST",
        body: JSON.stringify({
          student_id: student,
          difficulty: Number(data.get("difficulty")),
          task_mode: data.get("task_mode"),
        }),
      }),
    onSuccess: () => forecasts.refetch(),
  });
  return (
    <>
      <h2>{s.analytics}</h2>
      <p>{s.regulatory}</p>
      {teacher && (
        <>
          <h3>{s.model}</h3>
          <p>{s.modelHelp}</p>
          <form
            className={common.form}
            onSubmit={(event) => {
              event.preventDefault();
              train.mutate(new FormData(event.currentTarget));
            }}
          >
            <label>
              {s.datasetNote}
              <textarea
                name="dataset_note"
                minLength={15}
                maxLength={2000}
                required
                rows={3}
              />
            </label>
            <label>
              {s.model}
              <select name="dataset_kind">
                <option value="SYNTHETIC">{s.synthetic}</option>
                <option value="OBSERVED">{s.observed}</option>
              </select>
            </label>
            <button disabled={train.isPending}>
              {train.isPending ? s.preparingModel : s.prepareModel}
            </button>
            {train.error && <p role="alert">{train.error.message}</p>}
          </form>
          <AsyncView
            loading={models.isPending}
            error={models.error}
            retry={() => void models.refetch()}
            empty={!models.data?.items.length && s.noModels}
          >
            <div className={styles.list}>
              {models.data?.items.map((model) => (
                <article key={model.id} className={styles.item}>
                  <h3>
                    {s.model} ·{" "}
                    {new Date(model.created_at).toLocaleString("ru-RU")}
                  </h3>
                  <p>{model.dataset_note}</p>
                  <p>
                    {s.training}: {model.training_count} · {s.control}:{" "}
                    {model.test_count}
                  </p>
                  <p>
                    {s.mae}: {model.mae_overall} · {s.baseline}:{" "}
                    {model.baseline_mae_overall}
                  </p>
                  <p>{model.beats_baseline ? s.better : s.worse}</p>
                  <p>
                    {model.converged ? s.modelConverged : s.modelNotConverged}
                  </p>
                  <p className={styles.notice}>
                    {model.dataset_kind === "SYNTHETIC"
                      ? s.notValidated
                      : s.observedLimits}
                  </p>
                  <details>
                    <summary>{s.sourceVersion}</summary>
                    <p>{model.version}</p>
                    {model.comparisons.slice(0, 20).map((row) => (
                      <p key={row.sample}>
                        {Object.entries(row.actual)
                          .map(
                            ([axis, value]) =>
                              `${strings.axes[axis as keyof typeof strings.axes]}: ${s.actual} ${value}, ${s.predicted} ${row.predicted[axis]}`,
                          )
                          .join("; ")}
                      </p>
                    ))}
                  </details>
                </article>
              ))}
            </div>
          </AsyncView>
          <StudentPicker value={student} onChange={setStudent} />
          {student && (
            <form
              className={common.form}
              onSubmit={(event) => {
                event.preventDefault();
                forecast.mutate(new FormData(event.currentTarget));
              }}
            >
              <h3>{s.forecast}</h3>
              <label>
                {s.difficulty}
                <input
                  type="number"
                  name="difficulty"
                  min={1}
                  max={10}
                  defaultValue={3}
                  required
                />
              </label>
              <label>
                {strings.trainingMode}
                <select name="task_mode">
                  <option value="CARD_ACTIONS">
                    {strings.trainingModes.CARD_ACTIONS}
                  </option>
                  <option value="CARD_ENTRY">
                    {strings.trainingModes.CARD_ENTRY}
                  </option>
                </select>
              </label>
              <button disabled={forecast.isPending}>{s.calculate}</button>
              {forecast.error && <p role="alert">{forecast.error.message}</p>}
            </form>
          )}
        </>
      )}
      {(!teacher || student) && (
        <AsyncView
          loading={forecasts.isPending}
          error={forecasts.error}
          retry={() => void forecasts.refetch()}
          empty={!forecasts.data?.items.length && s.noForecast}
        >
          <div className={styles.list}>
            {forecasts.data?.items.map((item) => (
              <article key={item.id} className={styles.item}>
                <h3>
                  {s.forecast} ·{" "}
                  {new Date(item.created_at).toLocaleString("ru-RU")}
                </h3>
                <p>
                  {strings.trainingModes[item.task_mode]} · {s.difficulty}:{" "}
                  {item.difficulty}
                </p>
                <div className={styles.axes}>
                  {Object.entries(item.predicted_axes).map(([axis, value]) => (
                    <label key={axis}>
                      {strings.axes[axis as keyof typeof strings.axes]}: {value}
                      <progress max={100} value={value} />
                      {item.actual_axes && (
                        <span>
                          {s.actual}: {item.actual_axes[axis]}
                        </span>
                      )}
                    </label>
                  ))}
                </div>
                {!item.actual_axes && <p>{s.waitingActual}</p>}
                <p>
                  {s.mae}: {item.validation_mae} ·{" "}
                  {item.beats_baseline ? s.better : s.worse}
                </p>
                <p className={styles.notice}>
                  {item.dataset_kind === "SYNTHETIC"
                    ? s.notValidated
                    : s.observedLimits}
                </p>
                {item.recommendations.map((text) => (
                  <p key={text}>{text}</p>
                ))}
                <details>
                  <summary>{s.sourceVersion}</summary>
                  <p>{item.model_version}</p>
                </details>
              </article>
            ))}
          </div>
        </AsyncView>
      )}
    </>
  );
}
