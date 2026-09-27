import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "./api/client";
import { useAuth } from "./lib/auth";
import { strings } from "./lib/strings";
import styles from "./components/Common.module.css";
import welcome from "./components/Welcome.module.css";

export function Login() {
  const auth = useAuth();
  const [params] = useSearchParams();
  const role = params.get("showcase");
  const showcase = role === "teacher" || role === "student";
  const [login, setLogin] = useState(showcase ? `demo.${role}` : "");
  const [password, setPassword] = useState(showcase ? "Showcase112!" : "");
  const [totp, setTotp] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      await api("/auth/login", {
        method: "POST",
        body: JSON.stringify({ login, password, totp: totp || null }),
      });
      await auth.refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : strings.error);
    } finally {
      setPending(false);
    }
  }
  return (
    <div className={welcome.layout}>
      <section className={welcome.intro} aria-label={strings.product}>
        <h2>{strings.welcomeTitle}</h2>
        <p className={welcome.description}>{strings.welcomeDescription}</p>
        <ol className={welcome.steps}>
          {strings.welcomeSteps.map((step, index) => (
            <li key={step.title}>
              <span className={welcome.number} aria-hidden="true">
                {String(index + 1).padStart(2, "0")}
              </span>
              <div>
                <strong>{step.title}</strong>
                <p>{step.text}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>
      <form
        className={`${styles.form} ${welcome.form}`}
        onSubmit={(event) => void submit(event)}
      >
        <h1>{strings.signIn}</h1>
        <p className={welcome.description}>{strings.loginDescription}</p>
        {showcase && (
          <div className={welcome.demo}>
            <strong>{strings.showcaseAccess}</strong>
            <p>{strings.showcaseAccessHelp}</p>
          </div>
        )}
        <label>
          {strings.login}
          <input
            required
            maxLength={64}
            autoComplete="username"
            value={login}
            onChange={(event) => setLogin(event.target.value)}
          />
        </label>
        <label>
          {strings.password}
          <input
            required
            maxLength={256}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <label>
          {strings.totp}
          <input
            inputMode="numeric"
            pattern="[0-9]{6}"
            maxLength={6}
            autoComplete="one-time-code"
            value={totp}
            onChange={(event) => setTotp(event.target.value)}
          />
        </label>
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
        <button
          className={styles.primary}
          disabled={pending || !login || !password}
        >
          {pending ? strings.signingIn : strings.signIn}
        </button>
      </form>
    </div>
  );
}
