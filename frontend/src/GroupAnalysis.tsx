import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { learningStrings as s, strings } from "./lib/strings";
import styles from "./Learning.module.css";

type Data = {
  cards_count: number;
  students_count: number;
  forecast_students_count: number;
  actual_axes: Record<string, number | null>;
  predicted_axes: Record<string, number> | null;
  synthetic: boolean;
  violations: { code: string; count: number; hint: string }[];
};
export function GroupAnalysis({ groupId }: { groupId: string }) {
  const query = useQuery({
    queryKey: ["learning-group-analysis", groupId],
    queryFn: () => api<Data>(`/teacher/groups/${groupId}/analytics`),
  });
  const data = query.data;
  return (
    <AsyncView
      loading={query.isPending}
      error={query.error}
      retry={() => void query.refetch()}
      empty={
        !!data &&
        !data.cards_count &&
        !data.forecast_students_count &&
        s.groupNoResults
      }
    >
      {data && (
        <div className={styles.notice}>
          <h3>{s.groupActual}</h3>
          <p>
            {s.cardsCount}: {data.cards_count}
          </p>
          <div className={styles.axes}>
            {Object.entries(data.actual_axes).map(([axis, value]) => (
              <label key={axis}>
                {strings.axes[axis as keyof typeof strings.axes]}:{" "}
                {value ?? "—"}
                <progress max={100} value={value ?? 0} />
              </label>
            ))}
          </div>
          {data.predicted_axes && (
            <>
              <h3>{s.groupForecast}</h3>
              <p>
                {s.forecastCount}: {data.forecast_students_count} /{" "}
                {data.students_count}
              </p>
              <div className={styles.axes}>
                {Object.entries(data.predicted_axes).map(([axis, value]) => (
                  <label key={axis}>
                    {strings.axes[axis as keyof typeof strings.axes]}: {value}
                    <progress max={100} value={value} />
                  </label>
                ))}
              </div>
              <p>{data.synthetic ? s.notValidated : s.observedLimits}</p>
            </>
          )}
          <h3>{s.frequentErrors}</h3>
          {data.violations.length ? (
            data.violations.map((item) => (
              <p key={item.code}>
                {item.count} · {item.hint}
              </p>
            ))
          ) : (
            <p>{s.noErrors}</p>
          )}
        </div>
      )}
    </AsyncView>
  );
}
