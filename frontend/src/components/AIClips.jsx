import { useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;
function AIClips({ styles, clips, setClips }) {
  const [previewClipId, setPreviewClipId] = useState(null);
  const [previewErrors, setPreviewErrors] = useState({});

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
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/clips/auto`,
        {
          method: "POST",
        }
      );

      if (!response.ok) {
        throw new Error("Clip generation failed");
      }
const result = await response.json();

if (result.message) {
  alert(result.message);
  return;
}
      const clipsResponse = await fetch(`${API_BASE_URL}/api/clips`);
const updatedClips = await clipsResponse.json();

if (Array.isArray(updatedClips)) {
  setClips([...updatedClips].reverse().slice(0, 12));
}
    } catch (error) {
    console.error(error);
    alert("Could not generate a clip.");
}
};

const publishClip = async (clip) => {
  try {
    if (!clip.id) {
      alert("This older clip has no ID. Generate a newer clip first.");
      return;
    }

    if (!clip.video_path) {
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

    const result = await response.json();

    console.log("Publish request URL:", requestUrl);
    console.log("Publish response status:", response.status);
    console.log("Publish response JSON:", result);

    if (!response.ok) {
      throw new Error(result.detail || "Publish failed");
    }

    setClips((currentClips) =>
      currentClips.map((currentClip) =>
        currentClip.id === clip.id
          ? { ...currentClip, status: "Published" }
          : currentClip
      )
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
    style={styles.addCreatorButton}
  >
    Generate Clip
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
          {clips.length > 0 ? (
            clips.map((clip, index) => (
              <div
  key={`${clip.title}-${clip.started_at || "clip"}-${index}`}
  style={styles.clipCard}
>
                <div style={styles.clipPreview}>
  {previewClipId === clip.id && clip.id ? (
    <video
      src={getClipVideoUrl(clip.id)}
      controls
      preload="metadata"
      onError={() => handlePreviewError(clip.id)}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "cover",
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
    opacity: clip.status === "Published" ? 0.6 : 1,
    cursor: clip.status === "Published" ? "default" : "pointer",
  }}
>
  {clip.status === "Published" ? "✅ Published" : "Publish"}
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
      </section>
    </div>
  );
}

export default AIClips;