import { useEffect, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;

function Settings() {
  const [settings, setSettings] = useState(null);
  const [automation, setAutomation] = useState(null);
  const [r2Cleanup, setR2Cleanup] = useState(null);
  const [kickStatus, setKickStatus] = useState(null);
  const [youtubeStatus, setYoutubeStatus] = useState(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    fetch(`${API_BASE_URL}/api/settings/publishing`)
      .then((response) => response.json())
      .then(setSettings)
      .catch(() => setMessage("Unable to load settings."));
    fetch(`${API_BASE_URL}/api/settings/automation`)
      .then((response) => response.json())
      .then(setAutomation)
      .catch(() => setAutomation(null));
    fetch(`${API_BASE_URL}/api/settings/r2-cleanup`)
      .then((response) => response.json())
      .then(setR2Cleanup)
      .catch(() => setR2Cleanup(null));
    fetch(`${API_BASE_URL}/api/kick/status`)
      .then((response) => response.json())
      .then(setKickStatus)
      .catch(() => setKickStatus(null));
    fetch(`${API_BASE_URL}/api/youtube/status`)
      .then((response) => response.json())
      .then(setYoutubeStatus)
      .catch(() => setYoutubeStatus(null));
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
      <section style={{ marginTop: 24, padding: 24, background: "#1a2342", borderRadius: 12 }}>
        <h2>YouTube long-form monitoring</h2>
        {youtubeStatus ? (
          <div>
            <p>Integration: {youtubeStatus.enabled ? "Enabled" : "Disabled"}</p>
            <p>Configuration: {youtubeStatus.configured ? "Configured" : "Not configured"}</p>
            <p>Monitored channels: {youtubeStatus.connected_creator_count || 0}</p>
            <p>Approved media sources: {youtubeStatus.approved_source_count || 0}</p>
            <p>Last successful request: {youtubeStatus.last_successful_request || "Never"}</p>
            <p>Last polling error: {youtubeStatus.last_polling_error || "None"}</p>
            <p>Polling interval: {youtubeStatus.polling_interval_minutes} minutes</p>
            <p>Automatic generation: {youtubeStatus.automatic_generation_enabled ? "Enabled" : "Disabled"}</p>
          </div>
        ) : (
          <p>YouTube integration status is unavailable.</p>
        )}
      </section>
      <section style={{ marginTop: 24, padding: 24, background: "#1a2342", borderRadius: 12 }}>
        <h2>Kick integration</h2>
        {kickStatus ? (
          <div>
            <p>Integration: {kickStatus.enabled ? "Enabled" : "Disabled"}</p>
            <p>Configuration: {kickStatus.configured ? "Configured" : "Not configured"}</p>
            <p>Connected creators: {kickStatus.connected_creator_count || 0}</p>
            <p>Last successful request: {kickStatus.last_successful_request || "Never"}</p>
            <p>Last polling error: {kickStatus.last_polling_error || "None"}</p>
            <p>Rate limit: {kickStatus.rate_limit_status || "Unknown"}</p>
            <p>Polling interval: {kickStatus.polling_interval_seconds} seconds</p>
            <p>
              Playback ingestion:{" "}
              <strong>{kickStatus.playback_ingestion || "unavailable"}</strong>
            </p>
            <button
              disabled={!kickStatus.enabled || !kickStatus.configured}
              onClick={() => window.location.assign(`${API_BASE_URL}/api/kick/connect`)}
            >
              Connect Kick
            </button>
          </div>
        ) : (
          <p>Kick integration status is unavailable.</p>
        )}
      </section>
      <section style={{ marginTop: 24, padding: 24, background: "#1a2342", borderRadius: 12 }}>
        <h2>R2 lifecycle cleanup</h2>
        {r2Cleanup ? (
          <div>
            <p>Cleanup: <strong>{r2Cleanup.enabled ? "Enabled" : "Disabled"}</strong></p>
            <p>Dry run: <strong>{r2Cleanup.dry_run ? "Enabled" : "Disabled"}</strong></p>
            <p>Unpublished retention: {r2Cleanup.unpublished_retention_days} days</p>
            <p>Failed retention: {r2Cleanup.failed_retention_days} days</p>
            <p>Estimated reclaimable storage: {r2Cleanup.estimated_reclaimable_mb || 0} MB</p>
            <p>Last cleanup run: {r2Cleanup.last_cleanup_run || "Never"}</p>
            <p>Objects deleted: {r2Cleanup.objects_deleted || 0}</p>
            <p>Bytes reclaimed: {r2Cleanup.bytes_reclaimed_mb || 0} MB</p>
          </div>
        ) : (
          <p>R2 cleanup status is unavailable.</p>
        )}
      </section>
      <section
        style={{
          marginTop: 24,
          padding: 24,
          background: "#1a2342",
          borderRadius: 12,
        }}
      >
        <h2>Automatic clip generation</h2>
        {automation ? (
          <>
            <p>Status: {automation.enabled ? "Enabled" : "Disabled"}</p>
            <p>
              Automatic clips today: {automation.automatic_clips_created_today}
              {" / "}{automation.daily_clip_limit}
            </p>
            <p>
              Estimated outbound today: {automation.estimated_outbound_mb_today} MB
              {" / "}{automation.daily_outbound_budget_mb} MB
            </p>
            <p>
              Last automatic run: {automation.last_automatic_run || "Never"}
            </p>
            <p>Last skip reason: {automation.last_skip_reason || "None"}</p>
          </>
        ) : (
          <p>Automation status is unavailable.</p>
        )}
      </section>
    </div>
  );
}

export default Settings;
