import type { ReactNode } from "react";
import { strings } from "../lib/strings";
import styles from "./Common.module.css";

export function TableScroll({ children }: { children: ReactNode }) {
  return (
    <div
      className={styles.tableScroll}
      role="region"
      aria-label={strings.scrollTable}
      tabIndex={0}
    >
      {children}
    </div>
  );
}
