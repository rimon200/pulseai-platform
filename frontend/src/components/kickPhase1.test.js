import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../App.jsx", import.meta.url), "utf8");
const mission = readFileSync(new URL("./MissionControl.jsx", import.meta.url), "utf8");
const settings = readFileSync(new URL("./Settings.jsx", import.meta.url), "utf8");

test("creator form offers Twitch and Kick providers", () => {
  assert.match(mission, /option value="twitch"/);
  assert.match(mission, /option value="kick"/);
  assert.match(app, /provider: creatorProvider/);
});

test("creator rows render provider badges and provider-aware links", () => {
  assert.match(app, /isKick \? "KICK" : "TWITCH"/);
  assert.match(app, /https:\/\/kick\.com/);
  assert.match(app, /https:\/\/www\.twitch\.tv/);
});

test("Kick generation control is disabled with the supported limitation", () => {
  assert.match(app, /isKick && isLive/);
  assert.match(app, /disabled/);
  assert.match(
    app,
    /Clip generation unavailable pending supported Kick playback access\./,
  );
});

test("Settings displays Kick connection and playback diagnostics", () => {
  assert.match(settings, /\/api\/kick\/status/);
  assert.match(settings, /Connect Kick/);
  assert.match(settings, /playback_ingestion/);
  assert.match(settings, /Playback ingestion/);
});
