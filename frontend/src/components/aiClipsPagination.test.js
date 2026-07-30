import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { PRODUCTION_GENERATED_CLIP_FIXTURE } from "./aiClipsProductionFixture.js";
import {
  AI_CLIPS_PAGE_SIZE,
  buildClipListUrl,
  clipBelongsInFilter,
  clipStableKey,
  generatedClipBelongsInFilter,
  isLatestClipListRequest,
  isPublishableClipStatus,
  mergeClipPages,
  normalizeAndFilterClips,
  normalizeClip,
  prioritizeClip,
  refreshFirstClipPage,
} from "./aiClipsPagination.js";

test("the first AI Clips request asks for the newest 12", () => {
  const url = buildClipListUrl("https://api.invalid", 1, "All");
  assert.equal(AI_CLIPS_PAGE_SIZE, 12);
  assert.match(url, /limit=12/);
  assert.match(url, /page=1/);
  assert.match(url, /status=all/);
});

test("Load More requests the next filtered page", () => {
  const url = buildClipListUrl(
    "https://api.invalid",
    2,
    "Unpublished",
  );
  assert.match(url, /page=2/);
  assert.match(url, /status=ready_for_review/);
  assert.match(url, /uploaded_to_inbox/);
});

test("published filtering uses the published backend filter", () => {
  const url = buildClipListUrl(
    "https://api.invalid",
    1,
    "Published",
  );
  assert.match(url, /status=published/);
});

test("additional pages append without duplicate clip cards", () => {
  const merged = mergeClipPages(
    [{ id: "clip-1" }, { id: "clip-2" }],
    [{ id: "clip-2" }, { id: "clip-3" }],
  );
  assert.deepEqual(
    merged.map((clip) => clip.id),
    ["clip-1", "clip-2", "clip-3"],
  );
});

test("a refreshed first page puts the generated clip first without duplicates", () => {
  const refreshed = refreshFirstClipPage(
    [{ id: "clip-old-1" }, { id: "clip-new" }, { id: "clip-old-2" }],
    [{ id: "clip-new" }, { id: "clip-old-1" }],
  );
  assert.deepEqual(
    refreshed.map((clip) => clip.id),
    ["clip-new", "clip-old-1", "clip-old-2"],
  );
});

test("an unpublished generated clip belongs in All and Unpublished only", () => {
  assert.equal(
    generatedClipBelongsInFilter("All", "ready_for_review"),
    true,
  );
  assert.equal(
    generatedClipBelongsInFilter("Unpublished", "ready_for_review"),
    true,
  );
  assert.equal(
    generatedClipBelongsInFilter("Published", "ready_for_review"),
    false,
  );
});

test("a stale earlier list response cannot replace a newer refresh", () => {
  assert.equal(isLatestClipListRequest(4, 5), false);
  assert.equal(isLatestClipListRequest(5, 5), true);
});

test("old unpublished statuses retain the Publish action", () => {
  assert.equal(isPublishableClipStatus("ready_for_review"), true);
  assert.equal(isPublishableClipStatus("approved"), true);
  assert.equal(isPublishableClipStatus("publish_failed"), true);
  assert.equal(isPublishableClipStatus("published"), false);
  assert.equal(isPublishableClipStatus("archived"), false);
});

test("the production-shaped clip normalizes to its generated clip ID", () => {
  const normalized = normalizeClip(PRODUCTION_GENERATED_CLIP_FIXTURE);
  assert.equal(
    normalized.id,
    "da3d9508-6196-4291-8145-f029640b8028",
  );
  assert.equal(clipStableKey(normalized), normalized.id);
  assert.equal(normalized.twitch_clip_id, "PoisedEncouragingFish0Strog");
});

test("the production-shaped clip passes All and Unpublished", () => {
  const normalized = normalizeClip(PRODUCTION_GENERATED_CLIP_FIXTURE);
  assert.equal(clipBelongsInFilter(normalized, "All"), true);
  assert.equal(clipBelongsInFilter(normalized, "Unpublished"), true);
  assert.equal(clipBelongsInFilter(normalized, "Published"), false);
});

test("a generated clip refresh is inserted at position zero", () => {
  const normalized = normalizeClip(PRODUCTION_GENERATED_CLIP_FIXTURE);
  const prioritized = prioritizeClip(
    [{ id: "older" }, normalized],
    normalized.id,
  );
  const refreshed = refreshFirstClipPage(
    [{ id: "loaded-old-page" }],
    prioritized,
  );
  assert.equal(refreshed[0].id, normalized.id);
});

test("missing preview media does not remove the card", () => {
  const clips = normalizeAndFilterClips(
    [PRODUCTION_GENERATED_CLIP_FIXTURE],
    "All",
  );
  assert.equal(clips.length, 1);
  assert.equal(clips[0].durable_url, "");
  assert.ok(clips[0].object_key);
});

test("a Twitch source collision does not remove a new generated record", () => {
  const sourceCollision = {
    id: "older-generated-id",
    twitch_clip_id: PRODUCTION_GENERATED_CLIP_FIXTURE.twitch_clip_id,
    public_url: PRODUCTION_GENERATED_CLIP_FIXTURE.public_url,
  };
  const fresh = normalizeClip(PRODUCTION_GENERATED_CLIP_FIXTURE);
  const merged = refreshFirstClipPage([sourceCollision], [fresh]);
  assert.deepEqual(
    merged.map((clip) => clip.id),
    [fresh.id, "older-generated-id"],
  );
});

test("duplicate API responses create one generated card", () => {
  const normalized = normalizeClip(PRODUCTION_GENERATED_CLIP_FIXTURE);
  const merged = refreshFirstClipPage(
    [normalized],
    [
      normalizeClip({
        ...PRODUCTION_GENERATED_CLIP_FIXTURE,
        title: "Fresh server version",
      }),
    ],
  );
  assert.equal(merged.length, 1);
  assert.equal(merged[0].id, normalized.id);
  assert.equal(merged[0].title, "Fresh server version");
});

test("received and filtered diagnostics identify the generated clip", () => {
  const diagnostics = [];
  normalizeAndFilterClips(
    [PRODUCTION_GENERATED_CLIP_FIXTURE],
    "Published",
    (event, fields) => diagnostics.push({ event, fields }),
  );
  assert.equal(diagnostics[0].event, "received");
  assert.equal(
    diagnostics[0].fields.clip_id,
    PRODUCTION_GENERATED_CLIP_FIXTURE.clip_id,
  );
  assert.equal(diagnostics[1].event, "filtered");
  assert.match(diagnostics[1].fields.reason, /status_not_in_published/);
});

test("React cards use only the generated clip ID as their key", () => {
  const source = readFileSync(new URL("./AIClips.jsx", import.meta.url), "utf8");
  assert.match(source, /key=\{clip\.id\}/);
  assert.doesNotMatch(
    source,
    /key=\{clip\.id \|\| clip\.twitch_clip_id/,
  );
  assert.match(source, /Preview unavailable/);
});
