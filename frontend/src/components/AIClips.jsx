import { useCallback, useEffect, useRef, useState } from "react";
import {
  isGenerateButtonDisabled,
  pollGenerationJob,
  requestClipGeneration,
} from "./aiClipsGeneration";
import {
  AI_CLIP_FILTERS,
  buildClipListUrl,
  generatedClipBelongsInFilter,
  isLatestClipListRequest,
  isPublishableClipStatus,
  mergeClipPages,
  refreshFirstClipPage,
} from "./aiClipsPagination";

const API_BASE_URL = import.meta.env.VITE_API_URL;
const TIKTOK_RECONNECT_REQUIRED_MESSAGE = "TikTok authorization expired. Reconnect TikTok in Settings.";
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
  const [activeFilter, setActiveFilter] = useState("All");
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [generationStatus, setGenerationStatus] = useState("");
  const generationRequestActive = useRef(false);
  const pendingRenderedClipId = useRef("");
  const listRequestSequence = useRef(0);
  const loadClips = useCallback(async (
    requestedPage,
    requestedFilter,
    updateMode = "append",
  ) => {
    const requestSequence = listRequestSequence.current + 1;
    listRequestSequence.current = requestSequence;
    const response = await fetch(buildClipListUrl(
      API_BASE_URL,
      requestedPage,
      requestedFilter,
    ));
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Could not load AI Clips.");
    }
    if (!isLatestClipListRequest(
      requestSequence,
      listRequestSequence.current,
    )) {
      return;
    }
    const nextItems = Array.isArray(data) ? data : (data.items || []);
    setClips((currentClips) => (
      updateMode === "replace"
        ? mergeClipPages([], nextItems)
        : updateMode === "refresh-first"
          ? refreshFirstClipPage(currentClips, nextItems)
          : mergeClipPages(currentClips, nextItems)
    ));
    setPage(requestedPage);
    setHasMore(Boolean(data.has_more));
    setLoadError("");
  }, []);

  useEffect(() => {
    loadClips(1, activeFilter, "replace").catch((error) => {
      console.error("Could not load AI Clips page", error);
      setLoadError(error.message);
    });
  }, [activeFilter, loadClips]);

  useEffect(() => {
    const firstClipId = String(
      clips[0]?.id || clips[0]?.twitch_clip_id || "",
    ).trim();
    if (
      pendingRenderedClipId.current
      && firstClipId === pendingRenderedClipId.current
    ) {
      console.log(
        `FRONTEND CLIP RENDERED | clip_id=${firstClipId} | position=0`,
      );
      pendingRenderedClipId.current = "";
    }
  }, [clips]);

  const changeFilter = (nextFilter) => {
    if (nextFilter === activeFilter) {
      return;
    }
    listRequestSequence.current += 1;
    setClips([]);
    setPage(1);
    setHasMore(false);
    setLoadError("");
    setActiveFilter(nextFilter);
  };

  const loadMore = async () => {
    if (!hasMore || isLoadingMore) {
      return;
    }
    setIsLoadingMore(true);
    try {
      await loadClips(page + 1, activeFilter);
    } catch (error) {
      console.error("Could not load more AI Clips", error);
      setLoadError(error.message);
    } finally {
      setIsLoadingMore(false);
    }
  };

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
      if (outcome.kind === "job") {
        setGenerationStatus(outcome.message);
        const { state } = await pollGenerationJob({
          fetchImplementation: fetch,
          endpoint: `${API_BASE_URL}/api/clip-generation-jobs/${outcome.job.id}`,
          onUpdate: (jobState) => setGenerationStatus(jobState.label),
        });
        if (
          state.status === "completed"
          && state.outcome === "clip_created"
          && state.resultClipId
        ) {
          console.log(
            "FRONTEND CLIP REFRESH | "
            + `job_id=${outcome.job.id} | `
            + `result_clip_id=${state.resultClipId} | `
            + `active_filter=${activeFilter}`,
          );
          if (
            generatedClipBelongsInFilter(
              activeFilter,
              "ready_for_review",
            )
          ) {
            pendingRenderedClipId.current = state.resultClipId;
          }
          await loadClips(1, activeFilter, "refresh-first");
          setPage(1);
          return;
        }
        if (state.outcome === "no_clip_found") {
          alert("No suitable clip was found. Try again later.");
          return;
        }
        if (state.status === "deferred_memory") {
          alert(
            "Generation was safely deferred because the server was low on "
            + "memory. Please try again in a few minutes."
          );
          return;
        }
        alert(state.message || "Clip generation failed.");
        return;
      }
      if (outcome.kind === "success") {
        const resultClipId = String(outcome.clip?.id || "").trim();
        const resultStatus = outcome.clip?.status || "ready_for_review";
        console.log(
          "FRONTEND CLIP REFRESH | "
          + "job_id=direct | "
          + `result_clip_id=${resultClipId || "none"} | `
          + `active_filter=${activeFilter}`,
        );
        if (
          resultClipId
          && generatedClipBelongsInFilter(activeFilter, resultStatus)
        ) {
          pendingRenderedClipId.current = resultClipId;
        }
        await loadClips(1, activeFilter, "refresh-first");
        setPage(1);
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

    setClips((currentClips) => (
      activeFilter === "Unpublished"
        ? currentClips.filter(
          (currentClip) => !clipsMatch(currentClip, clip),
        )
        : currentClips.map((currentClip) => (
          clipsMatch(currentClip, clip)
            ? { ...currentClip, status: "uploaded_to_inbox" }
            : currentClip
        ))
    ));

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
  {generationStatus && (
    <span style={{ marginLeft: 12, opacity: 0.8 }}>
      {generationStatus}
    </span>
  )}
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
        <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
          {Object.keys(AI_CLIP_FILTERS).map((filterName) => (
            <button
              key={filterName}
              onClick={() => changeFilter(filterName)}
              disabled={activeFilter === filterName}
              style={styles.secondaryButton}
            >
              {filterName}
            </button>
          ))}
        </div>

        <div style={styles.clipGrid}>
          {loadError && (
            <div style={styles.emptyState}>{loadError}</div>
          )}
          {clips.length > 0 ? (
            clips.map((clip) => (
              <div
  key={clip.id || clip.twitch_clip_id || clip.public_url}
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
  disabled={!isPublishableClipStatus(clip.status)}
  style={{
    ...styles.secondaryButton,
    opacity: isPublishableClipStatus(clip.status) ? 1 : 0.6,
    cursor: isPublishableClipStatus(clip.status) ? "pointer" : "not-allowed",
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
              No {activeFilter.toLowerCase()} clips found.
            </div>
          )}
        </div>
        <div style={{ marginTop: 20 }}>
          {hasMore && (
            <button disabled={isLoadingMore} onClick={loadMore}>
              {isLoadingMore ? "Loading…" : "Load More"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}

export default AIClips;
