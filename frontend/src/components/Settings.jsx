import { useEffect, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;

function Settings() {
  const [settings, setSettings] = useState(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    fetch(`${API_BASE_URL}/api/settings/publishing`)
      .then((response) => response.json())
      .then(setSettings)
      .catch(() => setMessage("Unable to load settings."));
  }, []);

  const update = (name, value) => {
    setSettings((current) => ({ ...current, [name]: value }));
    setMessage("");
  };

  const save = async () => {
    const response = await fetch(`${API_BASE_URL}/api/settings/publishing`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
    const data = await response.json();
    if (!response.ok) {
      setMessage(data.detail || "Unable to save settings.");
      return;
    }
    setSettings(data);
    setMessage("Settings saved.");
  };

  if (!settings) return <p>Loading settings…</p>;

  return (
    <div>
      <h1>Settings</h1>
      <p>Publishing remains in Draft Upload mode until Direct Post is approved.</p>
      <p>Clips with unknown rights should be reviewed before automatic publishing.</p>
      <section style={{ marginTop: 24, padding: 24, background: "#1a2342", borderRadius: 12 }}>
        <label>Post mode{" "}
          <select
            value={settings.post_mode}
            onChange={(event) => update("post_mode", event.target.value)}
          >
            <option value="draft">Draft Upload</option>
            <option value="direct" disabled={!settings.direct_post_available}>
              Direct Post (unavailable)
            </option>
          </select>
        </label>
        <label style={{ display: "block", marginTop: 16 }}>
          <input
            type="checkbox"
            checked={settings.auto_publish_approved}
            onChange={(event) => update("auto_publish_approved", event.target.checked)}
          /> Explicitly approve automatic publishing
        </label>
        <label style={{ display: "block", marginTop: 16 }}>
          <input
            type="checkbox"
            checked={settings.auto_publish_enabled}
            onChange={(event) => update("auto_publish_enabled", event.target.checked)}
          /> Enable automatic publishing
        </label>
        {[
          ["daily_limit", "Daily limit"],
          ["min_gap_minutes", "Minimum gap (minutes)"],
          ["longform_target_percent", "Long-form target (%)"],
        ].map(([name, label]) => (
          <label key={name} style={{ display: "block", marginTop: 16 }}>
            {label}{" "}
            <input
              type="number"
              value={settings[name]}
              onChange={(event) => update(name, Number(event.target.value))}
            />
          </label>
        ))}
        <label style={{ display: "block", marginTop: 16 }}>
          Timezone{" "}
          <input
            value={settings.timezone}
            onChange={(event) => update("timezone", event.target.value)}
          />
        </label>
        <button onClick={save} style={{ marginTop: 20 }}>Save Settings</button>
        <button
          onClick={() => window.location.assign(`${API_BASE_URL}/api/tiktok/login`)}
          style={{ marginLeft: 12 }}
        >
          Connect / Reconnect TikTok
        </button>
        {message && <p>{message}</p>}
      </section>
    </div>
  );
}

export default Settings;
