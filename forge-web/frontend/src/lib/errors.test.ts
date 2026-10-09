import { describe, expect, it } from "vitest";
import { germanError, localError } from "./errors";

describe("server messages in German", () => {
  it("translates exact messages and messages with values", () => {
    expect(localError("wrong email or password", "de")).toBe("E-Mail oder Passwort stimmt nicht.");
    expect(localError("password: use at least 10 characters", "de")).toBe("Passwort: bitte mindestens 10 Zeichen.");
    expect(localError("this needs the editor role in the project", "de")).toContain("„Bearbeiter“");
    expect(localError("git push failed: rejected", "de")).toBe("git push fehlgeschlagen: rejected");
  });

  it("keeps what it does not know, and English for English pages", () => {
    expect(localError("something new went wrong", "de")).toBe("something new went wrong");
    expect(localError("wrong email or password", "en")).toBe("wrong email or password");
    expect(germanError("constructor")).toBeNull(); // no inherited object keys
  });
});
