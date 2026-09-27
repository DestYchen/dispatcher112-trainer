import type { EntryCard } from "./cardTypes";

export interface EntryDraft {
  card: EntryCard;
  revision: number;
  dirty: boolean;
}
const storageKey = (user: string, assignment: string) =>
  `dispatcher112-entry:${user}:${assignment}`;
export function readEntryDraft(
  user: string,
  assignment: string,
  fallback: EntryDraft,
): EntryDraft {
  const raw = localStorage.getItem(storageKey(user, assignment));
  return raw ? (JSON.parse(raw) as EntryDraft) : fallback;
}
export function writeEntryDraft(
  user: string,
  assignment: string,
  draft: EntryDraft,
) {
  localStorage.setItem(storageKey(user, assignment), JSON.stringify(draft));
  window.dispatchEvent(
    new CustomEvent("dispatcher-entry-draft", { detail: { assignment } }),
  );
}
