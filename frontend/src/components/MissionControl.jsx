function MissionControl({
  styles,
  loadCreators,
  isRefreshing,
  isLoadingCreators,
  creators,
  liveCreators,
  renderCreatorRow,
  activity,
  clips,
  showCreatorForm,
  setShowCreatorForm,
  closeCreatorForm,
  creatorName,
  setCreatorName,
  creatorChannel,
  setCreatorChannel,
  creatorError,
  setCreatorError,
  addCreator,
  isAddingCreator,
}) {
  return (
    <div>
      <div style={styles.pageHeader}>
        <div>
          <h1 style={styles.pageTitle}>Mission Control</h1>

          <p style={styles.subtitle}>
            Your AI content team is connected to real Twitch data.
          </p>
        </div>

        <div style={styles.headerButtons}>
          <button
            onClick={() => loadCreators(true)}
            disabled={isRefreshing}
            style={{
              ...styles.refreshButton,
              opacity: isRefreshing ? 0.65 : 1,
            }}
          >
            {isRefreshing ? "Refreshing..." : "↻ Refresh"}
          </button>

          <button
            onClick={() => {
              setCreatorError("");
              setShowCreatorForm(true);
            }}
            style={styles.addCreatorButton}
          >
            + Add Creator
          </button>
        </div>
      </div>

      {creatorError && !showCreatorForm && (
        <div style={{ ...styles.errorMessage, marginTop: "22px" }}>
          {creatorError}
        </div>
      )}

      <div style={styles.statsGrid}>
        <div style={styles.statCard}>
          <span style={styles.statLabel}>Live Streams</span>
          <strong style={styles.statNumber}>{liveCreators.length}</strong>
          <span style={styles.statDetail}>Live on Twitch now</span>
        </div>

        <div style={styles.statCard}>
          <span style={styles.statLabel}>Creators Monitored</span>
          <strong style={styles.statNumber}>{creators.length}</strong>
          <span style={styles.statDetail}>Saved Twitch channels</span>
        </div>

        <div style={styles.statCard}>
          <span style={styles.statLabel}>Clips Today</span>
          <strong style={styles.statNumber}>{clips.length}</strong>
          <span style={styles.statDetail}>Clips loaded from backend</span>
        </div>

        <div style={styles.statCard}>
          <span style={styles.statLabel}>System Status</span>
          <strong style={styles.onlineText}>ONLINE</strong>
          <span style={styles.statDetail}>Twitch API connected</span>
        </div>
      </div>

      <div style={styles.twoColumnGrid}>
        <section style={styles.panel}>
          <div style={styles.panelHeader}>
            <div>
              <h2 style={styles.panelTitle}>Live Streams</h2>
              <p style={styles.panelSubtitle}>
                Real creator information from Twitch.
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
              Add your first Twitch creator to begin monitoring.
            </div>
          )}
        </section>

        <section style={styles.panel}>
          <div style={styles.panelHeader}>
            <div>
              <h2 style={styles.panelTitle}>AI Activity</h2>
              <p style={styles.panelSubtitle}>
                Watch the PulseAI system come online.
              </p>
            </div>

            <span style={styles.workingBadge}>● Connected</span>
          </div>

          {activity.map((item) => (
            <div key={`${item.time}-${item.title}`} style={styles.activityRow}>
              <div style={styles.activityIcon}>{item.icon}</div>

              <div style={{ flex: 1 }}>
                <div style={styles.activityTitle}>{item.title}</div>
                <div style={styles.activityDetail}>{item.detail}</div>
              </div>

              <div style={styles.activityTime}>{item.time}</div>
            </div>
          ))}
        </section>
      </div>

      <section style={{ ...styles.panel, marginTop: "22px" }}>
        <div style={styles.panelHeader}>
          <div>
            <h2 style={styles.panelTitle}>Clip Queue</h2>
            <p style={styles.panelSubtitle}>
              Viral moments detected by PulseAI will appear here.
            </p>
          </div>

          <button style={styles.secondaryButton}>View all clips</button>
        </div>

        <div style={styles.clipGrid}>
          {clips.map((clip, index) => (
            <div
  key={clip.id || `${clip.title}-${index}`}
  style={styles.clipCard}
>
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
          ))}
        </div>
      </section>

      {showCreatorForm && (
        <div style={styles.modalOverlay}>
          <form onSubmit={addCreator} style={styles.modal}>
            <h2 style={styles.modalTitle}>Add Twitch Creator</h2>

            <p style={styles.modalDescription}>
              PulseAI will verify and save the channel through the backend.
            </p>

            <label style={styles.label}>Creator display name</label>

            <input
              value={creatorName}
              onChange={(event) => setCreatorName(event.target.value)}
              placeholder="Example: Pokimane"
              style={styles.formInput}
              disabled={isAddingCreator}
              autoFocus
            />

            <label style={styles.label}>Twitch channel name</label>

            <input
              value={creatorChannel}
              onChange={(event) => setCreatorChannel(event.target.value)}
              placeholder="Example: pokimane"
              style={styles.formInput}
              disabled={isAddingCreator}
            />

            {creatorError && (
              <div style={styles.errorMessage}>{creatorError}</div>
            )}

            <div style={styles.modalButtons}>
              <button
                type="button"
                onClick={closeCreatorForm}
                disabled={isAddingCreator}
                style={styles.cancelButton}
              >
                Cancel
              </button>

              <button
                type="submit"
                disabled={isAddingCreator}
                style={{
                  ...styles.saveButton,
                  opacity: isAddingCreator ? 0.65 : 1,
                }}
              >
                {isAddingCreator ? "Checking Twitch..." : "Add Creator"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
    );
}

export default MissionControl;