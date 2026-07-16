import { useState } from "react";

function Settings() {
  const [settings, setSettings] = useState({
    autoPublish: false,
    clipDetection: true,
    twitchNotifications: true,
  });

  const [saved, setSaved] = useState(false);

  const updateSetting = (name) => {
    setSettings((current) => ({
      ...current,
      [name]: !current[name],
    }));

    setSaved(false);
  };

  return (
    <div>
      <h1>Settings</h1>
      <p>Configure your PulseAI workspace.</p>

      <div
        style={{
          marginTop: "24px",
          padding: "24px",
          border: "1px solid #3d4d7a",
          borderRadius: "12px",
          background: "#1a2342",
        }}
      >
        <label style={{ display: "block", marginBottom: "20px" }}>
          <input
            type="checkbox"
            checked={settings.autoPublish}
            onChange={() => updateSetting("autoPublish")}
          />{" "}
          Auto Publish Clips
        </label>

        <label style={{ display: "block", marginBottom: "20px" }}>
          <input
            type="checkbox"
            checked={settings.clipDetection}
            onChange={() => updateSetting("clipDetection")}
          />{" "}
          AI Clip Detection
        </label>

        <label style={{ display: "block", marginBottom: "20px" }}>
          <input
            type="checkbox"
            checked={settings.twitchNotifications}
            onChange={() => updateSetting("twitchNotifications")}
          />{" "}
          Twitch Notifications
        </label>

        <button
          onClick={() => setSaved(true)}
          style={{
            padding: "10px 18px",
            border: "none",
            borderRadius: "8px",
            cursor: "pointer",
            fontWeight: "bold",
          }}
        >
          Save Settings
        </button>

        {saved && (
          <p style={{ marginTop: "16px" }}>
            ✅ Settings saved
          </p>
        )}
      </div>
    </div>
  );
}

export default Settings;