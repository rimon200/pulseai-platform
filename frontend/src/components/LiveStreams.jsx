import { useEffect, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;

function LiveStreams({
  styles,
  creators,
  liveCreators,
  isLoadingCreators,
  renderCreatorRow,
}) {
  const [youtubeUploads, setYoutubeUploads] = useState([]);
  const [youtubeMessage, setYoutubeMessage] = useState("");

  const loadYouTubeUploads = () => {
    fetch(`${API_BASE_URL}/api/youtube/uploads?limit=25`)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload) => setYoutubeUploads(payload.uploads || []))
      .catch(() => setYoutubeUploads([]));
  };

  useEffect(() => {
    loadYouTubeUploads();
  }, []);

  const generateYouTubeClips = async (upload) => {
    setYoutubeMessage("");
    const response = await fetch(`${API_BASE_URL}/api/clips/auto`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: "youtube", upload_id: upload.id }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setYoutubeMessage(payload.message || payload.detail?.message || payload.detail || "Unable to queue this upload.");
      return;
    }
    setYoutubeMessage("YouTube clip generation was queued.");
    loadYouTubeUploads();
  };

  return (
    <div>
      <div style={styles.pageHeader}>
        <div>
          <h1 style={styles.pageTitle}>Live Streams</h1>
          <p style={styles.subtitle}>
            Monitor Twitch and Kick live status plus authorized YouTube uploads.
          </p>
        </div>
      </div>

      <div style={styles.statsGrid}>
        <div style={styles.statCard}>
          <span style={styles.statLabel}>Currently Live</span>
          <strong style={styles.statNumber}>{liveCreators.length}</strong>
          <span style={styles.statDetail}>Streaming right now</span>
        </div>

        <div style={styles.statCard}>
          <span style={styles.statLabel}>Creators Monitored</span>
          <strong style={styles.statNumber}>{creators.length}</strong>
          <span style={styles.statDetail}>Saved Twitch, Kick, and YouTube channels</span>
        </div>
      </div>

      <section style={styles.panel}>
        <div style={styles.panelHeader}>
          <div>
            <h2 style={styles.panelTitle}>Monitored Creators</h2>
            <p style={styles.panelSubtitle}>
              Live provider status for every saved creator.
            </p>
          </div>

          <span style={styles.liveIndicator}>
            <span style={styles.liveDot} />
            Monitoring
          </span>
        </div>

        {isLoadingCreators ? (
          <div style={styles.emptyState}>Loading saved creators...</div>
        ) : creators.length > 0 ? (
          creators.map(renderCreatorRow)
        ) : (
          <div style={styles.emptyState}>
            Add your first Twitch, Kick, or YouTube creator from Mission Control.
          </div>
        )}
      </section>
      <section style={{ ...styles.panel, marginTop: 22 }}>
        <div style={styles.panelHeader}>
          <div>
            <h2 style={styles.panelTitle}>YouTube uploads</h2>
            <p style={styles.panelSubtitle}>Long-form uploads discovered through the official YouTube Data API.</p>
          </div>
        </div>
        {youtubeMessage && <p>{youtubeMessage}</p>}
        {youtubeUploads.length ? youtubeUploads.map((upload) => (
          <div key={upload.id} style={{ ...styles.creatorRow, alignItems: "flex-start" }}>
            <div style={{ flex: 1 }}>
              <strong><span style={{ color: "#ef4444" }}>YOUTUBE</span>{" "}{upload.channel_name}</strong>
              <div style={styles.streamTitle}>{upload.title}</div>
              <div style={styles.creatorMeta}>
                {upload.published_at || "Unknown upload date"} • {Math.round((upload.duration_seconds || 0) / 60)} min
              </div>
              <div style={styles.creatorMeta}>
                Source: {upload.source_status} • Analysis: {upload.processing_status} • Generated clips: {upload.clips_created || 0}
              </div>
            </div>
            <button
              style={styles.secondaryButton}
              disabled={upload.source_status !== "ready" || ["claimed", "downloading", "analyzing", "generating"].includes(upload.processing_status)}
              onClick={() => generateYouTubeClips(upload)}
            >
              {upload.processing_status === "failed" ? "Retry" : "Generate Clips"}
            </button>
          </div>
        )) : <div style={styles.emptyState}>No YouTube uploads detected yet.</div>}
      </section>
    </div>
  );
}

export default LiveStreams;
