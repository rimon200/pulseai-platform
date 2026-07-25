import { useEffect, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;

const normalizeClipStatus = (status) => {
  const value = String(status || "").trim().toLowerCase();
  if (value === "published") {
    return "Published";
  }
  if (value === "ready to review") {
    return "Ready to review";
  }
  return "";
};

const isPublishedClip = (clip) => normalizeClipStatus(clip?.status) === "Published";

function Publishing() {
  const [clips, setClips] = useState([]);

  useEffect(() => {
    async function loadClips() {
      try {
        const response = await fetch(`${API_BASE_URL}/api/clips`);
        const data = await response.json();

        if (!Array.isArray(data)) {
          setClips([]);
          return;
        }

        setClips(data.filter(isPublishedClip));
      } catch (error) {
        console.error(error);
        setClips([]);
      }
    }

    loadClips();
  }, []);

  return (
    <div>
      <h1>Publishing</h1>
      <p>Publish your AI clips to social platforms.</p>

      {clips.length === 0 ? (
        <p>No published clips yet.</p>
      ) : clips.map((clip, index) => (
        <div
          key={index}
          style={{
            border: "1px solid #3d4d7a",
            borderRadius: "12px",
            padding: "20px",
            marginBottom: "20px",
            background: "#1a2342",
          }}
        >
          <h3>{clip.title}</h3>

          <p>
            <strong>Creator:</strong> {clip.creator}
          </p>

          <p>
            🔥 Viral Score: {clip.score}
          </p>

          <p>Status: Published</p>
        </div>
      ))}
    </div>
  );
}

export default Publishing;