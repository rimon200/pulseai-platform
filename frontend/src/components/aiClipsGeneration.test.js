import assert from "node:assert/strict";
import test from "node:test";

import {
  GENERATION_ALREADY_RUNNING_MESSAGE,
  MEMORY_DEFERRED_USER_MESSAGE,
  classifyGenerationJob,
  isGenerateButtonDisabled,
  pollGenerationJob,
  requestClipGeneration,
} from "./aiClipsGeneration.js";

const response = (status, payload) => ({
  status,
  ok: status >= 200 && status < 300,
  json: async () => payload,
});

test("generated clip response is successful", async () => {
  const outcome = await requestClipGeneration(
    async () => response(200, {
      id: "generated-1",
      status: "ready_to_review",
      durable_url: "https://media.example/generated-1.mp4",
    }),
    "/api/clips/auto",
  );
  assert.equal(outcome.kind, "success");
  assert.equal(outcome.clip.id, "generated-1");
});

test("memory-deferred response is informational", async () => {
  const outcome = await requestClipGeneration(
    async () => response(200, {
      message: "Clip generation deferred because worker memory did not recover.",
    }),
    "/api/clips/auto",
  );
  assert.equal(outcome.kind, "deferred");
  assert.equal(outcome.message, MEMORY_DEFERRED_USER_MESSAGE);
});

test("409 response reports an existing generation job", async () => {
  const outcome = await requestClipGeneration(
    async () => response(409, {
      message: "Clip generation already in progress.",
    }),
    "/api/clips/auto",
  );
  assert.equal(outcome.kind, "busy");
  assert.equal(outcome.message, GENERATION_ALREADY_RUNNING_MESSAGE);
});

test("genuine backend failure preserves its detail", async () => {
  const outcome = await requestClipGeneration(
    async () => response(503, {
      detail: "Clip history is unavailable.",
    }),
    "/api/clips/auto",
  );
  assert.equal(outcome.kind, "error");
  assert.equal(outcome.message, "Clip history is unavailable.");
});

test("Generate Clip button is disabled during an active request", () => {
  assert.equal(isGenerateButtonDisabled(true, false), true);
  assert.equal(isGenerateButtonDisabled(false, true), true);
  assert.equal(isGenerateButtonDisabled(false, false), false);
});

test("202 response returns a durable queued job", async () => {
  const outcome = await requestClipGeneration(
    async () => response(202, { id: "job-1", status: "queued" }),
    "/api/clips/auto",
  );
  assert.equal(outcome.kind, "job");
  assert.equal(outcome.job.id, "job-1");
});

test("job polling reports stages and completed result", async () => {
  const jobs = [
    { id: "job-1", status: "queued" },
    { id: "job-1", status: "transcribing" },
    {
      id: "job-1",
      status: "completed",
      result_clip_id: "clip-1",
    },
  ];
  const labels = [];
  const result = await pollGenerationJob({
    fetchImplementation: async () => response(200, jobs.shift()),
    endpoint: "/api/clip-generation-jobs/job-1",
    onUpdate: (state) => labels.push(state.label),
    wait: async () => {},
  });
  assert.deepEqual(labels, ["Queued", "Transcribing", "Completed"]);
  assert.equal(result.state.resultClipId, "clip-1");
});

test("deferred and failed jobs retain real terminal messages", () => {
  const deferred = classifyGenerationJob({
    status: "deferred_memory",
    error_message: "Worker memory did not recover.",
  });
  const failed = classifyGenerationJob({
    status: "failed",
    error_message: "R2 upload failed.",
  });
  assert.equal(deferred.label, "Safely deferred for memory");
  assert.equal(deferred.terminal, true);
  assert.equal(failed.message, "R2 upload failed.");
});
