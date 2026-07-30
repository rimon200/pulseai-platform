export const AI_CLIPS_PAGE_SIZE = 12;

export const AI_CLIP_FILTERS = {
  All: "all",
  Unpublished: "unpublished",
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

export const clipStableKey = (clip) => String(
  clip?.id
  || clip?.twitch_clip_id
  || clip?.public_url
  || "",
).trim();

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
) => {
  const normalizedStatus = String(status || "").trim().toLowerCase();
  if (filter === "Published") {
    return normalizedStatus === "published";
  }
  if (filter === "Unpublished") {
    return isPublishableClipStatus(normalizedStatus);
  }
  return true;
};
