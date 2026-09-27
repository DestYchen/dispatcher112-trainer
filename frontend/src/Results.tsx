import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { learningStrings as learning, strings } from "./lib/strings";
import { EntryComparison } from "./components/EntryComparison";
import type { EntryScore } from "./lib/cardTypes";
import styles from "./components/Workspace.module.css";

interface ResultsData {
  lesson: { id: string; title: string; finished_at: string } | null;
  summary: {
    total: number | null;
    cards_total: number;
    cards_closed: number;
    cards_expired: number;
    axes: Record<string, number | null>;
  } | null;
  cards: {
    assignment_id: string;
    card_number: string;
    incident_type_name: string;
    total: number;
    violations: {
      code: string;
      severity: string;
      message: string;
      hint: string;
    }[];
    score: {
      criteria?: {
        passed: boolean | null;
        errors: string[];
        unavailable?: string[];
      };
      entry_fields?: EntryScore["entry_fields"];
      axes: Record<
        string,
        { score: number | null; skipped?: boolean; reason?: string }
      >;
    };
    teacher_override: { comment: string } | null;
  }[];
}
export function Results({ lessonId }: { lessonId?: string }) {
  const query = useQuery({
    queryKey: ["results", lessonId],
    queryFn: () =>
      api<ResultsData>(
        `/student/results${lessonId ? `?lesson_id=${lessonId}` : ""}`,
      ),
  });
  const data = query.data;
  return (
    <section className={styles.results}>
      <h1>{strings.results}</h1>
      <AsyncView
        loading={query.isPending}
        error={query.error}
        empty={!data?.lesson && strings.noResults}
        retry={() => void query.refetch()}
      >
        {data?.lesson && data.summary && (
          <>
            <h2>
              {data.lesson.title} ·{" "}
              {new Date(data.lesson.finished_at).toLocaleDateString("ru-RU")}
            </h2>
            <div className={styles.resultSummary}>
              <div>
                <strong className={styles.timer}>
                  {data.summary.total ?? "—"}
                </strong>
                <p>{strings.outOfHundred}</p>
                <p>
                  {strings.cardsTotal}: {data.summary.cards_total}
                </p>
                <p>
                  {strings.closedCards}: {data.summary.cards_closed}
                </p>
                <p>
                  {strings.expiredCards}: {data.summary.cards_expired}
                </p>
              </div>
              <dl className={styles.axes}>
                {Object.entries(data.summary.axes).map(([axis, value]) => (
                  <div key={axis}>
                    <dt>{strings.axes[axis as keyof typeof strings.axes]}</dt>
                    <dd>
                      <progress
                        className={styles.progress}
                        max={100}
                        value={value ?? 0}
                        aria-label={
                          strings.axes[axis as keyof typeof strings.axes]
                        }
                      />{" "}
                      {value ?? strings.skipped}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
            <AsyncView empty={data.cards.length === 0 && strings.noResultCards}>
              {data.cards.map((card) => (
                <article className={styles.section} key={card.assignment_id}>
                  <h2>
                    {card.card_number} · {card.incident_type_name} —{" "}
                    {card.total}
                  </h2>
                  {card.teacher_override && (
                    <p className={styles.hint}>
                      {strings.overridden}: {card.teacher_override.comment}
                    </p>
                  )}
                  {card.score.criteria && (
                    <div className={styles.hint}>
                      <strong>
                        {card.score.criteria.passed === null
                          ? learning.incompleteCriteria
                          : card.score.criteria.passed
                            ? learning.passed
                            : learning.failed}
                      </strong>
                      {card.score.criteria.errors.map((error) => (
                        <p key={error}>{error}</p>
                      ))}
                      {card.score.criteria.unavailable?.map((message) => (
                        <p key={message}>{message}</p>
                      ))}
                    </div>
                  )}
                  {card.score.entry_fields && (
                    <EntryComparison
                      score={{
                        total: card.total,
                        axes: card.score.axes,
                        entry_fields: card.score.entry_fields,
                        violations: card.violations,
                      }}
                    />
                  )}
                  {card.score.axes.literacy?.skipped && (
                    <p className={styles.hint}>
                      {card.score.axes.literacy.reason}
                    </p>
                  )}
                  {card.violations.length === 0 ? (
                    <p className={styles.ok}>✓ {strings.noViolations}</p>
                  ) : (
                    card.violations.map((violation, index) => (
                      <div key={index}>
                        <p className={styles.alarm}>▲ {violation.message}</p>
                        <p>{violation.hint}</p>
                      </div>
                    ))
                  )}
                </article>
              ))}
            </AsyncView>
          </>
        )}
      </AsyncView>
    </section>
  );
}
