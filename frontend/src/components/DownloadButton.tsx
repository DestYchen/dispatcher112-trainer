import { useMutation } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { exchangeStrings as t } from "../lib/strings";

export function DownloadButton({
  path,
  filename,
  children,
}: {
  path: string;
  filename: string;
  children: ReactNode;
}) {
  const download = useMutation({
    mutationFn: async () => {
      const response = await fetch(`/api/v1${path}`, {
        credentials: "include",
        signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) throw new Error(t.failed);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  });
  return (
    <>
      <button
        type="button"
        disabled={download.isPending}
        onClick={() => download.mutate()}
      >
        {download.isPending ? t.preparing : children}
      </button>
      {download.error && <p role="alert">{download.error.message}</p>}
    </>
  );
}
