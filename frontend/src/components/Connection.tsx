import { strings } from "../lib/strings";
import styles from "./Workspace.module.css";

export function Connection({
  connected,
  retryIn,
  reconnect,
}: {
  connected: boolean;
  retryIn: number;
  reconnect: () => void;
}) {
  if (connected) return <span className={styles.ok}>{strings.connected}</span>;
  return (
    <div className={styles.connection} role="status">
      <span>
        {strings.connectionLost} {retryIn} {strings.secondsShort}
      </span>
      <button onClick={reconnect}>{strings.retryNow}</button>
    </div>
  );
}
