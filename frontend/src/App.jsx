import { useCallback, useEffect, useState } from "react";
import Sidebar from "./components/Sidebar";
import MissionControl from "./components/MissionControl";

const API_BASE_URL = "http://127.0.0.1:8000";

function App() {
  const [activePage, setActivePage] = useState("Mission Control");
  const [creators, setCreators] = useState([]);

  const [showCreatorForm, setShowCreatorForm] = useState(false);
  const [creatorName, setCreatorName] = useState("");
  const [creatorChannel, setCreatorChannel] = useState("");
  const [creatorError, setCreatorError] = useState("");

  const [isLoadingCreators, setIsLoadingCreators] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isAddingCreator, setIsAddingCreator] = useState(false);
  const [deletingChannel, setDeletingChannel] = useState("");

  const menuItems = [
    { name: "Mission Control", icon: "🏠" },
    { name: "Live Streams", icon: "🔴" },
    { name: "AI Clips", icon: "✂️" },
    { name: "Publishing", icon: "📤" },
    { name: "Performance", icon: "📈" },
    { name: "AI Agents", icon: "🤖" },
    { name: "Settings", icon: "⚙️" },
  ];

  const activity = [
    {
      time: "Now",
      icon: "🔗",
      title: "Connected to Twitch",
      detail: "PulseAI is retrieving real Twitch channel information.",
    },
    {
      time: "Now",
      icon: "💾",
      title: "Creator storage enabled",
      detail: "Your monitored creators now survive browser refreshes.",
    },
    {
      time: "Next",
      icon: "✂️",
      title: "Clip intelligence",
      detail: "Automatic viral-moment detection will be built next.",
    },
  ];

  const clips = [
    {
      title: "No real clips generated yet",
      creator: "PulseAI",
      score: "—",
      status: "Waiting for clip intelligence",
    },
  ];

  const formatDuration = (startedAt) => {
    if (!startedAt) return "—";

    const startedTime = new Date(startedAt).getTime();

    if (Number.isNaN(startedTime)) {
      return "—";
    }

    const difference = Math.max(0, Date.now() - startedTime);
    const totalMinutes = Math.floor(difference / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;

    return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
  };

  const readResponse = async (response) => {
    try {
      return await response.json();
    } catch {
      throw new Error("The backend returned an unreadable response.");
    }
  };

  const loadCreators = useCallback(async (showRefreshState = false) => {
    if (showRefreshState) {
      setIsRefreshing(true);
    } else {
      setIsLoadingCreators(true);
    }

    try {
      const response = await fetch(`${API_BASE_URL}/creators`);
      const data = await readResponse(response);

      if (!response.ok) {
        throw new Error(data.detail || "Unable to load creators.");
      }

      setCreators(Array.isArray(data) ? data : []);
    } catch (error) {
      setCreatorError(error.message);
    } finally {
      setIsLoadingCreators(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadCreators();

    const intervalId = window.setInterval(() => {
      loadCreators(true);
    }, 60000);

    return () => window.clearInterval(intervalId);
  }, [loadCreators]);

  const addCreator = async (event) => {
    event.preventDefault();

    const cleanName = creatorName.trim();
    const cleanChannel = creatorChannel
      .trim()
      .replace(/^@/, "")
      .toLowerCase();

    if (!cleanName || !cleanChannel) {
      setCreatorError("Enter both the creator name and Twitch channel.");
      return;
    }

    setCreatorError("");
    setIsAddingCreator(true);

    try {
      const response = await fetch(`${API_BASE_URL}/creators`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: cleanName,
          channel: cleanChannel,
        }),
      });

      const data = await readResponse(response);

      if (!response.ok) {
        throw new Error(data.detail || "Unable to add this creator.");
      }

      setCreators((currentCreators) => [...currentCreators, data]);
      setCreatorName("");
      setCreatorChannel("");
      setShowCreatorForm(false);
    } catch (error) {
      setCreatorError(error.message);
    } finally {
      setIsAddingCreator(false);
    }
  };

  const deleteCreator = async (channel) => {
    const confirmed = window.confirm(
      `Remove @${channel} from PulseAI monitoring?`
    );

    if (!confirmed) return;

    setDeletingChannel(channel);
    setCreatorError("");

    try {
      const response = await fetch(
        `${API_BASE_URL}/creators/${encodeURIComponent(channel)}`,
        {
          method: "DELETE",
        }
      );

      const data = await readResponse(response);

      if (!response.ok) {
        throw new Error(data.detail || "Unable to remove this creator.");
      }

      setCreators((currentCreators) =>
        currentCreators.filter((creator) => creator.channel !== channel)
      );
    } catch (error) {
      setCreatorError(error.message);
    } finally {
      setDeletingChannel("");
    }
  };

  const closeCreatorForm = () => {
    if (isAddingCreator) return;

    setShowCreatorForm(false);
    setCreatorName("");
    setCreatorChannel("");
    setCreatorError("");
  };

  const liveCreators = creators.filter((creator) => creator.is_live);

  const renderCreatorRow = (creator) => {
    const isLive = creator.status === "LIVE";
    const hasError = creator.status === "ERROR";
    const isDeleting = deletingChannel === creator.channel;

    let badgeText = "OFFLINE";
    let badgeBackground = "#334155";
    let badgeColor = "#cbd5e1";

    if (isLive) {
      badgeText = "● LIVE";
      badgeBackground = "#991b1b";
      badgeColor = "#fee2e2";
    } else if (hasError) {
      badgeText = "ERROR";
      badgeBackground = "#78350f";
      badgeColor = "#fef3c7";
    }

    return (
      <div key={creator.channel} style={styles.creatorRow}>
        {creator.profile_image_url ? (
          <img
            src={creator.profile_image_url}
            alt={`${creator.display_name} profile`}
            style={styles.creatorImage}
          />
        ) : (
          <div style={styles.creatorAvatar}>
            {(creator.display_name || creator.channel || "?")
              .charAt(0)
              .toUpperCase()}
          </div>
        )}

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={styles.creatorName}>
            {creator.display_name || creator.channel}
          </div>

          <div style={styles.creatorMeta}>
            {isLive
              ? `${Number(creator.viewer_count || 0).toLocaleString()} viewers • ${formatDuration(
                  creator.started_at
                )}`
              : hasError
                ? creator.error
                : `@${creator.channel} • Currently offline`}
          </div>

          {isLive && creator.title && (
            <div style={styles.streamTitle}>{creator.title}</div>
          )}

          {isLive && creator.game_name && (
            <div style={styles.gameName}>{creator.game_name}</div>
          )}
        </div>

        <div style={styles.creatorActions}>
          <span
            style={{
              ...styles.statusBadge,
              backgroundColor: badgeBackground,
              color: badgeColor,
            }}
          >
            {badgeText}
          </span>

          <button
            onClick={() => deleteCreator(creator.channel)}
            disabled={isDeleting}
            title="Remove creator"
            style={{
              ...styles.deleteButton,
              opacity: isDeleting ? 0.5 : 1,
            }}
          >
            {isDeleting ? "…" : "×"}
          </button>
        </div>
      </div>
    );
  };

  const renderMissionControl = () => (
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
          <strong style={styles.statNumber}>0</strong>
          <span style={styles.statDetail}>Clip engine coming next</span>
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
          {clips.map((clip) => (
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

  const renderPlaceholder = () => (
    <div>
      <h1 style={styles.pageTitle}>{activePage}</h1>

      <p style={styles.subtitle}>
        This section will be built after Mission Control.
      </p>

      <section style={{ ...styles.panel, marginTop: "26px" }}>
        <h2 style={styles.panelTitle}>{activePage} is ready to build</h2>

        <p style={styles.panelSubtitle}>
          We will connect the real feature here step by step.
        </p>
      </section>
    </div>
  );

  return (
    <div style={styles.app}>
      <Sidebar
  activePage={activePage}
  setActivePage={setActivePage}
/>

      <main style={styles.main}>
        {activePage === "Mission Control"
          ? renderMissionControl()
          : renderPlaceholder()}
      </main>
    </div>
  );
}

const styles = {
  app: {
    display: "flex",
    minHeight: "100vh",
    backgroundColor: "#07111f",
    color: "#ffffff",
    fontFamily: "Arial, sans-serif",
  },

  sidebar: {
    width: "275px",
    minHeight: "100vh",
    backgroundColor: "#0b1424",
    borderRight: "1px solid #223047",
    padding: "28px 22px",
    boxSizing: "border-box",
    display: "flex",
    flexDirection: "column",
  },

  logo: {
    margin: 0,
    fontSize: "28px",
    color: "#ffffff",
  },

  tagline: {
    margin: "8px 0 0",
    color: "#94a3b8",
    fontSize: "14px",
  },

  divider: {
    height: "1px",
    backgroundColor: "#223047",
    margin: "25px 0",
  },

  menuButton: {
    width: "100%",
    display: "flex",
    alignItems: "center",
    gap: "12px",
    padding: "13px 14px",
    marginBottom: "7px",
    border: "none",
    borderRadius: "11px",
    fontSize: "16px",
    textAlign: "left",
    cursor: "pointer",
  },

  menuIcon: {
    width: "22px",
  },

  sidebarFooter: {
    marginTop: "auto",
    display: "flex",
    alignItems: "center",
    gap: "12px",
    paddingTop: "22px",
    borderTop: "1px solid #223047",
  },

  userAvatar: {
    width: "40px",
    height: "40px",
    borderRadius: "50%",
    backgroundColor: "#2563eb",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontWeight: "bold",
  },

  userName: {
    fontWeight: "bold",
    color: "#ffffff",
  },

  userPlan: {
    color: "#94a3b8",
    fontSize: "13px",
    marginTop: "3px",
  },

  main: {
    flex: 1,
    padding: "34px",
    boxSizing: "border-box",
    overflow: "auto",
  },

  pageHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "20px",
  },

  pageTitle: {
    margin: 0,
    color: "#ffffff",
    fontSize: "36px",
  },

  subtitle: {
    color: "#a7b4c8",
    margin: "8px 0 0",
    fontSize: "16px",
  },

  headerButtons: {
    display: "flex",
    alignItems: "center",
    gap: "11px",
  },

  refreshButton: {
    backgroundColor: "#253a5a",
    color: "#e2e8f0",
    border: "1px solid #475569",
    borderRadius: "11px",
    padding: "13px 17px",
    fontSize: "15px",
    cursor: "pointer",
  },

  addCreatorButton: {
    backgroundColor: "#2563eb",
    color: "#ffffff",
    border: "none",
    borderRadius: "11px",
    padding: "13px 20px",
    fontSize: "15px",
    fontWeight: "bold",
    cursor: "pointer",
  },

  statsGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(4, minmax(170px, 1fr))",
    gap: "18px",
    marginTop: "28px",
  },

  statCard: {
    backgroundColor: "#111d2f",
    border: "1px solid #2b3a52",
    borderRadius: "16px",
    padding: "20px",
    display: "flex",
    flexDirection: "column",
    gap: "9px",
  },

  statLabel: {
    color: "#b8c4d6",
    fontSize: "14px",
  },

  statNumber: {
    color: "#ffffff",
    fontSize: "30px",
  },

  statDetail: {
    color: "#7dd3fc",
    fontSize: "13px",
  },

  onlineText: {
    color: "#86efac",
    fontSize: "26px",
  },

  twoColumnGrid: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: "22px",
    marginTop: "22px",
  },

  panel: {
    backgroundColor: "#111d2f",
    border: "1px solid #2b3a52",
    borderRadius: "17px",
    padding: "22px",
  },

  panelHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: "16px",
    marginBottom: "18px",
  },

  panelTitle: {
    color: "#ffffff",
    margin: 0,
    fontSize: "21px",
  },

  panelSubtitle: {
    color: "#94a3b8",
    margin: "6px 0 0",
    fontSize: "14px",
  },

  liveIndicator: {
    display: "flex",
    alignItems: "center",
    gap: "7px",
    color: "#86efac",
    fontSize: "13px",
  },

  liveDot: {
    width: "8px",
    height: "8px",
    backgroundColor: "#22c55e",
    borderRadius: "50%",
  },

  workingBadge: {
    color: "#93c5fd",
    fontSize: "13px",
  },

  creatorRow: {
    display: "flex",
    alignItems: "center",
    gap: "13px",
    padding: "14px 0",
    borderTop: "1px solid #26364d",
  },

  creatorAvatar: {
    width: "42px",
    height: "42px",
    borderRadius: "12px",
    backgroundColor: "#253a5a",
    color: "#ffffff",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontWeight: "bold",
    flexShrink: 0,
  },

  creatorImage: {
    width: "42px",
    height: "42px",
    borderRadius: "12px",
    objectFit: "cover",
    flexShrink: 0,
  },

  creatorName: {
    color: "#ffffff",
    fontWeight: "bold",
  },

  creatorMeta: {
    color: "#b8c4d6",
    fontSize: "13px",
    marginTop: "5px",
  },

  streamTitle: {
    color: "#ffffff",
    fontSize: "13px",
    marginTop: "8px",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  },

  gameName: {
    color: "#7dd3fc",
    fontSize: "12px",
    marginTop: "5px",
  },

  creatorActions: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    flexShrink: 0,
  },

  statusBadge: {
    padding: "7px 9px",
    borderRadius: "8px",
    fontSize: "12px",
    fontWeight: "bold",
  },

  deleteButton: {
    width: "29px",
    height: "29px",
    borderRadius: "8px",
    border: "1px solid #475569",
    backgroundColor: "#1e293b",
    color: "#fca5a5",
    fontSize: "19px",
    lineHeight: 1,
    cursor: "pointer",
  },

  emptyState: {
    color: "#b8c4d6",
    padding: "24px 0",
    textAlign: "center",
  },

  activityRow: {
    display: "flex",
    alignItems: "flex-start",
    gap: "12px",
    padding: "13px 0",
    borderTop: "1px solid #26364d",
  },

  activityIcon: {
    width: "35px",
    height: "35px",
    borderRadius: "10px",
    backgroundColor: "#253a5a",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
  },

  activityTitle: {
    color: "#ffffff",
    fontWeight: "bold",
    fontSize: "14px",
  },

  activityDetail: {
    color: "#a7b4c8",
    fontSize: "13px",
    marginTop: "5px",
  },

  activityTime: {
    color: "#93a4bc",
    fontSize: "12px",
    whiteSpace: "nowrap",
  },

  secondaryButton: {
    backgroundColor: "#253a5a",
    color: "#e2e8f0",
    border: "1px solid #3a4d6a",
    borderRadius: "9px",
    padding: "9px 13px",
    cursor: "pointer",
  },

  clipGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(3, minmax(210px, 1fr))",
    gap: "18px",
  },

  clipCard: {
    backgroundColor: "#0c1728",
    border: "1px solid #293b56",
    borderRadius: "14px",
    overflow: "hidden",
  },

  clipPreview: {
    height: "115px",
    background:
      "linear-gradient(135deg, #1d4ed8 0%, #7c3aed 55%, #db2777 100%)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },

  playButton: {
    width: "46px",
    height: "46px",
    borderRadius: "50%",
    backgroundColor: "rgba(0, 0, 0, 0.55)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    paddingLeft: "3px",
  },

  clipContent: {
    padding: "15px",
  },

  clipTitle: {
    color: "#ffffff",
    fontWeight: "bold",
  },

  clipCreator: {
    color: "#a7b4c8",
    fontSize: "13px",
    marginTop: "5px",
  },

  clipFooter: {
    display: "flex",
    flexDirection: "column",
    gap: "9px",
    marginTop: "14px",
  },

  scoreBadge: {
    color: "#fcd34d",
    fontSize: "13px",
  },

  clipStatus: {
    color: "#93c5fd",
    fontSize: "12px",
  },

  modalOverlay: {
    position: "fixed",
    inset: 0,
    backgroundColor: "rgba(2, 8, 23, 0.8)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: "20px",
    zIndex: 1000,
  },

  modal: {
    width: "100%",
    maxWidth: "460px",
    backgroundColor: "#111d2f",
    border: "1px solid #3b4d68",
    borderRadius: "18px",
    padding: "26px",
    boxSizing: "border-box",
    boxShadow: "0 25px 60px rgba(0, 0, 0, 0.45)",
  },

  modalTitle: {
    color: "#ffffff",
    margin: 0,
    fontSize: "25px",
  },

  modalDescription: {
    color: "#a7b4c8",
    lineHeight: 1.5,
    margin: "10px 0 22px",
  },

  label: {
    display: "block",
    color: "#e2e8f0",
    fontWeight: "bold",
    fontSize: "14px",
    marginBottom: "8px",
  },

  formInput: {
    width: "100%",
    backgroundColor: "#0c1728",
    color: "#ffffff",
    border: "1px solid #475569",
    borderRadius: "11px",
    padding: "13px 14px",
    marginBottom: "18px",
    boxSizing: "border-box",
    fontSize: "15px",
    outline: "none",
  },

  errorMessage: {
    backgroundColor: "#451a1a",
    border: "1px solid #991b1b",
    color: "#fecaca",
    borderRadius: "10px",
    padding: "11px 13px",
    marginBottom: "18px",
    fontSize: "14px",
  },

  modalButtons: {
    display: "flex",
    justifyContent: "flex-end",
    gap: "11px",
    marginTop: "5px",
  },

  cancelButton: {
    backgroundColor: "#253a5a",
    color: "#e2e8f0",
    border: "1px solid #475569",
    borderRadius: "10px",
    padding: "11px 17px",
    cursor: "pointer",
  },

  saveButton: {
    backgroundColor: "#2563eb",
    color: "#ffffff",
    border: "none",
    borderRadius: "10px",
    padding: "11px 17px",
    fontWeight: "bold",
    cursor: "pointer",
  },
};

export default App;