import { useEffect, useState } from "react";

function Performance() {
  const [clips, setClips] = useState([]);

  useEffect(() => {
    async function loadPerformance() {
      try {
        const response = await fetch("http://127.0.0.1:8000/api/published");
        const data = await response.json();
        setClips(data);
      } catch (error) {
        console.error(error);
      }
    }

    loadPerformance();
  }, []);

  return (
    <div>
      <h1>Performance</h1>

      {clips.length === 0 ? (
        <p>No published clips yet.</p>
      ) : (
        clips.map((clip, index) => (
          <div
            key={index}
            style={{
              border: "1px solid #3b82f6",
              borderRadius: "12px",
              padding: "20px",
              marginBottom: "20px",
            }}
          >
            <h2>{clip.title}</h2>
            <p><strong>Creator:</strong> {clip.creator}</p>
            <p>🔥 Viral Score: {clip.score}</p>
            <p>✅ Published</p>
          </div>
        ))
      )}
    </div>
  );
}

export default Performance;