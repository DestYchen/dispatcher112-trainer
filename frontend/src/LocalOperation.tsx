import { useMutation } from "@tanstack/react-query";
import { api } from "./api/client";
import common from "./components/Common.module.css";
import { technicalStrings as t, exchangeStrings } from "./lib/strings";

export function LocalOperation({
  id,
  completed,
}: {
  id: string;
  completed: () => void;
}) {
  const download = useMutation({
    mutationFn: async () => {
      const result = await api<{ request: unknown; filename: string }>(
        `/admin/operations/jobs/${id}/local-request`,
        { method: "POST" },
      );
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(result.request, null, 2)], {
          type: "application/json",
        }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = result.filename;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  });
  const cancel = useMutation({
    mutationFn: () =>
      api(`/admin/operations/jobs/${id}/cancel`, { method: "POST" }),
    onSuccess: completed,
  });
  return (
    <section aria-label={t.localOperation}>
      <p>{t.localOperationHelp}</p>
      <p>
        <code>
          {`python scripts/technical_operations.py execute-request --request technical-operation-${id}.json`}
        </code>
      </p>
      <p>{t.requestLifetime}</p>
      <div className={common.toolbar}>
        <button
          disabled={download.isPending || cancel.isPending}
          onClick={() => download.mutate()}
        >
          {download.isPending ? exchangeStrings.preparing : t.downloadRequest}
        </button>
        <button
          disabled={download.isPending || cancel.isPending}
          onClick={() => cancel.mutate()}
        >
          {cancel.isPending ? t.cancelling : t.cancelRequest}
        </button>
      </div>
      {download.isSuccess && <p role="status">{t.requestDownloaded}</p>}
      {download.error && <p role="alert">{download.error.message}</p>}
      {cancel.error && <p role="alert">{cancel.error.message}</p>}
    </section>
  );
}
