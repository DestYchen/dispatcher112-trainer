import { Grammar } from "sip.js";
import { describe, expect, it } from "vitest";

describe("local PBX addressing", () => {
  it("uses a valid SIP hostname instead of Docker's numeric container id", () => {
    expect(
      Grammar.parse("sip:caller@telephony:5060;transport=ws", "SIP_URI"),
    ).not.toBe(-1);
    expect(
      Grammar.parse("sip:caller@9c8c20659856:5060;transport=ws", "SIP_URI"),
    ).toBe(-1);
  });
});
