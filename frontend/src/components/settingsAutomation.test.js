import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("./Settings.jsx", import.meta.url),
  "utf8",
);

test("Settings loads the bandwidth-aware automation status endpoint", () => {
  assert.match(source, /\/api\/settings\/automation/);
  assert.match(source, /automation\.enabled/);
  assert.match(source, /Automatic clips today:/);
  assert.match(source, /automation\.automatic_clips_created_today/);
  assert.match(source, /automation\.daily_clip_limit/);
  assert.match(source, /automation\.estimated_outbound_mb_today/);
  assert.match(source, /automation\.daily_outbound_budget_mb/);
  assert.match(source, /automation\.last_automatic_run/);
  assert.match(source, /automation\.last_skip_reason/);
});

test("Settings automation status does not expose credential fields", () => {
  assert.doesNotMatch(source, /access_token|secret_access_key|database_url/i);
});
