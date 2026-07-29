export const MEMORY_DEFERRED_BACKEND_MESSAGE =
  "Clip generation deferred because worker memory did not recover.";

export const MEMORY_DEFERRED_USER_MESSAGE =
  "Generation was safely deferred because the server was low on memory. "
  + "Please try again in a few minutes.";

export const GENERATION_ALREADY_RUNNING_MESSAGE =
  "A clip generation job is already running.";

export const NO_CLIP_FOUND_USER_MESSAGE =
  "No suitable clip was found. Try again later.";

export const JOB_STATUS_LABELS = {
  queued: "Queued",
  claimed: "Queued",
  downloading: "Downloading",
  transcribing: "Transcribing",
  scoring: "Scoring",
  rendering: "Rendering",
  uploading: "Uploading",
  completed: "Completed",
  deferred_memory: "Safely deferred for memory",
  failed: "Failed",
};

const responseMessage = (payload) => {
  if (!payload || typeof payload !== "object") {
    return "";
  }
  return [payload.detail, payload.message]
    .find((value) => typeof value === "string" && value.trim())
    ?.trim() || "";
};

const generatedClipFromPayload = (payload) => {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  const clip = payload.clip && typeof payload.clip === "object"
    ? payload.clip
    : payload;
  const status = String(clip.status || "").trim().toLowerCase();
  const generatedStatuses = new Set([
    "ready_to_review",
    "ready_for_review",
    "ready to review",
  ]);
  const hasStableIdentifier = Boolean(
    clip.id || clip.generated_clip_id || clip.twitch_clip_id
  );
  if (
    hasStableIdentifier
    && (
      generatedStatuses.has(status)
      || clip.video_path
      || clip.durable_url
      || clip.object_key
    )
  ) {
    return clip;
  }
  return null;
};

export const classifyGenerationResponse = (response, payload) => {
  const backendMessage = responseMessage(payload);

  if (response.status === 409) {
    return {
      kind: "busy",
      message: GENERATION_ALREADY_RUNNING_MESSAGE,
    };
  }

  if (!response.ok) {
    return {
      kind: "error",
      message: backendMessage || "Could not generate a clip.",
    };
  }

  if (
    response.status === 202
    && payload
    && typeof payload === "object"
    && payload.id
  ) {
    return {
      kind: "job",
      job: payload,
      message: JOB_STATUS_LABELS[payload.status] || "Queued",
    };
  }

  const generatedClip = generatedClipFromPayload(payload);
  if (generatedClip) {
    return {
      kind: "success",
      clip: generatedClip,
      message: backendMessage,
    };
  }

  if (backendMessage === MEMORY_DEFERRED_BACKEND_MESSAGE) {
    return {
      kind: "deferred",
      message: MEMORY_DEFERRED_USER_MESSAGE,
    };
  }

  if (backendMessage) {
    return {
      kind: "info",
      message: backendMessage,
    };
  }

  return {
    kind: "error",
    message: "The server returned an invalid clip generation response.",
  };
};

export const classifyGenerationJob = (job) => {
  const status = String(job?.status || "").trim().toLowerCase();
  const resultClipId = String(job?.result_clip_id || "").trim();
  const outcome = String(job?.outcome || "").trim().toLowerCase();
  const noClipFound = (
    status === "completed"
    && (outcome === "no_clip_found" || (!outcome && !resultClipId))
  );
  const message = typeof job?.error_message === "string"
    ? job.error_message.trim()
    : "";
  return {
    status,
    outcome: noClipFound ? "no_clip_found" : outcome,
    label: noClipFound
      ? NO_CLIP_FOUND_USER_MESSAGE
      : (JOB_STATUS_LABELS[status] || status || "Queued"),
    terminal: ["completed", "deferred_memory", "failed"].includes(status),
    resultClipId,
    message: noClipFound ? NO_CLIP_FOUND_USER_MESSAGE : message,
  };
};

export const requestClipGeneration = async (fetchImplementation, endpoint) => {
  const response = await fetchImplementation(endpoint, { method: "POST" });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  return classifyGenerationResponse(response, payload);
};

export const pollGenerationJob = async ({
  fetchImplementation,
  endpoint,
  onUpdate,
  wait = (milliseconds) => new Promise(
    (resolve) => setTimeout(resolve, milliseconds)
  ),
  intervalMilliseconds = 2000,
  maximumPolls = 600,
}) => {
  for (let poll = 0; poll < maximumPolls; poll += 1) {
    const response = await fetchImplementation(endpoint);
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      throw new Error(responseMessage(payload) || "Could not load generation status.");
    }
    const state = classifyGenerationJob(payload);
    onUpdate?.(state, payload);
    if (state.terminal) {
      return { state, job: payload };
    }
    await wait(intervalMilliseconds);
  }
  throw new Error("Clip generation status timed out.");
};

export const isGenerateButtonDisabled = (
  isGenerating,
  requestActive,
) => Boolean(isGenerating || requestActive);
