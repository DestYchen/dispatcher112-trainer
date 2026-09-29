import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import common from "./components/Common.module.css";
import styles from "./TeacherInsights.module.css";

interface Elo {
  students: { id: string; name: string; rating: number; level: number; attempts: number }[];
  scenarios: { id: string; title: string; difficulty_prior: number; rating: number; level: number; played: boolean }[];
  forecast: {
    attempts: number;
    mae: number | null;
    baseline_mae: number | null;
    bins: { from: number; to: number; count: number; predicted: number; actual: number }[];
  };
}
interface Recommend {
  student_rating: number;
  items: { scenario_id: string; title: string; rating: number; predicted_success: number }[];
}
interface Proposal {
  id: string;
  status: "PENDING" | "APPROVED" | "REJECTED";
  utterance: string;
  asked: string;
  count: number;
  slot: string;
  reply: string;
}
const SLOT_LABELS: Record<string, string> = {
  address: "Адрес",
  incident_type: "Что произошло",
  victims: "Пострадавшие",
  measures: "Принятые меры",
  none: "Не доклад (ответить репликой)",
};
const pct = (value: number) => `${Math.round(value * 100)}%`;

/** Reliability chart: predicted vs actual success per probability bin. The diagonal is a perfect forecast. */
function ForecastChart({ bins }: { bins: Elo["forecast"]["bins"] }) {
  const size = 220;
  const pad = 28;
  const scale = (value: number) => pad + value * (size - 2 * pad);
  return (
    <svg className={styles.chart} viewBox={`0 0 ${size} ${size}`} role="img"
      aria-label="Прогноз и фактический результат по группам заданий">
      <rect x={pad} y={pad} width={size - 2 * pad} height={size - 2 * pad} className={styles.frame} />
      <line x1={scale(0)} y1={size - scale(0)} x2={scale(1)} y2={size - scale(1)} className={styles.diagonal} />
      {bins.map((bin) => (
        <circle key={bin.from} cx={scale(bin.predicted)} cy={size - scale(bin.actual)}
          r={Math.max(3, Math.min(9, Math.sqrt(bin.count) * 1.2))} className={styles.point}>
          <title>{`Прогноз ${pct(bin.predicted)}, факт ${pct(bin.actual)}, заданий: ${bin.count}`}</title>
        </circle>
      ))}
      <text x={size / 2} y={size - 6} textAnchor="middle" className={styles.axis}>прогноз успеха</text>
      <text x={10} y={size / 2} textAnchor="middle" transform={`rotate(-90 10 ${size / 2})`} className={styles.axis}>факт</text>
    </svg>
  );
}

