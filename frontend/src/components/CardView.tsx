import { strings } from "../lib/strings";
import {
  countdown,
  remaining,
  type CardDetail,
  type LessonSettings,
} from "../lib/cardTypes";
import styles from "./Workspace.module.css";

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
  return (
    <article className={styles.card} aria-label={card.card_number}>
      <header className={styles.cardHeader}>
        <div>
          <p className={styles.metadata}>{strings.incidentCard}</p>
          <h1 className={styles.number}>{card.card_number}</h1>
        </div>
        <div>
          <p className={styles.metadata}>
            {new Date(card.registered_at).toLocaleString("ru-RU")}
          </p>
          {card.origin !== "EXTERNAL_SYSTEM" && (
            <p className={styles.metadata}>
              {strings.operator} {card.operator_workstation}
            </p>
          )}
        </div>
      </header>
      <div className={styles.timerArea}>
        <div>
          <div
            className={`${styles.timer} ${(primary ?? 1) < 0 && !primarySet ? styles.alarm : ""}`}
            data-testid="primary-timer"
          >
            {primarySet ? "✓" : countdown(primary)}
          </div>
          <p className={styles.metadata}>
            {primarySet
              ? detail.task_mode === "CARD_ENTRY"
                ? strings.incomingAccepted
                : strings.primarySet
              : strings.primaryTimer}
          </p>
          {!primarySet && (
            <DeadlineBar
              seconds={primary}
              total={settings.primary_status_deadline_sec}
            />
          )}
        </div>
        <div>
          <div
            className={`${styles.timer} ${(processing ?? 1) < 0 ? styles.alarm : ""}`}
            data-testid="processing-timer"
          >
            {detail.state === "CLOSED" ? "✓" : countdown(processing)}
          </div>
          <p className={styles.metadata}>
            {detail.state === "CLOSED"
              ? strings.cardClosed
              : strings.processingTimer}
          </p>
          {detail.state !== "CLOSED" && (
            <DeadlineBar
              seconds={processing}
              total={settings.card_processing_deadline_sec}
              label={strings.processingTimer}
            />
          )}
        </div>
      </div>
      <div className={styles.cardBody}>
        <div>
          <section className={styles.section}>
            <h2>{strings.applicant}</h2>
            <dl className={styles.fieldList}>
              <div>
                <dt>{strings.applicantName}</dt>
                <dd>{card.applicant.name || strings.notSpecified}</dd>
              </div>
              <div>
                <dt>{strings.applicantPhone}</dt>
                <dd>{card.applicant.phone || strings.notSpecified}</dd>
              </div>
            </dl>
            <Hint kind="applicant" enabled={settings.hints_enabled} />
          </section>
          <section className={styles.section}>
            <h2>{strings.address}</h2>
            <p className={styles.description}>{card.address.raw}</p>
            {card.address.clarification && (
              <p className={styles.metadata}>
                <em>{card.address.clarification}</em>
              </p>
            )}
            <Hint kind="address" enabled={settings.hints_enabled} />
          </section>
          <section className={styles.section}>
            <h2>{strings.description}</h2>
            <p className={styles.description}>{card.description}</p>
            <Hint kind="description" enabled={settings.hints_enabled} />
          </section>
        </div>
        <div>
          <section className={styles.section}>
            <h2>{strings.incidentType}</h2>
            <p className={styles.description}>
              <strong>{card.incident_type_name}</strong>
            </p>
          </section>
          <section className={styles.section}>
            {card.origin === "EXTERNAL_SYSTEM" ? (
              <p className={styles.readonly}>{strings.externalCard}</p>
            ) : (
              <>
                <h2>{strings.attributes}</h2>
                <div className={styles.tags}>
                  {card.attributes.map((attribute, index) => (
                    <span className={styles.tag} key={index}>
                      {attribute}
                    </span>
                  ))}
                </div>
                <Hint kind="attributes" enabled={settings.hints_enabled} />
              </>
            )}
            {card.modifiers.map((modifier) => (
              <p className={styles.alarm} key={modifier.code}>
                ⚠ {modifier.label}
              </p>
            ))}
          </section>
        </div>
      </div>
      <section className={`${styles.section} ${styles.dispatchSection}`}>
        <h2>{strings.notifications}</h2>
        <div className={styles.dispatchList}>
          {[...card.notification_list]
            .sort((a, b) => Number(b.is_own) - Number(a.is_own))
            .map((service) => (
              <div
                key={service.service_code}
                className={service.is_own ? styles.own : styles.notification}
              >
                <strong>
                  {service.service_name}
                  {service.is_own && ` — ${strings.ownService}`}
                </strong>
                <p className={styles.metadata}>
                  {service.last_status
                    ? `${strings.statusLabels[service.last_status.status as keyof typeof strings.statusLabels] ?? service.last_status.status} · ${new Date(service.last_status.at).toLocaleTimeString("ru-RU")} · ${service.last_status.by}`
                    : strings.noStatus}
                </p>
              </div>
            ))}
        </div>
        <Hint kind="notifications" enabled={settings.hints_enabled} />
      </section>
    </article>
  );
}
