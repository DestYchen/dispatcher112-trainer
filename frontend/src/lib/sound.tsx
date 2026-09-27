import { useState } from "react";
import { strings } from "./strings";
import styles from "../components/Workspace.module.css";

type SoundName = "new" | "warning" | "expired";
// eslint-disable-next-line react-refresh/only-export-components
export function playSound(name: SoundName) {
  const settings = JSON.parse(
    localStorage.getItem("dispatcher112-sound") ??
      '{"volume":0.4,"muted":false}',
  ) as { volume: number; muted: boolean };
  if (settings.muted) return;
  const audio = new Audio(`/sounds/${name}.wav`);
  audio.volume = Math.max(0, Math.min(1, settings.volume));
  void audio.play().catch(() => {
    /* Browser may require a user gesture before the first sound. */
  });
}
export function SoundControl() {
  const [settings, setSettings] = useState<{ volume: number; muted: boolean }>(
    () =>
      JSON.parse(
        localStorage.getItem("dispatcher112-sound") ??
          '{"volume":0.4,"muted":false}',
      ),
  );
  const save = (next: typeof settings) => {
    setSettings(next);
    localStorage.setItem("dispatcher112-sound", JSON.stringify(next));
  };
  return (
    <div className={styles.sound}>
      <label>
        {strings.volume}
        <input
          aria-label={strings.volume}
          type="range"
          min="0"
          max="1"
          step="0.1"
          value={settings.volume}
          onChange={(event) =>
            save({ ...settings, volume: Number(event.target.value) })
          }
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={settings.muted}
          onChange={(event) =>
            save({ ...settings, muted: event.target.checked })
          }
        />
        {strings.muted}
      </label>
    </div>
  );
}
