import { useEffect, useRef } from "react";
import { strings } from "../lib/strings";
import styles from "./Workspace.module.css";

export function TerminalDialog({
  number,
  status,
  comment,
  cancel,
  confirm,
  entryMode = false,
}: {
  number: string;
  status: string;
  comment: string;
  cancel: () => void;
  confirm: () => void;
  entryMode?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  const prior = useRef(document.activeElement as HTMLElement | null);
  useEffect(() => {
    const dialog = ref.current;
    const previousFocus = prior.current;
    dialog?.showModal();
    back.current?.focus();
    return () => {
      dialog?.close();
      previousFocus?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={styles.dialog}
      aria-labelledby="terminal-title"
      onCancel={(event) => {
        event.preventDefault();
        cancel();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const buttons = ref.current!.querySelectorAll("button");
        if (event.shiftKey && document.activeElement === buttons[0]) {
          event.preventDefault();
          buttons[buttons.length - 1].focus();
        }
        if (
          !event.shiftKey &&
          document.activeElement === buttons[buttons.length - 1]
        ) {
          event.preventDefault();
          buttons[0].focus();
        }
      }}
    >
      <h2 id="terminal-title">
        {entryMode ? strings.submitCard : strings.closeCard} {number}?
      </h2>
      <p>
        {entryMode
          ? strings.entryWarning
          : `${strings.terminalWarning} «${status}».`}
      </p>
      <p className={styles.readonly}>{comment || strings.noComment}</p>
      <div className={styles.dialogActions}>
        <button ref={back} onClick={cancel}>
          {strings.back}
        </button>
        <button onClick={confirm}>
          {entryMode ? strings.submitCard : strings.closeCard}
        </button>
      </div>
    </dialog>
  );
}