export function TeacherInsights() {
  const queryClient = useQueryClient();
  const [student, setStudent] = useState("");
  const elo = useQuery({ queryKey: ["elo"], queryFn: () => api<Elo>("/teacher/elo") });
  const recommend = useQuery({
    queryKey: ["elo-recommend", student],
    enabled: !!student,
    queryFn: () => api<Recommend>(`/teacher/elo/recommend?student_id=${student}`),
  });
  const proposals = useQuery({
    queryKey: ["dialogue-proposals"],
    queryFn: () => api<{ graph_version: number; items: Proposal[] }>("/teacher/dialogue/proposals"),
  });
  const decide = useMutation({
    mutationFn: (body: { id: string; approve: boolean; slot?: string }) =>
      api(`/teacher/dialogue/proposals/${body.id}`, {
        method: "POST",
        body: JSON.stringify({ approve: body.approve, slot: body.slot }),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["dialogue-proposals"] }),
  });
  const [slots, setSlots] = useState<Record<string, string>>({});
  const pending = proposals.data?.items.filter((p) => p.status === "PENDING") ?? [];

  return (
    <div className={styles.page}>
      <section>
        <h1>Уровень обучающихся</h1>
        <p className={styles.lead}>
          Рейтинг Эло, как в шахматах: сильный результат на сложной карточке поднимает рейтинг сильнее.
          Карточки получают рейтинг по результатам всех обучающихся, поэтому сложность уточняется сама.
        </p>
        <AsyncView loading={elo.isPending} error={elo.error} retry={() => void elo.refetch()}>
          <div className={styles.columns}>
            <table className={styles.table}>
              <thead>
                <tr><th>Обучающийся</th><th>Рейтинг</th><th>Уровень 1–10</th><th>Карточек</th><th /></tr>
              </thead>
              <tbody>
                {elo.data?.students.map((row) => (
                  <tr key={row.id} className={row.id === student ? styles.selected : undefined}>
                    <td>{row.name}</td>
                    <td>{row.rating}</td>
                    <td>{row.level}</td>
                    <td>{row.attempts}</td>
                    <td>
                      <button className={common.secondary} onClick={() => setStudent(row.id)}>
                        Что дать дальше
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className={styles.forecast}>
              <h2>Проверка прогноза</h2>
              {elo.data && elo.data.forecast.attempts > 0 ? (
                <>
                  <ForecastChart bins={elo.data.forecast.bins} />
                  <p>
                    Прогноз сделан до каждой попытки только по более ранним. Средняя ошибка{" "}
                    <strong>{pct(elo.data.forecast.mae ?? 0)}</strong> против{" "}
                    {pct(elo.data.forecast.baseline_mae ?? 0)} у прогноза «все одинаковые»
                    ({elo.data.forecast.attempts} попыток).
                  </p>
                </>
              ) : (
                <p>Появится после первых оценённых карточек.</p>
              )}
            </div>
          </div>
        </AsyncView>
        {student && (
          <AsyncView loading={recommend.isPending} error={recommend.error} retry={() => void recommend.refetch()}>
            <h2>Следующие карточки: шанс успеха около 70%</h2>
            <ol className={styles.list}>
              {recommend.data?.items.map((item) => (
                <li key={item.scenario_id}>
                  {item.title} <span className={styles.muted}>— прогноз {pct(item.predicted_success)}</span>
                </li>
              ))}
            </ol>
          </AsyncView>
        )}
      </section>

      <section>
        <h1>Разговор с руководителем: чему научилась система</h1>
        <p className={styles.lead}>
          Фразы, которые тренажёр не понял на занятиях. Ночью локальная модель предлагает, к чему они
          относятся. В разговор попадает только то, что вы подтвердили.
        </p>
        <AsyncView loading={proposals.isPending} error={proposals.error} retry={() => void proposals.refetch()}
          empty={pending.length === 0 && "Новых предложений нет."}>
          <table className={styles.table}>
            <thead>
              <tr><th>Что сказал обучающийся</th><th>Раз</th><th>Предложение модели</th><th /></tr>
            </thead>
            <tbody>
              {pending.map((p) => (
                <tr key={p.id}>
                  <td>«{p.utterance}»</td>
                  <td>{p.count}</td>
                  <td>
                    <select value={slots[p.id] ?? p.slot}
                      onChange={(event) => setSlots({ ...slots, [p.id]: event.target.value })}>
                      {Object.entries(SLOT_LABELS).map(([code, label]) => (
                        <option key={code} value={code}>{label}</option>
                      ))}
                    </select>
                    {(slots[p.id] ?? p.slot) === "none" && p.reply && (
                      <div className={styles.muted}>Ответ руководителя: «{p.reply}»</div>
                    )}
                  </td>
                  <td className={styles.actions}>
                    <button disabled={decide.isPending}
                      onClick={() => decide.mutate({ id: p.id, approve: true, slot: slots[p.id] ?? p.slot })}>
                      Подтвердить
                    </button>
                    <button className={common.secondary} disabled={decide.isPending}
                      onClick={() => decide.mutate({ id: p.id, approve: false })}>
                      Отклонить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </AsyncView>
      </section>
    </div>
  );
}
