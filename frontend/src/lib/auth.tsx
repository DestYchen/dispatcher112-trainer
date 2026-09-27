import { createContext, useContext, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, RequestError } from "../api/client";

export interface AuthUser {
  id: string;
  login: string;
  full_name: string;
  short_name: string;
  role: "ADMIN" | "TEACHER" | "STUDENT";
  service: { code: string; name: string } | null;
}
const AuthContext = createContext<{
  user: AuthUser | null;
  loading: boolean;
  error: Error | null;
  refresh: () => Promise<void>;
} | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: ["auth"],
    queryFn: async () => {
      try {
        return await api<AuthUser>("/auth/me");
      } catch (error) {
        if (error instanceof RequestError && error.status === 401) return null;
        throw error;
      }
    },
    retry: (attempt, error) =>
      attempt < 3 && (!(error instanceof RequestError) || error.status >= 500),
  });
  return (
    <AuthContext.Provider
      value={{
        user: query.data ?? null,
        loading: query.isPending,
        error:
          query.data &&
          (!(query.error instanceof RequestError) || query.error.status >= 500)
            ? null
            : query.error,
        refresh: async () => {
          await query.refetch();
        },
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider is required");
  return value;
}
