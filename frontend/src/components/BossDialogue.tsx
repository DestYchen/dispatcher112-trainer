import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import common from "./Common.module.css";
import styles from "./Phone.module.css";

interface Reply {
  node: string;
  text: string;
  audio_url: string | null;
  filled: string[];
  final: boolean;
  unmatched: boolean;
  extra: { hint?: string; missing?: string[] };
}
interface Line {
  who: "boss" | "me";
  text: string;
  hint?: string;
}

const SLOT_LABELS: Record<string, string> = {
  address: "адрес",
  incident_type: "что случилось",
  victims: "пострадавшие",
  measures: "принятые меры",
};

/** Interactive report to the duty officer: each answer is a pre-recorded line from the dialogue graph. */
export function BossDialogue({
  assignmentId,
  callId,
  calleeTitle,
  greeting,
  play,
  onFinished,
}: {
  assignmentId: string;
  callId: string;
  calleeTitle: string;
  greeting: string;
  play: (url: string) => void;
  onFinished: (text: string) => void;
}) {
  const queryClient = useQueryClient();
  const [lines, setLines] = useState<Line[]>([{ who: "boss", text: greeting }]);
  const [filled, setFilled] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLOListElement>(null);

  useEffect(() => inputRef.current?.focus(), [busy]);
  useEffect(() => {
    logRef.current?.lastElementChild?.scrollIntoView({ block: "nearest" });
  }, [lines]);

  const speak = (reply: Reply) => {
    if (reply.audio_url) return play(reply.audio_url);
    // No rendered file yet (new line approved today): read it with the local system voice.
    if ("speechSynthesis" in window) {
      const utterance = new SpeechSynthesisUtterance(reply.text);
      utterance.lang = "ru-RU";
      window.speechSynthesis.speak(utterance);
    }
  };

  const send = async () => {
    const said = text.trim();
    if (!said || busy) return;
    setBusy(true);
    setError(null);
    setLines((rows) => [...rows, { who: "me", text: said }]);
    setText("");
    try {
      const reply = await api<Reply>(`/student/assignments/${assignmentId}/dialogue`, {
        method: "POST",
        body: JSON.stringify({ call_id: callId, utterance: said }),
      });
      setLines((rows) => [...rows, { who: "boss", text: reply.text, hint: reply.extra.hint }]);
      setFilled(reply.filled);
      speak(reply);
      if (reply.final) {
        await queryClient.invalidateQueries();
        onFinished(reply.text);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Ошибка связи. Повторите фразу.");
      setLines((rows) => rows.slice(0, -1));
      setText(said);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.dialogue}>
      <ol className={styles.dialogueLog} ref={logRef} aria-live="polite">
        {lines.map((line, index) => (
          <li key={index} className={line.who === "boss" ? styles.boss : styles.me}>
            <span>{line.who === "boss" ? calleeTitle : "Вы"}:</span> {line.text}
            {line.hint && <em className={styles.hint}> {line.hint}</em>}
          </li>
        ))}
      </ol>
      <p className={styles.checklist} aria-label="Что уже доложено">
        {Object.entries(SLOT_LABELS).map(([slot, label]) => (
          <span key={slot} className={filled.includes(slot) ? styles.done : styles.todo}>
            {filled.includes(slot) ? "✓" : "○"} {label}
          </span>
        ))}
      </p>
      <form
        className={common.form}
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <label htmlFor="boss-turn">Ваш ответ руководителю</label>
        <input
          id="boss-turn"
          ref={inputRef}
          value={text}
          maxLength={2000}
          autoComplete="off"
          placeholder="Например: Берзарина 21, задымление мусоропровода, пострадавших нет"
          onChange={(event) => setText(event.target.value)}
          disabled={busy}
        />
        <button disabled={busy || !text.trim()}>{busy ? "…" : "Сказать"}</button>
        {error && <p role="alert">{error}</p>}
      </form>
    </div>
  );
}
