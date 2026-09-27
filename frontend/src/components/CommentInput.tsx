import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type RefObject } from "react";
import { api } from "../api/client";
import { strings } from "../lib/strings";
import styles from "./Workspace.module.css";

interface Issue {
  kind: string;
  severity: string;
  offset: number;
  length: number;
  text: string;
  message: string;
  suggestions: string[];
}
export function CommentInput({
  assignmentId,
  text,
  setText,
  inputRef,
  required,
  field = "comment",
}: {
  assignmentId: string;
  text: string;
  setText: (text: string) => void;
  inputRef: RefObject<HTMLTextAreaElement>;
  required: boolean;
  field?: "comment" | "report" | "address" | "description";
}) {
  const [debounced, setDebounced] = useState("");
  const [selectedIssue, setSelectedIssue] = useState<Issue | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  useEffect(() => {
    const timeout = setTimeout(() => {
      setDebounced(text);
      setSelectedIssue(null);
    }, 600);
    return () => clearTimeout(timeout);
  }, [text]);
  const query = useQuery({
    queryKey: ["check-text", assignmentId, field, debounced],
    enabled: !!debounced.trim(),
    retry: false,
    queryFn: () =>
      api<{
        issues: Issue[];
        grammar_available: boolean;
        notice: string | null;
      }>(`/student/assignments/${assignmentId}/check-text`, {
        method: "POST",
        body: JSON.stringify({ text: debounced, field }),
      }),
  });
  const issues = debounced === text ? (query.data?.issues ?? []) : [];
  const parts = [];
  let offset = 0;
  for (const issue of [...issues].sort((a, b) => a.offset - b.offset)) {
    if (issue.offset < offset) continue;
    parts.push(
      <span key={`before-${issue.offset}`}>
        {text.slice(offset, issue.offset)}
      </span>,
    );
    parts.push(
      <span
        key={`issue-${issue.offset}`}
        className={
          issue.severity === "CRITICAL"
            ? styles.criticalUnderline
            : styles.minorUnderline
        }
      >
        {text.slice(issue.offset, issue.offset + issue.length)}
      </span>,
    );
    offset = issue.offset + issue.length;
  }
  parts.push(
    <span key="tail">
      {text.slice(offset)}
      {"\n"}
    </span>,
  );
  return (
    <>
      <div className={styles.commentWrap}>
        <div
          className={styles.commentOverlay}
          aria-hidden="true"
          ref={(element) => {
            if (element) element.scrollTop = scrollTop;
          }}
        >
          {parts}
        </div>
        <textarea
          id={field === "report" ? "phone-report" : `card-${field}`}
          aria-label={
            field === "report"
              ? strings.phoneReport
              : field === "address"
                ? strings.address
                : field === "description"
                  ? strings.description
                  : undefined
          }
          ref={inputRef}
          value={text}
          required={required}
          className={`${styles.comment} ${styles.checkedComment}`}
          rows={field === "address" ? 2 : 4}
          data-field={field}
          maxLength={field === "address" ? 500 : 10000}
          onChange={(event) => setText(event.target.value)}
          onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
          onClick={(event) =>
            setSelectedIssue(
              issues.find(
                (issue) =>
                  event.currentTarget.selectionStart >= issue.offset &&
                  event.currentTarget.selectionStart <=
                    issue.offset + issue.length,
              ) ?? null,
            )
          }
        />
      </div>
      {text.trim() && (
        <div className={styles.textCheck} aria-live="polite">
          {debounced !== text || query.isPending ? (
            <div className={styles.skeletonRow}>{strings.checkingText}</div>
          ) : query.error ? (
            <p role="alert">
              {strings.checkTextUnavailable}{" "}
              <button type="button" onClick={() => void query.refetch()}>
                {strings.retryNow}
              </button>
            </p>
          ) : issues.length === 0 ? (
            <p>{query.data?.notice ?? strings.noTextIssues}</p>
          ) : (
            <div>
              {issues.map((issue, index) => (
                <button
                  type="button"
                  key={index}
                  className={issue.severity === "CRITICAL" ? styles.alarm : ""}
                  onClick={() => setSelectedIssue(issue)}
                >
                  {issue.text}: {issue.message}
                </button>
              ))}
            </div>
          )}
          {query.data?.notice && issues.length > 0 && (
            <p>{query.data.notice}</p>
          )}
          {selectedIssue && (
            <aside className={styles.hint} role="note">
              <p>{selectedIssue.message}</p>
              {selectedIssue.suggestions.length > 0 && (
                <>
                  <strong>{strings.suggestions}</strong>
                  <ul>
                    {selectedIssue.suggestions.map((suggestion) => (
                      <li key={suggestion}>{suggestion}</li>
                    ))}
                  </ul>
                </>
              )}
            </aside>
          )}
        </div>
      )}
    </>
  );
}
