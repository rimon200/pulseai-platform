import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../App.jsx", import.meta.url), "utf8");
const mission = readFileSync(new URL("./MissionControl.jsx", import.meta.url), "utf8");
const liveStreams = readFileSync(new URL("./LiveStreams.jsx", import.meta.url), "utf8");
const aiClips = readFileSync(new URL("./AIClips.jsx", import.meta.url), "utf8");
const settings = readFileSync(new URL("./Settings.jsx", import.meta.url), "utf8");

test("creator form supports YouTube without removing Twitch or Kick", () => {
  assert.match(mission, /option value="twitch"/);
  assert.match(mission, /option value="kick"/);
  assert.match(mission, /option value="youtube"/);
});

test("YouTube creator identity and provider-aware channel link render", () => {
  assert.match(app, /YOUTUBE/);
  assert.match(app, /youtube\.com\/@/);
  assert.match(app, /UPLOAD_MONITORING|Upload monitoring/);
});

test("YouTube upload dashboard shows durable source and processing state", () => {
  assert.match(liveStreams, /api\/youtube\/uploads/);
  assert.match(liveStreams, /Source:.*source_status/s);
  assert.match(liveStreams, /Analysis:.*processing_status/s);
  assert.match(liveStreams, /Generated clips:/);
  assert.match(liveStreams, /provider: "youtube"/);
});

test("Settings exposes YouTube diagnostics without API credentials", () => {
  assert.match(settings, /api\/youtube\/status/);
  assert.match(settings, /Approved media sources/);
  assert.doesNotMatch(settings, /YOUTUBE_API_KEY/);
});

test("AI Clips renders provider identity from the durable record", () => {
  assert.match(aiClips, /clip\.provider/);
});
