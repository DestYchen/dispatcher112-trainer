import { useState } from "react";
import { SoundControl } from "./lib/sound";
import { readActions } from "./lib/offline";
import { useQueryClient } from "@tanstack/react-query";
import { AdminHome } from "./Admin";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { api } from "./api/client";
import { AsyncView } from "./components/AsyncView";
import { Student } from "./Student";
import { TeacherHome } from "./Teacher";
import { Login } from "./Login";
import { SipConsole } from "./components/SipConsole";
import { Learning } from "./Learning";
import { useAuth, type AuthUser } from "./lib/auth";
import { learningStrings, strings } from "./lib/strings";
import styles from "./App.module.css";
import common from "./components/Common.module.css";

const routeByRole = {
  STUDENT: "/student",
  TEACHER: "/teacher",
  ADMIN: "/admin",
};
function RolePage({ role, user }: { role: AuthUser["role"]; user: AuthUser }) {
  if (role !== user.role)
    return <Navigate replace to={routeByRole[user.role]} />;
  return role === "TEACHER" ? (
    <TeacherHome />
  ) : role === "ADMIN" ? (
    <AdminHome />
  ) : (
    <Student />
  );
}
export function App() {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [logoutError, setLogoutError] = useState<string | null>(null);
  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <strong className={styles.brand}>{strings.brand}</strong>
        <span className={styles.training}>{strings.training}</span>
        <span className={styles.product}>
          {auth.user?.service?.name ?? strings.product}
        </span>
        <span className={styles.local}>
          {auth.user
            ? `${strings.roleLabels[auth.user.role]} · ${auth.user.short_name}`
            : strings.local}
        </span>
        {auth.user && <SoundControl />}
        {auth.user && (
          <button
            onClick={() =>
              void (async () => {
                if (readActions(auth.user!.id).length) {
                  setLogoutError(strings.pendingBeforeLogout);
                  return;
                }
                try {
                  await api("/auth/logout", { method: "POST" });
                  queryClient.clear();
                  await auth.refresh();
                  setLogoutError(null);
                } catch (error) {
                  setLogoutError(
                    error instanceof Error ? error.message : strings.error,
                  );
                }
              })()
            }
          >
            {strings.logout}
          </button>
        )}
      </header>
      {auth.user && auth.user.role !== "ADMIN" && (
        <div className={styles.utility}>
          <nav
            className={styles.navigation}
            aria-label={learningStrings.learning}
          >
            <NavLink
              className={common.navLink}
              to={routeByRole[auth.user.role]}
            >
              {learningStrings.workplace}
            </NavLink>
            <NavLink className={common.navLink} to="/learning">
              {learningStrings.learning}
            </NavLink>
          </nav>
          <SipConsole key={auth.user.id} />
        </div>
      )}
      {logoutError && (
        <p role="alert" className={common.error}>
          {logoutError}
        </p>
      )}
      <main className={auth.user ? styles.workMain : styles.main}>
        <section className={auth.user ? styles.workPanel : styles.panel}>
          <AsyncView
            loading={auth.loading}
            error={auth.error}
            retry={() => void auth.refresh()}
          >
            {!auth.user ? (
              <Login />
            ) : (
              <div>
                <Routes>
                  <Route
                    path="/learning"
                    element={
                      auth.user.role === "ADMIN" ? (
                        <Navigate replace to="/admin" />
                      ) : (
                        <Learning />
                      )
                    }
                  />
                  {(["STUDENT", "TEACHER", "ADMIN"] as const).map((role) => (
                    <Route
                      key={role}
                      path={routeByRole[role]}
                      element={<RolePage user={auth.user!} role={role} />}
                    />
                  ))}
                  <Route
                    path="*"
                    element={
                      <Navigate replace to={routeByRole[auth.user.role]} />
                    }
                  />
                </Routes>
              </div>
            )}
          </AsyncView>
        </section>
      </main>
      <footer className={styles.footer}>{strings.disclaimer}</footer>
    </div>
  );
}
