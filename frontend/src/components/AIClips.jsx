import { useCallback, useEffect, useRef, useState } from "react";
import {
  isGenerateButtonDisabled,
  requestClipGeneration,
} from "./aiClipsGeneration";

const API_BASE_URL = import.meta.env.VITE_API_URL;
const TIKTOK_RECONNECT_REQUIRED_MESSAGE = "TikTok authorization expired. Reconnect TikTok in Settings.";
const AI_CLIPS_PAGE_SIZE = 10;
const AI_CLIPS_STATUS = "ready_for_review";

const normalizeClipStatus = (status) => {
  const value = String(status || "").trim().toLowerCase();
  if (value === "published") {
    return "Published";
  }
  if (value === "ready to review") {
    return "Ready to review";
  }
  if (value === "ready_for_review") {
    return "Ready to review";
  }
  return "";
};

const isReviewableClip = (clip) => {
  const normalized = normalizeClipStatus(clip?.status);
  return normalized === "Ready to review";
};

const clipsMatch = (left, right) => {
  if (!left || !right) {
    return false;
  }

  const leftId = String(left.id || "").trim();
  const rightId = String(right.id || "").trim();
  if (leftId && rightId && leftId === rightId) {
    return true;
  }

  const leftTwitchClipId = String(left.twitch_clip_id || "").trim();
  const rightTwitchClipId = String(right.twitch_clip_id || "").trim();
  if (
    leftTwitchClipId
    && rightTwitchClipId
    && leftTwitchClipId === rightTwitchClipId
  ) {
    return true;
  }

  const leftPublicUrl = String(left.public_url || "").trim();
  const rightPublicUrl = String(right.public_url || "").trim();
  if (leftPublicUrl && rightPublicUrl && leftPublicUrl === rightPublicUrl) {
    return true;
  }

  return false;
};

function AIClips({ styles }) {
  const [clips, setClips] = useState([]);
  const [previewClipId, setPreviewClipId] = useState(null);
  const [previewErrors, setPreviewErrors] = useState({});
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const generationRequestActive = useRef(false);
  const reviewableClips = clips.filter(isReviewableClip);

  const loadClips = useCallback(async (requestedPage) => {
    const response = await fetch(
      `${API_BASE_URL}/api/clips?limit=${AI_CLIPS_PAGE_SIZE}`
      + `&page=${requestedPage}&status=${AI_CLIPS_STATUS}`,
    );
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Could not load AI Clips.");
    }
    setClips(Array.isArray(data) ? data : (data.items || []));
    setHasMore(Boolean(data.has_more));
    setLoadError("");
  }, []);

  useEffect(() => {
    loadClips(page).catch((error) => {
      console.error("Could not load AI Clips page", error);
      setLoadError(error.message);
    });
  }, [loadClips, page]);

  const getClipVideoUrl = (clipId, download = false) =>
    `${API_BASE_URL}/api/clips/${encodeURIComponent(clipId)}/video${
      download ? "?download=1" : ""
    }`;

  const togglePreview = (clipId) => {
    if (!clipId) {
      return;
    }

    setPreviewClipId((currentClipId) =>
      currentClipId === clipId ? null : clipId
    );

    setPreviewErrors((currentErrors) => {
      if (!currentErrors[clipId]) {
        return currentErrors;
      }

      return {
        ...currentErrors,
        [clipId]: "",
      };
    });
  };

  const handlePreviewError = (clipId) => {
    setPreviewErrors((currentErrors) => ({
      ...currentErrors,
      [clipId]:
        "Video preview is unavailable. The file may have been removed from Render storage.",
    }));
  };

  const generateClip = async () => {
    if (generationRequestActive.current) {
      return;
    }
    generationRequestActive.current = true;
    setIsGenerating(true);
    try {
      const outcome = await requestClipGeneration(
        fetch,
        `${API_BASE_URL}/api/clips/auto`,
      );
      if (outcome.kind === "success") {
        setPage(1);
        await loadClips(1);
        return;
      }
      alert(outcome.message);
    } catch (error) {
      console.error(error);
      alert(error.message || "Could not generate a clip.");
    } finally {
      generationRequestActive.current = false;
      setIsGenerating(false);
    }
  };

const publishClip = async (clip) => {
  try {
    if (!clip.video_path && !clip.durable_url) {
      alert("This clip is missing its local video file path.");
      return;
    }

    const requestUrl = `${API_BASE_URL}/api/publish`;

    const response = await fetch(
      requestUrl,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(clip),
      }
    );

    let result = {};
    try {
      result = await response.json();
    } catch {
      result = {};
    }

    console.log("Publish request URL:", requestUrl);
    console.log("Publish response status:", response.status);
    console.log("Publish response JSON:", result);

    if (!response.ok) {
      const detailMessage = typeof result.detail === "string"
        ? result.detail
        : "Publish failed";

      if (
        response.status === 401
        && detailMessage === TIKTOK_RECONNECT_REQUIRED_MESSAGE
      ) {
        alert(detailMessage);
        return;
      }

      throw new Error(detailMessage);
    }

    setClips((currentClips) =>
      currentClips.filter((currentClip) => !clipsMatch(currentClip, clip))
    );

    setPreviewClipId((currentPreviewClipId) =>
      currentPreviewClipId === clip.id ? null : currentPreviewClipId
    );
  } catch (error) {
    console.error(error);
    alert(error.message);
  }
};

