import { strings } from "../lib/strings";
import {
  countdown,
  remaining,
  type CardDetail,
  type LessonSettings,
} from "../lib/cardTypes";
import styles from "./Workspace.module.css";
import arm from "./Arm.module.css";

export function DeadlineBar({
  seconds,
  total,
  label = strings.primaryTimer,
}: {
  seconds: number | null;
  total: number;
  label?: string;
}) {
  const ratio = Math.max(0, Math.min(1, (seconds ?? 0) / total));
  return (
    <progress
      className={styles.progress}
      data-level={ratio > 0.5 ? "ok" : ratio >= 0.2 ? "warn" : "alarm"}
      max={total}
      value={Math.max(0, seconds ?? 0)}
      aria-label={label}
    />
  );
}
function Hint({
  kind,
  enabled,
}: {
  kind: keyof typeof strings.hints;
  enabled: boolean;
}) {
  return enabled ? (
    <p className={styles.hint}>? {strings.hints[kind]}</p>
  ) : null;
}
export function CardView({
  detail,
  settings,
  now,
}: {
  detail: CardDetail;
  settings: LessonSettings;
  now: number;
}) {
  const { card, timers, my_block: block } = detail;
  const primarySet =
    !!block.current_status ||
    (detail.task_mode === "CARD_ENTRY" &&
      detail.entry?.accepted_delay_ms != null);
  const primary = remaining(timers.primary_deadline_at, now);
  const processing = remaining(timers.processing_deadline_at, now);
  const own = card.notification_list.find((service) => service.is_own);
  const has = (code: string) => card.modifiers.some((modifier) => modifier.code === code);
  const statusText = (service: (typeof card.notification_list)[number]) =>
    service.last_status
      ? `${new Date(service.last_status.at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })} ${
          strings.statusLabels[service.last_status.status as keyof typeof strings.statusLabels] ?? service.last_status.status
        }`
      : strings.noStatus;
  const flagged = has("VICTIMS") || has("FATALITIES");
  // Laid out like the saved card on the ДДС АРМ-112 (memo «Работа на АРМ-112», pp. 23-24).
  return (
    <article className={`${styles.card} ${arm.saved}`} aria-label={card.card_number}>
      <div className={arm.savedHead}>
        <div className={arm.phone}>
          <span>АОН</span>
          <strong>{card.applicant.phone || "+7 (   )   -  -"}</strong>
        </div>
        <div className={arm.phone}>
          <span>предоставленный</span>
          <strong>{card.applicant.phone || "+7 (   )   -  -"}</strong>
        </div>
        <div className={arm.number}>
          <strong>Происшествие {card.card_number}</strong>
          <span>
            Сохр. {new Date(card.registered_at).toLocaleString("ru-RU")}
            {card.origin !== "EXTERNAL_SYSTEM" && card.operator_workstation
              ? ` · ${strings.operator} ${card.operator_workstation}`
              : ""}
          </span>
        </div>
        <div
          className={`${arm.clock} ${!primarySet && (primary ?? 1) < 0 ? arm.late : ""}`}
          data-testid="primary-timer"
          title={`${strings.processingTimer}: ${countdown(processing)}`}
        >
          <b>{primarySet ? "✓" : countdown(primary)}</b>
          <small>
            <span>
              {primarySet
                ? detail.task_mode === "CARD_ENTRY"
                  ? strings.incomingAccepted
                  : strings.primarySet
                : "до первичного статуса"}
            </span>
          </small>
        </div>
      </div>
      <p className={arm.processing} data-testid="processing-timer">
        {detail.state === "CLOSED"
          ? `✓ ${strings.cardClosed}`
          : `${strings.processingTimer}: ${countdown(processing)}`}
      </p>
      <div className={arm.savedBody}>
        <div className={arm.left}>
          <div className={arm.panel}>
            <strong>{card.applicant.name || strings.notSpecified}</strong>
            <Hint kind="applicant" enabled={settings.hints_enabled} />
          </div>
          <div className={`${arm.panel} ${arm.addressRow}`}>
            <strong>{card.address.raw}</strong>
            {card.address.clarification && <em>{card.address.clarification}</em>}
            <Hint kind="address" enabled={settings.hints_enabled} />
          </div>
          <div className={`${arm.panel} ${arm.grow}`}>
            <span className={arm.label}>Описание со слов заявителя</span>
            <p className={styles.description}>{card.description}</p>
            <Hint kind="description" enabled={settings.hints_enabled} />
          </div>
        </div>
        <div className={arm.right}>
          <div className={arm.flags}>
            <span>
              Пострадавшие: <b className={flagged ? arm.yes : undefined}>{flagged ? "да" : "нет"}</b>
            </span>
            <span>
              Угроза людям:{" "}
              <b className={has("THREAT_TO_PEOPLE") ? arm.yes : undefined}>{has("THREAT_TO_PEOPLE") ? "да" : "нет"}</b>
            </span>
            <span>
              Заблокированные:{" "}
              <b className={has("NO_ACCESS") ? arm.yes : undefined}>{has("NO_ACCESS") ? "да" : "нет"}</b>
            </span>
          </div>
          <div className={arm.incidentHead}>
            <span>{card.incident_type_name}</span>
          </div>
          {card.origin === "EXTERNAL_SYSTEM" ? (
            <div className={arm.panel}>
              <p className={styles.readonly}>{strings.externalCard}</p>
            </div>
          ) : (
            <div className={arm.panel}>
              <strong>
                {card.attributes.join(". ")}
                {card.attributes.length ? "." : ""}
              </strong>
              <Hint kind="attributes" enabled={settings.hints_enabled} />
            </div>
          )}
          <div className={arm.panel}>
            <span>
              Класс.: <strong>{card.incident_type_name};</strong>
            </span>
          </div>
          {card.modifiers
            .filter((m) => !["VICTIMS", "FATALITIES", "THREAT_TO_PEOPLE", "NO_ACCESS"].includes(m.code))
            .map((modifier) => (
              <p className={styles.alarm} key={modifier.code}>
                ⚠ {modifier.label}
              </p>
            ))}
        </div>
      </div>
      <div className={arm.darkBar} aria-label={strings.notifications}>
        <span className={arm.barLabel}>Службы:</span>
        <div className={arm.serviceTabs}>
          {own && (
            <span className={`${arm.darkTab} ${arm.ownTab}`} title={`${own.service_name} — ${strings.ownService}`}>
              <strong>{own.service_name}</strong>
              <small>{statusText(own)}</small>
            </span>
          )}
          {card.notification_list
            .filter((service) => !service.is_own)
            .map((service) => (
              <span className={arm.darkTab} key={service.service_code} title={service.service_name}>
                <strong>{service.service_name}</strong>
                <small>{statusText(service)}</small>
              </span>
            ))}
        </div>
      </div>
      <Hint kind="notifications" enabled={settings.hints_enabled} />
    </article>
  );
}
