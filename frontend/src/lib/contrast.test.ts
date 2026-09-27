import { expect, it } from "vitest";
import { readFileSync } from "node:fs";

const tokens = readFileSync("src/styles/tokens.css", "utf8");

const colors = Object.fromEntries(
  Array.from(tokens.matchAll(/--([\w-]+):\s*(#[\da-f]{6});/gi), (item) => [
    item[1],
    item[2],
  ]),
);
function luminance(hex: string) {
  const rgb = [1, 3, 5]
    .map((index) => parseInt(hex.slice(index, index + 2), 16) / 255)
    .map((value) =>
      value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
    );
  return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
}
it.each([
  ["ink", "panel"],
  ["ink", "surface"],
  ["ink", "panel-sunken"],
  ["ink-muted", "panel"],
  ["ink-muted", "surface"],
  ["ink-muted", "panel-sunken"],
  ["ink-muted", "select-bg"],
  ["ink-muted", "dispatch-bg"],
  ["select", "select-bg"],
  ["warn", "warn-bg"],
  ["alarm", "alarm-bg"],
  ["alarm", "panel"],
  ["ok", "panel"],
  ["ok", "surface"],
  ["ink-inverse", "ok"],
  ["ink-inverse", "select"],
  ["section-heading-ink", "section-heading-bg"],
])("text contrast %s on %s is at least 4.5:1", (foreground, background) => {
  const values = [
    luminance(colors[foreground]),
    luminance(colors[background]),
  ].sort((a, b) => a - b);
  expect((values[1] + 0.05) / (values[0] + 0.05)).toBeGreaterThanOrEqual(4.5);
});
