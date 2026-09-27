import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { allItems, api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { learningStrings as learning, strings } from "./lib/strings";
import { EntryEditor } from "./components/EntryEditor";
import type { EntryCard } from "./lib/cardTypes";
import common from "./components/Common.module.css";
import styles from "./components/Workspace.module.css";

interface Reference {
  expected_status: string;
  expected_status_chain: string[];
  comment_required: boolean;
  comment_must_contain: string[];
  report_required: boolean;
  report_callee_code: string | null;
  report_must_mention: string[];
  rationale: string;
  entry_description_keywords?: string[];
  trap: string | null;
}
export interface Scenario {
  id: string;
  title: string;
  difficulty: number;
  incident_type_id: string;
  reference: Reference;
  card_payload: {
    description: string;
    address: { raw: string; clarification?: string };
    attributes: string[];
    modifiers: string[];
    lesson_id?: string;
    incoming_message?: string;
    source_assignment_id?: string;
    applicant: { name: string; phone: string };
    notified_services: string[];
  };
  validation: { code: string; ok: boolean; message: string }[];
  review_comment: string | null;
}
const statuses = [
  "ACCEPTED",
  "NOT_ACCEPTED",
  "RESPONSE_STARTED",
  "ARRIVED",
  "WORK_IN_PROGRESS",
  "WORK_COMPLETED",
  "WORK_REFUSED",
];
export function Review({
  scenario,
  refresh,
  onJob,
}: {
  scenario: Scenario;
  refresh: () => Promise<unknown>;
  onJob: (id: string) => void;
}) {
  const [reference, setReference] = useState(scenario.reference);
  const [title, setTitle] = useState(scenario.title);
  const [difficulty, setDifficulty] = useState(scenario.difficulty);
  const [comment, setComment] = useState(scenario.review_comment ?? "");
  const [incoming, setIncoming] = useState(
    scenario.card_payload.incoming_message ?? "",
  );
  const [keywords, setKeywords] = useState(
    (scenario.reference.entry_description_keywords ?? []).join(", "),
  );
  const [entryCard, setEntryCard] = useState<EntryCard>({
    applicant: scenario.card_payload.applicant,
    address: {
      raw: scenario.card_payload.address.raw,
      clarification: scenario.card_payload.address.clarification ?? "",
    },
    incident_type_id: scenario.incident_type_id,
    description: scenario.card_payload.description,
    modifiers: scenario.card_payload.modifiers,
    notified_services: scenario.card_payload.notified_services,
  });
  const mutation = useMutation({
    mutationFn: async (command: "approve" | "reject") => {
      const result = await api<{ job_id?: string }>(
        `/teacher/scenarios/${scenario.id}/${command}`,
        {
          method: "POST",
          body: JSON.stringify(
            command === "approve"
              ? {
                  title,
                  difficulty,
                  reference: {
                    ...reference,
                    entry_description_keywords: keywords
                      .split(",")
                      .map((word) => word.trim())
                      .filter(Boolean),
                  },
                  description: entryCard.description,
                  review_comment: comment,
                  ...(incoming.trim() ? { incoming_message: incoming } : {}),
                  card: entryCard,
                }
              : { comment, regenerate: !!scenario.card_payload.lesson_id },
          ),
        },
      );
      if (result.job_id) onJob(result.job_id);
      await refresh();
    },
  });
  return (
    <div className={common.form} role="region" aria-label={strings.reference}>
      <h2>{scenario.title}</h2>
      <label>
        {learning.title}
        <input
          value={title}
          maxLength={255}
          required
          onChange={(event) => setTitle(event.target.value)}
        />
      </label>
      <p>{scenario.card_payload.address.raw}</p>
      <EntryEditor
        card={entryCard}
        onChange={setEntryCard}
        descriptionLimit={400}
        disabled={mutation.isPending}
      />
      <label>
        {strings.difficulty}
        <input
          type="number"
          min={1}
          max={10}
          value={difficulty}
          onChange={(event) => setDifficulty(Number(event.target.value))}
        />
      </label>
      <h2 className={styles.ok}>{strings.reference}</h2>
      <label>
        {strings.incomingMessage}
        <textarea
          maxLength={10000}
          value={incoming}
          onChange={(event) => setIncoming(event.target.value)}
        />
      </label>
      <label>
        {strings.entryKeywords}
        <input
          value={keywords}
          onChange={(event) => setKeywords(event.target.value)}
        />
      </label>
      <p>{strings.entryKeywordsHelp}</p>
      <label>
        {strings.expectedStatus}
        <select
          aria-label={strings.expectedStatus}
          value={reference.expected_status}
          onChange={(event) =>
            setReference({
              ...reference,
              expected_status: event.target.value,
              expected_status_chain: [event.target.value],
            })
          }
        >
          {statuses.slice(0, 2).map((status) => (
            <option value={status} key={status}>
              {
                strings.statusLabels[
                  status as keyof typeof strings.statusLabels
                ]
              }
            </option>
          ))}
        </select>
      </label>
      <label>
        {strings.expectedChain}
        <select
          multiple
          aria-label={strings.expectedChain}
          value={reference.expected_status_chain}
          onChange={(event) =>
            setReference({
              ...reference,
              expected_status_chain: Array.from(
                event.target.selectedOptions,
                (option) => option.value,
              ),
            })
          }
        >
          {[
            reference.expected_status,
            ...statuses.filter(
              (status) => status !== reference.expected_status,
            ),
          ].map((status) => (
            <option key={status} value={status}>
              {
                strings.statusLabels[
                  status as keyof typeof strings.statusLabels
                ]
              }
            </option>
          ))}
        </select>
      </label>
      <label>
        <input
          type="checkbox"
          checked={reference.comment_required}
          onChange={(event) =>
            setReference({
              ...reference,
              comment_required: event.target.checked,
            })
          }
        />
        {strings.commentRequired}
      </label>
      <fieldset>
        <legend>{strings.commentEntities}</legend>
        {Object.entries(strings.commentEntitiesLabels).map(([key, label]) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={reference.comment_must_contain.includes(key)}
              onChange={(event) =>
                setReference({
                  ...reference,
                  comment_must_contain: event.target.checked
                    ? [...reference.comment_must_contain, key]
                    : reference.comment_must_contain.filter(
                        (item) => item !== key,
                      ),
                })
              }
            />
            {label}
          </label>
        ))}
      </fieldset>
      <label>
        <input
          type="checkbox"
          checked={reference.report_required}
          onChange={(event) =>
            setReference({
              ...reference,
              report_required: event.target.checked,
            })
          }
        />
        {strings.reportRequired}
      </label>
      <label>
        {strings.reportRecipient}
        <select
          aria-label={strings.reportRecipient}
          value={reference.report_callee_code ?? ""}
          onChange={(event) =>
            setReference({
              ...reference,
              report_callee_code: event.target.value || null,
            })
          }
        >
          <option value="">{strings.notRequired}</option>
          {Object.entries(strings.recipients).map(([code, label]) => (
            <option value={code} key={code}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend>{strings.reportEntities}</legend>
        {Object.entries(strings.reportEntitiesLabels).map(([key, label]) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={reference.report_must_mention.includes(key)}
              onChange={(event) =>
                setReference({
                  ...reference,
                  report_must_mention: event.target.checked
                    ? [...reference.report_must_mention, key]
                    : reference.report_must_mention.filter(
                        (item) => item !== key,
                      ),
                })
              }
            />
            {label}
          </label>
        ))}
      </fieldset>
      <label>
        {strings.rationale}
        <textarea
          value={reference.rationale}
          aria-label={strings.rationale}
          onChange={(event) =>
            setReference({ ...reference, rationale: event.target.value })
          }
        />
      </label>
      <label>
        {strings.trap}
        <select
          aria-label={strings.trap}
          value={reference.trap ?? ""}
          onChange={(event) =>
            setReference({ ...reference, trap: event.target.value || null })
          }
        >
          <option value="">{strings.noTrap}</option>
          {Object.entries(strings.traps).map(([code, label]) => (
            <option value={code} key={code}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <h2>{strings.autoValidation}</h2>
      {scenario.validation.map((check) => (
        <p key={check.code} className={check.ok ? styles.ok : styles.warn}>
          {check.ok ? "✓ " : "▲ "}
          {check.message}
        </p>
      ))}
      <label>
        {strings.reviewComment}
        <textarea
          value={comment}
          aria-label={strings.reviewComment}
          onChange={(event) => setComment(event.target.value)}
        />
      </label>
      {mutation.error && (
        <p role="alert" className={styles.alarm}>
          {mutation.error.message}
        </p>
      )}
      <div className={common.toolbar}>
        <button
          disabled={mutation.isPending || !comment.trim()}
          onClick={() => mutation.mutate("reject")}
        >
          {scenario.card_payload.lesson_id
            ? strings.rejectRegenerate
            : strings.rejectScenario}
        </button>
        <button
          disabled={mutation.isPending}
          onClick={() => mutation.mutate("approve")}
        >
          {strings.approve}
        </button>
      </div>
    </div>
  );
}

export function Preparation({ lessonId }: { lessonId: string }) {
  const client = useQueryClient();
  const [groups, setGroups] = useState<string[]>([]);
  const [count, setCount] = useState(40);
  const [generationBackend, setGenerationBackend] = useState("local_llm");
  const [street, setStreet] = useState("");
  const locations = useQuery({
    queryKey: ["generation-locations"],
    queryFn: () =>
      api<{ items: { id: string; name: string }[] }>(
        "/teacher/generation/locations",
      ),
  });
  const [minimum, setMinimum] = useState(1);
  const [maximum, setMaximum] = useState(6);
  const [jobId, setJobId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [library, setLibrary] = useState(false);
  const groupQuery = useQuery({
    queryKey: ["incident-groups"],
    queryFn: () =>
      api<{ items: { id: string; name: string }[] }>(
        "/teacher/incident-groups",
      ),
  });
  const reviews = useQuery({
    queryKey: ["reviews", lessonId, library],
    queryFn: () =>
      allItems<Scenario>(
        `/teacher/scenarios?status=PENDING_REVIEW${library ? "" : `&lesson_id=${lessonId}`}`,
      ),
    refetchInterval: jobId ? 1000 : false,
  });
  const job = useQuery({
    queryKey: ["generation-job", jobId],
    enabled: !!jobId,
    queryFn: () =>
      api<{
        status: string;
        progress: number;
        generated: number;
        rejected: number;
        error: string | null;
      }>(`/teacher/jobs/${jobId}`),
    refetchInterval: (query) =>
      ["DONE", "FAILED"].includes(query.state.data?.status ?? "")
        ? false
        : 1000,
  });
  const generate = useMutation({
    mutationFn: async () => {
      const result = await api<{ job_id: string }>(
        `/teacher/lessons/${lessonId}/scenarios/generate`,
        {
          method: "POST",
          body: JSON.stringify({
            incident_group_ids: groups,
            difficulty_range: [minimum, maximum],
            generation_backend: generationBackend,
            street_ids: street ? [street] : [],
            count,
          }),
        },
      );
      setJobId(result.job_id);
    },
  });
  const assignment = useMutation({
    mutationFn: async () => {
      const [approved, lesson] = await Promise.all([
        allItems<Scenario>(
          `/teacher/scenarios?status=APPROVED&lesson_id=${lessonId}`,
        ),
        api<{
          participants: { student_id: string }[];
          settings: { training_mode?: string };
        }>(`/teacher/lessons/${lessonId}`),
      ]);
      await api(`/teacher/lessons/${lessonId}/assign`, {
        method: "POST",
        body: JSON.stringify({
          assignments: lesson.participants.flatMap((person) =>
            approved.items.map((scenario, index) => ({
              student_id: person.student_id,
              scenario_id: scenario.id,
              ...(lesson.settings.training_mode === "MIXED"
                ? { task_mode: index % 2 === 0 ? "CARD_ENTRY" : "CARD_ACTIONS" }
                : {}),
            })),
          ),
        }),
      });
      await client.invalidateQueries({ queryKey: ["lessons"] });
    },
  });
  const approveAll = useMutation({
    mutationFn: async () => {
      try {
        for (const row of reviews.data?.items ?? []) {
          if (row.validation.some((check) => !check.ok)) continue;
          await api(`/teacher/scenarios/${row.id}/approve`, {
            method: "POST",
            body: JSON.stringify({ difficulty: row.difficulty }),
          });
        }
      } finally {
        await reviews.refetch();
      }
    },
  });
  const scenario =
    reviews.data?.items.find((row) => row.id === selected) ??
    reviews.data?.items[0];
  return (
    <section className={styles.preparation}>
      <h2>{strings.prepareLesson}</h2>
      <AsyncView
        loading={groupQuery.isPending}
        error={groupQuery.error}
        empty={groupQuery.data?.items.length === 0 && strings.noGroups}
        retry={() => void groupQuery.refetch()}
      >
        <form
          className={common.form}
          onSubmit={(event) => {
            event.preventDefault();
            generate.mutate();
          }}
        >
          <label>
            {strings.groups}
            <select
              multiple
              aria-label={strings.groups}
              value={groups}
              onChange={(event) =>
                setGroups(
                  Array.from(
                    event.target.selectedOptions,
                    (option) => option.value,
                  ),
                )
              }
            >
              {groupQuery.data?.items.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </select>
          </label>
          <p>{strings.allGroupsHelp}</p>
          <div className={common.toolbar}>
            <label>
              {strings.minimumDifficulty}
              <input
                type="number"
                min={1}
                max={10}
                value={minimum}
                onChange={(event) => setMinimum(Number(event.target.value))}
              />
            </label>
            <label>
              {strings.maximumDifficulty}
              <input
                type="number"
                min={minimum}
                max={10}
                value={maximum}
                onChange={(event) => setMaximum(Number(event.target.value))}
              />
            </label>
            <label>
              {strings.scenarioCount}
              <input
                type="number"
                min={1}
                max={200}
                value={count}
                onChange={(event) => setCount(Number(event.target.value))}
              />
            </label>
          </div>
          <button
            disabled={
              generate.isPending ||
              job.data?.status === "RUNNING" ||
              job.data?.status === "QUEUED"
            }
          >
            {strings.generateScenarios}
          </button>
          <label>
            {learning.generation}
            <select
              aria-label={learning.generation}
              value={generationBackend}
              onChange={(event) => setGenerationBackend(event.target.value)}
            >
              <option value="local_llm">{learning.localModel}</option>
              <option value="template">{learning.template}</option>
            </select>
          </label>
          <AsyncView
            loading={locations.isPending}
            error={locations.error}
            retry={() => void locations.refetch()}
            empty={!locations.data?.items.length && learning.noLocations}
          >
            <label>
              {learning.location}
              <select
                aria-label={learning.location}
                value={street}
                onChange={(event) => setStreet(event.target.value)}
              >
                <option value="">{learning.allLocations}</option>
                {locations.data?.items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
          </AsyncView>
          {generate.error && <p role="alert">{generate.error.message}</p>}
        </form>
      </AsyncView>
      {jobId && (
        <AsyncView
          loading={job.isPending}
          error={job.error}
          retry={() => void job.refetch()}
        >
          {job.data && (
            <div className={styles.section}>
              <progress
                className={styles.progress}
                max={1}
                value={job.data.progress}
              />
              <p>
                {
                  strings.jobStatuses[
                    job.data.status as keyof typeof strings.jobStatuses
                  ]
                }{" "}
                · {strings.generated}: {job.data.generated} · {strings.rejected}
                : {job.data.rejected}
              </p>
              {job.data.error && <p role="alert">{job.data.error}</p>}
            </div>
          )}
        </AsyncView>
      )}
      <AsyncView
        loading={reviews.isPending}
        error={reviews.error}
        empty={reviews.data?.items.length === 0 && strings.noReviewScenarios}
        retry={() => void reviews.refetch()}
      >
        <button
          disabled={
            approveAll.isPending ||
            !reviews.data?.items.some((row) =>
              row.validation.every((check) => check.ok),
            )
          }
          onClick={() => approveAll.mutate()}
        >
          {strings.approveAll}
        </button>
        {approveAll.error && <p role="alert">{approveAll.error.message}</p>}
        <label>
          {strings.scenarioToReview}
          <select
            aria-label={strings.scenarioToReview}
            value={scenario?.id ?? ""}
            onChange={(event) => setSelected(event.target.value)}
          >
            {reviews.data?.items.map((row) => (
              <option key={row.id} value={row.id}>
                {row.title}
              </option>
            ))}
          </select>
        </label>
        {scenario && (
          <Review
            key={scenario.id}
            scenario={scenario}
            refresh={() => reviews.refetch()}
            onJob={setJobId}
          />
        )}
      </AsyncView>
      <label>
        <input
          type="checkbox"
          checked={library}
          onChange={(event) => setLibrary(event.target.checked)}
        />
        {strings.reviewLibrary}
      </label>
      <button
        disabled={assignment.isPending}
        onClick={() => assignment.mutate()}
      >
        {strings.assignApproved}
      </button>
      {assignment.isSuccess && (
        <p className={styles.ok}>{strings.assignmentsReady}</p>
      )}
      {assignment.error && <p role="alert">{assignment.error.message}</p>}
    </section>
  );
}
