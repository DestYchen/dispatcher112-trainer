import { useQuery } from "@tanstack/react-query";
import { strings } from "./lib/strings";
import styles from "./App.module.css";

async function getHealth(): Promise<{ requestId: string | null }> {
  const response = await fetch("/healthz", {
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new Error(strings.error);
  const data: unknown = await response.json();
  if (
    !data ||
    typeof data !== "object" ||
    !("status" in data) ||
    data.status !== "ok"
  ) {
    throw new Error(strings.invalidResponse);
  }
  return { requestId: response.headers.get("x-request-id") };
}

export function HealthScreen() {
  const health = useQuery({ queryKey: ["health"], queryFn: getHealth });

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <strong className={styles.brand}>{strings.brand}</strong>
        <span className={styles.training}>{strings.training}</span>
        <span className={styles.product}>{strings.product}</span>
        <span className={styles.local}>{strings.local}</span>
      </header>
      <div className={styles.statusbar}>{strings.system}</div>
      <main className={styles.main}>
        <section className={styles.panel} aria-labelledby="heading">
          <div className={styles.panelHeading}>
            <h1 id="heading">{strings.heading}</h1>
            <p>{strings.subtitle}</p>
          </div>
          <div className={styles.status} role="status" aria-live="polite">
            {health.isPending ? (
              <p>{strings.checking}</p>
            ) : health.isError ? (
              <>
                <h2 className={styles.error}>× {strings.error}</h2>
                <p>{strings.errorDescription}</p>
              </>
            ) : (
              <>
                <h2 className={styles.ok}>✓ {strings.ready}</h2>
                <p>{strings.readyDescription}</p>
                {health.data.requestId && (
                  <p className={styles.requestId}>
                    {strings.requestId}: {health.data.requestId}
                  </p>
                )}
              </>
            )}
          </div>
          <div className={styles.actions}>
            <button
              disabled={health.isFetching}
              onClick={() => void health.refetch()}
            >
              {strings.retry}
            </button>
          </div>
        </section>
      </main>
      <footer className={styles.footer}>{strings.disclaimer}</footer>
    </div>
  );
}
