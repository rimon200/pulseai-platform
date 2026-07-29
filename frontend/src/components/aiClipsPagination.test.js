import assert from "node:assert/strict";
import test from "node:test";

import {
  AI_CLIPS_PAGE_SIZE,
  buildClipListUrl,
  isPublishableClipStatus,
  mergeClipPages,
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
  assert.match(url, /status=unpublished/);
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

test("old unpublished statuses retain the Publish action", () => {
  assert.equal(isPublishableClipStatus("ready_for_review"), true);
  assert.equal(isPublishableClipStatus("approved"), true);
  assert.equal(isPublishableClipStatus("publish_failed"), true);
  assert.equal(isPublishableClipStatus("published"), false);
  assert.equal(isPublishableClipStatus("archived"), false);
});

