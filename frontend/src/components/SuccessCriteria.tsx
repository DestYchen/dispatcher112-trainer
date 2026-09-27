import { learningStrings as s } from "../lib/strings";

export const defaultCriteria = {
  min_total: 70,
  max_errors: 5,
  max_spelling_errors: 3,
  min_words: 0,
  sentence_end_required: false,
  required_terms: [] as string[],
};
export type Criteria = typeof defaultCriteria;

export function SuccessCriteria({
  value,
  onChange,
}: {
  value: Criteria;
  onChange: (value: Criteria) => void;
}) {
  return (
    <fieldset>
      <legend>{s.criteria}</legend>
      {(
        [
          ["min_total", s.minTotal, 100],
          ["max_errors", s.maxErrors, 1000],
          ["max_spelling_errors", s.maxSpelling, 1000],
          ["min_words", s.minWords, 100],
        ] as const
      ).map(([key, label, max]) => (
        <label key={key}>
          {label}
          <input
            type="number"
            min={0}
            max={max}
            required
            value={value[key]}
            onChange={(event) =>
              onChange({ ...value, [key]: Number(event.target.value) })
            }
          />
        </label>
      ))}
      <label>
        <input
          type="checkbox"
          checked={value.sentence_end_required}
          onChange={(event) =>
            onChange({ ...value, sentence_end_required: event.target.checked })
          }
        />
        {s.sentenceEnd}
      </label>
      <label>
        {s.requiredTerms}
        <input
          defaultValue={value.required_terms.join(", ")}
          onChange={(event) =>
            onChange({
              ...value,
              required_terms: event.target.value
                .split(",")
                .map((word) => word.trim())
                .filter(Boolean),
            })
          }
        />
      </label>
    </fieldset>
  );
}
