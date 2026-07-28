import { useEffect, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;
const STATUSES = "scheduled,publishing,uploaded_to_inbox,published,publish_failed,archived";

function Publishing() {
  const [items, setItems] = useState([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/clips?limit=10&page=${page}&status=${STATUSES}`,
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Unable to load publishing queue.");
      setItems(data.items || []);
      setHasMore(Boolean(data.has_more));
      setError("");
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [page]);

  const setStatus = async (clip, status) => {
    const response = await fetch(`${API_BASE_URL}/api/clips/${clip.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    if (!response.ok) throw new Error("Status update failed.");
    await load();
  };

  return (
    <div>
      <h1>Publishing</h1>
      <p>Scheduled, inbox-uploaded, published, failed, and archived clips.</p>
      {loading ? <p>Loading publishing history…</p> : error ? (
        <p style={{ color: "#ff8a8a" }}>{error}</p>
      ) : items.length === 0 ? (
        <p>No publishing records match this view.</p>
      ) : items.map((clip) => (
        <div key={clip.id} style={{
          marginTop: 16, padding: 20, borderRadius: 12,
          background: "#1a2342", border: "1px solid #3d4d7a",
        }}>
          <strong>{clip.title}</strong>
          <p>{clip.creator} · {clip.status}</p>
          {clip.tiktok_failure_reason && <p>{clip.tiktok_failure_reason}</p>}
          {clip.status === "archived" ? (
            <button onClick={() => setStatus(clip, "ready_for_review")}>Restore</button>
          ) : (
            <button onClick={() => setStatus(clip, "archived")}>Archive</button>
          )}
        </div>
      ))}
      <div style={{ marginTop: 20 }}>
        <button disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</button>
        <span style={{ margin: "0 12px" }}>Page {page}</span>
        <button disabled={!hasMore} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </div>
  );
}

export default Publishing;
