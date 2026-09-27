import { useEffect, useRef } from "react";
import { playSound } from "../lib/sound";
import {
  answerSip,
  dialSip,
  disableSip,
  enableSip,
  hangupSip,
  useSip,
} from "../lib/sip";
import common from "./Common.module.css";
import styles from "./Sip.module.css";

const connections = {
  OFF: "Гарнитура выключена",
  CONNECTING: "Подключение телефона…",
  READY: "Телефон готов",
  ERROR: "Телефон недоступен",
};
const calls = {
  IDLE: "Нет активного вызова",
  RINGING: "Входящий учебный вызов",
  DIALING: "Соединение…",
  CONNECTED: "Идёт разговор · ведётся учебная запись",
};

export function SipConsole() {
  const phone = useSip();
  const audio = useRef<HTMLAudioElement>(null);
  useEffect(() => {
    if (phone.call !== "RINGING") return;
    playSound("new");
    const ringing = setInterval(() => playSound("new"), 3000);
    return () => clearInterval(ringing);
  }, [phone.call]);
  useEffect(
    () => () => {
      void disableSip();
    },
    [],
  );
  return (
    <section className={styles.console} aria-label="Учебный IP-телефон">
      <audio ref={audio} autoPlay />
      <div aria-live="polite">
        <strong>{connections[phone.connection]}</strong>
        <p>
          {phone.call === "CONNECTED" && phone.diagnostic
            ? "Проверка звука · ваш голос возвращается в гарнитуру"
            : calls[phone.call]}
        </p>
      </div>
      <div className={common.toolbar}>
        {phone.connection !== "READY" ? (
          <button
            disabled={phone.connection === "CONNECTING"}
            onClick={() => {
              if (audio.current) void enableSip(audio.current);
            }}
          >
            Включить гарнитуру
          </button>
        ) : (
          <>
            <button onClick={() => void disableSip()}>
              Выключить гарнитуру
            </button>
            <button
              disabled={phone.call !== "IDLE"}
              onClick={() =>
                void dialSip("sip:9000@dispatcher112").catch((error) =>
                  useSip.setState({ error: error.message }),
                )
              }
            >
              Проверить звук
            </button>
          </>
        )}
        {phone.call === "RINGING" && (
          <button className={common.primary} onClick={() => void answerSip()}>
            Ответить на вызов
          </button>
        )}
        {phone.call !== "IDLE" && (
          <button onClick={() => void hangupSip()}>Завершить вызов</button>
        )}
      </div>
      {phone.error && (
        <p role="alert" className={common.error}>
          {phone.error}
        </p>
      )}
    </section>
  );
}
