import { useMutation, useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api } from "../api/client";
import type { EntryCard } from "../lib/cardTypes";
import { strings } from "../lib/strings";
import { AsyncView } from "./AsyncView";
import { CommentInput } from "./CommentInput";
import common from "./Common.module.css";
import styles from "./Workspace.module.css";

type Incident = {
  id: string;
  group_id?: string;
  name: string;
  attributes: Record<string, string>;
};
export function EntryEditor({
  card,
  onChange,
  disabled,
  assignmentId,
  descriptionLimit = 10000,
}: {
  card: EntryCard;
  onChange: (card: EntryCard) => void;
  disabled?: boolean;
  assignmentId?: string;
  descriptionLimit?: number;
}) {
  const [chosenGroup, setGroup] = useState("");
  const [search, setSearch] = useState("");
  const [serviceSearch, setServiceSearch] = useState("");
  const addressRef = useRef<HTMLTextAreaElement>(null);
  const descriptionRef = useRef<HTMLTextAreaElement>(null);
  const directory = useQuery({
    queryKey: ["entry-directory"],
    queryFn: () =>
      api<{
        groups: { id: string; name: string }[];
        services: { code: string; name: string }[];
        modifiers: { code: string; label: string }[];
      }>("/student/entry-directory"),
  });
  const chosen = useQuery({
    queryKey: ["entry-type", card.incident_type_id],
    enabled: !!card.incident_type_id,
    queryFn: () =>
      api<Incident>(`/student/entry-types/${card.incident_type_id}`),
  });
  const group = chosenGroup || chosen.data?.group_id || "";
  const incidents = useQuery({
    queryKey: ["entry-types", group, search],
    enabled: !!group,
    queryFn: () =>
      api<{ items: Incident[] }>(
        `/student/entry-types?group_id=${group}&q=${encodeURIComponent(search)}`,
      ),
  });
  const suggested = useMutation({
    mutationFn: async () => {
      const params = new URLSearchParams({
        incident_type_id: card.incident_type_id!,
      });
      card.modifiers.forEach((code) => params.append("modifiers", code));
      const result = await api<{ service_codes: string[] }>(
        `/student/entry-services?${params}`,
      );
      onChange({ ...card, notified_services: result.service_codes });
    },
  });
  const services =
    directory.data?.services.filter((row) =>
      row.name
        .toLocaleLowerCase("ru-RU")
        .includes(serviceSearch.toLocaleLowerCase("ru-RU")),
    ) ?? [];
  const toggle = (
    field: "modifiers" | "notified_services",
    code: string,
    checked: boolean,
  ) =>
    onChange({
      ...card,
      [field]: checked
        ? [...card[field], code]
        : card[field].filter((item) => item !== code),
    });
  return (
    <div className={`${common.form} ${styles.entryForm}`}>
      <div className={styles.cardBody}>
        <div className={styles.section}>
          <h2>{strings.applicant}</h2>
          <label>
            {strings.applicantName}
            <input
              maxLength={255}
              disabled={disabled}
              value={card.applicant.name}
              onChange={(event) =>
                onChange({
                  ...card,
                  applicant: { ...card.applicant, name: event.target.value },
                })
              }
            />
          </label>
          <label>
            {strings.applicantPhone}
            <input
              type="tel"
              maxLength={64}
              disabled={disabled}
              value={card.applicant.phone}
              onChange={(event) =>
                onChange({
                  ...card,
                  applicant: { ...card.applicant, phone: event.target.value },
                })
              }
            />
          </label>
          <h2>{strings.address}</h2>
          {assignmentId && !disabled ? (
            <CommentInput
              assignmentId={assignmentId}
              field="address"
              text={card.address.raw}
              setText={(raw) =>
                onChange({ ...card, address: { ...card.address, raw } })
              }
              inputRef={addressRef}
              required={false}
            />
          ) : (
            <label>
              {strings.address}
              <textarea
                maxLength={500}
                aria-label={strings.address}
                disabled={disabled}
                value={card.address.raw}
                onChange={(event) =>
                  onChange({
                    ...card,
                    address: { ...card.address, raw: event.target.value },
                  })
                }
              />
            </label>
          )}
          <label>
            {strings.addressClarification}
            <input
              maxLength={1000}
              disabled={disabled}
              value={card.address.clarification}
              onChange={(event) =>
                onChange({
                  ...card,
                  address: {
                    ...card.address,
                    clarification: event.target.value,
                  },
                })
              }
            />
          </label>
          <h2>{strings.description}</h2>
          {assignmentId && !disabled ? (
            <CommentInput
              assignmentId={assignmentId}
              field="description"
              text={card.description}
              setText={(description) => onChange({ ...card, description })}
              inputRef={descriptionRef}
              required={false}
            />
          ) : (
            <label>
              {strings.description}
              <textarea
                rows={4}
                aria-label={strings.description}
                maxLength={descriptionLimit}
                disabled={disabled}
                value={card.description}
                onChange={(event) =>
                  onChange({ ...card, description: event.target.value })
                }
              />
            </label>
          )}
        </div>
        <div className={styles.section}>
          <h2>{strings.incidentType}</h2>
          <AsyncView
            loading={directory.isPending}
            error={directory.error}
            empty={
              directory.data?.groups.length === 0 && strings.noEntryDirectory
            }
            retry={() => void directory.refetch()}
          >
            <label>
              {strings.incidentGroup}
              <select
                value={group}
                aria-label={strings.incidentGroup}
                disabled={disabled}
                onChange={(event) => {
                  setGroup(event.target.value);
                  setSearch("");
                  onChange({ ...card, incident_type_id: null });
                }}
              >
                <option value="">{strings.chooseGroup}</option>
                {directory.data?.groups.map((row) => (
                  <option value={row.id} key={row.id}>
                    {row.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {strings.searchIncident}
              <input
                disabled={disabled || !group}
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            {!group ? (
              <p>{strings.chooseGroupHelp}</p>
            ) : (
              <AsyncView
                loading={incidents.isPending}
                error={incidents.error}
                empty={
                  incidents.data?.items.length === 0 && strings.noIncidentTypes
                }
                retry={() => void incidents.refetch()}
              >
                <label>
                  {strings.incidentType}
                  <select
                    value={card.incident_type_id ?? ""}
                    aria-label={strings.incidentType}
                    disabled={disabled}
                    onChange={(event) =>
                      onChange({
                        ...card,
                        incident_type_id: event.target.value || null,
                      })
                    }
                  >
                    <option value="">{strings.chooseIncident}</option>
                    {chosen.data &&
                      !incidents.data?.items.some(
                        (row) => row.id === chosen.data.id,
                      ) && (
                        <option value={chosen.data.id}>
                          {chosen.data.name}
                        </option>
                      )}
                    {incidents.data?.items.map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.name}
                      </option>
                    ))}
                  </select>
                </label>
              </AsyncView>
            )}
            {card.incident_type_id && (
              <AsyncView
                loading={chosen.isPending}
                error={chosen.error}
                retry={() => void chosen.refetch()}
              >
                <div className={styles.tags}>
                  {Object.entries(chosen.data?.attributes ?? {}).map(
                    ([key, label]) => (
                      <span className={styles.tag} key={key}>
                        {label}
                      </span>
                    ),
                  )}
                </div>
              </AsyncView>
            )}
            <h2>{strings.specialModifiers}</h2>
            {directory.data?.modifiers.map((row) => (
              <label key={row.code}>
                <input
                  type="checkbox"
                  disabled={disabled}
                  checked={card.modifiers.includes(row.code)}
                  onChange={(event) =>
                    toggle("modifiers", row.code, event.target.checked)
                  }
                />
                {row.label}
              </label>
            ))}
          </AsyncView>
        </div>
      </div>
      <section className={`${styles.section} ${styles.dispatchSection}`}>
        <h2>{strings.notifications}</h2>
        <AsyncView
          loading={directory.isPending}
          error={directory.error}
          empty={
            directory.data?.services.length === 0 && strings.noEntryDirectory
          }
          retry={() => void directory.refetch()}
        >
          <div className={styles.tags}>
            {card.notified_services.map((code) => (
              <span className={styles.tag} key={code}>
                {directory.data?.services.find((row) => row.code === code)
                  ?.name ?? code}
              </span>
            ))}
          </div>
          {card.notified_services.length === 0 && (
            <p>{strings.noSelectedServices}</p>
          )}
          <button
            type="button"
            disabled={disabled || !card.incident_type_id || suggested.isPending}
            onClick={() => suggested.mutate()}
          >
            {suggested.isPending ? strings.loading : strings.suggestedServices}
          </button>
          {suggested.error && <p role="alert">{suggested.error.message}</p>}
          <label>
            {strings.searchService}
            <input
              value={serviceSearch}
              onChange={(event) => setServiceSearch(event.target.value)}
            />
          </label>
          <AsyncView empty={services.length === 0 && strings.noServicesFound}>
            <div
              className={styles.entryServices}
              tabIndex={0}
              role="region"
              aria-label={strings.notifications}
            >
              {services.map((row) => (
                <label key={row.code}>
                  <input
                    type="checkbox"
                    disabled={disabled}
                    checked={card.notified_services.includes(row.code)}
                    onChange={(event) =>
                      toggle(
                        "notified_services",
                        row.code,
                        event.target.checked,
                      )
                    }
                  />
                  {row.name}
                </label>
              ))}
            </div>
          </AsyncView>
        </AsyncView>
      </section>
    </div>
  );
}
