function Sidebar({ activePage, setActivePage }) {
  const menuItems = [
    { name: "Mission Control", icon: "🏠" },
    { name: "Live Streams", icon: "🔴" },
    { name: "AI Clips", icon: "✂️" },
    { name: "Publishing", icon: "📤" },
    { name: "Performance", icon: "📈" },
    { name: "AI Agents", icon: "🤖" },
    { name: "Settings", icon: "⚙️" },
  ];

  return (
    <aside style={styles.sidebar}>
      <div>
        <h2 style={styles.logo}>⚡ PulseAI</h2>
        <p style={styles.tagline}>Your AI content team</p>
      </div>

      <div style={styles.divider} />

      <nav>
        {menuItems.map((item) => {
          const isActive = activePage === item.name;

          return (
            <button
              key={item.name}
              onClick={() => setActivePage(item.name)}
              style={{
                ...styles.menuButton,
                backgroundColor: isActive ? "#1d4ed8" : "transparent",
                color: isActive ? "#ffffff" : "#cbd5e1",
              }}
            >
              <span style={styles.menuIcon}>{item.icon}</span>
              <span>{item.name}</span>
            </button>
          );
        })}
      </nav>

      <div style={styles.sidebarFooter}>
        <div style={styles.userAvatar}>D</div>

        <div>
          <div style={styles.userName}>Daniel</div>
          <div style={styles.userPlan}>Founder workspace</div>
        </div>
      </div>
    </aside>
  );
}

const styles = {
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
};

export default Sidebar;