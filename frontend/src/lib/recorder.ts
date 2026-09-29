/** Push-to-talk microphone capture as 16 kHz mono 16-bit PCM (what the offline Vosk STT expects). */
export interface Recording {
  stop: () => Promise<ArrayBuffer>;
  cancel: () => void;
}

const TARGET_RATE = 16000;

export async function startRecording(): Promise<Recording> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  // ScriptProcessor is deprecated but works everywhere without a separate worklet file.
  const processor = context.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  processor.onaudioprocess = (event) => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  source.connect(processor);
  processor.connect(context.destination);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    processor.disconnect();
    source.disconnect();
    stream.getTracks().forEach((track) => track.stop());
    void context.close();
  };
  return {
    cancel: close,
    stop: async () => {
      close();
      return toPcm16(chunks, context.sampleRate);
    },
  };
}

function toPcm16(chunks: Float32Array[], rate: number): ArrayBuffer {
  const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const input = new Float32Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    input.set(chunk, offset);
    offset += chunk.length;
  }
  // Average-downsample to 16 kHz, then clamp to 16-bit.
  const ratio = rate / TARGET_RATE;
  const output = new Int16Array(Math.floor(input.length / ratio));
  for (let i = 0; i < output.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    const sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output.buffer;
}
