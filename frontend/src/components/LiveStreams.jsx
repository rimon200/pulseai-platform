function LiveStreams({
  styles,
  creators,
  liveCreators,
  isLoadingCreators,
  renderCreatorRow,
}) {
  return (
    <div>
      <div style={styles.pageHeader}>
        <div>
          <h1 style={styles.pageTitle}>Live Streams</h1>
          <p style={styles.subtitle}>
            Monitor your saved Twitch creators and see who is live.
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
          <span style={styles.statDetail}>Saved Twitch channels</span>
        </div>
      </div>

      <section style={styles.panel}>
        <div style={styles.panelHeader}>
          <div>
            <h2 style={styles.panelTitle}>Monitored Creators</h2>
            <p style={styles.panelSubtitle}>
              Live Twitch status for every saved creator.
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
            Add your first Twitch creator from Mission Control.
          </div>
        )}
      </section>
    </div>
  );
}

export default LiveStreams;