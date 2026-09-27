import { api, RequestError } from "../api/client";
import type { EntryCard } from "./cardTypes";
import { readEntryDraft, writeEntryDraft } from "./entryDraft";

interface ActionBase {
  key: string;
  assignmentId: string;
  error?: string;
}
type StatusAction = { kind?: "status"; status: string; comment: string | null };
type ReportAction = {
  kind: "report";
  call_id: string;
  transcript: string;
  duration_ms: number;
};
type EntryAction =
  | { kind: "draft"; card: EntryCard; revision: number }
  | { kind: "submit-card"; revision: number };
export type QueuedAction = ActionBase &
  (StatusAction | ReportAction | EntryAction);
type NewAction = Omit<ActionBase, "key"> &
  (StatusAction | ReportAction | EntryAction);
const prefix = "dispatcher112-actions:";
export function readActions(userId: string): QueuedAction[] {
  const raw = localStorage.getItem(prefix + userId);
  if (!raw) return [];
  const result: unknown = JSON.parse(raw);
  if (!Array.isArray(result)) throw new Error("Invalid local action queue");
  return result as QueuedAction[];
}
export function writeActions(userId: string, actions: QueuedAction[]) {
  localStorage.setItem(prefix + userId, JSON.stringify(actions));
  window.dispatchEvent(new Event("dispatcher-actions"));
}
export function enqueue(userId: string, action: NewAction) {
  const queued = { ...action, key: crypto.randomUUID() };
  writeActions(userId, [...readActions(userId), queued]);
  return queued;
}
export function cancelAction(userId: string, key: string) {
  const actions = readActions(userId);
  const cancelled = actions.find((row) => row.key === key);
  writeActions(
    userId,
    actions.filter(
      (row) =>
        row.key !== key &&
        !(
          cancelled?.kind === "draft" &&
          row.assignmentId === cancelled.assignmentId &&
          row.kind === "submit-card"
        ),
    ),
  );
}
const flushing = new Map<string, Promise<void>>();
export function flushActions(userId: string): Promise<void> {
  const previous = flushing.get(userId);
  if (previous) return previous;
  const promise = (async () => {
    for (const action of readActions(userId)) {
      if (action.error) break;
      try {
        const report = action.kind === "report";
        const response = await api<{
          confirmation_audio_url?: string;
          confirmation_text?: string;
          revision?: number;
        }>(
          `/student/assignments/${action.assignmentId}/${action.kind ?? "status"}`,
          {
            method: "POST",
            headers: { "Idempotency-Key": action.key },
            body: JSON.stringify(
              action.kind === "draft"
                ? { card: action.card, revision: action.revision }
                : action.kind === "submit-card"
                  ? { revision: action.revision }
                  : report
                    ? {
                        call_id: action.call_id,
                        transcript: action.transcript,
                        duration_ms: action.duration_ms,
                      }
                    : {
                        status: action.status,
                        comment: action.comment,
                      },
            ),
          },
        );
        if (action.kind === "draft") {
          const local = readEntryDraft(userId, action.assignmentId, {
            card: action.card,
            revision: action.revision,
            dirty: true,
          });
          writeEntryDraft(userId, action.assignmentId, {
            ...local,
            revision: response.revision!,
            dirty: JSON.stringify(local.card) !== JSON.stringify(action.card),
          });
        }
        if (report) {
          localStorage.removeItem(
            `dispatcher112-phone:${userId}:${action.assignmentId}`,
          );
          window.dispatchEvent(
            new CustomEvent("dispatcher-report-saved", {
              detail: { assignmentId: action.assignmentId, ...response },
            }),
          );
        }
        writeActions(
          userId,
          readActions(userId).filter((item) => item.key !== action.key),
        );
      } catch (error) {
        if (
          error instanceof RequestError &&
          error.status >= 400 &&
          error.status < 500
        ) {
          writeActions(
            userId,
            readActions(userId).map((item) =>
              item.key === action.key
                ? { ...item, error: error.message }
                : item,
            ),
          );
        }
        throw error;
      }
    }
  })().finally(() => flushing.delete(userId));
  flushing.set(userId, promise);
  return promise;
}
