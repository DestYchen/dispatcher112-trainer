import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { allItems, api } from "./api/client";
import { useAuth } from "./lib/auth";
import { learningStrings as s, strings } from "./lib/strings";
import { AsyncView } from "./components/AsyncView";
import { LearningHistory } from "./LearningHistory";
import { LearningAnalytics } from "./LearningAnalytics";
import { ScenarioBank } from "./ScenarioBank";
import { GroupAnalysis } from "./GroupAnalysis";
import common from "./components/Common.module.css";
import styles from "./Learning.module.css";

type Material = {
  id: string;
  title: string;
  body: string;
  difficulty: number;
  filename: string | null;
  download_url: string | null;
  archived: boolean;
};
type Group = {
  id: string;
  title: string;
  students: { id: string; name: string }[];
};
type Module = {
  id: string;
  title: string;
  instructions: string;
  difficulty: number;
  group_ids: string[];
  materials?: Material[];
  completed_at?: string | null;
};

function MaterialView({ item }: { item: Material }) {
  return (
    <article className={styles.item}>
      <h3>{item.title}</h3>
      <p>
        {s.difficulty}: {item.difficulty}
      </p>
      <p className={styles.text}>{item.body}</p>
      {item.download_url && (
        <a className={styles.download} href={item.download_url}>
          {s.download}: {item.filename}
        </a>
      )}
      {item.archived && <p>{s.archived}</p>}
    </article>
  );
}
function Choices({
  name,
  items,
  selected = [],
}: {
  name: string;
  items: { id: string; name: string }[];
  selected?: string[];
}) {
  return (
    <div className={styles.choices}>
      {items.map((item) => (
        <label key={item.id}>
          <input
            type="checkbox"
            name={name}
            value={item.id}
            defaultChecked={selected.includes(item.id)}
          />
          {item.name}
        </label>
      ))}
    </div>
  );
}

