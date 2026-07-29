export const MEMORY_DEFERRED_BACKEND_MESSAGE =
  "Clip generation deferred because worker memory did not recover.";

export const MEMORY_DEFERRED_USER_MESSAGE =
  "Generation was safely deferred because the server was low on memory. "
  + "Please try again in a few minutes.";

export const GENERATION_ALREADY_RUNNING_MESSAGE =
  "A clip generation job is already running.";

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

export const isGenerateButtonDisabled = (
  isGenerating,
  requestActive,
) => Boolean(isGenerating || requestActive);
