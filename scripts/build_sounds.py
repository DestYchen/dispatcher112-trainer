"""Build the three local non-speech notification sounds, without network access."""
import math
import struct
import wave
from pathlib import Path

folder = Path(__file__).resolve().parents[1] / 'frontend' / 'public' / 'sounds'
folder.mkdir(parents=True, exist_ok=True)
rate = 22050
for name, frequency, duration in [('new', 740, 0.2), ('warning', 880, 0.35), ('expired', 220, 0.4)]:
    samples = []
    for index in range(round(rate * duration)):
        time = index / rate
        envelope = min(1, time / 0.01, (duration - time) / 0.03)
        if name == 'warning' and 0.12 <= time < 0.23:
            envelope = 0
        samples.append(struct.pack('<h', round(10000 * envelope * math.sin(2 * math.pi * frequency * time))))
    with wave.open(str(folder / (name + '.wav')), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(b''.join(samples))
