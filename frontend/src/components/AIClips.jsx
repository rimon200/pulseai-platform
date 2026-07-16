function AIClips({ styles, clips }) {
  return (
    <div>
      <div style={styles.pageHeader}>
        <div>
          <h1 style={styles.pageTitle}>AI Clips</h1>
          <p style={styles.subtitle}>
            Viral moments detected by PulseAI will appear here.
          </p>
        </div>
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
                  <span style={styles.playButton}>▶</span>
                </div>

                <div style={styles.clipContent}>
                  <div style={styles.clipTitle}>{clip.title}</div>
                  <div style={styles.clipCreator}>{clip.creator}</div>

                  <div style={styles.clipFooter}>
                    <span style={styles.scoreBadge}>
                      🔥 Viral score: {clip.score}
                    </span>

                    <span style={styles.clipStatus}>{clip.status}</span>
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