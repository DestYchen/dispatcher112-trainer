import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { EntryCard } from "../lib/cardTypes";
import styles from "./Arm.module.css";

type Incident = { id: string; group_id?: string; name: string; attributes: Record<string, string> };
type Directory = {
  groups: { id: string; name: string }[];
  services: { code: string; name: string }[];
  modifiers: { code: string; label: string }[];
};
const LEVELS = ["level1", "level2", "level3"] as const;
const LEVEL_TITLES = ["Признак", "Уточнение", "Характер"];

/**
 * Card entry laid out like the real АРМ-112: address and caller's words on the left, the
 * «что случилось?» questionnaire on the right (tag buttons from the real classifier), and the
 * notification list as the orange service bar. Tag clicks narrow the classifier to one final
 * type; services then follow automatically, as in ПОВ-112.
 */
export function ArmEntryEditor({
  card,
  onChange,
  disabled,
  actions,
}: {
  card: EntryCard;
  onChange: (card: EntryCard) => void;
  disabled?: boolean;
  actions: React.ReactNode;
}) {
  const directory = useQuery({
    queryKey: ["entry-directory"],
    queryFn: () => api<Directory>("/student/entry-directory"),
  });
  const chosen = useQuery({
    queryKey: ["entry-type", card.incident_type_id],
    enabled: !!card.incident_type_id,
    queryFn: () => api<Incident>(`/student/entry-types/${card.incident_type_id}`),
  });
  const [group, setGroup] = useState("");
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<Partial<Record<(typeof LEVELS)[number], string>>>({});
  const [adding, setAdding] = useState(false);
  const [serviceQuery, setServiceQuery] = useState("");
  const manual = useRef(new Set<string>());
  const activeGroup = group || chosen.data?.group_id || "";
  const types = useQuery({
    queryKey: ["entry-types", activeGroup],
    enabled: !!activeGroup,
    queryFn: () => api<{ items: Incident[] }>(`/student/entry-types?group_id=${activeGroup}&q=`),
  });

  // Restore the tag path when a saved draft already has a type.
  useEffect(() => {
    if (chosen.data && !Object.keys(picked).length) {
      setPicked({ ...chosen.data.attributes });
    }
  }, [chosen.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const matching = useMemo(
    () =>
      (types.data?.items ?? []).filter((row) =>
        LEVELS.every((level) => !picked[level] || row.attributes[level] === picked[level]),
      ),
    [types.data, picked],
  );
  // The next unanswered level with more than one option becomes the next row of buttons.
  const rows = LEVELS.map((level, index) => {
    const upstream = (types.data?.items ?? []).filter((row) =>
      LEVELS.slice(0, index).every((prev) => !picked[prev] || row.attributes[prev] === picked[prev]),
    );
    const options = [...new Set(upstream.map((row) => row.attributes[level]).filter(Boolean))];
    return { level, index, options };
  }).filter((row, i, all) => row.options.length > 0 && all.slice(0, i).every((prev) => picked[prev.level] || prev.options.length === 0));

  // One matching type = final type; pick it and pull its services.
  useEffect(() => {
    if (disabled) return;
    const exact = matching.length === 1 ? matching[0] : null;
    const id = exact?.id ?? null;
    if (id !== card.incident_type_id && (exact || Object.keys(picked).length)) {
      onChange({ ...card, incident_type_id: id });
    }
  }, [matching]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (disabled || !card.incident_type_id) return;
    const params = new URLSearchParams({ incident_type_id: card.incident_type_id });
    card.modifiers.forEach((code) => params.append("modifiers", code));
    let cancelled = false;
    void api<{ service_codes: string[] }>(`/student/entry-services?${params}`).then((result) => {
      if (cancelled) return;
      const merged = [...new Set([...result.service_codes, ...manual.current])];
      if (merged.join() !== card.notified_services.join()) onChange({ ...card, notified_services: merged });
    });
    return () => {
      cancelled = true;
    };
  }, [card.incident_type_id, card.modifiers.join()]); // eslint-disable-line react-hooks/exhaustive-deps

  const groups = (directory.data?.groups ?? []).filter((row) =>
    row.name.toLocaleLowerCase("ru-RU").includes(query.toLocaleLowerCase("ru-RU")),
  );
  const groupName = directory.data?.groups.find((row) => row.id === activeGroup)?.name;
  const serviceName = (code: string) => directory.data?.services.find((row) => row.code === code)?.name ?? code;
  const toggleModifier = (code: string) =>
    onChange({
      ...card,
      modifiers: card.modifiers.includes(code) ? card.modifiers.filter((m) => m !== code) : [...card.modifiers, code],
    });
  const choose = (level: (typeof LEVELS)[number], value: string) => {
    const index = LEVELS.indexOf(level);
    const next: typeof picked = {};
    LEVELS.slice(0, index).forEach((prev) => (next[prev] = picked[prev]));
    if (picked[level] !== value) next[level] = value;
    setPicked(next);
  };
  const resetGroup = () => {
    setGroup("");
    setPicked({});
    manual.current.clear();
    onChange({ ...card, incident_type_id: null, notified_services: [] });
  };
  const set = (patch: Partial<EntryCard>) => onChange({ ...card, ...patch });

  return (
    <div className={styles.arm}>
      <div className={styles.phones}>
        <label className={styles.phone}>
          <span>АОН</span>
          <input
            type="tel"
            maxLength={64}
            disabled={disabled}
            placeholder="+7 (   )   -  -"
            value={card.applicant.phone}
            onChange={(e) => set({ applicant: { ...card.applicant, phone: e.target.value } })}
          />
        </label>
        <label className={styles.phone}>
          <span>предоставленный</span>
          <input disabled placeholder="+7 (   )   -  -" value={card.applicant.phone} readOnly />
        </label>
        <label className={styles.phone}>
          <span>телефон на место</span>
          <input disabled placeholder="+7 (   )   -  -" readOnly />
        </label>
      </div>

      <div className={styles.columns}>
        <div className={styles.left}>
          <div className={styles.panel}>
            <input
              className={styles.applicant}
              maxLength={255}
              disabled={disabled}
              placeholder="Фамилия и имя заявителя"
              value={card.applicant.name}
              onChange={(e) => set({ applicant: { ...card.applicant, name: e.target.value } })}
            />
          </div>
          <div className={`${styles.panel} ${styles.address}`}>
            <label>
              <span className={styles.label}>Адрес:</span>
              <input
                maxLength={500}
                disabled={disabled}
                placeholder="Москва, улица, дом"
                value={card.address.raw}
                onChange={(e) => set({ address: { ...card.address, raw: e.target.value } })}
              />
            </label>
            <div className={styles.addressGrid} aria-hidden>
              {["Страна", "Субъект", "Населённый пункт", "Улица", "Дом/Вл.", "Корпус", "Подъезд", "Этаж", "Код"].map((name) => (
                <span key={name}>{name}:</span>
              ))}
            </div>
            <label>
              <span className={styles.label}>Описательный адрес / уточнение:</span>
              <textarea
                rows={2}
                maxLength={1000}
                disabled={disabled}
                placeholder="корпус, подъезд, этаж, код; ориентиры"
                value={card.address.clarification}
                onChange={(e) => set({ address: { ...card.address, clarification: e.target.value } })}
              />
            </label>
          </div>
          <div className={`${styles.panel} ${styles.grow}`}>
            <label>
              <span className={styles.label}>Описание со слов заявителя</span>
              <textarea
                rows={7}
                maxLength={1999}
                disabled={disabled}
                placeholder="введите"
                value={card.description}
                onChange={(e) => set({ description: e.target.value })}
              />
            </label>
            <span className={styles.counter}>{card.description.length} / 1999</span>
          </div>
        </div>

        <div className={styles.right}>
          <div className={styles.modifiers}>
            {directory.data?.modifiers.map((row) => (
              <button
                type="button"
                key={row.code}
                disabled={disabled}
                aria-pressed={card.modifiers.includes(row.code)}
                className={card.modifiers.includes(row.code) ? styles.on : undefined}
                onClick={() => toggleModifier(row.code)}
              >
                {row.label}
              </button>
            ))}
          </div>

          {!activeGroup ? (
            <div className={styles.panel}>
              <span className={styles.label}>Введите тип происшествия</span>
              <input
                className={styles.what}
                disabled={disabled}
                placeholder="что случилось?"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <div className={styles.tags}>
                {groups.map((row) => (
                  <button type="button" key={row.id} disabled={disabled} onClick={() => setGroup(row.id)}>
                    {row.name}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className={styles.incident}>
              <div className={styles.incidentHead}>
                <span>Происшествие: {groupName}</span>
                {!disabled && (
                  <button type="button" aria-label="Сменить тип происшествия" onClick={resetGroup}>
                    ✕
                  </button>
                )}
              </div>
              {rows.map((row) => (
                <div className={styles.question} key={row.level}>
                  <span>{LEVEL_TITLES[row.index]}</span>
                  <div className={styles.tags}>
                    {row.options.map((value) => (
                      <button
                        type="button"
                        key={value}
                        disabled={disabled}
                        aria-pressed={picked[row.level] === value}
                        className={picked[row.level] === value ? styles.on : undefined}
                        onClick={() => choose(row.level, value)}
                      >
                        {value}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              <p className={styles.final}>
                {card.incident_type_id && chosen.data
                  ? <>Итоговый тип: <strong>{chosen.data.name}</strong></>
                  : `Уточните признаки: подходит типов — ${matching.length}`}
              </p>
            </div>
          )}
        </div>
      </div>

      <div className={styles.serviceBar}>
        <span className={styles.barLabel}>Службы:</span>
        <div className={styles.serviceTabs}>
          {card.notified_services.map((code) => (
            <span className={styles.serviceTab} key={code}>
              <span aria-hidden>☎</span> {serviceName(code)}
              {!disabled && manual.current.has(code) && (
                <button
                  type="button"
                  aria-label={`Убрать ${serviceName(code)}`}
                  onClick={() => {
                    manual.current.delete(code);
                    set({ notified_services: card.notified_services.filter((c) => c !== code) });
                  }}
                >
                  ✕
                </button>
              )}
            </span>
          ))}
          {!disabled && (
            <button type="button" className={styles.add} aria-label="Добавить службу" onClick={() => setAdding(true)}>
              +
            </button>
          )}
        </div>
        <div className={styles.barActions}>{actions}</div>
      </div>

      {adding && (
        <div className={styles.modalBackdrop} role="dialog" aria-label="Добавьте службы">
          <div className={styles.modal}>
            <button type="button" className={styles.close} aria-label="Закрыть" onClick={() => setAdding(false)}>
              ✕
            </button>
            <h2>Добавьте службы</h2>
            <input autoFocus placeholder="Поиск …" value={serviceQuery} onChange={(e) => setServiceQuery(e.target.value)} />
            <ul>
              {directory.data?.services
                .filter((row) => !card.notified_services.includes(row.code))
                .filter((row) => row.name.toLocaleLowerCase("ru-RU").includes(serviceQuery.toLocaleLowerCase("ru-RU")))
                .map((row) => (
                  <li key={row.code}>
                    <button
                      type="button"
                      onClick={() => {
                        manual.current.add(row.code);
                        set({ notified_services: [...card.notified_services, row.code] });
                      }}
                    >
                      {row.name}
                    </button>
                  </li>
                ))}
            </ul>
            <button type="button" className={styles.saveClose} onClick={() => setAdding(false)}>
              Сохранить и закрыть
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