return (
    <div>
      <div style={styles.pageHeader}>
  <div>
    <h1 style={styles.pageTitle}>AI Clips</h1>
    <p style={styles.subtitle}>
      Viral moments detected by PulseAI will appear here.
    </p>
  </div>

  <button
    onClick={generateClip}
    disabled={isGenerateButtonDisabled(
      isGenerating,
      generationRequestActive.current,
    )}
    style={styles.addCreatorButton}
  >
    {isGenerating ? "Generating…" : "Generate Clip"}
  </button>
</div>

      <section style={styles.panel}>
        <div style={styles.panelHeader}>
          <div>
            <h2 style={styles.panelTitle}>Detected Clips</h2>
            <p style={styles.panelSubtitle}>
              Review clips found by the AI system.
            </p>
          </div>
        </div>

        <div style={styles.clipGrid}>
          {loadError && (
            <div style={styles.emptyState}>{loadError}</div>
          )}
          {reviewableClips.length > 0 ? (
            reviewableClips.map((clip, index) => (
              <div
  key={`${clip.title}-${clip.started_at || "clip"}-${index}`}
  style={styles.clipCard}
>
                <div
  style={{
    ...styles.clipPreview,
    aspectRatio: "9 / 16",
    height: "auto",
    backgroundColor: "#000",
    overflow: "hidden",
  }}
>
  {previewClipId === clip.id && clip.id ? (
    <video
      src={getClipVideoUrl(clip.id)}
      controls
      preload="metadata"
      onError={() => handlePreviewError(clip.id)}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "contain",
        display: "block",
        backgroundColor: "#000",
      }}
    />
  ) : clip.thumbnail_url ? (
    <img
      src={clip.thumbnail_url}
      alt={clip.title}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "cover",
        display: "block",
      }}
    />
  ) : (
    <span style={styles.playButton}>▶</span>
  )}
</div>
                {clip.id && previewErrors[clip.id] && (
  <div style={{ color: "#fca5a5", fontSize: 12, margin: "8px 12px 0" }}>
    {previewErrors[clip.id]}
  </div>
)}

                <div style={styles.clipContent}>
                  <div style={styles.clipTitle}>{clip.ai_title || clip.title}</div>
                  <div style={styles.clipCreator}>{clip.creator}</div>

                  <div style={styles.clipFooter}>
                    <span style={styles.scoreBadge}>
                      🔥 Viral score: {clip.score}
                    </span>

                    <span style={styles.clipStatus}>{clip.status}</span>
                    <button
  onClick={() => publishClip(clip)}
  disabled={false}
  style={{
    ...styles.secondaryButton,
    opacity: 1,
    cursor: "pointer",
  }}
>
  Publish
</button>

<button
  onClick={() => togglePreview(clip.id)}
  disabled={!clip.id}
  style={{
    ...styles.secondaryButton,
    opacity: clip.id ? 1 : 0.6,
    cursor: clip.id ? "pointer" : "not-allowed",
  }}
>
  {previewClipId === clip.id ? "Hide Preview" : "Preview"}
</button>

{clip.id ? (
  <a
    href={getClipVideoUrl(clip.id, true)}
    style={{ ...styles.secondaryButton, textDecoration: "none", display: "inline-block" }}
  >
    Download
  </a>
) : (
  <button
    disabled
    style={{ ...styles.secondaryButton, opacity: 0.6, cursor: "not-allowed" }}
  >
    Download
  </button>
)}

{clip.twitch_edit_url && (
  <a
    href={clip.twitch_edit_url}
    target="_blank"
    rel="noreferrer"
    style={{ marginLeft: 10 }}
  >
    🎬 Edit Clip
  </a>
)}
                    {clip.viewer_count && (
  <div style={{ marginTop: 8, fontSize: 12, opacity: 0.8 }}>
    👥 {clip.viewer_count.toLocaleString()} viewers
  </div>
)}

{clip.game && (
  <div style={{ fontSize: 12, opacity: 0.8 }}>
    🎮 {clip.game}
  </div>
)}
{clip.duration && (
  <div style={{ fontSize: 12, opacity: 0.8 }}>
    ⏱️ {clip.duration}s
    {clip.transcript && (
  <p className="clip-transcript">
    {clip.transcript}
  </p>
)}
  </div>
)}
                  </div>
                </div>
              </div>
            ))
          ) : (
            <div style={styles.emptyState}>
              No clips detected yet.
            </div>
          )}
        </div>
        <div style={{ marginTop: 20 }}>
          <button disabled={page === 1} onClick={() => setPage(page - 1)}>
            Previous
          </button>
          <span style={{ margin: "0 12px" }}>Page {page}</span>
          <button disabled={!hasMore} onClick={() => setPage(page + 1)}>
            Next
          </button>
        </div>
      </section>
    </div>
  );
}

export default AIClips;
