import { useEffect, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL;

function Publishing() {
  const [clips, setClips] = useState([]);
  const [publishedClips, setPublishedClips] = useState({});

  useEffect(() => {
    async function loadClips() {
      try {
        const response = await fetch(`${API_BASE_URL}/api/clips`);
        const data = await response.json();
        setClips(data);
      } catch (error) {
        console.error(error);
      }
    }

    loadClips();
  }, []);

  return (
    <div>
      <h1>Publishing</h1>
      <p>Publish your AI clips to social platforms.</p>

      {clips.map((clip, index) => (
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

          <p>Status: {clip.status}</p>

          <button
          disabled={publishedClips[index]}
  onClick={async () => {
    const requestUrl = `${API_BASE_URL}/api/publish`;

    if (!clip.video_path) {
      const errorMessage = "This clip is missing its local video file path.";
      console.error("Publish request not sent:", errorMessage, clip);
      alert(errorMessage);
      return;
    }

    try {
      const response = await fetch(requestUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(clip),
      });

      let data = null;
      try {
        data = await response.json();
      } catch {
        data = { detail: "The backend returned invalid JSON." };
      }

      console.log("Publish request URL:", requestUrl);
      console.log("Publish response status:", response.status);
      console.log("Publish response JSON:", data);

      if (!response.ok) {
        throw new Error(data.detail || data.message || "Publishing failed.");
      }

      alert(data.message || "Published successfully.");

      setPublishedClips((current) => ({
  ...current,
  [index]: true,
}));
    } catch (error) {
      console.error(error);
      alert(error.message || "Publishing failed.");
    }
  }}
>
  {publishedClips[index] ? "✅ Published" : "Publish"}
</button>
        </div>
      ))}
    </div>
  );
}

export default Publishing;