import { useEffect, useState } from "react";

function Publishing() {
  const [clips, setClips] = useState([]);
  const [publishedClips, setPublishedClips] = useState({});

  useEffect(() => {
    async function loadClips() {
      try {
        const response = await fetch("http://127.0.0.1:8000/api/clips");
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
    try {
      const response = await fetch("http://127.0.0.1:8000/api/publish", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(clip),
      });

      const data = await response.json();
      alert(data.message);

      setPublishedClips((current) => ({
  ...current,
  [index]: true,
}));
    } catch (error) {
      console.error(error);
      alert("Publishing failed.");
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