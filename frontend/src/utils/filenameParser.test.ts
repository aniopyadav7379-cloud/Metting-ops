import { describe, expect, it } from "vitest";

import { parseFilename } from "./filenameParser";

describe("parseFilename", () => {
  describe("pattern 1: notes__ / downloads__ Mac Notes + Voice Memos export", () => {
    it.each([
      [
        "notes__2024-11-05_233322__Call with Jason Allen.m4a",
        "notes",
        "2024-11-05",
        "23:33:22",
        "Call with Jason Allen",
      ],
      [
        "notes__2024-11-14_183247__Call with John Rahaghi.m4a",
        "notes",
        "2024-11-14",
        "18:32:47",
        "Call with John Rahaghi",
      ],
      [
        "downloads__2026-05-16_212900__Call with Shafen Khan.m4a",
        "downloads",
        "2026-05-16",
        "21:29:00",
        "Call with Shafen Khan",
      ],
      [
        "downloads__2026-05-14_161900__Legacy25 Capital.m4a",
        "downloads",
        "2026-05-14",
        "16:19:00",
        "Legacy25 Capital",
      ],
    ])(
      "parses %s with confidence 1.0",
      (filename, source, expDate, expTime, expTitle) => {
        const p = parseFilename(filename);
        expect(p.confidence).toBe(1.0);
        expect(p.source).toBe(source);
        expect(p.meetingDate).toBe(expDate);
        expect(p.meetingTime).toBe(expTime);
        expect(p.title).toBe(expTitle);
      },
    );

    it("handles titles with parentheses", () => {
      const p = parseFilename("notes__2025-03-04_091500__Doug (Crash).m4a");
      expect(p.confidence).toBe(1.0);
      expect(p.title).toBe("Doug (Crash)");
      expect(p.meetingTime).toBe("09:15:00");
    });

    it("accepts mp3 as well as m4a", () => {
      const p = parseFilename("notes__2024-09-12_103045__Random thought.mp3");
      expect(p.confidence).toBe(1.0);
      expect(p.meetingDate).toBe("2024-09-12");
    });

    it("strips a directory prefix", () => {
      const p = parseFilename(
        "/Volumes/media/audio-from-notes-voicememos-2026-05-20/" +
          "notes__2024-11-05_233322__Call with Jason Allen.m4a",
      );
      expect(p.confidence).toBe(1.0);
      expect(p.title).toBe("Call with Jason Allen");
    });
  });

  describe("pattern 2: ISO prefix + ' - '", () => {
    it("matches with .txt extension", () => {
      const p = parseFilename("2024-03-15 - Jane Smith.txt");
      expect(p.confidence).toBe(0.9);
      expect(p.source).toBe("generic");
      expect(p.meetingDate).toBe("2024-03-15");
      expect(p.title).toBe("Jane Smith");
    });
  });

  describe("pattern 3: ISO prefix + loose separator", () => {
    it("matches with underscore", () => {
      const p = parseFilename("2024-03-15_Jane Smith.txt");
      expect(p.confidence).toBe(0.8);
      expect(p.title).toBe("Jane Smith");
    });

    it("matches with single space", () => {
      const p = parseFilename("2024-03-15 Jane Smith Q1 sync.m4a");
      expect(p.confidence).toBe(0.8);
      expect(p.title).toBe("Jane Smith Q1 sync");
    });
  });

  describe("pattern 4: US date suffix", () => {
    it("expands 2-digit year (24 → 2024)", () => {
      const p = parseFilename("Jane Smith 3-15-24.txt");
      expect(p.confidence).toBe(0.7);
      expect(p.meetingDate).toBe("2024-03-15");
      expect(p.title).toBe("Jane Smith");
    });

    it("accepts 4-digit year", () => {
      const p = parseFilename("Quarterly review 3-15-2024.txt");
      expect(p.confidence).toBe(0.7);
      expect(p.meetingDate).toBe("2024-03-15");
    });
  });

  describe("pattern 5: ISO date anywhere", () => {
    it("recovers a title minus the matched date", () => {
      const p = parseFilename("zoom_2024-03-15_recording.mp4");
      expect(p.confidence).toBe(0.5);
      expect(p.meetingDate).toBe("2024-03-15");
      expect((p.title || "").toLowerCase()).toContain("zoom");
    });
  });

  describe("no match", () => {
    it("returns confidence 0 with a best-effort title", () => {
      const p = parseFilename("Just a random title.m4a");
      expect(p.confidence).toBe(0.0);
      expect(p.meetingDate).toBeNull();
      expect(p.meetingTime).toBeNull();
      expect(p.source).toBeNull();
      expect(p.title).toBe("Just a random title");
    });

    it("handles empty input", () => {
      const p = parseFilename("");
      expect(p.confidence).toBe(0.0);
      expect(p.title).toBeNull();
    });

    it("never throws on garbage", () => {
      expect(() => parseFilename(".m4a")).not.toThrow();
      expect(() => parseFilename("2024-13-99.m4a")).not.toThrow();
    });
  });
});
