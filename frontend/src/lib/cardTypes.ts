export interface CardSummary {
  task_mode?: "CARD_ACTIONS" | "CARD_ENTRY";
  assignment_id: string;
  card_number: string;
  state: string;
  origin: string;
  incident_type_name: string;
  address_short: string;
  registered_at: string;
  delivered_at: string;
  primary_deadline_at: string;
  processing_deadline_at: string | null;
  current_status: string | null;
  is_overdue: boolean;
  has_unread_description: boolean;
}
export interface LessonSettings {
  primary_status_deadline_sec: number;
  card_processing_deadline_sec: number;
  hints_enabled: boolean;
  grammar_check_enabled: boolean;
}
export interface StudentState {
  server_time: string;
  lesson: {
    id: string;
    title: string;
    status: string;
    settings: LessonSettings;
  } | null;
  workstation: { number: string } | null;
  cards: CardSummary[];
  stats: {
    closed: number;
    expired: number;
    avg_primary_delay_ms: number | null;
  };
}
export interface CardDetail {
  task_mode?: "CARD_ACTIONS" | "CARD_ENTRY";
  entry?: {
    incoming_channel?: "TEXT" | "VOICE";
    incoming_message: string;
    draft: EntryCard;
    revision: number;
    submitted_at: string | null;
    accepted_delay_ms: number | null;
    score: EntryScore | null;
  };
  assignment_id: string;
  state: string;
  card: {
    card_number: string;
    origin: string;
    registered_at: string;
    operator_workstation: string | null;
    applicant: { name: string; phone: string };
    address: {
      raw: string;
      clarification: string | null;
      lat?: number;
      lon?: number;
    };
    attributes: string[];
    incident_type_name: string;
    modifiers: { code: string; label: string }[];
    description: string;
    notification_list: {
      service_code: string;
      service_name: string;
      is_own: boolean;
      last_status: { status: string; at: string; by: string } | null;
    }[];
  };
  my_block: {
    service_code: string;
    current_status: string | null;
    available_statuses: {
      code: string;
      label: string;
      comment_required: boolean;
    }[];
    history: {
      id: string;
      status: string;
      label: string;
      comment: string | null;
      is_automatic: boolean;
      at: string;
      elapsed_ms: number;
    }[];
  };
  timers: {
    server_time: string;
    primary_deadline_at: string;
    processing_deadline_at: string | null;
  };
}
export function remaining(deadline: string | null, now: number) {
  return deadline ? Math.ceil((Date.parse(deadline) - now) / 1000) : null;
}
export function countdown(seconds: number | null) {
  if (seconds === null) return "—";
  const absolute = Math.abs(seconds);
  return `${seconds < 0 ? "−" : ""}${String(Math.floor(absolute / 60)).padStart(2, "0")}:${String(absolute % 60).padStart(2, "0")}`;
}
export function activeDeadline(card: CardSummary) {
  return card.current_status ||
    (card.task_mode === "CARD_ENTRY" && card.processing_deadline_at)
    ? card.processing_deadline_at
    : card.primary_deadline_at;
}

export interface EntryCard {
  applicant: { name: string; phone: string };
  address: { raw: string; clarification: string };
  incident_type_id: string | null;
  description: string;
  modifiers: string[];
  notified_services: string[];
}
export interface EntryScore {
  total: number;
  axes: Record<string, { score: number | null; reason?: string }>;
  entry_fields: {
    field: string;
    label: string;
    actual: unknown;
    expected: unknown;
    correct: boolean;
    actual_display?: string;
    expected_display?: string;
  }[];
  violations: { code: string; message: string; hint: string }[];
}
export function sortCards(cards: CardSummary[], now: number) {
  return [...cards].sort((a, b) => {
    if ((a.state === "CLOSED") !== (b.state === "CLOSED"))
      return a.state === "CLOSED" ? 1 : -1;
    const aLate = a.is_overdue || (remaining(activeDeadline(a), now) ?? 1) < 0;
    const bLate = b.is_overdue || (remaining(activeDeadline(b), now) ?? 1) < 0;
    if (aLate !== bLate) return aLate ? 1 : -1;
    return (
      (remaining(activeDeadline(a), now) ?? Infinity) -
      (remaining(activeDeadline(b), now) ?? Infinity)
    );
  });
}
