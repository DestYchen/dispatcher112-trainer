import {
  LessonList,
  type LessonSummary as Lesson,
} from "./components/LessonList";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { allItems, api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { learningStrings as learning, strings } from "./lib/strings";
import { defaultCriteria, SuccessCriteria } from "./components/SuccessCriteria";
import { Preparation } from "./Preparation";
import { TeacherLive } from "./TeacherLive";
import { TeacherReport } from "./TeacherReport";
import common from "./components/Common.module.css";

export function TeacherHome() {
  const [title, setTitle] = useState("");
  const [criteria, setCriteria] = useState(defaultCriteria);
  const [criteriaEnabled, setCriteriaEnabled] = useState(false);
  const [moduleId, setModuleId] = useState("");
  const [students, setStudents] = useState<string[]>([]);
  const [workstations, setWorkstations] = useState<Record<string, string>>({});
  const [settings, setSettings] = useState({
    primary_status_deadline_sec: 30,
    card_processing_deadline_sec: 180,
    max_concurrent_cards: 3,
    card_interval_sec: 45,
    hints_enabled: false,
    grammar_check_enabled: true,
    training_mode: "CARD_ACTIONS",
    incoming_channel: "TEXT",
  });
  const [liveLesson, setLiveLesson] = useState<Lesson | null>(null);
  const [reportLesson, setReportLesson] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [preparing, setPreparing] = useState<string | null>(null);
  const lessons = useQuery({
    queryKey: ["lessons"],
    queryFn: () => allItems<Lesson>("/teacher/lessons"),
  });
  const people = useQuery({
    queryKey: ["participants"],
    queryFn: () =>
      api<{
        students: { id: string; name: string }[];
        workstations: { id: string; number: string }[];
      }>("/teacher/participants"),
  });
  const scenarios = useQuery({
    queryKey: ["approved"],
    queryFn: () =>
      allItems<{ id: string; title: string }>(
        "/teacher/scenarios?status=APPROVED",
      ),
  });
  const creation = useMutation({
    mutationFn: async () => {
      const lesson = await api<Lesson>("/teacher/lessons", {
        method: "POST",
        body: JSON.stringify({
          title,
          participants: students.map((student_id) => ({
            student_id,
            workstation_id: workstations[student_id] || null,
          })),
          settings: {
            ...settings,
            success_criteria: criteriaEnabled ? criteria : null,
          },
        }),
      });
      setCreated(lesson.id);
      setPreparing(lesson.id);
      await lessons.refetch();
    },
  });
  const assignment = useMutation({
    mutationFn: async () => {
      await api(`/teacher/lessons/${created}/assign`, {
        method: "POST",
        body: JSON.stringify({
          assignments: students.flatMap((student) =>
            scenarios
              .data!.items.filter(
                (scenario) =>
                  !moduleId ||
                  modules.data?.items
                    .find((item) => item.id === moduleId)
                    ?.scenario_ids.includes(scenario.id),
              )
              .map((scenario, index) => ({
                student_id: student,
                scenario_id: scenario.id,
                ...(settings.training_mode === "MIXED"
                  ? {
                      task_mode:
                        index % 2 === 0 ? "CARD_ENTRY" : "CARD_ACTIONS",
                    }
                  : {}),
              })),
          ),
        }),
      });
      setCreated(null);
      setTitle("");
    },
  });
  const action = useMutation({
    mutationFn: async ({ id, command }: { id: string; command: string }) => {
      await api(`/teacher/lessons/${id}/${command}`, { method: "POST" });
      await lessons.refetch();
    },
  });
  const groups = useQuery({
    queryKey: ["learning-groups"],
    queryFn: () =>
      api<{
        items: { id: string; title: string; students: { id: string }[] }[];
      }>("/teacher/groups"),
  });
  const modules = useQuery({
    queryKey: ["learning-modules"],
    queryFn: () =>
      api<{ items: { id: string; title: string; scenario_ids: string[] }[] }>(
        "/teacher/modules",
      ),
  });
  const panelOpen = !!(preparing || reportLesson || liveLesson);
  const closePanel = () => {
    setPreparing(null);
    setReportLesson(null);
    setLiveLesson(null);
  };
  // A lesson console, report or preparation is its own screen; the browser Back button returns to the list.
  useEffect(() => {
    if (!panelOpen) return;
    window.scrollTo(0, 0);
    window.history.pushState({ teacherPanel: true }, "");
    const back = () => closePanel();
    window.addEventListener("popstate", back);
    return () => window.removeEventListener("popstate", back);
  }, [panelOpen]);
  if (panelOpen)
    return (
      <section className={common.page}>
        <div className={common.toolbar}>
          <button onClick={() => window.history.back()}>← К списку занятий</button>
        </div>
        {preparing && <Preparation key={preparing} lessonId={preparing} />}
        {reportLesson && <TeacherReport key={reportLesson} lessonId={reportLesson} />}
        {liveLesson && (
          <TeacherLive
            key={liveLesson.id}
            lessonId={liveLesson.id}
            title={liveLesson.title}
            refreshLessons={() => lessons.refetch()}
          />
        )}
      </section>
    );
  return (
    <section className={common.page}>
      <header className={common.pageHeading}>
        <div>
          <h1>{strings.teacherWorkspace}</h1>
          <p>{strings.teacherIntro}</p>
        </div>
      </header>
      <details className={common.formPanel}>
        <summary>{strings.newLesson}</summary>
        <AsyncView
          loading={people.isPending || scenarios.isPending}
          error={people.error || scenarios.error}
          empty={
            people.data?.students.length === 0 ? strings.noStudents : false
          }
          retry={() => {
            void people.refetch();
            void scenarios.refetch();
          }}
        >
          <form
            className={common.form}
            onSubmit={(event) => {
              event.preventDefault();
              creation.mutate();
            }}
          >
            <label>
              {strings.title}
              <input
                required
                value={title}
                disabled={!!created}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            <label>
              {strings.student}
              <select
                aria-label={strings.student}
                required
                multiple
                value={students}
                disabled={!!created}
                onChange={(event) =>
                  setStudents(
                    Array.from(
                      event.target.selectedOptions,
                      (option) => option.value,
                    ),
                  )
                }
              >
                {people.data?.students.map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.name}
                  </option>
                ))}
              </select>
            </label>
            {students.map((studentId) => (
              <label key={studentId}>
                {strings.workstation} ·{" "}
                {
                  people.data?.students.find(
                    (person) => person.id === studentId,
                  )?.name
                }
                <select
                  value={workstations[studentId] ?? ""}
                  disabled={!!created}
                  onChange={(event) =>
                    setWorkstations({
                      ...workstations,
                      [studentId]: event.target.value,
                    })
                  }
                >
                  <option value="">{strings.noWorkstation}</option>
                  {people.data?.workstations.map((station) => (
                    <option
                      key={station.id}
                      value={station.id}
                      disabled={students.some(
                        (id) =>
                          id !== studentId && workstations[id] === station.id,
                      )}
                    >
                      {station.number}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <fieldset disabled={!!created}>
              <legend>{strings.lessonParameters}</legend>
              <label>
                Входящее обращение
                <select
                  aria-label="Входящее обращение"
                  value={settings.incoming_channel}
                  onChange={(event) =>
                    setSettings({
                      ...settings,
                      incoming_channel: event.target.value,
                    })
                  }
                >
                  <option value="TEXT">Текстовое сообщение</option>
                  <option value="VOICE">Голосовой IP-вызов</option>
                </select>
              </label>
              <label>
                {strings.trainingMode}
                <select
                  value={settings.training_mode}
                  aria-label={strings.trainingMode}
                  onChange={(event) =>
                    setSettings({
                      ...settings,
                      training_mode: event.target.value,
                    })
                  }
                >
                  {Object.entries(strings.trainingModes).map(
                    ([code, label]) => (
                      <option value={code} key={code}>
                        {label}
                      </option>
                    ),
                  )}
                </select>
              </label>
              {(
                [
                  "primary_status_deadline_sec",
                  "card_processing_deadline_sec",
                  "max_concurrent_cards",
                  "card_interval_sec",
                ] as const
              ).map((key) => (
                <label key={key}>
                  {strings.settingLabels[key]}
                  <input
                    type="number"
                    min={1}
                    max={
                      key === "max_concurrent_cards"
                        ? 20
                        : key === "card_processing_deadline_sec"
                          ? 86400
                          : 3600
                    }
                    required
                    value={settings[key]}
                    onChange={(event) =>
                      setSettings({
                        ...settings,
                        [key]: Number(event.target.value),
                      })
                    }
                  />
                </label>
              ))}
              <label>
                <input
                  type="checkbox"
                  checked={settings.hints_enabled}
                  onChange={(event) =>
                    setSettings({
                      ...settings,
                      hints_enabled: event.target.checked,
                    })
                  }
                />
                {strings.hintsEnabled}
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={settings.grammar_check_enabled}
                  onChange={(event) =>
                    setSettings({
                      ...settings,
                      grammar_check_enabled: event.target.checked,
                    })
                  }
                />
                {strings.grammarEnabled}
              </label>
            </fieldset>
            <AsyncView
              loading={groups.isPending || modules.isPending}
              error={groups.error || modules.error}
              retry={() => {
                void groups.refetch();
                void modules.refetch();
              }}
            >
              <label>
                {learning.groups}
                <select
                  disabled={!!created}
                  defaultValue=""
                  onChange={(event) => {
                    const group = groups.data?.items.find(
                      (item) => item.id === event.target.value,
                    );
                    if (group) {
                      setStudents(group.students.map((item) => item.id));
                      setWorkstations({});
                    }
                  }}
                >
                  <option value="">{learning.chooseGroup}</option>
                  {groups.data?.items.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.title}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {learning.moduleTitle}
                <select
                  disabled={!!created}
                  value={moduleId}
                  onChange={(event) => setModuleId(event.target.value)}
                >
                  <option value="">{strings.assignPrepared}</option>
                  {modules.data?.items
                    .filter((item) => item.scenario_ids.length)
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.title}
                      </option>
                    ))}
                </select>
              </label>
            </AsyncView>
            <label>
              <input
                type="checkbox"
                checked={criteriaEnabled}
                disabled={!!created}
                onChange={(event) => setCriteriaEnabled(event.target.checked)}
              />
              {learning.criteria}
            </label>
            {criteriaEnabled && (
              <SuccessCriteria value={criteria} onChange={setCriteria} />
            )}
            {!created && (
              <button disabled={creation.isPending}>
                {strings.createLesson}
              </button>
            )}
            {created && (
              <button
                type="button"
                disabled={assignment.isPending || !scenarios.data?.items.length}
                onClick={() => assignment.mutate()}
              >
                {strings.assignPrepared}
              </button>
            )}
            {(creation.error || assignment.error) && (
              <p role="alert">
                {(creation.error || assignment.error)?.message}
              </p>
            )}
          </form>
        </AsyncView>
      </details>
      {action.error && <p role="alert">{action.error.message}</p>}
      <AsyncView
        loading={lessons.isPending}
        error={lessons.error}
        empty={lessons.data?.items.length === 0 && strings.noLessons}
        retry={() => void lessons.refetch()}
      >
        <LessonList
          lessons={lessons.data?.items ?? []}
          pending={action.isPending}
          onLive={(row) => {
            setLiveLesson(row);
            setReportLesson(null);
            setPreparing(null);
          }}
          onReport={(id) => {
            setReportLesson(id);
            setLiveLesson(null);
            setPreparing(null);
          }}
          onPrepare={(id) => {
            setPreparing(id);
            setReportLesson(null);
            setLiveLesson(null);
          }}
          onAction={(id, command) => action.mutate({ id, command })}
        />
      </AsyncView>
    </section>
  );
}
