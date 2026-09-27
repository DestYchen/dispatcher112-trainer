import { useEffect, useRef, useState } from "react";
import type { CardDetail } from "../lib/cardTypes";
import { strings } from "../lib/strings";
import { CommentInput } from "./CommentInput";
import { TerminalDialog } from "./TerminalDialog";
import { PhonePanel } from "./PhonePanel";
import styles from "./Workspace.module.css";
import type { QueuedAction } from "../lib/offline";

export function ActionPanel({
  detail,
  hints,
  pending,
  sending,
  onSend,
  onCancelPending,
}: {
  detail: CardDetail;
  hints: boolean;
  pending: QueuedAction | undefined;
  sending: boolean;
  onSend: (status: string, comment: string) => boolean;
  onCancelPending: () => void;
}) {
  const [status, setStatus] = useState("");
  const [comment, setComment] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [phone, setPhone] = useState(false);
  const phoneButton = useRef<HTMLButtonElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.inert = phone;
  }, [phone]);
  const closePhone = () => {
    setPhone(false);
    setTimeout(() => phoneButton.current?.focus(), 0);
  };
  const commentRef = useRef<HTMLTextAreaElement>(null);
  const available = detail.my_block.available_statuses;
  const selected = available.find((item) => item.code === status);
  const terminal = status === "WORK_COMPLETED" || status === "WORK_REFUSED";
  const choose = (code: string) => {
    setStatus(code);
    if (available.find((item) => item.code === code)?.comment_required)
      commentRef.current?.focus();
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (phone) {
        if (event.key === "Escape") {
          event.preventDefault();
          closePhone();
        }
        return;
      }
      if (event.altKey && event.code === "KeyT" && detail.state !== "CLOSED") {
        event.preventDefault();
        setPhone(true);
        return;
      }
      if (!event.altKey || pending || sending) return;
      if (event.code === "KeyC") {
        event.preventDefault();
        commentRef.current?.focus();
      }
      const code =
        event.code === "Digit1"
          ? "ACCEPTED"
          : event.code === "Digit2"
            ? "NOT_ACCEPTED"
            : null;
      if (code && available.some((item) => item.code === code)) {
        event.preventDefault();
        setStatus(code);
        if (code === "NOT_ACCEPTED") commentRef.current?.focus();
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [available, pending, sending, phone, detail.state]);
  const send = () => {
    if (!onSend(status, comment)) return;
    setConfirm(false);
    setStatus("");
    setComment("");
  };
  return (
    <aside
      id="student-actions"
      className={styles.actions}
      aria-label={strings.yourActions}
    >
      <div ref={bodyRef}>
        <div className={styles.actionHeading}>
          <h2>{strings.yourActions}</h2>
          <button
            ref={phoneButton}
            type="button"
            disabled={detail.state === "CLOSED"}
            onClick={() => setPhone(true)}
          >
            {strings.phone} · Alt+T
          </button>
        </div>
        {hints && !detail.my_block.current_status && (
          <p className={styles.hint}>{strings.hintSteps}</p>
        )}
        <div className={styles.section}>
          {pending && (
            <div role="status" className={styles.connection}>
              <div>
                <p>{pending.error ?? strings.queuedAction}</p>
                {pending.error && (
                  <button onClick={onCancelPending}>
                    {strings.cancelQueued}
                  </button>
                )}
              </div>
            </div>
          )}
          {detail.state === "CLOSED" ? (
            <p>{strings.cardClosed}</p>
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (
                  selected &&
                  (!selected.comment_required || comment.trim().length >= 15)
                ) {
                  if (terminal) setConfirm(true);
                  else send();
                }
              }}
            >
              <div className={styles.actionList}>
                {available.map((item) => (
                  <button
                    type="button"
                    disabled={!!pending || sending}
                    className={`${styles.actionButton} ${status === item.code ? styles.chosen : ""} ${item.code === "ACCEPTED" && status !== item.code ? styles.accept : ""}`}
                    aria-pressed={status === item.code}
                    key={item.code}
                    onClick={() => choose(item.code)}
                    title={
                      item.code.startsWith("WORK_") &&
                      ["WORK_COMPLETED", "WORK_REFUSED"].includes(item.code)
                        ? strings.terminalWarning
                        : undefined
                    }
                  >
                    {item.label}
                    {["WORK_COMPLETED", "WORK_REFUSED"].includes(item.code) &&
                      " 🔒"}
                    {hints && item.code === "ACCEPTED" && " · Alt+1"}
                    {hints && item.code === "NOT_ACCEPTED" && " · Alt+2"}
                  </button>
                ))}
              </div>
              <label htmlFor="card-comment">
                {selected?.comment_required
                  ? strings.requiredComment
                  : strings.comment}
              </label>
              <CommentInput
                assignmentId={detail.assignment_id}
                text={comment}
                setText={setComment}
                inputRef={commentRef}
                required={!!selected?.comment_required}
              />
              <p className={styles.metadata}>{strings.commentHelp}</p>
              <button
                className={styles.actionButton}
                disabled={
                  !!pending ||
                  sending ||
                  !selected ||
                  (selected.comment_required && comment.trim().length < 15)
                }
              >
                {sending ? strings.saving : strings.saveStatus}
              </button>
            </form>
          )}
        </div>
        <section className={styles.section}>
          <h2>{strings.history}</h2>
          <ol className={styles.history}>
            {detail.my_block.history.map((event) => (
              <li key={event.id}>
                <strong>{event.label}</strong>
                <p className={styles.metadata}>
                  {new Date(event.at).toLocaleTimeString("ru-RU")} ·{" "}
                  {(event.elapsed_ms / 1000).toFixed(1)} {strings.secondsUnit}
                </p>
                {event.comment && <p>{event.comment}</p>}
              </li>
            ))}
          </ol>
        </section>
        {confirm && selected && (
          <TerminalDialog
            number={detail.card.card_number}
            status={selected.label}
            comment={comment}
            cancel={() => setConfirm(false)}
            confirm={send}
          />
        )}
      </div>
      {phone && detail.state !== "CLOSED" && (
        <PhonePanel
          assignmentId={detail.assignment_id}
          close={closePhone}
          pending={!!pending}
        />
      )}
    </aside>
  );
}
