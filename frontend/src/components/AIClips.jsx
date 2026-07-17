function AIClips({ styles, clips }) {
  const generateClip = async () => {
    try {
      const response = await fetch(
        "http://127.0.0.1:8000/api/clips/generate",
        {
          method: "POST",
        }
      );

      if (!response.ok) {
        throw new Error("Clip generation failed");
      }

      window.location.reload();
    } catch (error) {
      console.error(error);
      alert("Could not generate a clip.");
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
            clips.map((clip) => (
              <div key={clip.title} style={styles.clipCard}>
                <div style={styles.clipPreview}>
  {clip.thumbnail_url ? (
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

                <div style={styles.clipContent}>
                  <div style={styles.clipTitle}>{clip.title}</div>
                  <div style={styles.clipCreator}>{clip.creator}</div>

                  <div style={styles.clipFooter}>
                    <span style={styles.scoreBadge}>
                      🔥 Viral score: {clip.score}
                    </span>

                    <span style={styles.clipStatus}>{clip.status}</span>
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