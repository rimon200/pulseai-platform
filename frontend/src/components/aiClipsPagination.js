export const AI_CLIPS_PAGE_SIZE = 12;

export const AI_CLIP_FILTERS = {
  All: "all",
  Unpublished: [
    "ready_for_review",
    "ready_to_review",
    "approved",
    "scheduled",
    "publish_failed",
    "publishing",
    "uploaded_to_inbox",
  ].join(","),
  Published: "published",
};

export const buildClipListUrl = (apiBaseUrl, page, filter) => {
  const parameters = new URLSearchParams({
    limit: String(AI_CLIPS_PAGE_SIZE),
    page: String(page),
    status: AI_CLIP_FILTERS[filter] || AI_CLIP_FILTERS.All,
  });
  return `${apiBaseUrl}/api/clips?${parameters.toString()}`;
};

export const isLatestClipListRequest = (
  requestSequence,
  latestRequestSequence,
) => requestSequence === latestRequestSequence;

const normalizeStatus = (status) => {
  const normalized = String(status || "").trim().toLowerCase()
    .replaceAll(" ", "_");
  return normalized === "ready_to_review"
    ? "ready_for_review"
    : normalized;
};

export const normalizeClip = (clip) => {
  if (!clip || typeof clip !== "object") {
    return null;
  }
  const generatedClipId = String(
    clip.generated_clip_id
    || clip.clip_id
    || clip.id
    || "",
  ).trim();
  return {
    ...clip,
    id: generatedClipId,
    generated_clip_id: generatedClipId,
    status: normalizeStatus(clip.status),
    durable_url: String(clip.durable_url || "").trim(),
    preview_url: String(
      clip.preview_url || clip.durable_url || "",
    ).trim(),
    preview_available: Boolean(
      clip.preview_available
      || clip.preview_url
      || clip.durable_url,
    ),
  };
};

export const clipPreviewUrl = (clip) => String(
  clip?.preview_url || clip?.durable_url || "",
).trim();

export const clipStableKey = (clip) => String(
  clip?.generated_clip_id
  || clip?.id
  || clip?.clip_id
  || "",
).trim();

export const UNPUBLISHED_CLIP_STATUSES = new Set([
  "ready_for_review",
  "approved",
  "scheduled",
  "publish_failed",
  "publishing",
  "uploaded_to_inbox",
]);

export const clipBelongsInFilter = (clip, filter) => {
  const status = normalizeStatus(clip?.status);
  if (filter === "Published") {
    return status === "published";
  }
  if (filter === "Unpublished") {
    return UNPUBLISHED_CLIP_STATUSES.has(status);
  }
  return true;
};

export const normalizeAndFilterClips = (
  clips,
  filter,
  onDiagnostic = () => {},
) => {
  const accepted = [];
  for (const rawClip of clips) {
    const clip = normalizeClip(rawClip);
    const clipId = clipStableKey(clip);
    onDiagnostic("received", {
      clip_id: clipId,
      status: clip?.status || "unknown",
      active_filter: filter,
    });
    if (!clipId) {
      onDiagnostic("filtered", {
        clip_id: "none",
        reason: "missing_generated_clip_id",
      });
      continue;
    }
    if (!clipBelongsInFilter(clip, filter)) {
      onDiagnostic("filtered", {
        clip_id: clipId,
        reason: `status_not_in_${String(filter).toLowerCase()}`,
      });
      continue;
    }
    accepted.push(clip);
  }
  return accepted;
};

export const prioritizeClip = (clips, preferredClipId) => {
  const preferredKey = String(preferredClipId || "").trim();
  if (!preferredKey) {
    return clips;
  }
  const preferredIndex = clips.findIndex(
    (clip) => clipStableKey(clip) === preferredKey,
  );
  if (preferredIndex <= 0) {
    return clips;
  }
  return [
    clips[preferredIndex],
    ...clips.slice(0, preferredIndex),
    ...clips.slice(preferredIndex + 1),
  ];
};

export const mergeClipPages = (currentClips, nextClips) => {
  const merged = [];
  const seen = new Set();
  for (const clip of [...currentClips, ...nextClips]) {
    const key = clipStableKey(clip);
    if (key && seen.has(key)) {
      continue;
    }
    if (key) {
      seen.add(key);
    }
    merged.push(clip);
  }
  return merged;
};

export const refreshFirstClipPage = (currentClips, freshFirstPage) => (
  mergeClipPages(freshFirstPage, currentClips)
);

export const isPublishableClipStatus = (status) => [
  "ready_for_review",
  "ready to review",
  "approved",
  "scheduled",
  "publish_failed",
].includes(String(status || "").trim().toLowerCase());

export const generatedClipBelongsInFilter = (
  filter,
  status = "ready_for_review",
) => clipBelongsInFilter({ status }, filter);
