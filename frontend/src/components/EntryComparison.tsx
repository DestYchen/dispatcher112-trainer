import type { EntryScore } from "../lib/cardTypes";
import { strings } from "../lib/strings";
import styles from "./Workspace.module.css";

const display = (value: unknown): string =>
  Array.isArray(value)
    ? value.join(", ") || strings.notSpecified
    : String(value || strings.notSpecified);
export function EntryComparison({ score }: { score: EntryScore }) {
  return (
    <section className={styles.section}>
      <h2>
        {strings.entryReview} · {score.total} / 100
      </h2>
      {Object.entries(score.axes).map(([axis, data]) => (
        <p key={axis}>
          {strings.axes[axis as keyof typeof strings.axes]}:{" "}
          {data.score ?? strings.skipped}
          {data.reason && ` · ${data.reason}`}
        </p>
      ))}
      {score.entry_fields.map((field) => (
        <div key={field.field} className={styles.entryComparison}>
          <h3 className={field.correct ? styles.ok : styles.alarm}>
            {field.correct ? "✓" : "▲"} {field.label} ·{" "}
            {field.correct ? strings.fieldMatches : strings.fieldDiffers}
          </h3>
          <dl className={styles.fieldList}>
            <div>
              <dt>{strings.actualValue}</dt>
              <dd>{field.actual_display ?? display(field.actual)}</dd>
            </div>
            <div>
              <dt>{strings.expectedValue}</dt>
              <dd>{field.expected_display ?? display(field.expected)}</dd>
            </div>
          </dl>
        </div>
      ))}
      {score.violations
        .filter((row) => row.code !== "CARD_FIELD_MISMATCH")
        .map((row, index) => (
          <p key={index} className={styles.alarm}>
            ▲ {row.message} {row.hint}
          </p>
        ))}
    </section>
  );
}
