import type { ReactNode } from "react";
import { strings } from "../lib/strings";
import styles from "./Common.module.css";

export function AsyncView({
  loading,
  error,
  empty,
  retry,
  children,
}: {
  loading?: boolean;
  error?: Error | null;
  empty?: string | false;
  retry?: () => void;
  children: ReactNode;
}) {
  if (loading)
    return (
      <div className={styles.skeleton} role="status">
        {strings.loading}
        <div className={styles.skeletonRow} />
        <div className={styles.skeletonRow} />
        <div className={styles.skeletonRow} />
      </div>
    );
  if (error)
    return (
      <div className={styles.error} role="alert">
        <p>{error.message || strings.error}</p>
        <button onClick={retry}>{strings.retry}</button>
      </div>
    );
  if (empty) return <div className={styles.empty}>{empty}</div>;
  return <>{children}</>;
}
