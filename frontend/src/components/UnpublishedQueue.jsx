import { useCallback, useEffect, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;
const QUEUE_STATUSES = "ready_for_review,approved,scheduled,publish_failed";

function UnpublishedQueue({ styles }) {
  const [items, setItems] = useState([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState({});

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/clips?limit=10&page=${page}&status=${QUEUE_STATUSES}`,
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Unable to load queue.");
      setItems(data.items || []);
      setHasMore(Boolean(data.has_more));
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(() => { load(); }, [load]);

  const update = async (clip, payload) => {
    const response = await fetch(
      `${API_BASE_URL}/api/clips/${encodeURIComponent(clip.id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Update failed.");
    await load();
  };

  const regenerate = async (clip) => {
    const response = await fetch(
      `${API_BASE_URL}/api/clips/${encodeURIComponent(clip.id)}/caption/regenerate`,
      { method: "POST" },
    );
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.detail || "Caption regeneration failed.");
    }
    await load();
  };

  const publish = async (clip) => {
    const response = await fetch(`${API_BASE_URL}/api/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: clip.id }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Publish failed.");
    alert(data.message);
    await load();
  };

  if (loading) return <p>Loading unpublished queue…</p>;
  if (error) return <p style={{ color: "#ff8a8a" }}>{error}</p>;

  return (
    <div>
      <h1 style={styles.pageTitle}>Unpublished Queue</h1>
      <p style={styles.subtitle}>Review every durable unpublished clip.</p>
      {items.length === 0 ? (
        <section style={styles.panel}>No unpublished clips are waiting.</section>
      ) : (
        <div style={styles.clipGrid}>
          {items.map((clip) => {
            const draft = editing[clip.id] ?? clip.ai_tiktok_description ?? "";
            return (
              <article key={clip.id} style={styles.clipCard}>
                <video
                  controls
                  preload="metadata"
                  src={clip.durable_url || `${API_BASE_URL}/api/clips/${clip.id}/video`}
                  style={{ width: "100%", aspectRatio: "9 / 16", background: "#000" }}
                />
                <h3>{clip.title || "Untitled clip"}</h3>
                <p>{clip.creator} · {clip.duration_profile || "short"} · {clip.status}</p>
                <p>
                  Rights: <strong>{clip.rights_status || "unknown"}</strong>
                  {(clip.rights_status || "unknown") === "unknown"
                    ? " — verify authorization before automatic publishing."
                    : ""}
                </p>
                <textarea
                  aria-label="TikTok caption"
                  value={draft}
                  onChange={(event) => setEditing({
                    ...editing,
                    [clip.id]: event.target.value,
                  })}
                  style={{ width: "100%", minHeight: 100 }}
                />
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <button onClick={() => update(clip, {
                    ai_post_caption: draft,
                    ai_tiktok_description: draft,
                  })}>Save caption</button>
                  <button onClick={() => regenerate(clip)}>Regenerate</button>
                  <button onClick={() => navigator.clipboard.writeText(draft)}>Copy</button>
                  <button onClick={() => update(clip, { status: "approved" })}>Approve</button>
                  <button onClick={() => update(clip, { status: "rejected" })}>Reject</button>
                  <button onClick={() => update(clip, { status: "archived" })}>Archive</button>
                  <button onClick={() => publish(clip)}>Publish now</button>
                  <button onClick={() => {
                    const scheduledFor = window.prompt("Schedule ISO date/time");
                    if (scheduledFor) update(clip, {
                      status: "scheduled",
                      scheduled_for: scheduledFor,
                    });
                  }}>Schedule</button>
                </div>
              </article>
            );
          })}
        </div>
      )}
      <div style={{ marginTop: 20 }}>
        <button disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</button>
        <span style={{ margin: "0 12px" }}>Page {page}</span>
        <button disabled={!hasMore} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </div>
  );
}

export default UnpublishedQueue;
