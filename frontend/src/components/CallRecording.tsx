import { useState } from "react";
import { strings } from "../lib/strings";

export function CallRecording({ callId }: { callId: string }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(false);
  async function download() {
    setPending(true);
    setError(false);
    try {
      const response = await fetch(
        `/api/v1/sip/calls/${callId}/recording?format=mp3`,
        {
          credentials: "include",
          signal: AbortSignal.timeout(65000),
        },
      );
      if (!response.ok) throw new Error("Recording unavailable");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `call-${callId}.mp3`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setError(true);
    } finally {
      setPending(false);
    }
  }
  return (
    <div>
      <audio
        controls
        preload="none"
        src={`/api/v1/sip/calls/${callId}/recording`}
      />
      <button type="button" disabled={pending} onClick={() => void download()}>
        {pending ? strings.recordingPreparing : strings.recordingMp3}
      </button>
      {error && <p role="alert">{strings.recordingFailed}</p>}
    </div>
  );
}
