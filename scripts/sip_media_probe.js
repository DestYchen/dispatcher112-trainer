// A known signal traverses the actual WebRTC codec, SRTP, PBX echo and jitter buffer.
// Both source and returned audio are sampled on the same AudioContext clock.
async () => {
  const pc = window.sipTestConnections.at(-1);
  if (pc.connectionState !== "connected") throw new Error("Media is not connected");
  const context = new AudioContext({ latencyHint: "interactive", sampleRate: 48000 });
  await context.resume();
  const sender = pc.getSenders().find(sender => sender.track?.kind === "audio");
  const original = sender.track;
  const destination = context.createMediaStreamDestination();
  await sender.replaceTrack(destination.stream.getAudioTracks()[0]);
  const returned = context.createMediaStreamSource(new MediaStream(pc.getReceivers().map(receiver => receiver.track)));
  const merger = context.createChannelMerger(2);
  const processor = context.createScriptProcessor(1024, 2, 1);
  const source = context.createBufferSource();
  const duration = 5, sampleRate = context.sampleRate;
  const buffer = context.createBuffer(1, duration * sampleRate, sampleRate);
  const signal = buffer.getChannelData(0);
  for (let pulse = 0; pulse < 12; pulse++) {
    const start = Math.round((1 + pulse * 0.3) * sampleRate);
    for (let i = 0; i < sampleRate * 0.05; i++) signal[start + i] = 0.45 * Math.sin(2 * Math.PI * 800 * i / sampleRate);
  }
  source.buffer = buffer;
  source.connect(destination);
  source.connect(merger, 0, 0);
  returned.connect(merger, 0, 1);
  merger.connect(processor);
  processor.connect(context.destination);
  let offset = 0;
  const starts = [[], []], last = [-sampleRate, -sampleRate];
  processor.onaudioprocess = event => {
    for (let channel = 0; channel < 2; channel++) {
      const data = event.inputBuffer.getChannelData(channel);
      for (let i = 0; i < data.length; i++) {
        if (Math.abs(data[i]) < 0.06) continue;
        if (offset + i - last[channel] > sampleRate * 0.2) starts[channel].push(offset + i);
        last[channel] = offset + i;
      }
    }
    offset += event.inputBuffer.length;
  };
  source.start(context.currentTime + 0.2);
  await new Promise(resolve => setTimeout(resolve, (duration + 0.5) * 1000));
  const delays = starts[0].map(start => {
    const returned = starts[1].find(value => value > start && value < start + sampleRate * 0.3);
    return returned === undefined ? null : (returned - start) * 1000 / sampleRate;
  });
  await sender.replaceTrack(original);
  processor.disconnect(); merger.disconnect(); source.disconnect(); returned.disconnect();
  await context.close();
  const stats = await pc.getStats();
  return {measurement: "audio round trip through actual SRTP and codec", sample_rate: sampleRate,
    source_pulses: starts[0].length, returned_pulses: starts[1].length, delays_ms: delays,
    media: [...stats.values()].filter(item => ["inbound-rtp", "outbound-rtp"].includes(item.type))};
}