function TeacherLibrary({ view }: { view: "library" | "groups" }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState<Group | null>(null);
  const [bankOpen, setBankOpen] = useState(false);
  const [analysisGroup, setAnalysisGroup] = useState<string | null>(null);
  const materials = useQuery({
    queryKey: ["learning-materials"],
    queryFn: () => api<{ items: Material[] }>("/teacher/materials"),
  });
  const groups = useQuery({
    queryKey: ["learning-groups"],
    queryFn: () => api<{ items: Group[] }>("/teacher/groups"),
  });
  const modules = useQuery({
    queryKey: ["learning-modules"],
    queryFn: () => api<{ items: Module[] }>("/teacher/modules"),
  });
  const people = useQuery({
    queryKey: ["participants"],
    queryFn: () =>
      api<{ students: { id: string; name: string }[] }>(
        "/teacher/participants",
      ),
  });
  const scenarios = useQuery({
    queryKey: ["approved"],
    queryFn: () =>
      allItems<{ id: string; title: string }>(
        "/teacher/scenarios?status=APPROVED",
      ),
  });
  const mutation = useMutation({
    mutationFn: async ({
      path,
      body,
      method = "POST",
    }: {
      path: string;
      body?: FormData | string;
      method?: string;
    }) => api(path, { method, body }),
    onSuccess: async () => {
      await client.invalidateQueries({
        predicate: (query) => String(query.queryKey[0]).startsWith("learning-"),
      });
    },
  });
  return (
    <>
      {mutation.error && (
        <p role="alert" className={common.error}>
          {mutation.error.message}
        </p>
      )}
      {mutation.isSuccess && <p role="status">{s.saved}</p>}
      {view === "groups" ? (
        <>
          <h2>{s.groups}</h2>
          <AsyncView
            loading={groups.isPending || people.isPending}
            error={groups.error || people.error}
            retry={() => {
              void groups.refetch();
              void people.refetch();
            }}
          >
            <form
              key={editing?.id ?? "new"}
              className={common.form}
              onSubmit={(event) => {
                event.preventDefault();
                const form = event.currentTarget;
                const data = new FormData(form);
                mutation.mutate(
                  {
                    path: `/teacher/groups${editing ? `/${editing.id}` : ""}`,
                    method: editing ? "PATCH" : "POST",
                    body: JSON.stringify({
                      title: data.get("title"),
                      student_ids: data.getAll("student_ids"),
                    }),
                  },
                  {
                    onSuccess: () => {
                      setEditing(null);
                      form.reset();
                    },
                  },
                );
              }}
            >
              <label>
                {s.title}
                <input
                  name="title"
                  defaultValue={editing?.title ?? ""}
                  required
                  maxLength={255}
                />
              </label>
              <fieldset>
                <legend>{s.students}</legend>
                <Choices
                  name="student_ids"
                  items={people.data?.students ?? []}
                  selected={editing?.students.map((item) => item.id)}
                />
              </fieldset>
              <button disabled={mutation.isPending}>
                {editing ? s.save : s.createGroup}
              </button>
              {editing && (
                <button type="button" onClick={() => setEditing(null)}>
                  {s.cancel}
                </button>
              )}
            </form>
            <AsyncView empty={!groups.data?.items.length && s.noGroups}>
              <div className={styles.list}>
                {groups.data?.items.map((group) => (
                  <article className={styles.item} key={group.id}>
                    <h3>{group.title}</h3>
                    <p>{group.students.map((item) => item.name).join(", ")}</p>
                    <button onClick={() => setEditing(group)}>{s.edit}</button>
                    <button
                      onClick={() =>
                        setAnalysisGroup(
                          analysisGroup === group.id ? null : group.id,
                        )
                      }
                    >
                      {s.groupAnalysis}
                    </button>
                    {analysisGroup === group.id && (
                      <GroupAnalysis groupId={group.id} />
                    )}
                  </article>
                ))}
              </div>
            </AsyncView>
          </AsyncView>
        </>
      ) : (
        <>
          <h2>{s.library}</h2>
          <details className={common.formPanel}>
            <summary>{s.createMaterial}</summary>
            <form
              className={common.form}
              onSubmit={(event) => {
                event.preventDefault();
                const form = event.currentTarget;
                const data = new FormData(form);
                const file = data.get("file");
                if (file instanceof File && !file.size) data.delete("file");
                mutation.mutate(
                  { path: "/teacher/materials", body: data },
                  { onSuccess: () => form.reset() },
                );
              }}
            >
              <label>
                {s.title}
                <input name="title" required maxLength={255} />
              </label>
              <label>
                {s.body}
                <textarea name="body" maxLength={30000} rows={5} />
              </label>
              <label>
                {s.difficulty}
                <input
                  type="number"
                  name="difficulty"
                  defaultValue={1}
                  min={1}
                  max={10}
                  required
                />
              </label>
              <label>
                {s.document}
                <input type="file" name="file" accept=".pdf,.docx" />
              </label>
              <button disabled={mutation.isPending}>{s.createMaterial}</button>
            </form>
          </details>
          <AsyncView
            loading={materials.isPending}
            error={materials.error}
            retry={() => void materials.refetch()}
            empty={!materials.data?.items.length && s.noMaterials}
          >
            <div className={styles.list}>
              {materials.data?.items.map((item) => (
                <div key={item.id}>
                  <MaterialView item={item} />
                  <DownloadButton
                    path={`/teacher/materials/${item.id}/export.xml`}
                    filename={`material-${item.id}.xml`}
                  >
                    {exchangeStrings.material}
                  </DownloadButton>
                  <button
                    disabled={mutation.isPending}
                    onClick={() =>
                      mutation.mutate({
                        path: `/teacher/materials/${item.id}`,
                        method: "PATCH",
                        body: JSON.stringify({ archived: !item.archived }),
                      })
                    }
                  >
                    {item.archived ? s.restore : s.archive}
                  </button>
                </div>
              ))}
            </div>
          </AsyncView>
          <details
            className={common.formPanel}
            onToggle={(event) => setBankOpen(event.currentTarget.open)}
          >
            <summary>{s.scenarioBank}</summary>
            {bankOpen && <ScenarioBank />}
          </details>
          <h2>{s.moduleTitle}</h2>
          <AsyncView
            loading={
              modules.isPending || groups.isPending || scenarios.isPending
            }
            error={modules.error || groups.error || scenarios.error}
            retry={() => {
              void modules.refetch();
              void groups.refetch();
              void scenarios.refetch();
            }}
          >
            <details className={common.formPanel}>
              <summary>{s.createModule}</summary>
              <form
                className={common.form}
                onSubmit={(event) => {
                  event.preventDefault();
                  const form = event.currentTarget;
                  const data = new FormData(form);
                  mutation.mutate(
                    {
                      path: "/teacher/modules",
                      body: JSON.stringify({
                        title: data.get("title"),
                        instructions: data.get("instructions"),
                        difficulty: Number(data.get("difficulty")),
                        material_ids: data.getAll("material_ids"),
                        scenario_ids: data.getAll("scenario_ids"),
                      }),
                    },
                    { onSuccess: () => form.reset() },
                  );
                }}
              >
                <label>
                  {s.title}
                  <input name="title" required maxLength={255} />
                </label>
                <label>
                  {s.instructions}
                  <textarea
                    name="instructions"
                    required
                    maxLength={10000}
                    rows={4}
                  />
                </label>
                <label>
                  {s.difficulty}
                  <input
                    type="number"
                    name="difficulty"
                    defaultValue={1}
                    min={1}
                    max={10}
                    required
                  />
                </label>
                <fieldset>
                  <legend>{s.materials}</legend>
                  <Choices
                    name="material_ids"
                    items={(materials.data?.items ?? [])
                      .filter((item) => !item.archived)
                      .map((item) => ({ id: item.id, name: item.title }))}
                  />
                </fieldset>
                <fieldset>
                  <legend>{s.scenarios}</legend>
                  <Choices
                    name="scenario_ids"
                    items={(scenarios.data?.items ?? []).map((item) => ({
                      id: item.id,
                      name: item.title,
                    }))}
                  />
                </fieldset>
                <button disabled={mutation.isPending}>{s.createModule}</button>
              </form>
            </details>
            <AsyncView
              empty={!modules.data?.items.length && s.noTeacherModules}
            >
              <div className={styles.list}>
                {modules.data?.items.map((item) => (
                  <article key={item.id} className={styles.item}>
                    <h3>{item.title}</h3>
                    <p className={styles.text}>{item.instructions}</p>
                    <p>
                      {item.group_ids
                        .map(
                          (id) =>
                            groups.data?.items.find((group) => group.id === id)
                              ?.title,
                        )
                        .filter(Boolean)
                        .join(", ")}
                    </p>
                    <form
                      className={common.form}
                      onSubmit={(event) => {
                        event.preventDefault();
                        const data = new FormData(event.currentTarget);
                        mutation.mutate({
                          path: `/teacher/modules/${item.id}/groups/${data.get("group_id")}`,
                        });
                      }}
                    >
                      <label>
                        {s.chooseGroup}
                        <select
                          name="group_id"
                          aria-label={s.chooseGroup}
                          required
                          defaultValue=""
                        >
                          <option value="">{s.chooseGroup}</option>
                          {groups.data?.items.map((group) => (
                            <option value={group.id} key={group.id}>
                              {group.title}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button disabled={mutation.isPending}>{s.assign}</button>
                    </form>
                  </article>
                ))}
              </div>
            </AsyncView>
          </AsyncView>
        </>
      )}
    </>
  );
}

function StudentModules() {
  const query = useQuery({
    queryKey: ["student-modules"],
    queryFn: () => api<{ items: Module[] }>("/student/modules"),
  });
  const mark = useMutation({
    mutationFn: (id: string) =>
      api(`/student/modules/${id}/complete`, { method: "POST" }),
    onSuccess: () => query.refetch(),
  });
  return (
    <>
      <h2>{s.modules}</h2>
      {mark.error && (
        <p className={common.error} role="alert">
          {mark.error.message}
        </p>
      )}
      <AsyncView
        loading={query.isPending}
        error={query.error}
        retry={() => void query.refetch()}
        empty={!query.data?.items.length && s.noModules}
      >
        <div className={styles.list}>
          {query.data?.items.map((item) => (
            <article key={item.id} className={styles.item}>
              <h3>
                {item.title} · {s.difficulty}: {item.difficulty}
              </h3>
              <p className={styles.text}>{item.instructions}</p>
              <div className={styles.list}>
                {item.materials?.map((material) => (
                  <MaterialView key={material.id} item={material} />
                ))}
              </div>
              <p>{s.practice}</p>
              {item.completed_at ? (
                <p>
                  ✓ {s.readAt}:{" "}
                  {new Date(item.completed_at).toLocaleString("ru-RU")}
                </p>
              ) : (
                <button
                  disabled={mark.isPending}
                  onClick={() => mark.mutate(item.id)}
                >
                  {s.read}
                </button>
              )}
            </article>
          ))}
        </div>
      </AsyncView>
    </>
  );
}

export function Learning() {
  const { user } = useAuth();
  const teacher = user?.role === "TEACHER";
  const [view, setView] = useState<
    "library" | "groups" | "history" | "analytics"
  >("library");
  return (
    <section className={common.page}>
      <h1>{s.learning}</h1>
      <nav className={common.toolbar} aria-label={s.learning}>
        {(["library", "groups", "history", "analytics"] as const)
          .filter((key) => teacher || key !== "groups")
          .map((key) => (
            <button
              key={key}
              aria-pressed={view === key}
              onClick={() => setView(key)}
            >
              {key === "library" && !teacher ? s.modules : s[key]}
            </button>
          ))}
      </nav>
      {(view === "library" || view === "groups") &&
        (teacher ? <TeacherLibrary view={view} /> : <StudentModules />)}
      {view === "history" && <LearningHistory teacher={teacher} />}
      {view === "analytics" && <LearningAnalytics teacher={teacher} />}
      <p className={styles.notice}>{strings.training}</p>
    </section>
  );
}
import { DownloadButton } from "./components/DownloadButton";
import { exchangeStrings } from "./lib/strings";
